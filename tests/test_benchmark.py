import importlib.util
import json
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/benchmark_client.py"
spec = importlib.util.spec_from_file_location("benchmark", SOURCE)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def event(value):
    return ("data: " + (value if isinstance(value, str) else json.dumps(value)) + "\n\n").encode().splitlines(keepends=True)


class BenchmarkTests(unittest.TestCase):
    def test_ttft_excludes_role_and_counts_usage_instead_of_chunks(self):
        lines = event({"choices": [{"delta": {"role": "assistant"}}]})
        lines += event({"choices": [{"delta": {"content": "two tokens"}}]})
        lines += event({"choices": [{"delta": {}, "finish_reason": "length"}],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 2}})
        lines += event("[DONE]")
        times = iter([10.1, 10.3, 10.8, 10.9])
        result = benchmark.read_stream(lines, 10, clock=lambda: next(times))
        self.assertAlmostEqual(result["ttft_seconds"], .3)
        self.assertAlmostEqual(result["latency_seconds"], .9)
        self.assertEqual(result["output_tokens"], 2)
        self.assertNotIn("content", result)

    def test_incomplete_or_usage_free_stream_is_rejected(self):
        with self.assertRaises(ValueError):
            benchmark.read_stream(event({"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]}), 0)
        with self.assertRaises(ValueError):
            benchmark.read_stream(event("[DONE]"), 0)

    def test_throughput_divides_total_tokens_by_shared_wall_time(self):
        rows = [{"ok": True, "output_tokens": 32, "prompt_tokens": 100, "ttft_seconds": 1, "latency_seconds": 4},
                {"ok": True, "output_tokens": 32, "prompt_tokens": 100, "ttft_seconds": 2, "latency_seconds": 4},
                {"ok": False}]
        result = benchmark.summarize(rows, 5)
        self.assertEqual(result["output_tokens_per_second"], 12.8)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["ttft_seconds"]["median"], 1.5)
        self.assertEqual(result["ttft_seconds"]["p95_nearest_rank"], 2)

    def test_prompts_repeat_across_configs_but_differ_across_cases(self):
        first = benchmark.messages("long", "c1/0")
        self.assertEqual(first, benchmark.messages("long", "c1/0"))
        self.assertNotEqual(first[0]["content"][:16], benchmark.messages("long", "c2/0")[0]["content"][:16])
        self.assertGreater(len(first[0]["content"]), len(benchmark.messages("short", "c1/0")[0]["content"]))
