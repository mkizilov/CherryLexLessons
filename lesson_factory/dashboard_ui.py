import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from html import escape
import json
import re

from .core import AuthoringError, LANGUAGES, RETURN_OFFSETS, stage_identity


ACTIVE_STATES = frozenset({'launching', 'running'})
JOB_COLUMNS = (
    {'name': 'study', 'label': 'Study language', 'field': 'study', 'align': 'left', 'sortable': True},
    {'name': 'base_checkpoint', 'label': 'Base', 'field': 'base_checkpoint', 'align': 'left'},
    {'name': 'status', 'label': 'State', 'field': 'status', 'align': 'left'},
    {'name': 'session_id', 'label': 'OpenCode session ID', 'field': 'session_id', 'align': 'left'},
    {'name': 'attempt_count', 'label': 'Attempts', 'field': 'attempt_count', 'align': 'left'},
)
TABLE_FIELDS = ('id', 'study', 'base_checkpoint', 'state', 'status', 'session_id', 'session_label', 'attempt_count')
TERMINAL_STATES = frozenset({'succeeded', 'cancelled'})
STATE_LABELS = {
    'queued': 'Queued', 'launching': 'Launching', 'running': 'Running',
    'retry_wait': 'Retry wait', 'interrupted': 'Interrupted', 'blocked': 'Blocked',
    'succeeded': 'Verified complete', 'cancelled': 'Cancelled',
}
CSS = '''
:root { --paper:#FBF4EA; --surface:#FFFDF9; --ink:#2A1418; --muted:#6B4E4A;
  --cherry:#C6233F; --line:#E1D2C8; --sunken:#F3E8D8; }
body { background:var(--paper); color:var(--ink); font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
.nicegui-content { padding:0; gap:0; }
.dashboard-shell { width:100%; max-width:1480px; margin:auto; padding:32px 40px 60px; gap:24px; }
.brand { font-size:13px; font-weight:750; letter-spacing:.16em; color:#84112C; text-transform:uppercase; }
h1 { margin:0; font-size:clamp(27px,3vw,40px); font-weight:680; line-height:1.14; letter-spacing:-.04em; }
h2 { margin:0; font-size:21px; font-weight:660; letter-spacing:-.02em; }
h3 { margin:0; font-size:16px; font-weight:650; }
.muted { color:var(--muted); }
.eyebrow { font-size:11px; font-weight:750; text-transform:uppercase; letter-spacing:.12em; color:var(--muted); }
.panel { background:var(--surface); border:1px solid var(--line); border-radius:20px; box-shadow:none; padding:24px; gap:18px; }
.panel .q-field { background:var(--surface); }
.dashboard-tabs { border-bottom:1px solid var(--line); }
.dashboard-tabs .q-tab { min-height:54px; padding:0 24px; text-transform:none; font-weight:650; }
.dashboard-panels { background:transparent; width:100%; }
.dashboard-panels .q-tab-panel { padding:24px 0 0; }
.queue-grid { display:grid; width:100%; grid-template-columns:minmax(290px,350px) minmax(0,1fr); gap:24px; align-items:start; }
.stats-grid { display:grid; width:100%; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }
.stat { background:var(--surface); border:1px solid var(--line); border-radius:16px; padding:18px; gap:4px; }
.stat-value { font-size:29px; font-weight:650; letter-spacing:-.04em; line-height:1.15; }
.notice { background:var(--sunken); border:1px solid var(--line); border-radius:12px; padding:14px 16px; font-size:13px; line-height:1.55; width:100%; white-space:pre-wrap; }
.notice-warning { background:#FFF0DD; color:#713D15; border-color:#D9B694; }
.notice-error { background:#FDDDE1; color:#84112C; border-color:#D69DA8; }
.q-btn { border-radius:10px; min-height:40px; text-transform:none; font-weight:620; letter-spacing:0; }
.q-btn:focus-visible,.q-field:focus-within { outline:2px solid #A5163A; outline-offset:3px; }
.q-field--outlined .q-field__control { border-radius:10px; }
.q-field__label { color:var(--muted); }
.q-table__container { background:transparent; box-shadow:none; width:100%; }
.q-table th { color:var(--muted); font-size:11px; font-weight:750; text-transform:uppercase; letter-spacing:.07em; }
.q-table td { font-size:13px; }
.state-chip { display:inline-flex; border-radius:6px; padding:4px 8px; background:var(--sunken); color:var(--muted); font-size:11px; font-weight:700; white-space:nowrap; }
.state-running,.state-launching { background:#E4EEFF; color:#204B91; }
.state-succeeded { background:#E3F2DA; color:#1B5222; }
.state-blocked,.state-interrupted { background:#FFE6D3; color:#904010; }
.state-retry_wait { background:#FFF1BF; color:#715507; }
.detail-grid { display:grid; width:100%; grid-template-columns:120px minmax(0,1fr); gap:8px 16px; font-size:13px; }
.detail-value { overflow-wrap:anywhere; white-space:pre-wrap; }
.mono { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:12px; }
.session-code { display:inline-block; max-width:28ch; white-space:normal; overflow-wrap:anywhere; user-select:all; vertical-align:middle; }
.preview-frame { width:100%; min-height:740px; border:1px solid var(--line); background:var(--surface); border-radius:16px; }
.dialog-card { width:min(800px,94vw); max-width:94vw!important; max-height:90vh; overflow:auto; }
.empty { padding:32px 16px; text-align:center; align-items:center; color:var(--muted); width:100%; }
@media(max-width:1000px) { .dashboard-shell { padding:24px; } .queue-grid { grid-template-columns:1fr; } }
@media(max-width:600px) { .dashboard-shell { padding:20px 14px; } .panel { padding:18px; border-radius:16px; } .stats-grid { grid-template-columns:repeat(2,1fr); } .detail-grid { grid-template-columns:90px minmax(0,1fr); } .preview-frame { min-height:600px; } }
@media(prefers-reduced-motion:reduce) { *,*::before,*::after { animation:none!important; transition:none!important; } }
'''


def safe_text(value, limit=700):
    text = str(value if value is not None else '')
    text = re.sub(r'https?://[^\s<>"\']+', '[URL redacted]', text, flags=re.I)
    text = re.sub(r'(?i)\b(?:bearer\s+|sk[-_])[A-Za-z0-9_.\-/+=]{4,}', '[credential redacted]', text)
    text = re.sub(r'(?i)\b(api[-_ ]?key|authorization|password|secret|token)\b\s*[:=]\s*["\']?[^\s,"\'}]+', r'\1=[redacted]', text)
    text = re.sub(r'(?:[^\s]*[/\\])?secrets[/\\][^\s]*', '[private path]', text, flags=re.I)
    return text[:limit] + ('…' if len(text) > limit else '')


def safe_error(error):
    if isinstance(error, (AuthoringError, ValueError)):
        return safe_text(error)
    return f'{type(error).__name__}: the operation could not be completed. Private diagnostics were not exposed.'


def bounded_integer(value, label, maximum=None):
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a whole number.')
    try:
        number = int(value)
        valid = float(value) == number
    except (TypeError, ValueError, OverflowError):
        valid = False
        number = 0
    if not valid or number < 1 or (maximum is not None and number > maximum):
        suffix = f' from 1 to {maximum}' if maximum else ' greater than zero'
        raise ValueError(f'{label} must be a whole number{suffix}.')
    return number


def integer_error(value, label, maximum):
    try:
        bounded_integer(value, label, maximum)
    except ValueError as error:
        return str(error)
    return None


def coordinator_hint(languages, parallel):
    if integer_error(parallel, 'Parallel coordinators', 12):
        return 'Enter a whole number from 1 to 12. This cap counts coordinators only.'
    count = len(set(languages or []))
    cap = int(parallel)
    return (f'Cap {cap} · {count} selected study {"language" if count == 1 else "languages"} · '
            f'at most {min(cap, count)} {"coordinator" if min(cap, count) == 1 else "coordinators"} in this batch. '
            'One family per study language runs at a time; blockers can reduce this further. '
            'Review subagents and translation/API workers are additional, not included in this cap.')


