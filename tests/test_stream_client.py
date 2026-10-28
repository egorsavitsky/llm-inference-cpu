"""SSE framing and client timing tests."""

import importlib.util
import json
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / "scripts/stream_client.py"
spec = importlib.util.spec_from_file_location("stream_client", PATH)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


def event(value):
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n").encode()


def chunk(content=None, finish=None):
    return {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}]}


def collect(data, times=None):
    clock = iter(times or range(1, 30))
    return client.collect_stream(data.splitlines(keepends=True), 0, lambda: next(clock))


class StreamTests(unittest.TestCase):
    def test_role_events_excluded_and_chunks_are_not_tokens(self):
        data = event({"choices": [{"delta": {"role": "assistant", "content": ""}}]})
        data += event(chunk("Привет")) + event(chunk(" мир", "stop"))
        data += event({"choices": [], "usage": {"completion_tokens": 5}}) + b"data: [DONE]\n\n"
        result = collect(data, [1, 3, 7, 8, 10])
        self.assertEqual(result["answer"], "Привет мир")
        self.assertEqual(result["ttft_seconds"], 3)
        self.assertEqual(result["total_seconds"], 10)
        self.assertEqual(result["after_first_content_seconds"], 7)
        self.assertEqual(result["content_chunks"], 2)
        self.assertEqual(result["e2e_output_tokens_per_second"], 0.5)

    def test_multiline_sse_comments_and_other_fields(self):
        lines = b': keepalive\nevent: message\ndata: {"a":\ndata: 1}\n\n'
        self.assertEqual(list(client.sse_events(lines.splitlines(keepends=True))), ['{"a":\n1}'])

    def test_dropped_connection_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "completed response"):
            collect(event(chunk("text", "stop")))

    def test_incomplete_event_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "Incomplete SSE"):
            collect(b'data: {"choices": []}\n')

    def test_missing_usage_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "token usage"):
            collect(event(chunk("text", "stop")) + b"data: [DONE]\n\n")

    def test_stream_error_does_not_echo_server_payload(self):
        with self.assertRaisesRegex(RuntimeError, "^Server returned a streaming error$"):
            collect(event({"error": "sensitive body"}))

    def test_no_finish_reason_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "completed response"):
            collect(event(chunk("text")) + b"data: [DONE]\n\n")

    def test_role_only_response_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "No generated text"):
            collect(event(chunk("", "stop")) + b"data: [DONE]\n\n")


if __name__ == "__main__":
    unittest.main()
