import asyncio
import json
import logging
from http import HTTPStatus
import unittest
from unittest.mock import patch

from service.events import SafeEventHandler, sanitize
from service.middleware import RequestEvents


class LogTests(unittest.TestCase):
    def event(self, **extra):
        return {"event": "request_finished", "request_id": "a" * 32,
                "method": "POST", "route": "/v1/chat/completions", "status": 200,
                "duration_ms": 10.25, "outcome": "complete", **extra}

    def test_extra_fields_removed(self):
        clean = sanitize(self.event(prompt="SECRET", response="SECRET", authorization="SECRET"))
        self.assertNotIn("SECRET", json.dumps(clean))
        self.assertEqual(clean["status"], 200)

    def test_raw_paths_and_methods_rejected(self):
        self.assertIsNone(sanitize(self.event(route="/SECRET")))
        self.assertIsNone(sanitize(self.event(method="SECRET")))

    def test_bad_structures_do_not_raise(self):
        for field in ("route", "method", "outcome", "request_id"):
            self.assertIsNone(sanitize(self.event(**{field: []})))
        self.assertIsNone(sanitize([]))

    def test_nonfinite_numbers_and_boolean_status_rejected(self):
        self.assertIsNone(sanitize(self.event(duration_ms=float("nan"))))
        self.assertIsNone(sanitize(self.event(duration_ms=float("inf"))))
        self.assertIsNone(sanitize(self.event(status=True)))

    def test_handler_does_not_format_message_or_exception(self):
        record = logging.LogRecord("vllm.engine", logging.ERROR, __file__, 1, "SECRET %s", ("SECRET",), None)
        record.getMessage = lambda: self.fail("Raw log message was formatted")
        with patch("service.events.emit") as emit:
            SafeEventHandler().emit(record)
        self.assertEqual(emit.call_args.args[0], {"event": "runtime_log", "source": "vllm", "level": "ERROR"})


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def run_app(self, app, path="/v1/chat/completions"):
        events, sent = [], []
        async def receive():
            return {"type": "http.request", "body": b"SECRET_PROMPT"}
        async def send(message):
            sent.append(message)
        with patch("service.middleware.emit", side_effect=events.append):
            await RequestEvents(app)({"type": "http", "path": path, "method": "POST",
                                      "query_string": b"SECRET_QUERY", "headers": [(b"authorization", b"SECRET_KEY")]}, receive, send)
        return events, sent

    async def test_stream_logs_completion_only_after_last_body(self):
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"SECRET_OUTPUT", "more_body": True})
            await send({"type": "http.response.body", "body": b"END"})
        events, sent = await self.run_app(app)
        self.assertEqual([event["event"] for event in events], ["request_started", "request_finished"])
        self.assertNotIn("SECRET", json.dumps(events))
        self.assertEqual(sent[1]["body"], b"SECRET_OUTPUT")
        self.assertEqual(events[-1]["outcome"], "complete")

    async def test_unknown_path_is_classified(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 404})
            await send({"type": "http.response.body", "body": b""})
        events, _ = await self.run_app(app, "/SECRET_PATH")
        self.assertEqual(events[-1]["route"], "other")
        self.assertNotIn("SECRET", json.dumps(events))

    async def test_http_status_enum_survives_strict_event_validation(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": HTTPStatus.BAD_REQUEST})
            await send({"type": "http.response.body", "body": b"SECRET_VALIDATION_ERROR"})
        events, _ = await self.run_app(app)
        clean = sanitize(events[-1])
        self.assertIsNotNone(clean)
        self.assertEqual(clean["status"], 400)

    async def test_exception_is_logged_without_message(self):
        events = []
        async def app(scope, receive, send):
            raise ValueError("SECRET_ERROR")
        with patch("service.middleware.emit", side_effect=events.append):
            with self.assertRaises(ValueError):
                await RequestEvents(app)({"type": "http", "path": "/health", "method": "GET"}, None, None)
        self.assertEqual(events[-1]["status"], 500)
        self.assertEqual(events[-1]["outcome"], "error")
        self.assertNotIn("SECRET", json.dumps(events))

    async def test_cancelled_request_is_recorded(self):
        events = []
        async def app(scope, receive, send):
            raise asyncio.CancelledError()
        with patch("service.middleware.emit", side_effect=events.append):
            with self.assertRaises(asyncio.CancelledError):
                await RequestEvents(app)({"type": "http", "path": "/health", "method": "GET"}, None, None)
        self.assertEqual(events[-1]["outcome"], "disconnect")

    async def test_disconnect_after_complete_does_not_relabel_response(self):
        events = []
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"done"})
            await receive()
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            pass
        with patch("service.middleware.emit", side_effect=events.append):
            await RequestEvents(app)({"type": "http", "path": "/health", "method": "GET"}, receive, send)
        self.assertEqual(events[-1]["outcome"], "complete")


if __name__ == "__main__":
    unittest.main()