def stage_label(base_checkpoint, stage):
    unlock = stage_identity(base_checkpoint, stage)['checkpoint']
    return f'Stage {stage} · {"Base" if stage == 0 else f"Return {stage}"} · after {unlock} words'


def stage_hint():
    offsets = [f'+{offset}' for offset in RETURN_OFFSETS]
    return (f'Stage is the family unlock step: the base, then returns {", ".join(offsets[:-1])} and {offsets[-1]} words later. '
            'It is not a lesson page. Pages are browsed inside the rendered preview; a practice-only return reuses earlier pages with new questions.')


def is_active_worker(job):
    return job.get('state') in ACTIVE_STATES or (job.get('state') in {'interrupted', 'blocked', 'retry_wait'} and bool(job.get('session_id') or job.get('attempt_id')))


def active_scope(jobs, batch_id):
    visible = [job for job in jobs if is_active_worker(job)]
    here = sum(job.get('batch_id') == batch_id for job in visible)
    elsewhere = len(visible) - here
    text = (f'Selected batch · {here} running, launching, reserved or needs-attention jobs, all shown without paging. '
            'Interrupted and blocked workers keep their recorded session IDs visible; unstarted, queued and finished jobs stay in All batch jobs.')
    if elsewhere:
        text += f' {elsewhere} more active or needing attention in other batches; select that batch to see them.'
    return text


def batch_views(snapshot):
    """Prefer live work on first load; never override an explicit valid selection."""
    counts = {}
    for job in snapshot.get('jobs', []):
        counts.setdefault(job['batch_id'], Counter())[job.get('state')] += 1
    def priority(batch):
        count = counts.get(batch['id'], Counter())
        active = sum(count[state] for state in ACTIVE_STATES)
        unfinished = sum(n for state, n in count.items() if state not in TERMINAL_STATES)
        return (bool(active), bool(unfinished and not batch['paused']), bool(unfinished), batch.get('created_at', 0))
    batches = snapshot.get('batches', [])
    preferred = max(batches, key=priority)['id'] if batches else None
    ordered = sorted(batches, key=lambda batch: batch.get('created_at', 0), reverse=True)
    options = {}
    for batch in ordered:
        count = counts.get(batch['id'], Counter())
        total = sum(count.values())
        active = sum(count[state] for state in ACTIVE_STATES)
        unfinished = sum(n for state, n in count.items() if state not in TERMINAL_STATES)
        languages = batch['languages']
        label = 'All 12 languages' if len(languages) == 12 else ', '.join(LANGUAGES.get(code, code) for code in languages)
        state = 'Finished' if total and not unfinished else 'Paused' if batch['paused'] else 'Scheduling'
        options[batch['id']] = f'{label} · {total} families · {active} active · {count["succeeded"]} complete · {state} · {batch["id"][-8:]}'
    return options, preferred


