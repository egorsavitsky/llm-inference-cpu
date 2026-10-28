import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/run_benchmark.py"
spec = importlib.util.spec_from_file_location("benchmark_runner", SOURCE)
runner = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SOURCE.parent))
try:
    spec.loader.exec_module(runner)
finally:
    sys.path.pop(0)


class BenchmarkHistoryTests(unittest.TestCase):
    def test_failed_attempt_session_also_marks_pod_as_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "benchmark-failed-session.json").write_text(json.dumps({"server_pod_uid": "pod-a"}))
            (root / "benchmark-complete.json").write_text(json.dumps({"environment": {"server_pod_uid": "pod-a"}}))
            (root / "benchmark-other.json").write_text(json.dumps({"environment": {"server_pod_uid": "pod-b"}}))
            self.assertEqual(runner.previous_attempts(root, "pod-a"),
                             ["benchmark-complete.json", "benchmark-failed-session.json"])
            self.assertEqual(runner.previous_attempts(root, "new-pod"), [])

    def test_broken_history_is_not_silently_accepted_as_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "benchmark-broken.json").write_text("not complete JSON")
            with self.assertRaises(ValueError):
                runner.previous_attempts(root, "pod-a")

    def test_completed_cases_survive_an_interrupted_last_write(self):
        checkpoint = '{"event":"benchmark_case","case":{"requests":[1]}}\n{"event":"bench'
        events, truncated = runner.read_checkpoint_events(checkpoint)
        self.assertTrue(truncated)
        self.assertEqual(events[0]["case"]["requests"], [1])

    def test_malformed_completed_or_middle_line_is_rejected(self):
        for checkpoint in ('broken\n', 'broken\n{"event":"benchmark_case"}\n'):
            with self.subTest(checkpoint=checkpoint), self.assertRaises(ValueError):
                runner.read_checkpoint_events(checkpoint)

    def test_complete_json_without_newline_is_preserved(self):
        events, truncated = runner.read_checkpoint_events('{"event":"benchmark_result","result":{}}')
        self.assertFalse(truncated)
        self.assertEqual(events[0]["event"], "benchmark_result")
