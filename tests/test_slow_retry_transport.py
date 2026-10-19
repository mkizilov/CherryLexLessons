import contextlib
import copy
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lesson_factory import translation as transport
from lesson_factory import translation_scheduler as scheduler


class SlowRetryTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "secrets").mkdir()
        (self.root / "secrets/contactbox_key").write_text("offline-fixture")
        self.body, self.summary = transport.prepare({"study_language": "de", "source_locale": "en"}, "fr", "same focused request")
        self.journal = self.root / "work/translations/default/fr/journal.json"
        self.now = 1000.0
        self.waits = []
        self.calls = []
        self.validations = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def read(self):
        return json.loads(self.journal.read_bytes())

    def response(self, status="incomplete", output=None, **options):
        value = {"status": status, "output": [] if output is None else output, **options}
        if status == "incomplete" and "incomplete_details" not in options:
            value["incomplete_details"] = {"reason": "max_output_tokens"}
        return value

    def sse(self, event):
        return b"data: " + transport.encode(event) + b"\n\n"

    def opener(self, response=None, *, before=b"", after=None, http_status=None):
        def open_fixture(request, timeout):
            self.calls.append(request)
            if http_status is not None:
                raise urllib.error.HTTPError("https://example.invalid", http_status, "synthetic", {} if after is None else {"Retry-After": after}, io.BytesIO(transport.encode(response or {"error": {"code": "server_error"}})))
            stream = io.BytesIO(before + self.sse({"type": "response." + response["status"], "response": response}))
            stream.headers = {} if after is None else {"Retry-After": after}
            return stream
        return open_fixture

    def execute(self, opener, **options):
        return transport.execute(self.root, self.body, self.summary, lambda _: True, gate=lambda: True, opener=opener, clock=self.clock, jitter=lambda: 1, **options)

    def seed(self, count=4, final=None, *, before=b""):
        for number in range(1, count + 1):
            opener = self.opener(final, before=before) if final is not None and number == count else self.opener(http_status=503)
            with self.assertRaises(transport.TranslationError):
                self.execute(opener, resume=True, retry=number > 1)
            if number < count:
                self.now = self.read()["retry_at"]
        self.calls.clear()

    def policy(self, **changes):
        previous = self.read()
        value = {
            "max_attempts": 8,
            "appended_delays_seconds": [1800] * 4,
            "extension_receipt": {
                "schema": 1,
                "scope": "default",
                "locale": "fr",
                "request_hash": self.summary["hash"],
                "prior_attempt": previous["attempt"],
                "next_attempt": previous["attempt"] + 1,
                "prior_error_code": previous["error_code"],
                "prior_outcome_fingerprint": transport.retry_outcome_fingerprint(self.journal, previous["attempt"]),
                "earliest_due": self.now + 1801,
                "operator_reason": "Explicit synthetic offline fixture authorization",
                "grant_sha256": "a" * 64,
                "created_at": self.now,
            },
        }
        value["extension_receipt"].update(changes)
        self.receipt = value
        def validate():
            self.validations.append(self.now)
            return copy.deepcopy(self.receipt)
        return transport.RetryPolicy(value, validate_receipt=validate)

    def worker(self, policy, opener, **options):
        def attempt(*, resume, retry, remaining, stop_event):
            return self.execute(opener, resume=resume, retry=retry, wall_timeout=remaining, stop_event=stop_event, retry_policy=policy)
        with contextlib.redirect_stdout(io.StringIO()):
            return scheduler.retry_worker(attempt, self.journal, resume=True, retry_policy=policy, clock=self.clock, sleep=self.sleep, **options)

    def test_default_budget_and_empty_anomaly_classification_unchanged(self):
        self.seed()
        previous = self.read()
        self.assertEqual(previous["attempt"], 4)
        self.assertFalse(previous["retry_eligible"])
        with self.assertRaisesRegex(transport.TranslationError, "no automatic retry"):
            self.execute(self.opener(http_status=503), resume=True, retry=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(transport.classify_provider(self.response()), ("max_output_tokens", False))
        self.assertEqual(transport.classify_provider(self.response(status="completed")), ("provider_incomplete", False))

    def test_four_added_attempts_preserve_identity_and_historical_bytes(self):
        self.seed()
        originals = {str(path): path.read_bytes() for path in self.journal.parent.glob("attempt-*/*") if path.is_file()}
        policy = self.policy()
        with self.assertRaisesRegex(transport.TranslationError, "Provider failed"):
            self.worker(policy, self.opener(self.response()), deadline=21600)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.waits, [1801] * 4)
        self.assertEqual(self.read()["attempt"], 8)
        self.assertFalse(self.read()["retry_eligible"])
        self.assertIsNone(self.read()["retry_at"])
        self.assertEqual(self.read()["billing_outcome"], "unknown")
        self.assertEqual(self.read()["retry_policy"], dict(policy))
        self.assertEqual(originals, {name: Path(name).read_bytes() for name in originals})
        self.assertGreater(len(self.validations), 4)
        for request in self.calls:
            self.assertEqual(request.data, transport.encode(self.body))
            self.assertNotIn("max_output_tokens", json.loads(request.data))
        with self.assertRaises(transport.TranslationError):
            self.worker(policy, self.opener(self.response()), deadline=21600)
        self.assertEqual(len(self.calls), 4)

    def test_completed_empty_is_opt_in_and_slow_for_repeated_anomalies(self):
        self.seed(final=self.response(status="completed"))
        self.assertEqual(self.read()["state"], "rejected")
        self.assertFalse(self.read()["retry_eligible"])
        policy = self.policy()
        with self.assertRaises(transport.TranslationError):
            self.worker(policy, self.opener(self.response(status="completed")), deadline=21600)
        self.assertEqual(self.read()["state"], "provider_failed")
        self.assertEqual(self.read()["error_code"], "provider_completed_empty")
        self.assertEqual(self.read()["attempt"], 8)
        self.assertEqual(self.waits, [1801] * 4)

    def test_initial_due_resume_and_new_due_never_reset(self):
        self.seed()
        policy = self.policy()
        original = self.journal.read_bytes()
        due = policy["extension_receipt"]["earliest_due"]
        with self.assertRaisesRegex(transport.TranslationError, "deadline"):
            self.worker(policy, self.opener(self.response()), deadline=100)
        self.assertEqual(self.journal.read_bytes(), original)
        self.assertEqual(self.calls, [])
        self.assertEqual(policy["extension_receipt"]["earliest_due"], due)
        with self.assertRaisesRegex(transport.TranslationError, "deadline"):
            self.worker(policy, self.opener(self.response()), deadline=1800)
        self.assertEqual(self.waits, [100, 1701, 99])
        self.assertEqual(self.read()["attempt"], 5)
        second_due = self.read()["retry_at"]
        with self.assertRaisesRegex(transport.TranslationError, "deadline"):
            self.worker(policy, self.opener(self.response()), deadline=200)
        self.assertEqual(self.read()["retry_at"], second_due)
        self.assertEqual(len(self.calls), 1)

    def test_retry_after_and_jitter_choose_larger_delay(self):
        self.seed()
        policy = self.policy()
        self.now = policy["extension_receipt"]["earliest_due"]
        with self.assertRaises(transport.TranslationError):
            self.execute(self.opener(self.response(), after="2700"), resume=True, retry=True, retry_policy=policy)
        self.assertEqual(self.read()["retry_at"], self.now + 2701)
        self.assertEqual(self.read()["retry_after"], 2700)
        with self.assertRaisesRegex(transport.TranslationError, "not due"):
            self.execute(self.opener(self.response()), resume=True, retry=True, retry_policy=policy)
        self.assertEqual(len(self.calls), 1)

    def test_cancellation_during_wait_keeps_receipt_and_no_dispatch(self):
        self.seed()
        policy = self.policy()
        stop = threading.Event()
        original = self.journal.read_bytes()
        def cancel(seconds):
            stop.set()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(transport.TranslationError, "cancelled"):
            scheduler.retry_worker(lambda **_: self.fail("dispatch"), self.journal, resume=True, retry_policy=policy, clock=self.clock, sleep=cancel, stop_event=stop, deadline=21600)
        self.assertEqual(self.journal.read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_real_partial_refusal_and_ambiguous_completed_output_never_repurchase(self):
        cases = [
            self.response(status="completed", output=[{"type": "message", "content": [{"type": "output_text", "text": "not JSON but real paid content"}]}]),
            self.response(status="completed", output=[{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]),
            self.response(status="completed", output=[{"type": "unknown_tool", "result": "paid"}]),
            self.response(status="completed", output=None, error={"code": "context_length_exceeded"}),
        ]
        for final in cases:
            with self.subTest(final=final):
                self.assertIsNone(transport.slow_retry_reason(final, self.sse({"type": "response.completed", "response": final})))
        self.seed(final=cases[0])
        policy = self.policy()
        self.now = policy["extension_receipt"]["earliest_due"]
        with self.assertRaisesRegex(transport.TranslationError, "offline repair"):
            self.execute(self.opener(self.response()), resume=True, retry=True, retry_policy=policy)
        self.assertEqual(self.calls, [])

    def test_prior_delta_or_placeholder_text_blocks_empty_terminal_retry(self):
        for event in (
            {"type": "response.output_text.delta", "delta": "some output"},
            {"type": "response.output_item.done", "sub2api_placeholder": True, "item": {"type": "message", "content": [{"type": "output_text", "text": '{"stages": []}'}]}},
            {"type": "response.unknown_output", "value": "ambiguous"},
            {"type": "response.refusal.delta", "delta": "no"},
        ):
            with self.subTest(event=event):
                self.assertIsNone(transport.slow_retry_reason(self.response(), self.sse(event) + self.sse({"type": "response.incomplete", "response": self.response()})))
        self.seed()
        policy = self.policy()
        self.now = policy["extension_receipt"]["earliest_due"]
        with self.assertRaises(transport.TranslationError):
            self.execute(self.opener(self.response(), before=self.sse({"type": "response.output_text.delta", "delta": '{"stages": []}'})), resume=True, retry=True, retry_policy=policy)
        self.assertFalse(self.read()["retry_eligible"])
        self.assertFalse(transport.retry_status(self.journal, policy)["retry_eligible"])

    def test_permanent_provider_errors_stay_nonretryable_under_opt_in(self):
        for code in ("invalid_api_key", "billing_error", "insufficient_quota", "context_length_exceeded", "content_filter", "refusal", "permission_denied"):
            record = {"state": "provider_failed", "error_code": code}
            final = self.response(status="failed", error={"code": code})
            raw = self.sse({"type": "response.failed", "response": final})
            with self.subTest(code=code):
                self.assertFalse(transport.retained_retry_eligible(record, final, raw))
        self.seed()
        policy = self.policy()
        self.now = policy["extension_receipt"]["earliest_due"]
        with self.assertRaises(transport.TranslationError):
            self.execute(self.opener(self.response(status="failed", error={"code": "insufficient_quota"})), resume=True, retry=True, retry_policy=policy)
        self.assertFalse(self.read()["retry_eligible"])
        self.assertFalse(transport.retry_status(self.journal, policy)["retry_eligible"])

    def test_operator_callback_revocation_blocks_before_credentials(self):
        self.seed()
        policy = self.policy()
        self.now = policy["extension_receipt"]["earliest_due"]
        self.receipt["extension_receipt"]["operator_reason"] = "changed"
        (self.root / "secrets/contactbox_key").unlink()
        with self.assertRaisesRegex(transport.TranslationError, "receipt changed"):
            self.execute(self.opener(self.response()), resume=True, retry=True, retry_policy=policy)
        self.assertEqual(self.calls, [])

    def test_bare_policy_and_invalid_shapes_fail_closed(self):
        self.seed()
        policy = self.policy()
        with self.assertRaisesRegex(transport.TranslationError, "validator"):
            self.execute(self.opener(self.response()), resume=True, retry=True, retry_policy=dict(policy))
        for change in ({"max_attempts": 9}, {"max_attempts": True}, {"appended_delays_seconds": [120] * 4}):
            data = {**dict(policy), **change}
            with self.subTest(change=change), self.assertRaises(transport.TranslationError):
                transport.RetryPolicy(data, validate_receipt=lambda: data)
        mutated = policy["extension_receipt"]
        mutated["prior_attempt"] = 99
        self.assertEqual(policy["extension_receipt"]["prior_attempt"], 4)
        with self.assertRaises(Exception):
            policy._encoded = b"{}"

    def test_tampered_evidence_and_missing_policy_cannot_reset_budget(self):
        self.seed()
        policy = self.policy()
        path = self.journal.parent / "attempt-4/raw.sse"
        original = path.read_bytes()
        path.write_bytes(original + b" changed")
        with self.assertRaisesRegex(transport.TranslationError, "evidence changed"):
            transport.retry_status(self.journal, policy)
        path.write_bytes(original)
        self.now = policy["extension_receipt"]["earliest_due"]
        with self.assertRaises(transport.TranslationError):
            self.execute(self.opener(self.response()), resume=True, retry=True, retry_policy=policy)
        with self.assertRaisesRegex(transport.TranslationError, "validated operator receipt"):
            self.execute(self.opener(self.response()), resume=True, retry=True)
        self.assertEqual(len(self.calls), 1)

    def test_orphan_next_attempt_and_request_drift_are_refused(self):
        self.seed()
        policy = self.policy()
        folder = self.journal.parent / "attempt-5"
        folder.mkdir()
        with self.assertRaisesRegex(transport.TranslationError, "orphan"):
            transport.retry_status(self.journal, policy)
        folder.rmdir()
        original = self.journal.parent / "attempt-1/request.json"
        original.write_bytes(original.read_bytes() + b" ")
        with self.assertRaisesRegex(transport.TranslationError, "lineage evidence"):
            transport.retry_status(self.journal, policy)

    def test_cancellation_and_generation_deadline_do_not_purchase_next_attempt(self):
        self.seed()
        policy = self.policy()
        self.now = policy["extension_receipt"]["earliest_due"]
        owner = self
        class SlowStream(io.BytesIO):
            def __iter__(self):
                yield b": heartbeat\n\n"
                owner.now += 5
                yield b": heartbeat\n\n"
        with self.assertRaises(transport.GenerationDeadline):
            self.execute(lambda *a, **k: SlowStream(), resume=True, retry=True, retry_policy=policy, wall_timeout=1)
        self.assertEqual(self.read()["error_code"], "generation_deadline")
        self.assertFalse(self.read()["retry_eligible"])
        with self.assertRaisesRegex(transport.TranslationError, "no automatic retry"):
            self.execute(self.opener(self.response()), resume=True, retry=True, retry_policy=policy)
        self.assertEqual(self.calls, [])

    def test_completed_success_resumes_without_policy_or_repurchase(self):
        self.seed()
        policy = self.policy()
        self.now = policy["extension_receipt"]["earliest_due"]
        candidate = {"stages": [{"stage": stage, "lesson_md": "lesson", "quiz_md": "quiz", "adaptation_notes": []} for stage in range(5)]}
        final = self.response(status="completed", output=[{"type": "message", "content": [{"type": "output_text", "text": json.dumps(candidate)}]}])
        record = self.execute(self.opener(final), resume=True, retry=True, retry_policy=policy)
        self.assertEqual(record["attempt"], 5)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(self.execute(lambda *a, **k: self.fail("repurchase"), resume=True), record)
        self.assertEqual(len(self.calls), 1)

    def test_budget_consumed_by_policy_validation_is_rechecked_before_dispatch(self):
        self.seed()
        policy = self.policy()
        def validate():
            self.now += 4000
            return copy.deepcopy(self.receipt)
        object.__setattr__(policy, "_validate_receipt", validate)
        with self.assertRaisesRegex(transport.TranslationError, "deadline"):
            self.worker(policy, self.opener(self.response()), deadline=3600)
        self.assertEqual(self.calls, [])

    def test_attempt_under_four_still_has_slow_minimum_when_opted_in(self):
        self.seed(count=1, final=self.response())
        policy = self.policy()
        self.now = policy["extension_receipt"]["earliest_due"]
        with self.assertRaises(transport.TranslationError):
            self.execute(self.opener(http_status=503), resume=True, retry=True, retry_policy=policy)
        self.assertEqual(self.read()["attempt"], 2)
        self.assertEqual(self.read()["retry_at"], self.now + 1801)

    def test_receipt_cannot_shorten_retry_after(self):
        self.seed()
        for path in (self.journal, self.journal.parent / "attempt-4/journal.json"):
            record = json.loads(path.read_bytes())
            record["retry_after"] = 3600
            path.write_bytes(transport.encode(record))
        policy = self.policy()
        with self.assertRaisesRegex(transport.TranslationError, "shortens Retry-After"):
            transport.retry_status(self.journal, policy)

    def test_missing_output_is_not_confirmed_empty(self):
        for final in ({"status": "completed"}, {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}):
            wire = self.sse({"type": "response." + final["status"], "response": final})
            self.assertIsNone(transport.slow_retry_reason(final, wire))
            recovered = transport.read_stream(io.BytesIO(wire), bytearray())
            self.assertIsNone(transport.slow_retry_reason(recovered, wire))

    def test_any_earlier_substantive_evidence_blocks_extension(self):
        self.seed()
        folder = self.journal.parent / "attempt-1"
        final = self.response(status="completed", output=[{"type": "message", "content": [{"type": "output_text", "text": "real retained output"}]}])
        (folder / "response.json").write_bytes(transport.encode(final))
        (folder / "raw.sse").write_bytes(self.sse({"type": "response.completed", "response": final}))
        policy = self.policy()
        self.assertFalse(transport.retry_status(self.journal, policy)["retry_eligible"])
        self.now = policy["extension_receipt"]["earliest_due"]
        with self.assertRaisesRegex(transport.TranslationError, "no automatic retry"):
            self.execute(self.opener(self.response()), resume=True, retry=True, retry_policy=policy)
        self.assertEqual(self.calls, [])

    def test_later_due_time_cannot_be_shortened(self):
        self.seed()
        policy = self.policy()
        self.now = policy["extension_receipt"]["earliest_due"]
        with self.assertRaises(transport.TranslationError):
            self.execute(self.opener(self.response()), resume=True, retry=True, retry_policy=policy)
        for path in (self.journal, self.journal.parent / "attempt-5/journal.json"):
            record = json.loads(path.read_bytes())
            record["retry_at"] = self.now + 10
            path.write_bytes(transport.encode(record))
        with self.assertRaisesRegex(transport.TranslationError, "minimum delay"):
            transport.retry_status(self.journal, policy)

    def test_ambiguous_http_body_does_not_authorize_purchase(self):
        record = {"state": "http_error", "http_status": 503, "error_code": "http_503"}
        for raw in (b"real prose possibly paid content", b'{"text":"ambiguous"}', b'{"status":"completed","output":[]}'):
            with self.subTest(raw=raw):
                self.assertFalse(transport.retained_retry_eligible(record, None, raw))

    def pending_fifth_without_raw(self):
        self.seed()
        policy = self.policy()
        folder = self.journal.parent / "attempt-5"
        folder.mkdir()
        record = {**self.read(), "attempt": 5, "state": "pending", "retry_eligible": False, "retry_at": None, "error_code": None, "retry_policy": dict(policy)}
        record.pop("http_status", None)
        (folder / "request.json").write_bytes(transport.encode(self.body))
        (folder / "summary.json").write_bytes(transport.encode(self.summary))
        (folder / "journal.json").write_bytes(transport.encode(record))
        self.journal.write_bytes(transport.encode(record))
        return policy, folder

    def test_pending_fifth_without_raw_recovers_then_waits_without_early_post(self):
        policy, folder = self.pending_fifth_without_raw()
        historical = {path: path.read_bytes() for number in range(1, 5) for path in (self.journal.parent / f"attempt-{number}").iterdir() if path.is_file()}
        self.assertFalse(transport.retry_status(self.journal, policy)["retry_eligible"])
        with self.assertRaisesRegex(transport.TranslationError, "deadline"):
            self.worker(policy, self.opener(self.response()), deadline=100)
        recovered = self.read()
        self.assertEqual(recovered["attempt"], 5)
        self.assertEqual(recovered["state"], "uncertain")
        self.assertEqual(recovered["error_code"], "interrupted_attempt")
        self.assertTrue(recovered["raw_missing_before_recovery"])
        self.assertEqual(recovered["billing_outcome"], "unknown")
        self.assertEqual(recovered["retry_at"], recovered["retry_scheduled_at"] + 1801)
        self.assertEqual((folder / "raw.sse").read_bytes(), b"")
        self.assertEqual(self.calls, [])
        with self.assertRaisesRegex(transport.TranslationError, "not due"):
            self.execute(self.opener(self.response()), resume=True, retry=True, retry_policy=policy)
        with self.assertRaisesRegex(transport.TranslationError, "deadline"):
            self.worker(policy, self.opener(self.response()), deadline=200)
        self.assertEqual(self.read()["retry_at"], recovered["retry_at"])
        self.assertEqual(self.calls, [])
        self.assertEqual(historical, {path: path.read_bytes() for path in historical})

    def test_missing_raw_allowed_only_latest_pending_after_anchor(self):
        policy, folder = self.pending_fifth_without_raw()
        for path in (self.journal, folder / "journal.json"):
            record = json.loads(path.read_bytes())
            record["state"] = "uncertain"
            path.write_bytes(transport.encode(record))
        with self.assertRaisesRegex(transport.TranslationError, "Missing retained slow retry raw"):
            transport.retry_status(self.journal, policy)
        (folder / "raw.sse").write_bytes(b"")
        (self.journal.parent / "attempt-1/raw.sse").unlink()
        with self.assertRaisesRegex(transport.TranslationError, "Missing retained slow retry raw"):
            transport.retry_status(self.journal, policy)

    def test_pending_terminal_without_raw_is_not_confirmed_empty(self):
        policy, folder = self.pending_fifth_without_raw()
        (folder / "response.json").write_bytes(transport.encode(self.response(status="completed")))
        with self.assertRaisesRegex(transport.TranslationError, "pending"):
            self.execute(self.opener(self.response()), resume=True, retry_policy=policy)
        self.assertFalse(self.read()["retry_eligible"])
        self.assertFalse(transport.retry_status(self.journal, policy)["retry_eligible"])
        self.assertEqual(self.calls, [])

    def test_verified_partial_terminal_failure_is_opt_in_and_retains_bytes(self):
        self.seed()
        policy = self.policy()
        self.now = policy["extension_receipt"]["earliest_due"]
        pieces = ['{"stages":[{"stage":', '0', ',"lesson_md":"unfinished']
        before = self.sse({"type": "codex.rate_limits", "rate_limits": {"limit": 100}, "credits": {"has_credits": True}}) + self.sse({"type": "codex.response.metadata", "headers": {"x-request-id": "fixture"}})
        before += b"".join(self.sse({"type": "response.output_text.delta", "delta": piece, "output_index": 0, "content_index": 0, "item_id": "msg"}) for piece in pieces)
        final = self.response(output=[{"type": "message", "content": [{"type": "output_text", "text": "".join(pieces)}]}])
        with self.assertRaises(transport.TranslationError):
            self.execute(self.opener(final, before=before), resume=True, retry=True, retry_policy=policy)
        self.assertTrue(self.read()["retry_eligible"])
        self.assertEqual(self.read()["error_code"], "max_output_tokens")
        self.assertEqual(self.read()["retry_at"], self.now + 1801)
        self.assertEqual((self.journal.parent / "attempt-5/raw.sse").read_bytes(), before + self.sse({"type": "response.incomplete", "response": final}))
        self.assertTrue(transport.retry_status(self.journal, policy)["retry_eligible"])
        self.assertEqual(transport.classify_provider(final), ("max_output_tokens", False))

    def test_error_event_partial_terminal_matches_normalized_response(self):
        event = {"type": "error", "code": "upstream_stream_read_error", "message": "stream interrupted", "param": None, "sequence_number": 5}
        wire = self.sse({"type": "response.output_text.delta", "delta": '{"stages":[', "output_index": 0, "content_index": 0, "item_id": "msg"}) + self.sse(event)
        response = transport.read_stream(io.BytesIO(wire), bytearray())
        self.assertTrue(transport.retained_retry_eligible({"state": "provider_failed"}, response, wire))
        self.assertFalse(transport.retained_retry_eligible({"state": "rejected"}, response, wire))
        self.assertFalse(transport.retained_retry_eligible({"state": "uncertain"}, response, wire))

    def test_complete_json_in_any_retained_partial_form_blocks_purchase(self):
        candidate = {"stages": [{"stage": stage, "lesson_md": "lesson", "quiz_md": "quiz", "adaptation_notes": []} for stage in range(5)]}
        text = json.dumps(candidate)
        failure = self.response(status="failed", error={"code": "upstream_stream_read_error"})
        terminal = self.sse({"type": "response.failed", "response": failure})
        record = {"state": "provider_failed"}
        for visible in (text, "```json\n" + text + "\n```", "prefix " + text + " trailing", text + '{"unfinished":', '{broken preface}\n' + text):
            for event in (
                {"type": "response.output_text.delta", "delta": visible},
                {"type": "response.output_text.done", "text": visible},
                {"type": "response.content_part.done", "part": {"type": "output_text", "text": visible}},
                {"type": "response.output_item.done", "item": {"type": "message", "content": [{"type": "output_text", "text": visible}]}},
            ):
                with self.subTest(event=event["type"], variant=visible[:10]):
                    self.assertFalse(transport.retained_retry_eligible(record, failure, self.sse(event) + terminal))
        final = {**failure, "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
        self.assertFalse(transport.retained_retry_eligible(record, final, self.sse({"type": "response.failed", "response": final})))

    def test_complete_candidate_across_done_and_mixed_views_blocks(self):
        candidate = json.dumps({"stages": [{"stage": stage, "lesson_md": "lesson", "quiz_md": "quiz", "adaptation_notes": []} for stage in range(5)]})
        response = self.response()
        terminal = self.sse({"type": "response.incomplete", "response": response})
        forms = [
            [{"type": "response.output_text.done", "output_index": 0, "content_index": index, "text": text} for index, text in enumerate((candidate[:-1], candidate[-1:]))],
            [{"type": "response.content_part.done", "output_index": 0, "content_index": index, "part": {"type": "output_text", "text": text}} for index, text in enumerate((candidate[:-1], candidate[-1:]))],
            [{"type": "response.output_item.done", "output_index": index, "item": {"type": "message", "content": [{"type": "output_text", "text": text}]}} for index, text in enumerate((candidate[:-1], candidate[-1:]))],
            [{"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": candidate[:-1]}, {"type": "response.output_text.done", "output_index": 0, "content_index": 1, "text": candidate[-1:]}],
        ]
        for events in forms:
            raw = b"".join(self.sse(event) for event in events) + terminal
            with self.subTest(types=[event["type"] for event in events]):
                self.assertFalse(transport.retained_retry_eligible({"state": "provider_failed"}, response, raw))

    def test_partial_conflicting_terminal_unknown_events_and_permanent_fields_refused(self):
        prefix = self.sse({"type": "response.output_text.delta", "delta": '{"stages":['})
        failure = self.response(status="failed", error={"code": "upstream_stream_read_error"})
        terminal = self.sse({"type": "response.failed", "response": failure})
        record = {"state": "provider_failed"}
        for extra in (
            self.sse({"type": "response.completed", "response": self.response(status="completed")}),
            terminal,
            self.sse({"type": "codex.response.metadata", "headers": {"output": []}}),
            self.sse({"type": "response.unknown_output", "value": "ambiguous"}),
            self.sse({"type": "response.refusal.done", "text": "no"}),
        ):
            self.assertFalse(transport.retained_retry_eligible(record, failure, prefix + extra + terminal))
        self.assertFalse(transport.retained_retry_eligible(record, failure, prefix))
        for details in ({"reason": "context_length_exceeded"}, {"reason": "capacity_exceeded"}, {"reason": "billing_error"}):
            final = {**failure, "incomplete_details": details}
            self.assertFalse(transport.retained_retry_eligible(record, final, prefix + self.sse({"type": "response.failed", "response": final})))

    def test_exact_empty_skeleton_then_truncated_family_is_opt_in_partial(self):
        skeleton = {"stages": [{"stage": stage, "lesson_md": "", "quiz_md": "", "adaptation_notes": []} for stage in range(5)]}
        text = json.dumps(skeleton) + '\n{"stages":[{"stage":0,"lesson_md":"unfinished'
        failed = self.response(status="failed", error={"code": "upstream_stream_read_error"})
        wire = self.sse({"type": "response.output_text.delta", "delta": text}) + self.sse({"type": "response.failed", "response": failed})
        self.assertTrue(transport.retained_retry_eligible({"state": "provider_failed"}, failed, wire))
        self.assertFalse(transport.retained_retry_eligible({"state": "rejected"}, failed, wire))
        completed = self.response(status="completed", output=[{"type": "message", "content": [{"type": "output_text", "text": json.dumps(skeleton)}]}])
        self.assertFalse(transport.retained_retry_eligible({"state": "rejected"}, completed, self.sse({"type": "response.completed", "response": completed})))
        with self.assertRaises(transport.TranslationError):
            transport.validate_candidate(skeleton)
        self.seed()
        policy = self.policy()
        self.now = policy["extension_receipt"]["earliest_due"]
        with self.assertRaises(transport.TranslationError):
            self.execute(lambda *a, **k: io.BytesIO(wire), resume=True, retry=True, retry_policy=policy)
        self.assertTrue(self.read()["retry_eligible"])
        self.assertEqual(self.read()["retry_at"], self.now + 1801)

    def test_empty_skeleton_never_masks_later_complete_family(self):
        skeleton = {"stages": [{"stage": stage, "lesson_md": "", "quiz_md": "", "adaptation_notes": []} for stage in range(5)]}
        substantive = copy.deepcopy(skeleton)
        substantive["stages"][3]["quiz_md"] = "paid output"
        for prefix in ("", "broken { preface "):
            for suffix in (json.dumps(substantive), "broken { between " + json.dumps(substantive)):
                text = prefix + json.dumps(skeleton) + "\n" + suffix
                response = self.response()
                wire = self.sse({"type": "response.output_text.done", "text": text}) + self.sse({"type": "response.incomplete", "response": response})
                self.assertFalse(transport.retained_retry_eligible({"state": "provider_failed"}, response, wire))

    def test_nonexact_skeletons_and_duplicate_properties_remain_blocked(self):
        skeleton = {"stages": [{"stage": stage, "lesson_md": "", "quiz_md": "", "adaptation_notes": []} for stage in range(5)]}
        cases = []
        for field, value in (("lesson_md", " "), ("quiz_md", "output"), ("adaptation_notes", ["note"]), ("stage", True), ("stage", 3)):
            candidate = copy.deepcopy(skeleton)
            candidate["stages"][0][field] = value
            cases.append(candidate)
        candidate = copy.deepcopy(skeleton)
        candidate["stages"][0]["extra"] = ""
        cases.append(candidate)
        candidate = copy.deepcopy(skeleton)
        del candidate["stages"][0]["quiz_md"]
        cases.append(candidate)
        cases.append({**skeleton, "extra": []})
        cases.append({"stages": skeleton["stages"][:-1]})
        response = self.response()
        for candidate in cases:
            text = json.dumps(candidate)
            wire = self.sse({"type": "response.output_text.done", "text": text}) + self.sse({"type": "response.incomplete", "response": response})
            self.assertFalse(transport.retained_retry_eligible({"state": "provider_failed"}, response, wire))
        duplicate = json.dumps(skeleton).replace('"stage": 0', '"stage": 1, "stage": 0')
        wire = self.sse({"type": "response.output_text.done", "text": duplicate}) + self.sse({"type": "response.incomplete", "response": response})
        self.assertFalse(transport.retained_retry_eligible({"state": "provider_failed"}, response, wire))

    def test_stopped_socket_and_cancelled_exception_preserve_unknown_billing(self):
        self.seed()
        policy = self.policy()
        self.now = policy["extension_receipt"]["earliest_due"]
        with patch("lesson_factory.translation.open_response", side_effect=InterruptedError("synthetic cancellation")):
            with self.assertRaises(InterruptedError):
                transport.execute(self.root, self.body, self.summary, lambda _: True, gate=lambda: True, resume=True, retry=True, retry_policy=policy, clock=self.clock)
        self.assertEqual(self.read()["error_code"], "cancelled")
        self.assertEqual(self.read()["billing_outcome"], "unknown")
        self.assertFalse(self.read()["retry_eligible"])


if __name__ == "__main__":
    unittest.main()