def selected_languages(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError('Select at least one study language.')
    if any(value not in LANGUAGES for value in values):
        raise ValueError('Select supported study languages only.')
    return list(dict.fromkeys(values))


def iframe_document(document):
    return ('<iframe id="lesson-preview-frame" class="preview-frame" '
            'title="Read-only lesson and quiz preview" sandbox="allow-scripts" '
            'referrerpolicy="no-referrer" srcdoc="' + escape(document, quote=True) + '"></iframe>')


def safe_job(job):
    fields = ('id', 'batch_id', 'language', 'base_checkpoint', 'run', 'path', 'state',
              'reason', 'attempt_id', 'session_id')
    result = {key: safe_text(job.get(key)) for key in fields}
    state = str(job.get('state', ''))
    result['state'] = state if state in STATE_LABELS else 'unknown'
    result['status'] = STATE_LABELS.get(state, 'Unknown')
    result['study'] = LANGUAGES.get(job.get('language'), 'Unknown')
    metadata = job.get('launch_metadata') or {}
    result['diagnostic'] = safe_text(metadata.get('diagnostic'))
    result['external'] = metadata.get('mode') == 'external'
    result['phase'] = safe_text(metadata.get('phase'))
    if result['external'] and state in ACTIVE_STATES:
        result['status'] = 'Manual recovery · slot held'
    result['session_label'] = ('Current session' if state in ACTIVE_STATES else
                               'Session needing attention' if state in {'blocked', 'interrupted', 'retry_wait'} else 'Saved session')
    result['attempt_count'] = len(job.get('attempts') or [])
    result['attempts'] = [
        {key: safe_text(attempt.get(key)) for key in
         ('id', 'session_id', 'state', 'started_at', 'ended_at', 'exit_code', 'completion_status', 'completion_reason') if key in attempt}
        for attempt in job.get('attempts', []) if isinstance(attempt, dict)
    ]
    return result


def may_resume(job):
    if job.get('state') not in {'interrupted', 'blocked'}:
        return False
    attempted = bool(job.get('attempts') or job.get('attempt_id'))
    return bool(job.get('session_id')) or not attempted


def queue_summary(snapshot):
    jobs = snapshot.get('jobs', [])
    counts = Counter(job.get('state') for job in jobs)
    unfinished = {job.get('batch_id') for job in jobs if job.get('state') not in TERMINAL_STATES}
    caps = [batch['max_parallel'] for batch in snapshot.get('batches', [])
            if not batch['paused'] and batch['id'] in unfinished]
    return {
        'total': len(jobs), 'active': sum(counts[state] for state in ACTIVE_STATES),
        'complete': counts['succeeded'], 'attention': counts['blocked'] + counts['interrupted'],
        'cap': min(caps) if caps else 0,
    }


@dataclass(frozen=True)
class PlanDraft:
    languages: tuple
    count: int
    parallel: int
    plan: dict

    @property
    def authorization(self):
        return {
            'paid_scope_acknowledged': True,
            'families': [{'language': row['language'], 'base_checkpoint': row['base_checkpoint']}
                         for row in self.plan['families']],
            'max_translation_attempts': 4,
        }


class DashboardController:
    def __init__(self, store, runner, preview):
        self.store = store
        self.runner = runner
        self.preview = preview
        self.runner_lock = asyncio.Lock()

    async def plan(self, languages, count, parallel):
        languages = selected_languages(languages)
        count = bounded_integer(count, 'Families per language', 99999)
        parallel = bounded_integer(parallel, 'Parallel coordinators', 12)
        plan = await asyncio.to_thread(self.store.plan, languages, count)
        return PlanDraft(tuple(languages), count, parallel, deepcopy(plan))

    async def create(self, draft, acknowledged):
        if acknowledged is not True:
            raise ValueError('Explicitly acknowledge the paid scope before creating the queue.')
        if not draft.plan.get('families'):
            raise ValueError('The plan contains no families. Preview the next families again.')
        return await asyncio.to_thread(self.store.create_batch, list(draft.languages), draft.count,
                                       draft.parallel, draft.authorization)

    async def snapshot(self):
        return await asyncio.to_thread(self.store.snapshot)

    async def batch_action(self, action, batch_id=None, parallel=None):
        if action == 'parallel':
            value = bounded_integer(parallel, 'Parallel coordinators', 12)
            return await asyncio.to_thread(self.store.set_parallel, batch_id, value)
        if action not in {'pause', 'resume', 'pause_all'}:
            raise ValueError('Unknown queue action.')
        async with self.runner_lock:
            method = getattr(self.store, action)
            return await asyncio.to_thread(method, *([] if action == 'pause_all' else [batch_id]))

    async def recovery_details(self, job_id):
        async with self.runner_lock:
            return await asyncio.to_thread(self.runner.recovery_details, job_id)

    async def job_action(self, action, job_id, acknowledged=False, operator_note=''):
        if action not in {'resume', 'stop', 'reconcile', 'handoff'}:
            raise ValueError('Unknown job action.')
        async with self.runner_lock:
            job = await asyncio.to_thread(self.store.get_job, job_id)
            if action == 'resume':
                if not acknowledged:
                    raise ValueError('Confirm that the blocker has been addressed before resuming.')
                if not may_resume(job):
                    raise ValueError('This job cannot resume safely: an attempted job needs its exact recorded session ID.')
                return await asyncio.to_thread(self.runner.resume_job, job_id)
            if action == 'stop':
                if not acknowledged:
                    raise ValueError('Explicit confirmation is required to stop an active coordinator.')
                if job.get('state') not in ACTIVE_STATES:
                    raise ValueError('This coordinator is no longer active. Refresh its state before taking another action.')
                return await asyncio.to_thread(self.runner.stop_job, job_id)
            if not acknowledged:
                raise ValueError('Explicit confirmation is required for manual recovery.')
            if action == 'handoff':
                return await asyncio.to_thread(self.runner.handoff_job, job_id)
            if not isinstance(operator_note, str) or not operator_note.strip():
                raise ValueError('Describe the repair or verification before resolving this job.')
            return await asyncio.to_thread(self.runner.reconcile_job, job_id, operator_note.strip())

    async def catalog(self, include_corrections=False):
        if include_corrections:
            return await asyncio.to_thread(self.preview.catalog, include_corrections=True)
        return await asyncio.to_thread(self.preview.catalog)

    async def render(self, relative, locale, stage):
        document = await asyncio.to_thread(self.preview.render, relative, locale, stage)
        return iframe_document(document)


class DashboardRuntime:
    def __init__(self, controller, interval=1.0):
        self.controller = controller
        self.interval = interval
        self.task = None
        self.closed = False
        self.stop_event = asyncio.Event()
        self.ready = False
        self.error = None
        self.last_events = []

    async def start(self):
        if self.task is not None:
            return
        async with self.controller.runner_lock:
            self.last_events = await asyncio.to_thread(self.controller.runner.recover)
        self.ready = True
        self.task = asyncio.create_task(self._loop(), name='lesson-dashboard-scheduler')

    async def _loop(self):
        while not self.stop_event.is_set():
            try:
                async with self.controller.runner_lock:
                    outcomes = await asyncio.to_thread(self.controller.runner.tick)
                if outcomes:
                    self.last_events = outcomes[-20:]
                self.error = None
            except Exception as error:
                self.error = safe_error(error)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        if self.closed:
            return
        self.closed = True
        self.stop_event.set()
        if self.task is not None:
            await self.task
        async with self.controller.runner_lock:
            await asyncio.to_thread(self.controller.runner.close)
        self.ready = False


class DashboardPage:
    def __init__(self, controller, runtime):
        from nicegui import ui
        self.ui = ui
        self.controller = controller
        self.runtime = runtime
        self.snapshot_data = {'batches': [], 'jobs': []}
        self.catalog_data = []
        self.draft = None
        self.busy = False
        self.refreshing = False
        self.last_snapshot = None
        self.last_job = None
        self.last_batch_id = None
        self.rendered_selection = None
        self.preview_selection = (None, None, None)
        self.preview_generation = 0
        self.build()

    def identified(self, element, identifier, label=None):
        element.props(f'id={identifier}')
        if label:
            element.props(f'aria-label="{label}"')
        return element

    def heading(self, text, level=2):
        with self.ui.element(f'h{level}') as heading:
            self.ui.label(text)
        return heading

    def notice(self, text, kind=''):
        return self.ui.label(text).classes(f'notice {"notice-" + kind if kind else ""}')

    def button(self, text, callback, identifier, primary=False, **kwargs):
        button = self.ui.button(text, on_click=callback, **kwargs)
        button.props('unelevated' if primary else 'outline')
        return self.identified(button, identifier)

    def build(self):
        ui = self.ui
        ui.colors(primary='#C6233F', secondary='#6B4E4A', positive='#1B5222', negative='#A5163A')
        ui.add_css(CSS)
        with self.identified(ui.column(), 'dashboard-root').classes('dashboard-shell'):
            with ui.row().classes('w-full items-center justify-between gap-4'):
                with ui.column().classes('gap-2'):
                    ui.label('CherryLex / authoring studio').classes('brand')
                    self.heading('A little language. A complete family.', 1)
                    ui.label('Plan carefully. Queue deliberately. Preview what is actually written.').classes('muted')
                ui.label('LOCAL ONLY · 127.0.0.1').classes('state-chip')
            self.runtime_notice = self.notice('Loading the local queue. Startup never authorizes new work.')
            self.runtime_notice.props('role=status aria-live=polite')
            with ui.tabs().classes('dashboard-tabs w-full').props('align=left') as tabs:
                queue_tab = ui.tab('Queue', icon='view_list')
                library_tab = ui.tab('Library preview', icon='auto_stories')
            with ui.tab_panels(tabs, value=queue_tab).classes('dashboard-panels'):
                with ui.tab_panel(queue_tab):
                    with ui.column().classes('w-full gap-6'):
                        self.build_stats()
                        with ui.element('div').classes('queue-grid'):
                            self.build_plan()
                            self.build_queue()
                with ui.tab_panel(library_tab):
                    self.build_library()
            ui.label('Closing this browser does not stop the queue. Stopping this server does not kill supervised coordinators.').classes('muted text-xs')
        ui.timer(2.0, self.refresh)
        ui.timer(0.1, self.initial_load, once=True)

    def build_stats(self):
        with self.ui.element('div').classes('stats-grid'):
            self.stats = {}
            for key, title, hint in (
                ('total', 'Families in queue', 'Base + four returns each'),
                ('active', 'Active / reserved', 'Coordinator slots; review workers add more'),
                ('complete', 'Verified complete', 'Registry and bundle verified'),
                ('attention', 'Needs attention', 'Blocked or interrupted'),
            ):
                with self.ui.column().classes('stat'):
                    self.ui.label(title).classes('eyebrow')
                    self.stats[key] = self.ui.label('—').classes('stat-value')
                    self.ui.label(hint).classes('muted text-xs')

    def validate_plan(self):
        error = integer_error(self.parallel.value, 'Parallel coordinators', 12)
        count_error = integer_error(self.count.value, 'Families per language', 99999)
        self.parallel_hint.set_text(coordinator_hint(self.languages.value, self.parallel.value))
        valid = not error and not count_error and bool(self.languages.value)
        self.plan_button.set_enabled(bool(valid and not self.busy))
        return error or count_error or ('Select at least one study language.' if not self.languages.value else None)

    def invalidate_plan(self):
        self.draft = None
        self.confirm_plan.disable()
        error = self.validate_plan()
        self.plan_status.set_text(error or 'Selection changed. Preview again to confirm the exact scope.')
        self.plan_body.clear()

    def build_plan(self):
        ui = self.ui
        with ui.card().classes('panel w-full'):
            ui.label('01 / Plan').classes('eyebrow')
            self.heading('Next families')
            ui.label('One or several study languages. Their authoring order stays intact.').classes('muted text-sm')
            self.languages = self.identified(ui.select(
                options=LANGUAGES, value=['de'], multiple=True, label='Study languages',
                on_change=lambda: self.invalidate_plan(),
            ).props('outlined use-chips').classes('w-full'), 'plan-languages')
            with ui.row().classes('gap-2'):
                self.button('Select all 12', lambda: self.languages.set_value(list(LANGUAGES)), 'plan-all')
                self.button('Clear', lambda: self.languages.set_value([]), 'plan-clear')
            self.count = self.identified(ui.number('Next families per language', value=1, min=1, max=99999, step=1,
                                                 on_change=lambda: self.invalidate_plan()).props('outlined decimals=0').classes('w-full'), 'plan-count')
            self.parallel = self.identified(ui.number('Parallel coordinators · 1–12', value=1, step=1,
                                                    validation=lambda value: integer_error(value, 'Parallel coordinators', 12),
                                                    on_change=lambda: self.invalidate_plan()).props('outlined').classes('w-full'), 'plan-parallel')
            self.parallel_hint = ui.label(coordinator_hint(['de'], 1)).classes('muted text-xs').props('role=status aria-live=polite')
            self.notice('One family = base + 4 returns · 125–250 questions.\nOrder: 20, 40, …, 200, then 10, 30, …, 190. Blocked families are shown, never skipped.')
            self.plan_button = self.button('Preview next families', self.preview_plan, 'plan-preview', primary=True).classes('w-full')
            self.plan_status = ui.label('Preview is read-only. No coordinator or paid request is started.').classes('muted text-xs').props('role=status aria-live=polite')
            self.plan_body = ui.column().classes('w-full gap-3')
            self.confirm_plan = self.button('Review queue scope', self.show_confirmation, 'plan-confirm').classes('w-full')
            self.confirm_plan.disable()

    async def preview_plan(self):
        if self.busy:
            return
        self.busy = True
        self.plan_button.disable()
        self.confirm_plan.disable()
        self.plan_status.set_text('Checking vocabulary, authoring order and existing ownership…')
        self.draft = None
        self.plan_body.clear()
        selection = (list(self.languages.value), self.count.value, self.parallel.value)
        try:
            draft = await self.controller.plan(*selection)
            if selection != (list(self.languages.value), self.count.value, self.parallel.value):
                self.plan_status.set_text('Selection changed while checking. Preview again.')
                return
            self.draft = draft
            with self.plan_body:
                for group in draft.plan['languages']:
                    with self.ui.column().classes('w-full gap-1'):
                        self.ui.label(LANGUAGES[group['language']]).classes('font-semibold')
                        bases = ', '.join(str(row['base_checkpoint']) for row in group['families'])
                        self.ui.label(f'Base checkpoints: {bases or "None available"}').classes('text-sm')
                        reasons = list(dict.fromkeys([group.get('blocked_reason')] + [row.get('reason') for row in group['families']]))
                        for reason in filter(None, reasons):
                            self.notice(safe_text(reason), 'warning')
            total = len(draft.plan['families'])
            self.plan_status.set_text(f'{total} exact families · {total * 5} stages · up to {total * 40} translation attempts.')
            self.confirm_plan.set_enabled(total > 0)
        except Exception as error:
            self.plan_status.set_text(safe_error(error))
        finally:
            self.busy = False
            self.validate_plan()

    def show_confirmation(self):
        if self.draft is None:
            return
        ui = self.ui
        draft = self.draft
        rows = draft.plan['families']
        with ui.dialog().props('persistent') as dialog, ui.card().classes('panel dialog-card'):
            ui.label('02 / Explicit authorization').classes('eyebrow')
            self.heading('Create a paused queue')
            self.notice('Creation records the approved scope only. Nothing starts until you press Resume scheduling. Blocked jobs remain visible and do not bypass their checks.')
            ui.table(columns=[
                {'name': 'language', 'label': 'Study', 'field': 'language', 'align': 'left'},
                {'name': 'base_checkpoint', 'label': 'Base', 'field': 'base_checkpoint', 'align': 'left'},
                {'name': 'state', 'label': 'Preflight', 'field': 'state', 'align': 'left'},
            ], rows=[{key: row[key] for key in ('run', 'language', 'base_checkpoint', 'state')} for row in rows],
                row_key='run', pagination=10).classes('w-full')
            total = len(rows)
            self.notice(f'{total} families / {total * 5} stages / {total * 125:,}–{total * 250:,} questions.\n'
                        f'Ten full-family translations per family, up to four total attempts per locale: at most {total * 40} provider attempts. '
                        'Retries can cost again; uncertain responses may already be billed. Coordinator and reviewer model usage is additional. '
                        'This is an attempt limit, not a price estimate.', 'warning')
            ui.label(coordinator_hint(draft.languages, draft.parallel)).classes('text-sm')
            ack = self.identified(ui.checkbox('I authorize this exact family scope and bounded paid translation attempts.'), 'paid-scope-ack')
            error_label = ui.label('').classes('text-negative text-sm').props('role=alert')
            with ui.row().classes('w-full justify-end gap-2'):
                cancel = self.button('Back', dialog.close, 'create-back')
                async def create():
                    create_button.disable()
                    cancel.disable()
                    try:
                        batch_id = await self.controller.create(draft, ack.value)
                        dialog.close()
                        self.invalidate_plan()
                        self.batch_select.set_value(batch_id)
                        await self.refresh(force=True)
                        ui.notify('Queue created paused. Resume scheduling is a separate action.', type='positive')
                    except Exception as error:
                        error_label.set_text(safe_error(error))
                    finally:
                        create_button.set_enabled(bool(ack.value))
                        cancel.enable()
                create_button = self.button('Create paused queue', create, 'create-queue', primary=True)
                create_button.disable()
                ack.on_value_change(lambda event: create_button.set_enabled(event.value is True))
        dialog.open()

    def build_queue(self):
        ui = self.ui
        with ui.column().classes('w-full gap-6 min-w-0'):
            with ui.card().classes('panel w-full'):
                with ui.row().classes('w-full justify-between items-center'):
                    with ui.column().classes('gap-1'):
                        ui.label('03 / Run deliberately').classes('eyebrow')
                        self.heading('Queue control')
                    self.button('Refresh', lambda: self.refresh(force=True), 'queue-refresh', icon='refresh')
                self.cap_notice = self.notice('Use one batch for the clearest concurrency control. Across resumed unfinished batches, the smallest coordinator cap applies globally.')
                self.batch_select = self.identified(ui.select({}, label='Batch', on_change=lambda: self.update_batch()).props('outlined').classes('w-full'), 'batch-select')
                self.batch_status = ui.label('No queue yet. Start with a read-only plan.').classes('muted text-sm')
                self.show_active_batch = self.button('Show active batch', self.select_active_batch, 'batch-show-active', icon='arrow_forward')
                self.show_active_batch.set_visibility(False)
                with ui.row().classes('w-full items-center gap-3'):
                    self.batch_parallel = self.identified(ui.number('Coordinator cap · 1–12', step=1, value=1,
                        validation=lambda value: integer_error(value, 'Parallel coordinators', 12),
                        on_change=lambda: self.validate_batch_cap()).props('outlined').classes('w-48'), 'batch-parallel')
                    self.parallel_save = self.button('Apply cap', lambda: self.batch_mutation('parallel'), 'batch-parallel-save')
                self.batch_cap_hint = ui.label('').classes('muted text-xs').props('role=status aria-live=polite')
                with ui.row().classes('gap-2'):
                    self.resume_batch = self.button('Resume scheduling', lambda: self.batch_mutation('resume'), 'batch-resume', primary=True, icon='play_arrow')
                    self.pause_batch = self.button('Pause scheduling', lambda: self.batch_mutation('pause'), 'batch-pause', icon='pause')
                    self.button('Pause all scheduling', lambda: self.batch_mutation('pause_all'), 'queue-pause-all')
                ui.label('Pause prevents new launches; it does not stop active work. Use a job’s Stop action only when you intend to interrupt its coordinator.').classes('muted text-xs')
                self.progress = ui.linear_progress(value=0, show_value=False).classes('w-full')
                self.progress_label = ui.label('0 of 0 families verified complete').classes('muted text-xs')
                with ui.column().classes('w-full gap-1'):
                    self.heading('Active / recovery workers', 3)
                    self.active_scope = ui.label(active_scope([], None)).classes('muted text-xs')
                self.active_table = self.job_table('active-jobs-table', None, 'Active and reserved coordinators in the selected batch')
                self.active_table.props('no-data-label="No running, launching, reserved or recovery jobs in this batch."')
                with ui.column().classes('w-full gap-1'):
                    self.heading('All batch jobs', 3)
                    ui.label('Every job in queue order, including queued and finished ones, eight per page.').classes('muted text-xs')
                self.jobs_table = self.job_table('jobs-table', 8, 'All jobs in the selected batch')
            with ui.card().classes('panel w-full'):
                self.heading('Job details')
                self.job_select = self.identified(ui.select({}, label='Inspect a job', on_change=lambda: self.update_job()).props('outlined').classes('w-full'), 'job-select')
                self.job_details = ui.column().classes('w-full gap-3')
                with ui.row().classes('gap-2'):
                    self.inspect_job_button = self.button('Inspect session / recovery', self.inspect_recovery, 'job-inspect', icon='manage_search')
                    self.resume_job_button = self.button('Resume in queue', lambda: self.confirm_job_action('resume'), 'job-resume')
                    self.stop_job_button = self.button('Stop active coordinator', lambda: self.confirm_job_action('stop'), 'job-stop', color='negative')
                ui.label('Verification reads the real family registry and immutable bundle. It never forces a job complete. Raw logs and credentials are never served here.').classes('muted text-xs')
                self.update_job()

    def job_table(self, identifier, pagination, label):
        table = self.identified(self.ui.table(columns=[dict(column) for column in JOB_COLUMNS], rows=[], row_key='id',
                                              pagination=pagination, selection='single', on_select=self.select_table_job),
                                identifier, label)
        table.add_slot('body-cell-status', '<q-td :props="props"><span :class="\'state-chip state-\' + props.row.state">{{ props.row.status }}</span></q-td>')
        table.add_slot('body-cell-session_id', '''<q-td :props="props">
            <div v-if="props.row.session_id"><div class="muted text-xs">{{ props.row.session_label }}</div>
            <span class="mono session-code">{{ props.row.session_id }}</span>
            <q-btn flat dense round icon="content_copy" :aria-label="'Copy session ID ' + props.row.session_id"
                @click.stop="$parent.$emit('copy-session', props.row.id)" /></div>
            <span v-else class="muted">Not recorded</span></q-td>''')
        table.on('copy-session', self.copy_table_session)
        return table

    def copy_table_session(self, event):
        job = next((row for row in self.snapshot_data['jobs'] if row['id'] == event.args), None)
        if job and job.get('session_id'):
            self.ui.clipboard.write(safe_text(job['session_id']))

    def copyable(self, label, value, identifier):
        with self.ui.row().classes('w-full items-center gap-2 no-wrap'):
            self.identified(self.ui.input(label, value=value).props('outlined readonly').classes('grow min-w-0 mono'), identifier)
            self.button('Copy', lambda: self.ui.clipboard.write(value), identifier + '-copy', icon='content_copy').props(f'aria-label="Copy {label}"')

    def select_table_job(self, event):
        if event.selection:
            self.job_select.set_value(event.selection[0]['id'])

    def sync_table_selection(self):
        for table in (self.active_table, self.jobs_table):
            selected = [row for row in table.rows if row['id'] == self.job_select.value]
            if table.selected != selected:
                table.selected = selected
                table.update()

    def set_table_rows(self, table, rows):
        if table.rows != rows:
            table.rows = rows
            table.update()

    def validate_batch_cap(self):
        batch = next((row for row in self.snapshot_data['batches'] if row['id'] == self.batch_select.value), None)
        error = integer_error(self.batch_parallel.value, 'Parallel coordinators', 12)
        self.parallel_save.set_enabled(bool(batch and not error and self.runtime.ready))
        self.batch_cap_hint.set_text(coordinator_hint(batch['languages'], self.batch_parallel.value) if batch else '')

    def select_active_batch(self):
        _, preferred = batch_views(self.snapshot_data)
        if preferred is not None:
            self.batch_select.set_value(preferred)
            active = next((job for job in self.snapshot_data['jobs'] if job['batch_id'] == preferred and job['state'] in ACTIVE_STATES), None)
            if active:
                self.job_select.set_value(active['id'])

    def update_batch(self):
        _, preferred = batch_views(self.snapshot_data)
        elsewhere = sum(job['batch_id'] != self.batch_select.value and job['state'] in ACTIVE_STATES for job in self.snapshot_data['jobs'])
        here = any(job['batch_id'] == self.batch_select.value and job['state'] in ACTIVE_STATES for job in self.snapshot_data['jobs'])
        self.show_active_batch.set_visibility(bool(elsewhere and not here and preferred != self.batch_select.value))
        self.show_active_batch.set_text(f'Show running batch · {elsewhere} running elsewhere')
        batch = next((row for row in self.snapshot_data['batches'] if row['id'] == self.batch_select.value), None)
        for control in (self.batch_parallel, self.parallel_save, self.resume_batch, self.pause_batch):
            control.set_enabled(batch is not None and self.runtime.ready)
        self.active_scope.set_text(active_scope(self.snapshot_data['jobs'], batch['id'] if batch else None))
        if batch is None:
            self.batch_status.set_text('No queue yet. Start with a read-only plan.')
            for table in (self.active_table, self.jobs_table):
                if table.rows:
                    table.rows = []
                    table.selected = []
                    table.update()
            self.last_batch_id = None
            self.progress.set_value(0)
            self.progress_label.set_text('0 of 0 families verified complete')
            self.validate_batch_cap()
            return
        if batch['id'] != self.last_batch_id:
            self.batch_parallel.set_value(batch['max_parallel'])
            self.last_batch_id = batch['id']
        mode = 'Paused — new launches disabled' if batch['paused'] else 'Resumed — eligible jobs may launch'
        self.batch_status.set_text(f'{mode} · cap {batch["max_parallel"]} · {safe_text(batch["id"], 80)}')
        self.resume_batch.set_enabled(batch['paused'] and self.runtime.ready)
        self.pause_batch.set_enabled(not batch['paused'] and self.runtime.ready)
        self.validate_batch_cap()
        rows = [safe_job(row) for row in self.snapshot_data['jobs'] if row['batch_id'] == batch['id']]
        table_rows = [{key: row[key] for key in TABLE_FIELDS} for row in rows]
        self.set_table_rows(self.active_table, [row for row in table_rows if is_active_worker(row)])
        self.set_table_rows(self.jobs_table, table_rows)
        self.sync_table_selection()
        complete = sum(row['state'] == 'succeeded' for row in rows)
        self.progress.set_value(complete / len(rows) if rows else 0)
        self.progress_label.set_text(f'{complete} of {len(rows)} families verified complete')

    def update_job(self):
        job = next((row for row in self.snapshot_data['jobs'] if row['id'] == self.job_select.value), None)
        self.resume_job_button.set_enabled(bool(job and may_resume(job) and self.runtime.ready))
        metadata = (job.get('launch_metadata') or {}) if job else {}
        awaiting_terminal = metadata.get('mode') == 'external' and metadata.get('phase') == 'awaiting_terminal'
        external_stopped = metadata.get('mode') == 'external' and metadata.get('phase') == 'stopped'
        self.stop_job_button.set_text('Cancel unstarted handoff' if awaiting_terminal else 'End recovery without resolving' if external_stopped else 'Stop active coordinator')
        self.stop_job_button.set_enabled(bool(job and job['state'] in ACTIVE_STATES and self.runtime.ready))
        self.inspect_job_button.set_enabled(bool(job and self.runtime.ready and not getattr(self, 'inspecting', False)))
        self.sync_table_selection()
        signature = json.dumps(safe_job(job), sort_keys=True) if job else ''
        if signature == self.last_job:
            return
        self.last_job = signature
        self.job_details.clear()
        with self.job_details:
            if job is None:
                self.ui.label('Select a queued family to inspect its exact attempt and session.').classes('muted text-sm')
                return
            row = safe_job(job)
            self.ui.label(row['status']).classes('state-chip state-' + row['state'])
            with self.ui.element('div').classes('detail-grid'):
                for label, value in [('Family', f'{row["study"]} / base {row["base_checkpoint"]}'),
                                     ('Run', row['run']), ('Attempt', row['attempt_id'] or 'Not launched'),
                                     ('Attempts', str(row['attempt_count']))]:
                    self.ui.label(label).classes('muted')
                    self.ui.label(value).classes('detail-value mono')
            if row['session_id']:
                self.copyable(row['session_label'] + ' · OpenCode ID', row['session_id'], 'job-session')
            else:
                self.ui.label('OpenCode session ID: not recorded. No session identity will be guessed.').classes('muted text-sm')
            if row['external'] and row['state'] in ACTIVE_STATES:
                self.notice('Manual terminal recovery is reserved for this exact session. Its language and coordinator slot stay held until verified resolution.', 'warning')
            if row['external'] and row['state'] in ACTIVE_STATES and row['phase'] == 'stopped':
                self.ui.label('Terminal work stopped. Inspect and verify the repaired family, or end recovery without resolving to keep this family blocked for another same-session repair.').classes('muted text-sm')
            for reason in dict.fromkeys(filter(None, (row['reason'], row['diagnostic']))):
                self.notice(reason, 'warning')
            if job['state'] in {'blocked', 'interrupted'} and not may_resume(job):
                self.notice('Resume blocked: this attempt has no unambiguous recorded session ID. No fresh coordinator will be started to guess its identity.', 'error')
            if row['attempts']:
                with self.ui.expansion('Attempt history · saved session IDs', icon='history').classes('w-full'):
                    for index, attempt in enumerate(reversed(row['attempts'])):
                        self.ui.label(' · '.join(f'{key}: {value}' for key, value in attempt.items() if key != 'session_id')).classes('mono detail-value')
                        if attempt.get('session_id'):
                            self.copyable('Attempt session ID', attempt['session_id'], f'attempt-session-{index}')

    async def inspect_recovery(self):
        job_id = self.job_select.value
        if not job_id or getattr(self, 'inspecting', False):
            return
        self.inspecting = True
        self.inspect_job_button.disable()
        try:
            details = await self.controller.recovery_details(job_id)
            self.show_recovery(details)
        except Exception as error:
            self.ui.notify(safe_error(error), type='negative')
        finally:
            self.inspecting = False
            self.inspect_job_button.set_enabled(bool(self.job_select.value and self.runtime.ready))

    def show_recovery(self, details):
        ui = self.ui
        process = details['process']
        with ui.dialog().props('persistent') as dialog, ui.card().classes('panel dialog-card'):
            self.heading('Inspect the saved session')
            ui.label('Read-only inspection. Opening this dialog does not resume work or resolve the job.').classes('muted text-sm')
            ui.label(f'Job: {safe_text(details["job_id"])} · attempt: {safe_text(details.get("attempt_id")) or "not launched"}').classes('mono detail-value')
            if details.get('session_id'):
                self.copyable('OpenCode session ID', safe_text(details['session_id']), 'recovery-session')
            else:
                self.notice('No saved OpenCode session ID. Session identity cannot be supplied or guessed.', 'warning')
            ui.label(' · '.join(f'{name}: {safe_text(process.get(name))}' for name in ('supervisor', 'child', 'group', 'lock'))).classes('text-sm detail-value')
            if process.get('diagnostic'):
                self.notice(safe_text(process['diagnostic']), 'warning')
            if details.get('inspect_command'):
                with ui.expansion('View session locally · read-only export', icon='visibility').classes('w-full'):
                    ui.label('Run this exact command in your terminal. Export does not continue the session; its output may contain private content. Do not share it unredacted.').classes('muted text-sm')
                    self.copyable('Read-only inspection command', details['inspect_command'], 'session-inspect-command')
            if details.get('resume_command'):
                self.notice('Manual recovery reserved. Run this tracked command once in your terminal. It reopens the same session; no prompt is sent automatically. The coordinator slot stays reserved until verified resolution.', 'warning')
                self.copyable('Tracked terminal recovery command', details['resume_command'], 'session-resume-command')
            self.notice('Manual recovery never bypasses verification. Further session work may cost within the original authorization; saved payment journals and retry limits remain unchanged.')
            with ui.row().classes('gap-2'):
                handoff = self.button('Reserve manual terminal recovery…', lambda: self.confirm_recovery_action('handoff', details, dialog), 'recovery-handoff')
                handoff.set_enabled(details.get('can_handoff') is True)
                resolve = self.button('Verify repaired family…', lambda: self.confirm_recovery_action('reconcile', details, dialog), 'recovery-resolve')
                resolve.set_enabled(details.get('can_resolve') is True)
            if not details.get('can_handoff') and details.get('handoff_reason'):
                ui.label(safe_text(details['handoff_reason'])).classes('muted text-sm')
            if not details.get('can_resolve'):
                ui.label('Verified resolution is unavailable while process ownership is active, unproven or ineligible. Stop work and inspect again; no force-complete action exists.').classes('muted text-sm')
            with ui.row().classes('w-full justify-end'):
                self.button('Close inspection', dialog.close, 'recovery-close')
        dialog.open()

    def confirm_recovery_action(self, action, details, inspection):
        ui = self.ui
        handoff = action == 'handoff'
        with ui.dialog().props('persistent') as dialog, ui.card().classes('panel dialog-card'):
            self.heading('Reserve this session for terminal recovery?' if handoff else 'Verify the repaired family?')
            ui.label(f'Exact job: {safe_text(details["job_id"])} · session: {safe_text(details.get("session_id")) or "not recorded"}').classes('mono detail-value')
            if handoff:
                self.notice('This reserves a tracked manual attempt but launches nothing. The original session and environment must be verified and stopped. Its language and coordinator slot remain held until verified resolution; do not run an untracked replacement.', 'warning')
                note = None
                ack_text = 'I authorize manual recovery of this exact session with its existing paid-request limits.'
            else:
                self.notice('All relevant processes must be verified stopped. The backend independently checks the complete immutable family, registry and publication receipt. A note or a model reply is not proof of completion; failed verification leaves the job unresolved.', 'warning')
                note = self.identified(ui.textarea('What was repaired or verified?').props('outlined maxlength=1000').classes('w-full'), 'recovery-note')
                ack_text = 'I have stopped manual work and request independent verification, not forced completion.'
            ack = self.identified(ui.checkbox(ack_text), 'recovery-action-ack')
            error_label = ui.label('').classes('text-negative text-sm').props('role=alert')
            submitting = False
            def validate():
                confirm.set_enabled(not submitting and ack.value is True and (handoff or bool(note.value.strip())))
            with ui.row().classes('w-full justify-end gap-2'):
                cancel = self.button('Cancel', dialog.close, 'recovery-action-cancel')
                async def submit():
                    nonlocal submitting
                    if submitting or self.busy:
                        return
                    submitting = True
                    self.busy = True
                    confirm.disable()
                    cancel.disable()
                    try:
                        result = await self.controller.job_action(action, details['job_id'], ack.value, '' if note is None else note.value)
                        dialog.close()
                        inspection.close()
                        await self.refresh(force=True)
                        if handoff:
                            self.show_recovery(result)
                        else:
                            ui.notify('Family independently verified. Job resolved from its immutable evidence.', type='positive')
                    except Exception as error:
                        error_label.set_text(safe_error(error))
                    finally:
                        self.busy = False
                        submitting = False
                        validate()
                        cancel.enable()
                confirm = self.button('Reserve manual recovery' if handoff else 'Verify and resolve', submit, 'recovery-action-confirm', primary=True)
                confirm.disable()
                ack.on_value_change(validate)
                if note is not None:
                    note.on_value_change(validate)
        dialog.open()

    async def batch_mutation(self, action):
        if self.busy:
            return
        self.busy = True
        try:
            await self.controller.batch_action(action, self.batch_select.value, self.batch_parallel.value)
            await self.refresh(force=True)
            self.ui.notify('Scheduling settings saved.', type='positive')
        except Exception as error:
            self.ui.notify(safe_error(error), type='negative')
        finally:
            self.busy = False

    def confirm_job_action(self, action):
        job_id = self.job_select.value
        if not job_id:
            return
        ui = self.ui
        job = next((row for row in self.snapshot_data['jobs'] if row['id'] == job_id), {})
        metadata = job.get('launch_metadata') or {}
        awaiting_terminal = action == 'stop' and metadata.get('mode') == 'external' and metadata.get('phase') == 'awaiting_terminal'
        external_stopped = action == 'stop' and metadata.get('mode') == 'external' and metadata.get('phase') == 'stopped'
        with ui.dialog().props('persistent') as dialog, ui.card().classes('panel dialog-card'):
            self.heading('Resume the exact job' if action == 'resume' else 'Cancel unstarted handoff?' if awaiting_terminal else 'End recovery without resolving?' if external_stopped else 'Stop active coordinator?')
            if action == 'resume':
                self.notice('Resume retains this job, its exact recorded session and existing payment journals. '
                            'It does not create a fresh attempt to bypass a blocker. A never-launched job must pass preflight. '
                            'Scheduling stays subject to the batch’s paused state.', 'warning')
                ack = ui.checkbox('I have addressed the reported blocker and authorize this exact job to resume.')
            elif awaiting_terminal:
                self.notice('If the terminal helper has not started, this cancels its reservation without completing the family and invalidates the copied command. If it starts before confirmation, Stop interrupts its tracked work instead; in-flight work may already be billed.', 'warning')
                ack = ui.checkbox('I authorize cancellation, or stopping this exact coordinator if the terminal has started.')
            elif external_stopped:
                self.notice('The backend must verify the tracked recovery process has stopped. This ends the attempt as interrupted, never complete. This family and its successors remain blocked; its exact saved session and payment journals can be used for another explicit repair.', 'warning')
                ack = ui.checkbox('I want to end this stopped recovery attempt without marking the family complete.')
            else:
                self.notice('This interrupts the supervised coordinator, not just scheduling. In-flight work may already be billed. '
                            'Evidence and session identity are preserved; ambiguous work cannot be restarted automatically.', 'warning')
                ack = ui.checkbox('I understand this stops active work and may leave an uncertain paid request.')
            self.identified(ack, f'job-{action}-ack')
            with ui.row().classes('w-full justify-end gap-2'):
                self.button('Cancel', dialog.close, f'job-{action}-cancel')
                async def confirm():
                    button.disable()
                    success = await self.job_mutation(action, job_id=job_id, acknowledged=ack.value)
                    if success:
                        dialog.close()
                    else:
                        button.set_enabled(bool(ack.value))
                button = self.button('Confirm resume' if action == 'resume' else 'Confirm cancellation / stop' if awaiting_terminal else 'End without resolving' if external_stopped else 'Confirm stop', confirm, f'job-{action}-confirm', primary=True)
                button.disable()
                ack.on_value_change(lambda event: button.set_enabled(event.value is True))
        dialog.open()

    async def job_mutation(self, action, job_id=None, acknowledged=False):
        if self.busy:
            return False
        self.busy = True
        try:
            await self.controller.job_action(action, job_id or self.job_select.value, acknowledged)
            await self.refresh(force=True)
            self.ui.notify('Job state refreshed from its evidence.', type='positive')
            return True
        except Exception as error:
            self.ui.notify(safe_error(error), type='negative')
            return False
        finally:
            self.busy = False

    def build_library(self):
        ui = self.ui
        with ui.column().classes('w-full gap-6'):
            with ui.card().classes('panel w-full'):
                with ui.row().classes('w-full items-center justify-between'):
                    with ui.column().classes('gap-1'):
                        ui.label('Read-only library').classes('eyebrow')
                        self.heading('See the actual lesson')
                    self.button('Refresh library', self.load_catalog, 'library-refresh', icon='refresh')
                ui.label('Original drafts and pack files. Missing or invalid content is shown as a diagnostic, never fabricated.').classes('muted text-sm')
                self.correction_opt_in = self.identified(ui.checkbox(
                    'Include correction previews (explicit edition; not production approved)', value=False,
                    on_change=self.correction_opt_in_changed,
                ), 'library-correction-opt-in')
                ui.label('Correction rows are unverified until rendered. Opt-in does not select an edition, adopt content, change history or authorize queue work.').classes('muted text-xs')
                self.family_select = self.identified(ui.select({}, label='Lesson family', on_change=lambda: self.library_selection()).props('outlined').classes('w-full'), 'library-family')
                with ui.row().classes('w-full gap-4 items-center'):
                    self.locale_select = self.identified(ui.select({}, label='Learner locale', on_change=lambda: self.invalidate_preview()).props('outlined').classes('grow min-w-48'), 'library-locale')
                    self.stage_select = self.identified(ui.select({}, label='Stage', on_change=lambda: self.invalidate_preview()).props('outlined').classes('grow min-w-48'), 'library-stage')
                    self.render_button = self.button('Render preview', self.render_preview, 'library-render', primary=True)
                    self.render_button.disable()
                    self.reload_preview_button = self.button('Reload selected files', lambda: self.render_preview(force=True), 'library-reload', icon='refresh')
                    self.reload_preview_button.disable()
                self.identified(ui.label(stage_hint()).classes('muted text-xs'), 'library-stage-hint')
                self.library_status = ui.label('Loading actual files…').classes('muted text-sm').props('role=status aria-live=polite')
                self.notice('Preview only. Answer interactions stay inside a sandbox and do not save learner progress. Queue polling keeps this loaded view; Reload selected files reads fresh content. This is not native app or screen-reader verification.')
            self.preview_host = ui.column().classes('w-full')
            with self.preview_host:
                with ui.column().classes('empty panel'):
                    ui.icon('auto_stories', size='36px')
                    self.heading('A reading room, not a published app', 3)
                    ui.label('Choose an actual family, learner locale and stage, then render.').classes('text-sm')

    def correction_opt_in_changed(self, event):
        if event.value is not True:
            # Revoke synchronously at the checkbox event, before NiceGUI schedules
            # async catalog work. Reselecting the same tuple cannot revive it.
            self.preview_generation += 1
            self.apply_catalog([row for row in self.catalog_data if row.get('kind') != 'correction'])
        return self.load_catalog()

    def apply_catalog(self, records):
        self.catalog_data = records
        options = {row['relative']: safe_text(row['label'], 300) for row in records}
        # A correction edition always requires selecting its exact dropdown row.
        original = next((row['relative'] for row in records if row.get('kind') != 'correction'), None)
        value = self.family_select.value if self.family_select.value in options else original
        if self.family_select.options != options or self.family_select.value != value:
            self.family_select.set_options(options, value=value)
        self.library_selection()
        return options

    async def load_catalog(self):
        # Most recent opt-in/refresh wins; an older asynchronous result cannot re-add rows.
        request = getattr(self, 'catalog_request', 0) + 1
        self.catalog_request = request
        self.catalog_loading = True
        include_corrections = self.correction_opt_in.value is True
        if not include_corrections:
            # Revoke correction selection before waiting on any filesystem work.
            self.apply_catalog([row for row in self.catalog_data if row.get('kind') != 'correction'])
        self.library_status.set_text('Reading available original files' + (' and bounded correction receipts…' if include_corrections else '…'))
        try:
            records = await self.controller.catalog(include_corrections)
            if request != self.catalog_request or include_corrections != (self.correction_opt_in.value is True):
                return
            options = self.apply_catalog(records)
            if self.rendered_selection is not None:
                self.library_status.set_text(f'{len(options)} actual records. Loaded preview preserved; use Reload selected files to read content changes.')
            else:
                self.library_status.set_text(f'{len(options)} actual records. Select and render to inspect.' if options else 'No lesson families are available yet.')
        except Exception as error:
            if request == self.catalog_request:
                # A failed refresh must not leave revoked/stale correction rows selectable.
                self.apply_catalog([])
                self.library_status.set_text(safe_error(error))
        finally:
            if request == self.catalog_request:
                self.catalog_loading = False

    def library_selection(self):
        record = next((row for row in self.catalog_data if row['relative'] == self.family_select.value), None)
        locales = {code: LANGUAGES.get(code, code) for code in record['locales']} if record else {}
        base_checkpoint = record.get('base_checkpoint') if record else None
        if base_checkpoint is None and record:
            match = re.search(r'/(\d{6})$', str(record.get('relative', '')))
            base_checkpoint = int(match.group(1)) if match else 0
        stages = {stage: stage_label(base_checkpoint, stage) for stage in record['stages']} if record else {}
        locale = self.locale_select.value if self.locale_select.value in locales else next(iter(locales), None)
        stage = self.stage_select.value if self.stage_select.value in stages else next(iter(stages), None)
        if self.locale_select.options != locales or self.locale_select.value != locale:
            self.locale_select.set_options(locales, value=locale)
        if self.stage_select.options != stages or self.stage_select.value != stage:
            self.stage_select.set_options(stages, value=stage)
        self.invalidate_preview()

    def update_preview_buttons(self):
        correction = str(self.family_select.value).startswith('correction-edition:')
        available = bool(self.locale_select.options and self.stage_select.options and not getattr(self, 'rendering', False) and
                         (not correction or self.correction_opt_in.value is True))
        selected = (self.family_select.value, self.locale_select.value, self.stage_select.value)
        self.render_button.set_enabled(available and selected != self.rendered_selection)
        self.reload_preview_button.set_enabled(available)

    def invalidate_preview(self):
        selected = (self.family_select.value, self.locale_select.value, self.stage_select.value)
        if selected != self.preview_selection:
            self.preview_selection = selected
            self.preview_generation += 1
        if self.rendered_selection is not None and selected != self.rendered_selection:
            self.rendered_selection = None
            self.preview_host.clear()
            self.library_status.set_text('Selection changed. Render again to inspect the selected content.')
            with self.preview_host:
                self.notice('Preview cleared because the family, learner locale or stage changed. Render the new selection.')
        self.update_preview_buttons()

    async def render_preview(self, force=False):
        selected = (self.family_select.value, self.locale_select.value, self.stage_select.value)
        if (selected[0] not in self.family_select.options or selected[1] not in self.locale_select.options or
                selected[2] not in self.stage_select.options or
                (str(selected[0]).startswith('correction-edition:') and self.correction_opt_in.value is not True) or
                getattr(self, 'rendering', False) or
                (not force and selected == self.rendered_selection)):
            return
        generation = self.preview_generation
        self.rendering = True
        self.update_preview_buttons()
        self.library_status.set_text('Rendering the selected grammar lesson and quiz…')
        try:
            document = await self.controller.render(*selected)
            if (generation != self.preview_generation or
                    selected != (self.family_select.value, self.locale_select.value, self.stage_select.value) or
                    (str(selected[0]).startswith('correction-edition:') and self.correction_opt_in.value is not True)):
                self.library_status.set_text('Selection changed. Render again for the new selection.')
                return
            self.preview_host.clear()
            self.rendered_selection = selected
            with self.preview_host:
                self.ui.html(document, sanitize=False).classes('w-full')
            self.library_status.set_text(f'Read-only preview · {safe_text(selected[0])} · learner {selected[1]} · {self.stage_select.options.get(selected[2], f"stage {selected[2]}")}')
        except Exception as error:
            prefix = 'Reload failed; the previous loaded preview is unchanged. ' if self.rendered_selection is not None else ''
            self.library_status.set_text(prefix + safe_error(error))
        finally:
            self.rendering = False
            self.update_preview_buttons()

    async def initial_load(self):
        await self.refresh(force=True)
        await self.load_catalog()

    async def refresh(self, force=False):
        if self.refreshing or (self.busy and not force):
            return
        self.refreshing = True
        try:
            snapshot = await self.controller.snapshot()
            summary = queue_summary(snapshot)
            if self.runtime.error:
                self.runtime_notice.set_text('Scheduler needs attention: ' + self.runtime.error)
            elif not self.runtime.ready:
                self.runtime_notice.set_text('Recovering local queue evidence. New work is not authorized on startup.')
            else:
                self.runtime_notice.set_text(f'Local scheduler ready · {summary["active"]} active or reserved coordinator slots · effective cap {summary["cap"]}. '
                                             'Queues start paused; only Resume scheduling enables new launches.')
            safe_snapshot = {'batches': [{key: value for key, value in row.items() if key != 'authorization'} for row in snapshot['batches']],
                             'jobs': [safe_job(row) for row in snapshot['jobs']]}
            signature = json.dumps((self.runtime.ready, safe_snapshot), sort_keys=True)
            if not force and signature == self.last_snapshot:
                return
            self.last_snapshot = signature
            self.snapshot_data = snapshot
            for key, label in self.stats.items():
                label.set_text(str(summary[key]))
            options, preferred = batch_views(snapshot)
            selected = self.batch_select.value if self.batch_select.value in options else preferred
            if self.batch_select.options != options or self.batch_select.value != selected:
                self.batch_select.set_options(options, value=selected)
            jobs = {row['id']: f'{LANGUAGES.get(row["language"], row["language"])} · base {row["base_checkpoint"]} · {STATE_LABELS.get(row["state"], "Unknown")}' for row in snapshot['jobs']}
            visible = [row for row in snapshot['jobs'] if row['batch_id'] == selected]
            first_job = next((row['id'] for row in visible if row['state'] in ACTIVE_STATES), visible[0]['id'] if visible else next(iter(jobs), None))
            job_id = self.job_select.value if self.job_select.value in jobs else first_job
            if self.job_select.options != jobs or self.job_select.value != job_id:
                self.job_select.set_options(jobs, value=job_id)
            self.update_batch()
            self.update_job()
        except Exception as error:
            self.runtime_notice.set_text('Queue refresh failed: ' + safe_error(error))
        finally:
            self.refreshing = False


class LocalOnlyMiddleware:
    def __init__(self, app, port):
        self.app = app
        self.hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}

    async def __call__(self, scope, receive, send):
        if scope['type'] not in {'http', 'websocket'}:
            await self.app(scope, receive, send)
            return
        headers = [(key.lower(), value.decode('latin1')) for key, value in scope.get('headers', [])]
        hosts = [value for key, value in headers if key == b'host']
        origins = [value for key, value in headers if key == b'origin']
        valid = len(hosts) == 1 and hosts[0] in self.hosts and len(origins) <= 1
        valid = valid and (not origins or origins[0] == f'http://{hosts[0]}')
        if not valid:
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
            else:
                await send({'type': 'http.response.start', 'status': 403, 'headers': [(b'content-type', b'text/plain')]})
                await send({'type': 'http.response.body', 'body': b'Local dashboard origin required.'})
            return

        async def secured_send(message):
            if message['type'] == 'http.response.start':
                message = {**message, 'headers': [*message.get('headers', []),
                           (b'content-security-policy', b"frame-ancestors 'none'"),
                           (b'x-frame-options', b'DENY'),
                           (b'referrer-policy', b'no-referrer')]}
            await send(message)

        await self.app(scope, receive, secured_send)


def install_dashboard(root, store, runner, preview, port=8091):
    from nicegui import app, core, ui
    app.add_middleware(LocalOnlyMiddleware, port=port)
    core.sio.eio.cors_allowed_origins = [f'http://127.0.0.1:{port}', f'http://localhost:{port}']
    controller = DashboardController(store, runner, preview)
    runtime = DashboardRuntime(controller)
    app.on_startup(runtime.start)
    app.on_shutdown(runtime.stop)

    @ui.page('/', title='CherryLex · Lesson studio')
    async def index():
        DashboardPage(controller, runtime)

    return runtime
