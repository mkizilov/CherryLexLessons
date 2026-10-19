"""Offline recovery of retained complete families from failed/incomplete/completed provider streams.

Sanitized shapes of the de20->ar, en20->de/hi, ar20->es and ar20->de failures. No network, no paid request.
Run: python3 -m unittest tests.test_retained_output_recovery -v
"""
import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from lesson_factory import core, family, translation_recovery as recovery
from lesson_factory.translation import TranslationError, encode
from tests import test_translation_recovery as original


def family_value(text="Synthetic localized teaching"):
    return {"stages": [{"adaptation_notes": [f"note {stage}"], "lesson_md": "" if stage == 3 else f"{text} {stage}", "quiz_md": f"{text} quiz {stage}", "stage": stage} for stage in family.STAGES]}


def skeleton():
    return {"stages": [{"adaptation_notes": [""], "lesson_md": "", "quiz_md": "", "stage": stage} for stage in family.STAGES]}


def wire(*events):
    return b"".join(b"data: " + encode(value) + b"\n\n" for value in events)


def message_events(index, item_id, text, done=True, chunk=40):
    events = [{"type": "response.output_item.added", "output_index": index, "item": {"type": "message", "id": item_id, "status": "in_progress", "content": []}}]
    events += [{"type": "response.output_text.delta", "output_index": index, "content_index": 0, "item_id": item_id, "delta": text[start:start + chunk]} for start in range(0, len(text), chunk)]
    if done:
        events.append({"type": "response.output_text.done", "output_index": index, "content_index": 0, "item_id": item_id, "text": text})
        events.append({"type": "response.output_item.done", "output_index": index, "item": {"type": "message", "id": item_id, "status": "completed", "content": [{"type": "output_text", "text": text}]}})
    return events


def reasoning(index):
    return [{"type": "response.output_item.added", "output_index": index, "item": {"type": "reasoning", "id": f"rs_{index}"}}, {"type": "response.output_item.done", "output_index": index, "item": {"type": "reasoning", "id": f"rs_{index}", "summary": []}}]


STREAM_ERROR = {"type": "error", "sequence_number": 0, "error": {"type": "upstream_error", "message": "stream_read_error", "code": "stream_read_error"}}


