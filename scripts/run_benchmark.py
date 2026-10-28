"""Run the load client and save timings, configuration and cache history."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from check_kubernetes import (
    ROOT, RESULTS, get_json, kubectl, kubectl_command, exec_python, ready_pod,
)


SNAPSHOT = '''
import json, os
from pathlib import Path
fields = {name: Path("/sys/fs/cgroup/" + name).read_text().strip()
          for name in ("cpu.max", "cpu.stat", "memory.current", "memory.peak", "memory.max")
          if Path("/sys/fs/cgroup/" + name).exists()}
print(json.dumps({"uid":os.getuid(),"config":json.loads(Path("/app/config/server.json").read_text()),
                  "thread_environment":{key:os.environ.get(key) for key in ("OMP_NUM_THREADS","VLLM_CPU_OMP_THREADS_BIND","VLLM_CPU_KVCACHE_SPACE")},
                  "cgroup":fields}))
'''


CLIENT_SNAPSHOT = '''
import json
from pathlib import Path
print(json.dumps({name:Path("/sys/fs/cgroup/"+name).read_text().strip()
                  for name in ("cpu.max","cpu.stat","memory.current","memory.peak","memory.max","memory.events")}))
'''


def previous_attempts(folder, pod_uid):
    found = []
    for path in folder.glob("benchmark-*.json"):
        result = json.loads(path.read_text())
        recorded_uid = result.get("server_pod_uid") or result.get("environment", {}).get("server_pod_uid")
        if recorded_uid == pod_uid:
            found.append(path.name)
    return sorted(found)


def read_checkpoint_events(text):
    """Read JSON Lines, allowing an interrupted final write."""
    lines = text.splitlines()
    events = []
    truncated_last_line = False
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            if index == len(lines) - 1 and not text.endswith(("\n", "\r")):
                truncated_last_line = True
                break
            raise ValueError("Malformed completed checkpoint line") from None
        if not isinstance(event, dict):
            raise ValueError("Checkpoint event must be an object")
        events.append(event)
    return events, truncated_last_line


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--requests", type=int, default=8)
    parser.add_argument(
        "--allow-reused-pod", action="store_true",
        help="Allow a Pod used by an earlier benchmark (warm prefix cache)",
    )
    args = parser.parse_args()
    if not re.fullmatch("[a-z0-9][a-z0-9-]{0,40}", args.label):
        raise ValueError("Invalid result label")
    RESULTS.mkdir(parents=True, exist_ok=True)
    output = RESULTS / ("benchmark-" + args.label + ".json")
    if (
        output.exists()
        or (RESULTS / ("benchmark-" + args.label + "-session.json")).exists()
        or (RESULTS / ("benchmark-" + args.label + ".partial")).exists()
    ):
        raise ValueError("Choose a new label to preserve previous measurements")
    server = ready_pod("app=cpu-llm", timeout=30)
    prior = previous_attempts(RESULTS, server["metadata"]["uid"])
    if prior and not args.allow_reused_pod:
        raise RuntimeError("This Pod has earlier benchmark attempts; recreate it before a fresh comparison, or explicitly select --allow-reused-pod")
    kubectl("apply", "-f", str(ROOT / "k8s/benchmark-client.yaml"))
    helper = ready_pod("app=benchmark-client", timeout=120)
    before = json.loads(exec_python(server["metadata"]["name"], "server", SNAPSHOT).stdout)
    client_before = json.loads(exec_python(helper["metadata"]["name"], "client", CLIENT_SNAPSHOT).stdout)
    (RESULTS / ("benchmark-" + args.label + "-session.json")).write_text(json.dumps({
        "server_pod_uid": server["metadata"]["uid"], "config": before["config"],
        "started_at_utc": datetime.now(timezone.utc).isoformat(), "earlier_attempts_in_same_pod": prior,
    }, indent=2) + "\n")
    command = kubectl_command(
        "exec", "-i", helper["metadata"]["name"], "-c", "client", "--",
        "python", "-", "--requests", str(args.requests),
    )
    temporary = RESULTS / ("benchmark-" + args.label + ".partial")
    with temporary.open("w") as stream:
        try:
            process = subprocess.run(command, input=(ROOT / "scripts/benchmark_client.py").read_text(),
                                     text=True, stdout=stream, timeout=3600)
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
    events, truncated_last_line = read_checkpoint_events(temporary.read_text())
    completed = [event["result"] for event in events if event["event"] == "benchmark_result"]
    if not completed or exit_code == 124 or truncated_last_line:
        failure = {"client_exit_code": exit_code, "complete": False,
                   "truncated_last_line": truncated_last_line,
                   "completed_cases": [event["case"] for event in events if event["event"] == "benchmark_case"]}
        (RESULTS / ("benchmark-" + args.label + "-failure.json")).write_text(json.dumps(failure, indent=2) + "\n")
        raise RuntimeError("Benchmark incomplete; completed cases saved separately")
    result = completed[-1]
    current = get_json("get", "pod", server["metadata"]["name"])
    after = json.loads(exec_python(server["metadata"]["name"], "server", SNAPSHOT).stdout)
    client_after = json.loads(exec_python(helper["metadata"]["name"], "client", CLIENT_SNAPSHOT).stdout)
    current_client = get_json("get", "pod", helper["metadata"]["name"])
    if current_client["metadata"]["uid"] != helper["metadata"]["uid"] or any(
            item.get("restartCount", 0) for item in current_client["status"].get("containerStatuses", [])):
        raise RuntimeError("Load client restarted; measurements require review")
    if current["metadata"]["uid"] != server["metadata"]["uid"]:
        raise RuntimeError("Server changed during benchmark")
    statuses = current["status"].get("containerStatuses", [])
    if any(item.get("restartCount", 0) for item in statuses):
        raise RuntimeError("Server container restarted; measurements require review")
    cpu = next(line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
               if line.startswith("model name"))
    result["environment"] = {"label": args.label, "cpu_model": cpu,
                             "server_pod_uid": server["metadata"]["uid"],
                             "server_image": server["spec"]["containers"][0]["image"],
                             "server_image_id": statuses[0]["imageID"],
                             "resources": server["spec"]["containers"][0]["resources"],
                             "before": before, "after": after,
                             "client_resources": helper["spec"]["containers"][0]["resources"],
                             "client_cgroup_before": client_before, "client_cgroup_after": client_after,
                             "earlier_attempts_in_same_pod": prior,
                             "allow_reused_pod": args.allow_reused_pod,
                             "network_policies": [item["metadata"]["name"] for item in get_json("get", "networkpolicy")["items"]],
                             "saved_at_utc": datetime.now(timezone.utc).isoformat()}
    result["client_exit_code"] = exit_code
    output.write_text(json.dumps(result, indent=2) + "\n")
    temporary.unlink()
    for case in result["cases"]:
        summary = case["summary"]
        print(json.dumps({"workload": case["workload"], "concurrency": case["concurrency"],
                          "errors": summary["errors"], "input_tokens": summary["prompt_tokens"]["median"],
                          "ttft_median_seconds": summary["ttft_seconds"]["median"],
                          "latency_median_seconds": summary["latency_seconds"]["median"],
                          "output_tokens_per_second": summary["output_tokens_per_second"]}), flush=True)
    print("Saved " + str(output), flush=True)
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    import fcntl
    with open("/tmp/ml-vllm-lesson.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
