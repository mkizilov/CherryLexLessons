"""Invocation-local reuse of a proved source contract, never a persistent cache.

Every warm gate still validates the full frozen family/history and exact current
source/review/tool bytes. Only repeated source semantic parsing is avoided.
"""
import os
import threading
import time

from . import core, family
from .translation import TranslationError

VALIDATOR_FILES = (
    'tools/validate-family.cjs', 'tools/content-contract.cjs',
    'tools/lesson-policy.cjs', 'tools/quiz-policy.cjs',
)


class SourceValidation:
    def __init__(self, root, run, context, stop):
        self.root, self.run, self.context, self.stop = root, run, context, stop
        self.lock = threading.Lock()
        self.proof = None
        self.original = None
        self.contract = None

    def _snapshot(self, context):
        # Exact legacy wire-evidence shape: changing this would invalidate saved
        # requests. The tooling proof is local and never added to provider input.
        for stage in family.STAGES:
            core.safe_path(self.root, f'{self.run}/stage-{stage}/brief.json')
            for name in ('lesson.md', 'quiz.md', 'adaptation-notes.json'):
                core.safe_path(self.root, f'{self.run}/{context["source_locale"]}/stage-{stage}/{name}')
        original = {
            'binding': {
                'context_hash': context['context_hash'],
                'brief_digests': {f'stage:{stage}': core.digest(family.family_path(self.root, self.run) / f'stage-{stage}/brief.json') for stage in family.STAGES},
                'source_digests': family.family_pair_digests(self.root, self.run, context['source_locale']),
            },
            'context_digest': core.digest(family.family_path(self.root, self.run) / 'family-context.json'),
            'reviews': {role: core.digest(family.family_review_path(self.root, self.run, role)) for role in ('pedagogical', 'assessment', 'format')},
        }
        tools = {path: core.digest(core.safe_path(self.root, path)) for path in VALIDATOR_FILES}
        return original, tools, os.environ.get('NODE_BINARY', 'node')

    def _check(self, expires):
        if self.stop.is_set():
            raise TranslationError('Family source gate cancelled; evidence preserved')
        if time.monotonic() >= expires:
            raise TranslationError('Family source gate deadline exceeded; evidence preserved')

    def establish(self, timeout):
        expires = time.monotonic() + timeout
        try:
            self._check(expires)
            before = self._snapshot(self.context)
            family.require_family_source_reviews(self.root, self.run, timeout=max(.001, expires - time.monotonic()), stop_event=self.stop)
            report = family.validate_family(self.root, self.run, self.context['source_locale'], timeout=max(.001, expires - time.monotonic()), stop_event=self.stop)
            current = family.load_family(self.root, self.run)
            after = self._snapshot(current)
            self._check(expires)
            if current != self.context or before != after:
                raise TranslationError('Family source/context/approval/tooling drift during validation')
            self.proof = after
            self.original = after[0]
            self.contract = report['family_contract']
        except Exception:
            self.stop.set()
            raise

    def check(self, timeout):
        expires = time.monotonic() + timeout
        acquired = False
        try:
            self._check(expires)
            if self.proof is None:
                raise TranslationError('Source proof has not been established')
            while not acquired:
                self._check(expires)
                acquired = self.lock.acquire(timeout=min(.1, max(0, expires - time.monotonic())))
            self._check(expires)
            current = family.load_family(self.root, self.run)
            self._check(expires)
            if current != self.context or self._snapshot(current) != self.proof:
                raise TranslationError('Family source/context/approval/history/tooling drift')
            self._check(expires)
        except Exception:
            self.stop.set()
            raise
        finally:
            if acquired:
                self.lock.release()