class RetainedOutputRecoveryTests(unittest.TestCase):
    setUp = original.TranslationRecoveryTests.setUp
    scaffold = original.TranslationRecoveryTests.scaffold
    attempted = original.TranslationRecoveryTests.attempted
    authorize = original.TranslationRecoveryTests.authorize
    prepare_retry = original.TranslationRecoveryTests.prepare_retry

    def renewal(self, raw, *, state="provider_failed", error_code="stream_read_error", response=None, raw_candidate=None):
        self.prepare_retry()
        grant = self.authorize()
        logs = self.attempted(scope=grant["scope"])
        attempt = logs / "attempt-1"
        (attempt / "raw.sse").write_bytes(raw)
        if response is not None:
            core.write_json(attempt / "response.json", response)
        if raw_candidate is not None:
            core.write_json(attempt / "raw-candidate.json", raw_candidate)
        record = core.read_json(logs / "journal.json")
        record.update(state=state, error_code=error_code, retry_eligible=state == "provider_failed", retry_at=1.0 if state == "provider_failed" else None)
        core.write_json(attempt / "journal.json", record)
        core.write_json(logs / "journal.json", record)
        return grant, logs

    def snapshot(self):
        return recovery._inventory(self.root, ".")

    def test_stream_error_after_complete_item_then_truncated_item_is_offline_not_purchase(self):
        text = json.dumps(family_value())
        raw = wire(*reasoning(0), *message_events(1, "msg_complete", text), *reasoning(2), *message_events(3, "msg_truncated", text[: len(text) // 2], done=False), STREAM_ERROR)
        grant, logs = self.renewal(raw, response={"status": "failed", "error": STREAM_ERROR["error"], "usage": None})
        items = recovery.retained_family_items(logs)
        self.assertEqual([item["candidate"] for item in items], [family_value()])
        self.assertEqual(items[0]["error_code"], "stream_read_error")
        self.assertEqual({source["output_index"] for source in items[0]["sources"]}, {1})
        plan = recovery.plan_translation_recovery(self.root, self.run)
        self.assertEqual(plan["current_dispositions"]["fr"], "offline_recovery")
        self.assertEqual(plan["effective_attempt_limits"]["fr"], 0)
        self.assertIn("no new purchase", plan["current_reasons"]["fr"])
        with self.assertRaisesRegex(TranslationError, "no new purchase"):
            recovery.recovery_for_locale(self.root, self.run, "fr")

    def test_incomplete_max_output_tokens_and_failed_upstream_with_whitespace_item(self):
        text = json.dumps(family_value())
        cases = (
            ("max_output_tokens", {"type": "response.incomplete", "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [{"type": "reasoning"}, {"type": "message", "id": "msg_a", "content": [{"type": "output_text", "text": text}]}, {"type": "message", "id": "msg_b", "content": [{"type": "output_text", "text": text[:100]}]}]}}),
            ("upstream_error", {"type": "response.failed", "response": {"status": "failed", "error": {"code": "upstream_error", "message": "Upstream request failed"}}}),
        )
        for code, terminal in cases:
            with self.subTest(code=code):
                raw = wire(*message_events(0, "item_space", " "), *message_events(6, "msg_family", text), terminal)
                grant, logs = self.renewal(raw, error_code=code, response=terminal["response"])
                self.assertEqual([item["candidate"] for item in recovery.retained_family_items(logs)], [family_value()])
                self.assertEqual(recovery.plan_translation_recovery(self.root, self.run)["current_dispositions"]["fr"], "offline_recovery")
                self.tearDown_case()

    def tearDown_case(self):
        shutil.rmtree(self.root / "work/translations")

    def test_completed_response_with_empty_template_and_real_item_never_concatenates(self):
        real = json.dumps(family_value())
        template = json.dumps(skeleton()).replace("},{", "} ,{")
        output = [{"type": "reasoning"}, {"type": "message", "id": "msg_template", "content": [{"type": "output_text", "text": template}]}, {"type": "message", "id": "msg_real", "content": [{"type": "output_text", "text": real}]}]
        raw = wire(*message_events(1, "msg_template", template), *message_events(6, "msg_real", real), {"type": "response.completed", "response": {"status": "completed", "output": output}})
        grant, logs = self.renewal(raw, state="rejected", error_code="validation_rejected", response={"status": "completed", "output": output}, raw_candidate=output)
        items = {item["candidate_sha256"]: item for item in recovery.retained_family_items(logs)}
        self.assertEqual(sorted(item["placeholder"] for item in items.values()), [False, True])
        self.assertIn(family_value(), [item["candidate"] for item in items.values()])
        # Rejected terminal: execute already refuses automatic retry; a pristine destination stays plannable (no crash on missing candidate.json).
        plan = recovery.plan_translation_recovery(self.root, self.run)
        self.assertEqual(plan["current_dispositions"]["fr"], "retry")
        destination = family.family_locale_path(self.root, self.run, "fr")
        (destination / "stage-0/lesson.md").write_text("partial editor German page")
        plan = recovery.plan_translation_recovery(self.root, self.run)
        self.assertEqual(plan["current_dispositions"]["fr"], "offline_recovery")
        self.assertIn("fifteen files", plan["current_reasons"]["fr"])
        self.assertEqual((destination / "stage-0/lesson.md").read_text(), "partial editor German page")

    def test_commentary_fences_and_placeholder_only_streams(self):
        value = family_value()
        for text in (f"Here is the family:\n```json\n{json.dumps(value)}\n```\nDone.", json.dumps(value, indent=2)):
            with self.subTest(text=text[:20]):
                folder = self.root / "work/scratch-attempt"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "request.json").write_bytes(b"{}")
                (folder / "raw.sse").write_bytes(wire(*message_events(0, "msg", text)))
                self.assertEqual([item["candidate"] for item in recovery.retained_family_items(folder)], [value])
                self.assertTrue(recovery._completed_evidence(folder))
        (folder / "raw.sse").write_bytes(wire(*message_events(0, "msg", json.dumps(skeleton())), *message_events(1, "msg2", "not json at all")))
        self.assertEqual([item["placeholder"] for item in recovery.retained_family_items(folder)], [True])
        self.assertFalse(recovery._completed_evidence(folder))
        self.assertTrue(recovery._completed_evidence(folder, require_content=False))

    def test_control_character_corruption_is_reported_not_repaired(self):
        value = family_value()
        corrupted = json.dumps(value).replace("Synthetic", "T\\u0000fa")
        grant, logs = self.renewal(wire(*message_events(1, "msg", corrupted), STREAM_ERROR))
        items = recovery.retained_family_items(logs)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["anomalies"])
        self.assertIn("\x00", items[0]["candidate"]["stages"][0]["lesson_md"])
        self.assertEqual(recovery.plan_translation_recovery(self.root, self.run)["current_dispositions"]["fr"], "offline_recovery")

    def test_complete_expert_tree_without_publication_is_preserved_over_retained_output(self):
        text = json.dumps(family_value())
        grant, logs = self.renewal(wire(*message_events(0, "msg", text), STREAM_ERROR))
        destination = family.family_locale_path(self.root, self.run, "fr")
        for stage in family.STAGES:
            (destination / f"stage-{stage}/lesson.md").write_text("" if stage == 3 else f"expert {stage}")
            (destination / f"stage-{stage}/quiz.md").write_text(f"expert quiz {stage}")
            core.write_json(destination / f"stage-{stage}/adaptation-notes.json", [])
        before = recovery._inventory(self.root, f"{self.run}/fr")
        self.assertEqual(recovery.recovery_for_locale(self.root, self.run, "fr")["disposition"], "preserve")
        self.assertEqual(recovery._inventory(self.root, f"{self.run}/fr"), before)

    def extract(self, output, **kwargs):
        def validator(command, timeout, stop_event=None):
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "family_contract": [{"stage": stage} for stage in family.STAGES]}), "")
        with patch("lesson_factory.translation_scheduler.validate_process", side_effect=validator) as process:
            result = recovery.extract_retained_family(self.root, self.run, "fr", output, **kwargs)
        self.assertIn("--candidate", process.call_args.args[0])
        return result

    def test_extract_writes_unapproved_draft_and_exact_provenance_without_touching_evidence(self):
        text = json.dumps(family_value())
        grant, logs = self.renewal(wire(*message_events(0, "msg_template", json.dumps(skeleton())), *message_events(1, "msg", text), STREAM_ERROR))
        before = self.snapshot()
        output = self.root / "work/content-recovery/fr.json"
        manifest = self.extract(output)
        self.assertEqual(json.loads(output.read_text()), family_value())
        self.assertFalse(manifest["approved"])
        self.assertFalse(manifest["adopted"])
        self.assertTrue(manifest["provider_authored_text"])
        self.assertTrue(manifest["selected"]["request_current"])
        self.assertEqual(manifest["selected"]["error_code"], "stream_read_error")
        self.assertEqual(manifest["grant"]["grant_id"], grant["grant_id"])
        self.assertTrue(manifest["validation"]["family_contract_equal"])
        self.assertEqual(len(manifest["retained_items"]), 2)
        after = self.snapshot()
        self.assertEqual(set(after) - set(before), {"work/content-recovery", "work/content-recovery/fr.json", "work/content-recovery/fr.json.provenance.json"})
        self.assertEqual({name: after[name] for name in before}, before)
        with self.assertRaisesRegex(TranslationError, "refusing overwrite"):
            self.extract(output)
        for forbidden in (logs / "draft.json", family.family_locale_path(self.root, self.run, "fr") / "draft.json", self.root / "family_packs/de/x.json"):
            with self.subTest(path=forbidden), self.assertRaisesRegex(TranslationError, "outside provider evidence"):
                self.extract(forbidden)

    def test_extract_requires_explicit_item_when_several_usable_families_exist(self):
        first, second = json.dumps(family_value("one")), json.dumps(family_value("two"))
        self.renewal(wire(*message_events(0, "a", first), *message_events(1, "b", second), STREAM_ERROR))
        with self.assertRaisesRegex(TranslationError, "select one with --item"):
            self.extract(self.root / "work/content-recovery/fr.json")
        chosen = [item for item in recovery.retained_family_items(self.root / f"work/translations/{recovery._load_grant(self.root, self.run)['scope']}/fr") if item["candidate"] == family_value("two")][0]
        manifest = self.extract(self.root / "work/content-recovery/fr.json", item=chosen["candidate_sha256"][:12])
        self.assertEqual(manifest["selected"]["candidate_sha256"], chosen["candidate_sha256"])

    def test_partial_editor_tree_reconstruction_provenance_and_offline_status(self):
        value = family_value()
        corrupted = json.dumps(family_value("other")).replace("other", "T\\u0000fa")
        grant, logs = self.renewal(wire(*message_events(0, "bad", corrupted), *message_events(1, "good", json.dumps(value)), STREAM_ERROR))
        destination = family.family_locale_path(self.root, self.run, "fr")
        (destination / "stage-0/lesson.md").write_text(value["stages"][0]["lesson_md"])
        (destination / "stage-0/quiz.md").write_text("editor-revised quiz")
        before = self.snapshot()
        status = recovery.offline_recovery_status(self.root, self.run)
        self.assertEqual(self.snapshot(), before)
        entry = status["offline_locales"]["fr"]
        self.assertIn("fifteen files", entry["reason"])
        self.assertEqual(entry["alternative_count"], 2)
        chosen = next(item for item in entry["alternatives"] if not item["anomalies"])
        files = chosen["reconstruction"]["files"]
        self.assertEqual(chosen["reconstruction"]["destination_state"], "partial")
        self.assertEqual(files["stage-0/lesson.md"]["status"], "matches_retained_draft")
        self.assertEqual(files["stage-0/quiz.md"]["status"], "editor_or_other")
        self.assertEqual(files["stage-0/adaptation-notes.json"]["status"], "missing")
        self.assertEqual(files["stage-3/lesson.md"]["status"], "matches_retained_draft")
        self.assertEqual(files["stage-1/lesson.md"]["status"], "empty_scaffold")
        with self.assertRaisesRegex(TranslationError, "select one with --item"):
            self.extract(self.root / "work/content-recovery/fr.json")
        manifest = self.extract(self.root / "work/content-recovery/fr.json", item=chosen["candidate_sha256"])
        self.assertEqual(manifest["selected"]["anomalies"], [])
        self.assertEqual(json.loads((self.root / "work/content-recovery/fr.json").read_text()), value)
        self.assertEqual(manifest["reconstruction"]["files"]["stage-0/quiz.md"]["status"], "editor_or_other")
        self.assertEqual(self.snapshot(), {**before, **{name: value for name, value in self.snapshot().items() if name.startswith("work/content-recovery")}})

    def test_cli_extract_routes_and_requires_output(self):
        path = Path(__file__).resolve().parents[1] / "tools/reauthorize-translations.py"
        specification = importlib.util.spec_from_file_location("reauthorize_translations_extract", path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        with patch.object(module, "extract_retained_family", return_value={"draft": True}) as extract, patch.object(module, "plan_translation_recovery") as plan, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(module.main(["--root", str(self.root), "--run", self.run, "--extract-retained", "fr", "--output", "work/content-recovery/fr.json"]), 0)
            extract.assert_called_once()
            self.assertEqual(module.main(["--root", str(self.root), "--run", self.run, "--extract-retained", "fr"]), 1)
            self.assertEqual(module.main(["--root", str(self.root), "--run", self.run, "--output", "x.json"]), 1)
            plan.assert_not_called()
        with patch.object(module, "offline_recovery_status", return_value={"offline_locales": {}}) as status, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(["--root", str(self.root), "--run", self.run, "--offline-status"]), 0)
            status.assert_called_once()


if __name__ == "__main__":
    unittest.main()
