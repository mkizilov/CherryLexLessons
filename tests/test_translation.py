import email.utils
import http.server
import time
import io
import threading
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lesson_factory.translation import NoRedirect, TranslationError, encode, execute, parse_candidate, prepare, read_stream, usage_report, validate_candidate


CONTEXT = {"study_language": "de", "source_locale": "en"}
CANDIDATE = {"stages": [{"stage": stage, "lesson_md": "日本語 درس", "quiz_md": "问题", "adaptation_notes": []} for stage in range(5)]}


def stage_candidate(stage=1, **changes):
    candidate = json.loads(encode(CANDIDATE))
    candidate["stages"][stage].update(changes)
    return candidate


def response(candidate=CANDIDATE, status="completed"):
    return {"id": "resp_test", "status": status, "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(candidate)}]}], "usage": {"input_tokens": 1000, "output_tokens": 200, "input_tokens_details": {"cached_tokens": 500}, "output_tokens_details": {"reasoning_tokens": 120}}}


def sse(value):
    return b"data: " + encode(value) + b"\n\n"


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "secrets").mkdir()
        (self.root / "secrets/contactbox_key").write_text("test-secret")
        self.body, self.summary = prepare({**CONTEXT, "known_words": ["日本語"], "source_stages": CANDIDATE["stages"]}, "ja", "complete prompt")

    def tearDown(self):
        self.temp.cleanup()

    def journal(self):
        return json.loads((self.root / "work/translations/default/ja/journal.json").read_text())

    def call(self, value=None, **kwargs):
        wire = sse({"type": "response.completed", "response": value or response()})
        return execute(self.root, self.body, self.summary, lambda candidate: True, gate=lambda: True, opener=lambda request, timeout: io.BytesIO(wire), **kwargs)

    def test_destination_source_and_study_are_rejected(self):
        for study, source in (("en", "es"), ("de", "en")):
            for destination in (study, source):
                with self.assertRaisesRegex(TranslationError, "Destination"):
                    prepare({"study_language": study, "source_locale": source}, destination, "prompt")
        self.assertEqual(prepare(CONTEXT, "es", "prompt")[1]["target"], "es")
        for context in ({}, {"study_language": "de"}, {"study_language": "de", "source_locale": "es"}, {"snapshot": CONTEXT}):
            with self.subTest(context=context), self.assertRaisesRegex(TranslationError, "Pinned"):
                prepare(context, "fr", "prompt")

    def test_missing_journal_does_not_repurchase_or_overwrite(self):
        folder = self.root / "work/translations/default/ja"
        self.call(resume=True)
        (folder / "journal.json").unlink()
        before = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(TranslationError, "retained attempt evidence"):
            execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, resume=True, opener=lambda *_: self.fail("repurchase"))
        self.assertEqual(before, {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()})

    def test_changed_request_evidence_never_resumes_recovers_or_repairs(self):
        repair = self.root / "repair.json"
        repair.write_bytes(encode(CANDIDATE))
        for state in ("completed", "pending", "rejected", "http_error"):
            scope = f"evidence-{state}"
            self.call(scope=scope)
            folder = self.root / "work/translations" / scope / "ja"
            journal = json.loads((folder / "journal.json").read_bytes())
            journal.update(state=state, retry_eligible=state == "http_error", retry_at=0)
            (folder / "journal.json").write_bytes(encode(journal))
            request = folder / "attempt-1/request.json"
            request.write_bytes(request.read_bytes() + b" ")
            before = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
            options = {"repair_candidate": repair} if state == "rejected" else {"resume": True, "retry": state == "http_error"}
            with self.subTest(state=state), self.assertRaisesRegex(TranslationError, "request evidence changed"):
                execute(self.root, self.body, self.summary, lambda _: True, gate=lambda: True, scope=scope, opener=lambda *_: self.fail("repurchase"), **options)
            self.assertEqual(before, {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()})

    def test_existing_next_attempt_directory_is_preserved(self):
        now = [1000.0]
        with self.assertRaises(TranslationError):
            self.call(dict(response(status="failed"), error={"code": "server_error"}), clock=lambda: now[0], jitter=lambda: 0)
        now[0] = self.journal()["retry_at"]
        folder = self.root / "work/translations/default/ja/attempt-2"
        folder.mkdir()
        (folder / "request.json").write_bytes(b"retained original request")
        with self.assertRaisesRegex(TranslationError, "already has retained evidence"):
            self.call(resume=True, retry=True, clock=lambda: now[0])
        self.assertEqual((folder / "request.json").read_bytes(), b"retained original request")
        self.assertEqual(self.journal()["attempt"], 1)

    def test_cumulative_http_retry_budget_and_schedule(self):
        now = [1000.0]
        def opener(request, timeout):
            raise urllib.error.HTTPError("https://example.invalid", 503, "unavailable", {}, io.BytesIO(b"failed"))
        for attempt, delay in enumerate((120, 300, 1800, None), 1):
            with self.assertRaisesRegex(TranslationError, "HTTP 503"):
                execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, opener=opener, retry=attempt > 1, resume=True, clock=lambda: now[0], jitter=lambda: 2)
            record = self.journal()
            self.assertEqual(record["attempt"], attempt)
            self.assertEqual(record["retry_eligible"], delay is not None)
            if delay is not None:
                self.assertEqual(record["retry_at"], now[0] + delay + 2)
                with self.assertRaisesRegex(TranslationError, "not due"):
                    self.call(retry=True, resume=True, clock=lambda: now[0])
                now[0] = record["retry_at"]
        with self.assertRaisesRegex(TranslationError, "permanent|budget"):
            self.call(retry=True, resume=True, clock=lambda: now[0])
        for attempt in range(1, 5):
            self.assertEqual((self.root / f"work/translations/default/ja/attempt-{attempt}/raw.sse").read_bytes(), b"failed")

    def test_retry_after_headers_seconds_and_http_date(self):
        for header in ("800", email.utils.formatdate(1800, usegmt=True)):
            with self.subTest(header=header):
                scope = "seconds" if header == "800" else "date"
                def opener(request, timeout):
                    raise urllib.error.HTTPError("https://example.invalid", 429, "busy", {"Retry-After": header}, io.BytesIO(b"busy"))
                with self.assertRaises(TranslationError):
                    execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, opener=opener, scope=scope, clock=lambda: 1000, jitter=lambda: 1)
                record = json.loads((self.root / f"work/translations/{scope}/ja/journal.json").read_bytes())
                self.assertEqual(record["retry_after"], 800)
                self.assertEqual(record["retry_at"], 1801)

    def test_http_and_provider_classification(self):
        for status in (400, 401, 402, 403, 404, 422, 408, 425, 429, 500, 502, 503, 504):
            scope = str(status)
            def opener(request, timeout):
                raise urllib.error.HTTPError("https://example.invalid", status, "error", {}, io.BytesIO(b"error"))
            with self.assertRaises(TranslationError):
                execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, opener=opener, scope=scope)
            record = json.loads((self.root / f"work/translations/{scope}/ja/journal.json").read_bytes())
            self.assertEqual(record["retry_eligible"], status in (408, 425, 429, 500, 502, 503, 504))
        for code in ("server_error", "upstream_stream_error", "invalid_api_key", "insufficient_quota", "context_length_exceeded", "content_filter", "unknown_capacity"):
            final = dict(response(status="failed"), error={"code": code})
            with self.assertRaises(TranslationError):
                self.call(final, scope=code)
            record = json.loads((self.root / f"work/translations/{code}/ja/journal.json").read_bytes())
            self.assertEqual(record["retry_eligible"], code in ("server_error", "upstream_stream_error"))
            self.assertEqual(record["error_code"], code)

    def test_http_body_usage_permanent_error_and_header_redaction(self):
        payload = {"error": {"code": "insufficient_quota"}, "usage": response()["usage"]}
        headers = {"Retry-After": "300", "Authorization": "Bearer test-secret", "Set-Cookie": "private", "X-Upstream": "Bearer test-secret"}
        def opener(request, timeout):
            raise urllib.error.HTTPError("https://example.invalid", 429, "error", headers, io.BytesIO(encode(payload)))
        with self.assertRaises(TranslationError):
            execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, opener=opener)
        record = self.journal()
        self.assertFalse(record["retry_eligible"])
        self.assertEqual(record["usage"], payload["usage"])
        folder = self.root / "work/translations/default/ja/attempt-1"
        self.assertEqual((folder / "raw.sse").read_bytes(), encode(payload))
        self.assertEqual(json.loads((folder / "response.json").read_bytes()), payload)
        self.assertNotIn("test-secret", (folder / "response-headers.json").read_text())
        self.assertNotIn("private", (folder / "response-headers.json").read_text())

    def test_sse_provider_error_keeps_usage_and_exact_raw(self):
        wire = sse({"type": "error", "error": {"code": "upstream_stream_error"}, "usage": response()["usage"]})
        with self.assertRaises(TranslationError):
            execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, opener=lambda request, timeout: io.BytesIO(wire))
        self.assertTrue(self.journal()["retry_eligible"])
        self.assertEqual(self.journal()["usage"], response()["usage"])
        self.assertEqual((self.root / "work/translations/default/ja/attempt-1/raw.sse").read_bytes(), wire)

    def test_pending_raw_terminal_recovery_preserves_classification(self):
        for status, code in (("completed", None), ("failed", "insufficient_quota"), ("failed", "server_error")):
            scope = status if code is None else code
            final = response(status=status)
            if code:
                final["error"] = {"code": code}
            try:
                self.call(final, scope=scope)
            except TranslationError:
                pass
            folder = self.root / f"work/translations/{scope}/ja"
            record = json.loads((folder / "journal.json").read_bytes())
            record["state"] = "pending"
            (folder / "journal.json").write_bytes(encode(record))
            (folder / "attempt-1/response.json").unlink()
            (folder / "candidate.json").unlink(missing_ok=True)
            with self.assertRaisesRegex(TranslationError, "fresh resume"):
                self.call(scope=scope, resume=True, retry=True)
            recovered = json.loads((folder / "journal.json").read_bytes())
            self.assertEqual(recovered["retry_eligible"], code == "server_error")
            self.assertEqual(recovered["usage"], final["usage"])
            self.assertEqual(json.loads((folder / "attempt-1/response.json").read_bytes()), final)
            if code is None:
                self.assertEqual(recovered["state"], "rejected")

    def test_pending_http_status_and_retry_after_recovered(self):
        for status in (401, 429):
            scope = str(status)
            def opener(request, timeout):
                raise urllib.error.HTTPError("https://example.invalid", status, "error", {"Retry-After": "3600"}, io.BytesIO(b""))
            with self.assertRaises(TranslationError):
                execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, opener=opener, scope=scope, clock=lambda: 1000)
            folder = self.root / f"work/translations/{scope}/ja"
            record = json.loads((folder / "journal.json").read_bytes())
            record.update(state="pending", http_status=None)
            (folder / "journal.json").write_bytes(encode(record))
            with self.assertRaisesRegex(TranslationError, "fresh resume"):
                self.call(scope=scope, resume=True, retry=True, clock=lambda: 1000, jitter=lambda: 1)
            recovered = json.loads((folder / "journal.json").read_bytes())
            self.assertEqual(recovered["retry_eligible"], status == 429)
            self.assertEqual(recovered["http_status"], status)
            if status == 429:
                self.assertEqual(recovered["retry_at"], 4601)

    def test_default_transport_interrupts_slow_trickle_and_error_body(self):
        for status in (200, 429):
            stop = threading.Event()
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_POST(self):
                    self.send_response(status)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    try:
                        while not stop.wait(0.01):
                            self.wfile.write(b":")
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            started = time.monotonic()
            try:
                with patch("lesson_factory.translation.ENDPOINT", f"http://127.0.0.1:{server.server_port}/responses"):
                    with self.assertRaises(TimeoutError):
                        execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, scope=str(status), wall_timeout=0.1, timeout=1)
                self.assertLess(time.monotonic() - started, 0.5)
                folder = self.root / f"work/translations/{status}/ja/attempt-1"
                self.assertTrue((folder / "raw.sse").read_bytes().startswith(b":"))
                record = json.loads((folder / "journal.json").read_bytes())
                self.assertEqual(record["error_code"], "generation_deadline")
                self.assertFalse(record["retry_eligible"])
            finally:
                stop.set()
                server.shutdown()
                server.server_close()
                worker.join()

    def test_default_transport_cancellation_interrupts_error_body(self):
        cancelled = threading.Event()
        stop = threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.send_response(429)
                self.end_headers()
                self.wfile.write(b"partial")
                self.wfile.flush()
                time.sleep(0.05)
                cancelled.set()
                stop.wait(1)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        started = time.monotonic()
        try:
            with patch("lesson_factory.translation.ENDPOINT", f"http://127.0.0.1:{server.server_port}/responses"):
                with self.assertRaises(InterruptedError):
                    execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, stop_event=cancelled, timeout=1)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(self.journal()["state"], "uncertain")
            self.assertFalse(self.journal()["retry_eligible"])
            self.assertEqual((self.root / "work/translations/default/ja/attempt-1/raw.sse").read_bytes(), b"partial")
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            worker.join()

    def local_transport(self, send, **options):
        stopped = threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    send(self.wfile, stopped)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch("lesson_factory.translation.ENDPOINT", f"http://127.0.0.1:{server.server_port}/responses"):
                return execute(self.root, self.body, self.summary, options.pop("validate", lambda value: True), gate=lambda: True, **options)
        finally:
            stopped.set()
            server.shutdown()
            server.server_close()
            worker.join()

    def test_delayed_connection_is_aborted_before_request_dispatch(self):
        for cancelled in (False, True):
            stop = threading.Event()
            shutdown = threading.Event()
            budgets = []
            class Socket:
                def shutdown(self, how):
                    shutdown.set()
                def close(self):
                    pass
            def connect(connection):
                budgets.append(connection.timeout)
                if cancelled:
                    stop.set()
                time.sleep(0.12)
                connection.sock = Socket()
                self.assertTrue(shutdown.wait(0.3))
            scope = "connect-cancel" if cancelled else "connect-deadline"
            with patch("lesson_factory.translation.ENDPOINT", "http://127.0.0.1:1/responses"), patch("lesson_factory.translation.http.client.HTTPConnection.connect", connect), patch("lesson_factory.translation.http.client.HTTPConnection.request", side_effect=AssertionError("late request dispatched")):
                with self.assertRaises(InterruptedError if cancelled else TimeoutError):
                    execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, scope=scope, timeout=1, wall_timeout=0.05, stop_event=stop)
            record = json.loads((self.root / f"work/translations/{scope}/ja/journal.json").read_bytes())
            reason = "cancelled" if cancelled else "generation_deadline"
            self.assertEqual(record["error_code"], reason)
            self.assertEqual(record["transport"]["termination_reason"], reason)
            self.assertFalse(record["retry_eligible"])
            self.assertLessEqual(budgets[0], 0.05)

    def test_delayed_localhost_connect_sends_no_late_post(self):
        for cancelled in (False, True):
            received = []
            stop = threading.Event()
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_POST(self):
                    received.append(True)
                    self.send_response(200)
                    self.end_headers()
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            original = http.client.HTTPConnection.connect
            def delayed(connection):
                if cancelled:
                    stop.set()
                time.sleep(0.12)
                original(connection)
            try:
                with patch("lesson_factory.translation.ENDPOINT", f"http://127.0.0.1:{server.server_port}/responses"), patch("lesson_factory.translation.http.client.HTTPConnection.connect", delayed):
                    with self.assertRaises(InterruptedError if cancelled else TimeoutError):
                        execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, scope=f"late-connect-{cancelled}", timeout=1, wall_timeout=0.05, stop_event=stop)
                self.assertEqual(received, [])
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    def test_healthy_heartbeats_outlive_socket_idle_budget(self):
        def send(output, stopped):
            for _ in range(12):
                output.write(b": heartbeat\n\n")
                output.flush()
                if stopped.wait(0.03):
                    return
            output.write(sse({"type": "response.completed", "response": response()}))
            output.flush()
        record = self.local_transport(send, timeout=0.15, wall_timeout=2)
        self.assertEqual(record["state"], "completed")
        self.assertGreater(record["transport"]["elapsed_seconds"], 0.3)
        self.assertEqual(record["transport"]["termination_reason"], "provider_terminal")
        self.assertEqual(record["transport"]["received_bytes"], len((self.root / "work/translations/default/ja/attempt-1/raw.sse").read_bytes()))
        self.assertEqual(record["input_hash"], self.summary["hash"])

    def test_stalled_socket_is_retryable_idle_timeout(self):
        def send(output, stopped):
            output.write(b": heartbeat\n\n")
            output.flush()
            stopped.wait(2)
        with self.assertRaises(TimeoutError):
            self.local_transport(send, timeout=0.12, wall_timeout=2)
        record = self.journal()
        self.assertTrue(record["retry_eligible"])
        self.assertEqual(record["error_code"], "socket_timeout")
        self.assertEqual(record["transport"]["termination_reason"], "socket_timeout")
        self.assertLess(record["transport"]["elapsed_seconds"], 0.5)

    def test_late_terminal_after_wall_deadline_never_repurchased(self):
        def send(output, stopped):
            for _ in range(20):
                output.write(b": heartbeat\n\n")
                output.flush()
                if stopped.wait(0.02):
                    return
            output.write(sse({"type": "response.completed", "response": response()}))
            output.flush()
        with self.assertRaises(TimeoutError):
            self.local_transport(send, timeout=0.2, wall_timeout=0.15)
        record = self.journal()
        self.assertEqual(record["error_code"], "generation_deadline")
        self.assertFalse(record["retry_eligible"])
        self.assertGreater(record["transport"]["received_bytes"], 0)
        self.assertFalse((self.root / "work/translations/default/ja/candidate.json").exists())
        with self.assertRaisesRegex(TranslationError, "no automatic retry"):
            self.call(resume=True, retry=True, clock=lambda: 10**12)
        self.assertEqual(self.journal()["attempt"], 1)

    def test_terminal_survives_postvalidation_past_transport_deadline(self):
        def send(output, stopped):
            output.write(sse({"type": "response.completed", "response": response()}))
            output.flush()
        def validate(value):
            folder = self.root / "work/translations/default/ja/attempt-1"
            self.assertEqual(json.loads((folder / "response.json").read_bytes()), response())
            self.assertEqual(self.journal()["provider_status"], "completed")
            time.sleep(0.3)
            return True
        record = self.local_transport(send, timeout=0.2, wall_timeout=0.15, validate=validate)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["transport"]["termination_reason"], "provider_terminal")
        self.assertEqual(record["usage"], response()["usage"])

    def test_default_stream_cancellation_has_actual_cause_and_safe_diagnostics(self):
        cancelled = threading.Event()
        def send(output, stopped):
            output.write(b": heartbeat\n\n")
            output.flush()
            time.sleep(0.05)
            cancelled.set()
            stopped.wait(2)
        with self.assertRaises(InterruptedError):
            self.local_transport(send, timeout=1, wall_timeout=2, stop_event=cancelled)
        record = self.journal()
        self.assertEqual(record["error_code"], "cancelled")
        self.assertFalse(record["retry_eligible"])
        self.assertEqual(record["transport"]["termination_reason"], "cancelled")
        self.assertNotIn("test-secret", encode(record).decode())

    def test_journal_symlinks_fail_closed(self):
        outside = self.root / "outside"
        outside.mkdir()
        translations = self.root / "work/translations"
        translations.mkdir(parents=True)
        (translations / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(TranslationError, "Symlink"):
            self.call(scope="linked")
        self.assertEqual(list(outside.iterdir()), [])
        folder = translations / "default/ja"
        folder.mkdir(parents=True)
        (folder / "attempt.lock").symlink_to(outside / "lock")
        with self.assertRaisesRegex(TranslationError, "Symlink"):
            self.call()
        self.assertFalse((outside / "lock").exists())

    def test_pending_completed_response_never_resends(self):
        self.call()
        path = self.root / "work/translations/default/ja/journal.json"
        pending = self.journal()
        pending["state"] = "pending"
        path.write_bytes(encode(pending))
        with self.assertRaisesRegex(TranslationError, "fresh resume"):
            self.call(retry=True, resume=True)
        self.assertEqual(self.journal()["state"], "rejected")
        self.assertFalse(self.journal()["retry_eligible"])
        self.assertEqual(self.journal()["usage"], response()["usage"])

    def test_pending_restart_is_recovered_without_resend(self):
        with self.assertRaises(TranslationError):
            execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, opener=lambda request, timeout: io.BytesIO(b""), clock=lambda: 1000)
        path = self.root / "work/translations/default/ja/journal.json"
        pending = self.journal()
        pending["state"] = "pending"
        path.write_bytes(encode(pending))
        with self.assertRaisesRegex(TranslationError, "fresh resume"):
            self.call(retry=True, resume=True, clock=lambda: 2000)
        record = self.journal()
        self.assertEqual(record["attempt"], 1)
        self.assertEqual(record["state"], "uncertain")
        self.assertEqual(record["error_code"], "interrupted_attempt")
        self.assertEqual(self.call(retry=True, resume=True, clock=lambda: record["retry_at"])["attempt"], 2)

    def test_cancelled_and_redirect_not_retryable(self):
        def redirect(request, timeout):
            NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.invalid")
        with self.assertRaisesRegex(TranslationError, "redirect"):
            execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, opener=redirect)
        self.assertEqual(self.journal()["state"], "permanent_error")
        self.assertFalse(self.journal()["retry_eligible"])

    def test_cancellation_closes_stream_and_progressive_raw_survives(self):
        owner = self
        class Cancelled(io.BytesIO):
            def __iter__(self):
                yield b": heartbeat\n"
                owner.assertEqual((owner.root / "work/translations/default/ja/attempt-1/raw.sse").read_bytes(), b": heartbeat\n")
                raise KeyboardInterrupt()
        stream = Cancelled()
        with self.assertRaises(KeyboardInterrupt):
            execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, opener=lambda request, timeout: stream)
        self.assertTrue(stream.closed)
        self.assertEqual(self.journal()["state"], "uncertain")
        self.assertFalse(self.journal()["retry_eligible"])

    def test_attempt_lock_blocks_nested_dispatch(self):
        def opener(request, timeout):
            with self.assertRaisesRegex(TranslationError, "in flight"):
                self.call(retry=True, resume=True)
            return io.BytesIO(sse({"type": "response.completed", "response": response()}))
        self.assertEqual(execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, opener=opener)["state"], "completed")

    def test_completed_and_rejected_never_repurchase(self):
        self.call()
        with self.assertRaisesRegex(TranslationError, "completed"):
            self.call(retry=True)
        (self.root / "work/translations/default/ja/candidate.json").unlink()
        with self.assertRaisesRegex(TranslationError, "missing"):
            self.call(retry=True, resume=True)
        with self.assertRaises(TranslationError):
            self.call(response({"lesson_md": "x"}), scope="rejected")
        with self.assertRaisesRegex(TranslationError, "offline repair"):
            self.call(retry=True, resume=True, scope="rejected")

    def test_wall_budget_checked_between_reads_and_stream_closed(self):
        now = [1000]
        class Slow(io.BytesIO):
            def __iter__(self):
                yield b": heartbeat\n"
                now[0] += 20
                yield b": heartbeat\n"
        stream = Slow()
        with self.assertRaisesRegex(TimeoutError, "between socket reads"):
            execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, opener=lambda request, timeout: stream, clock=lambda: now[0], wall_timeout=10)
        self.assertTrue(stream.closed)
        self.assertFalse(self.journal()["retry_eligible"])
        self.assertEqual(self.journal()["error_code"], "generation_deadline")
        self.assertIsNone(self.journal()["estimated_cost_usd"])
        self.assertEqual((self.root / "work/translations/default/ja/attempt-1/raw.sse").read_bytes(), b": heartbeat\n" * 2)

    def test_finite_budgets_and_stop_before_dispatch(self):
        for value in (0, float("inf"), float("nan")):
            with self.assertRaisesRegex(TranslationError, "finite"):
                self.call(timeout=value)
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            self.call(stop_event=event)
        self.assertFalse((self.root / "work/translations/default/ja/journal.json").exists())

    def test_request_has_no_output_caps_and_utf8_guard(self):
        self.assertEqual(self.body["reasoning"], {"effort": "xhigh"})
        self.assertTrue(self.body["stream"])
        self.assertFalse(self.body["store"])
        self.assertFalse({"max_tokens", "max_output_tokens", "max_completion_tokens", "truncation"} & self.body.keys())
        self.assertEqual(self.summary["bytes"], len(encode(self.body)))
        with self.assertRaisesRegex(TranslationError, "nothing truncated; no output cap"):
            prepare({**CONTEXT, "text": "日" * 1000}, "ja", "prompt", 100)

    def test_request_hash_binds_full_prior_and_planning_histories(self):
        histories = [{"key": key, "lesson_md": f"{key} full lesson", "quiz_md": f"{key} full quiz"} for key in ("prior", "planning")]
        source = {**CONTEXT, "source_stages": CANDIDATE["stages"], "localized_external_history": histories, "snapshot": {**CONTEXT, "external_history": histories, "stages": [{"stage": stage, "prior_external_keys": ["prior"], "planning_external_keys": ["planning"]} for stage in range(5)]}}
        body, summary = prepare(source, "fr", "complete prompt")
        self.assertEqual(json.loads(body["input"][1]["content"])["context"], source)
        for index in range(2):
            changed = json.loads(encode(source))
            changed["localized_external_history"][index]["quiz_md"] += " changed"
            self.assertNotEqual(prepare(changed, "fr", "complete prompt")[1]["hash"], summary["hash"])
            changed = json.loads(encode(source))
            changed["snapshot"]["external_history"][index]["lesson_md"] += " changed"
            self.assertNotEqual(prepare(changed, "fr", "complete prompt")[1]["hash"], summary["hash"])
        with self.assertRaisesRegex(TranslationError, "nothing truncated; no output cap"):
            prepare(source, "fr", "complete prompt", summary["bytes"] - 1)

    def test_default_guard_blocks_oversized_full_history_without_truncation(self):
        source = {**CONTEXT, "localized_external_history": [{"lesson_md": "prior" * 70000, "quiz_md": "complete prior quiz"}, {"lesson_md": "future" * 70000, "quiz_md": "complete future quiz"}]}
        original = encode(source)
        with self.assertRaisesRegex(TranslationError, "above 524288; nothing truncated; no output cap"):
            prepare(source, "fr", "complete prompt")
        self.assertEqual(encode(source), original)
        self.assertFalse((self.root / "work").exists())

    def test_gate_before_credentials_and_network(self):
        (self.root / "secrets/contactbox_key").unlink()
        with self.assertRaisesRegex(TranslationError, "required"):
            execute(self.root, self.body, self.summary, lambda candidate: True)
        def reject():
            raise TranslationError("approval stale")
        with self.assertRaisesRegex(TranslationError, "approval stale"):
            execute(self.root, self.body, self.summary, lambda candidate: True, gate=reject)

    def test_completed_unicode_escaped_json_and_usage(self):
        result = self.call()
        self.assertEqual(result["state"], "completed")
        self.assertAlmostEqual(result["estimated_cost_usd"], 0.00305)
        self.assertEqual(result["reasoning_tokens"], 120)
        candidate = json.loads((self.root / "work/translations/default/ja/candidate.json").read_text())
        self.assertEqual(candidate, CANDIDATE)
        self.assertNotIn("test-secret", (self.root / "work/translations/default/ja/journal.json").read_text())

    def test_resume_skips_network_and_edited_outputs_rejected(self):
        self.call()
        with patch("lesson_factory.translation.open_response", side_effect=AssertionError("network")):
            result = execute(self.root, self.body, self.summary, lambda candidate: True, gate=lambda: True, resume=True)
        self.assertEqual(result["attempt"], 1)
        (self.root / "work/translations/default/ja/candidate.json").write_text("edited")
        with self.assertRaisesRegex(TranslationError, "edited"):
            self.call(resume=True)

    def test_changed_source_stale(self):
        self.call()
        body, summary = prepare({**CONTEXT, "source_stages": stage_candidate(lesson_md="changed")["stages"]}, "ja", "complete prompt")
        with self.assertRaisesRegex(TranslationError, "stale"):
            execute(self.root, body, summary, lambda candidate: True, gate=lambda: True, resume=True)

    def test_incomplete_never_parsed_as_good(self):
        with self.assertRaisesRegex(TranslationError, "incomplete"):
            self.call(response(status="incomplete"))
        self.assertEqual(self.journal()["state"], "provider_failed")
        self.assertFalse((self.root / "work/translations/default/ja/candidate.json").exists())

    def test_eof_uncertain_no_auto_retry(self):
        opener = lambda request, timeout: io.BytesIO(sse({"type": "response.output_text.delta", "delta": "partial"}))
        with self.assertRaisesRegex(TranslationError, "uncertain"):
            execute(self.root, self.body, self.summary, lambda candidate: True, gate=lambda: True, opener=opener)
        self.assertEqual(self.journal()["state"], "uncertain")
        with self.assertRaisesRegex(TranslationError, "no automatic retry"):
            self.call(resume=True)
        with patch("lesson_factory.translation.time.time", return_value=10**12):
            result = self.call(retry=True, resume=True, clock=lambda: 10**12)
        self.assertEqual(result["attempt"], 2)

    def test_dropped_connection_retains_raw_uncertain(self):
        class Broken:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b": heartbeat\n"
                raise TimeoutError("lost")
        with self.assertRaises(TimeoutError):
            execute(self.root, self.body, self.summary, lambda candidate: True, gate=lambda: True, opener=lambda request, timeout: Broken())
        self.assertEqual(self.journal()["state"], "uncertain")
        self.assertEqual((self.root / "work/translations/default/ja/attempt-1/raw.sse").read_bytes(), b": heartbeat\n")

    def test_multi_event_extraction_uses_final_not_duplicated_deltas(self):
        final = response()
        wire = sse({"type": "response.output_text.delta", "delta": "ignored"}) + sse({"type": "response.output_item.done", "output_index": 0, "item": final["output"][0]}) + sse({"type": "response.completed", "response": {"status": "completed", "id": "resp_test", "output": []}})
        self.assertEqual(parse_candidate(read_stream(io.BytesIO(wire), bytearray())), CANDIDATE)

    def test_multi_event_fragments(self):
        text = json.dumps(CANDIDATE)
        wire = b"".join(sse({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": "m", "delta": part}) for part in (text[:10], text[10:]))
        wire += sse({"type": "response.completed", "response": {"status": "completed", "output": []}})
        self.assertEqual(parse_candidate(read_stream(io.BytesIO(wire), bytearray())), CANDIDATE)

    def test_malformed_rejected_evidence_retained(self):
        with self.assertRaises(TranslationError):
            self.call(response({"lesson_md": "x"}))
        self.assertEqual(self.journal()["state"], "rejected")
        self.assertTrue((self.root / "work/translations/default/ja/attempt-1/raw-candidate.json").exists())

    def test_http_error_no_retry(self):
        calls = []
        def opener(request, timeout):
            calls.append(request)
            self.assertEqual(self.journal()["state"], "pending")
            raise urllib.error.HTTPError("https://example.invalid", 503, "unavailable", {}, io.BytesIO(b"failed"))
        with self.assertRaisesRegex(TranslationError, "HTTP 503"):
            execute(self.root, self.body, self.summary, lambda candidate: True, gate=lambda: True, opener=opener)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.journal()["state"], "http_error")

    def test_validator_false_report_rejected(self):
        wire = sse({"type": "response.completed", "response": response()})
        with self.assertRaisesRegex(TranslationError, "Structural"):
            execute(self.root, self.body, self.summary, lambda candidate: {"ok": False}, gate=lambda: True, opener=lambda request, timeout: io.BytesIO(wire))
        self.assertEqual(self.journal()["state"], "rejected")

    def test_changed_gate_after_response_rejected(self):
        calls = []
        def gate():
            calls.append(True)
            if len(calls) > 2:
                raise TranslationError("English changed during request")
        wire = sse({"type": "response.completed", "response": response()})
        with self.assertRaisesRegex(TranslationError, "changed during"):
            execute(self.root, self.body, self.summary, lambda candidate: True, gate=gate, opener=lambda request, timeout: io.BytesIO(wire))
        self.assertEqual(self.journal()["state"], "rejected")
        self.assertFalse((self.root / "work/translations/default/ja/candidate.json").exists())

    def test_redirect_refused(self):
        with self.assertRaisesRegex(TranslationError, "redirect refused"):
            NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.invalid")

    def test_missing_usage_unknown_not_free(self):
        self.assertIsNone(usage_report(None)["estimated_cost_usd"])
        self.assertIsNone(usage_report({})["estimated_cost_usd"])

    def test_unexpected_extracted_text_rejected(self):
        bad = response()
        bad["output"][0]["content"].append({"type": "refusal", "refusal": "no"})
        with self.assertRaisesRegex(TranslationError, "Unexpected"):
            parse_candidate(bad)

    def test_request_evidence_exists_before_dispatch_and_all_outcomes(self):
        for status in ("completed", "incomplete", "failed"):
            scope = status
            attempt = 1
            def opener(request, timeout):
                folder = self.root / f"work/translations/{scope}/ja/attempt-{attempt}"
                self.assertEqual((folder / "request.json").read_bytes(), encode(self.body))
                self.assertEqual(json.loads((folder / "summary.json").read_bytes()), self.summary)
                self.assertEqual(request.full_url, "https://api.contactboxtools.me/v1/responses")
                self.assertEqual(request.data, encode(self.body))
                self.assertNotIn(b"test-secret", (folder / "request.json").read_bytes())
                return io.BytesIO(sse({"type": "response.completed", "response": response(status=status)}))
            if status == "completed":
                execute(self.root, self.body, self.summary, lambda candidate: True, gate=lambda: True, opener=opener, scope=scope)
            else:
                with self.assertRaises(TranslationError):
                    execute(self.root, self.body, self.summary, lambda candidate: True, gate=lambda: True, opener=opener, scope=scope)
            self.assertTrue((self.root / f"work/translations/{scope}/ja/attempt-{attempt}/request.json").exists())

    def test_changed_guarded_request_rejected_before_key(self):
        self.body["max_output_tokens"] = 100
        with self.assertRaisesRegex(TranslationError, "changed"):
            self.call()

    def test_empty_lesson_requires_explicit_reviewed_practice_stage(self):
        for lesson in ("", " \r\n\t"):
            candidate = stage_candidate(lesson_md=lesson)
            with self.subTest(lesson=lesson):
                with self.assertRaisesRegex(TranslationError, "nonempty"):
                    validate_candidate(candidate)
                with self.assertRaisesRegex(TranslationError, "nonempty"):
                    parse_candidate(response(candidate))
                self.assertEqual(validate_candidate(candidate, reviewed_practice_stages=(1,)), candidate)
                self.assertEqual(parse_candidate(response(candidate), reviewed_practice_stages=(1,)), candidate)
        for lesson in (None, [], False, 0):
            with self.subTest(lesson=lesson), self.assertRaises(TranslationError):
                validate_candidate(stage_candidate(lesson_md=lesson), reviewed_practice_stages=(1,))
        with self.assertRaisesRegex(TranslationError, "fields mismatch"):
            validate_candidate(stage_candidate(lesson_md="", extra=True), reviewed_practice_stages=(1,))

    def test_practice_cannot_add_lesson_or_empty_quiz(self):
        with self.assertRaisesRegex(TranslationError, "Practice-only"):
            validate_candidate(CANDIDATE, reviewed_practice_stages=(1,))
        for quiz in ("", " \n", None):
            with self.subTest(quiz=quiz), self.assertRaises(TranslationError):
                validate_candidate(stage_candidate(lesson_md="", quiz_md=quiz), reviewed_practice_stages=(1,))

    def test_practice_execution_and_resume_preserve_empty_lesson(self):
        candidate = stage_candidate(lesson_md="")
        self.assertEqual(self.call(response(candidate), reviewed_practice_stages=(1,))["state"], "completed")
        (self.root / "secrets/contactbox_key").unlink()
        with patch("lesson_factory.translation.open_response", side_effect=AssertionError("network")):
            result = execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, resume=True, reviewed_practice_stages=(1,))
            with self.assertRaisesRegex(TranslationError, "practice policy changed"):
                execute(self.root, self.body, self.summary, lambda value: True, gate=lambda: True, resume=True)
        self.assertEqual(result["attempt"], 1)
        self.assertEqual(json.loads((self.root / "work/translations/default/ja/candidate.json").read_bytes()), candidate)

    def test_practice_rejects_nonempty_response_without_automatic_retry(self):
        with self.assertRaisesRegex(TranslationError, "Practice-only"):
            self.call(reviewed_practice_stages=(1,))
        self.assertEqual(self.journal()["state"], "rejected")
        self.assertFalse((self.root / "work/translations/default/ja/candidate.json").exists())
        with self.assertRaisesRegex(TranslationError, "no automatic retry"):
            self.call(response(stage_candidate(lesson_md="")), resume=True, reviewed_practice_stages=(1,))

    def test_practice_offline_repair_retains_complete_response_evidence(self):
        with self.assertRaisesRegex(TranslationError, "Practice-only"):
            self.call(reviewed_practice_stages=(1,))
        folder = self.root / "work/translations/default/ja/attempt-1"
        original = {name: (folder / name).read_bytes() for name in ("raw.sse", "request.json", "response.json")}
        candidate = stage_candidate(lesson_md=" \r\n")
        repaired = self.root / "repaired.json"
        repaired.write_bytes(encode(candidate))
        (self.root / "secrets/contactbox_key").unlink()
        with patch("lesson_factory.translation.open_response", side_effect=AssertionError("network")):
            result = execute(self.root, self.body, self.summary, lambda value: value == candidate, gate=lambda: True, repair_candidate=repaired, reviewed_practice_stages=(1,))
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["attempt"], 1)
        self.assertEqual(original, {name: (folder / name).read_bytes() for name in original})

    def test_family_scope_alone_does_not_authorize_empty_lesson(self):
        with self.assertRaisesRegex(TranslationError, "nonempty"):
            self.call(response(stage_candidate(lesson_md="")), scope="families/de/000020")
        record = json.loads((self.root / "work/translations/families/de/000020/ja/journal.json").read_bytes())
        self.assertEqual(record["state"], "rejected")

    def test_invalid_practice_stages_rejected_before_credentials(self):
        (self.root / "secrets/contactbox_key").unlink()
        for stages in (1, True, (True,), (0,), (5,), (1, 1), "practice"):
            with self.subTest(stages=stages), self.assertRaisesRegex(TranslationError, "unique return stage"):
                self.call(reviewed_practice_stages=stages)

    def test_separate_families_have_distinct_full_family_journals(self):
        request_ids = set()
        for base in (20, 40):
            scope = f"families/de/{base:06d}"
            record = self.call(scope=scope)
            self.assertEqual(record["attempt"], 1)
            request_ids.add(record["request_id"])
            folder = self.root / "work/translations" / scope / "ja"
            self.assertTrue((folder / "attempt-1/request.json").is_file())
            self.assertEqual(len(json.loads((folder / "candidate.json").read_bytes())["stages"]), 5)
        self.assertEqual(len(request_ids), 2)


if __name__ == "__main__":
    unittest.main()
