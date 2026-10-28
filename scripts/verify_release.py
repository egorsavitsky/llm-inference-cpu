"""Final offline acceptance check against an already deployed local service."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from check_kubernetes import ROOT, RESULTS, get_json, kubectl, exec_python, ready_pod, TCP_PROBE


RUNTIME = '''
import hashlib, importlib.metadata, json, os
from pathlib import Path
print(json.dumps({"uid":os.getuid(), "vllm_version":importlib.metadata.version("vllm"),
                  "torch_version":importlib.metadata.version("torch"),
                  "config":json.loads(Path("/app/config/server.json").read_text()),
                  "runtime_source_hashes":{name:hashlib.sha256((Path("/app")/name).read_text().encode()).hexdigest()
                    for name in ("requirements.txt","config/logging.json","service/__init__.py","service/events.py","service/launch.py","service/middleware.py")}}))
'''

PRIVACY_LOADER = '''
import json, sys, types
bundle = json.load(sys.stdin)
module = types.ModuleType("stream_client")
exec(compile(bundle["stream_client"], "stream_client.py", "exec"), module.__dict__)
sys.modules["stream_client"] = module
namespace = {"__name__":"private_probe_client"}
exec(compile(bundle["privacy_client"], "privacy_client.py", "exec"), namespace)
namespace["BASE"] = "http://cpu-llm:8000"
namespace["main"]()
'''


def main():
    if os.getuid() != 0:
        raise RuntimeError("Run this host check with sudo in Ubuntu")
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "release-verification-status.txt").write_text("RUNNING\n")
    print("Checking source tests and server-side Kustomize validation.", flush=True)
    unit = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-q"],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    if unit.returncode:
        raise RuntimeError("Source tests failed; run unittest directly for diagnostics")
    unit_count = re.search(r"\bRan (\d+) tests?\b", unit.stdout + unit.stderr)
    assert unit_count is not None
    kubectl("apply", "--dry-run=server", "-k", str(ROOT))
    server = ready_pod("app=cpu-llm", timeout=30)
    name = server["metadata"]["name"]
    runtime = json.loads(exec_python(name, "server", RUNTIME).stdout)
    assert runtime["uid"] == 10001
    assert runtime["vllm_version"] == "0.27.0+cpu" and runtime["torch_version"].endswith("+cpu")
    assert runtime["config"] == json.loads((ROOT / "config/server.json").read_text())
    assert all(hashlib.sha256((ROOT / filename).read_text().encode()).hexdigest() == digest
               for filename, digest in runtime["runtime_source_hashes"].items())
    lock = json.loads(kubectl("exec", name, "-c", "server", "--", "python", "/app/scripts/check_environment.py").stdout)
    assert lock["event"] == "dependency_lock_verified"
    policy = get_json("get", "networkpolicy", "model-offline")
    assert policy["spec"].get("egress", []) == [] and "Egress" in policy["spec"]["policyTypes"]
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app":"cpu-llm"}
    pvc = get_json("get", "pvc", "model-files")
    assert pvc["status"]["phase"] == "Bound"
    kubectl("apply", "-f", str(ROOT / "k8s/benchmark-client.yaml"))
    client = ready_pod("app=benchmark-client", timeout=120)["metadata"]["name"]
    ready_pod("app=egress-probe", timeout=30)
    address = get_json("get", "service", "egress-probe")["spec"]["clusterIP"]
    control = json.loads(exec_python(client, "client", TCP_PROBE, address).stdout)["reachable"]
    blocked = not json.loads(exec_python(name, "server", TCP_PROBE, address).stdout)["reachable"]
    assert control and blocked
    print("Checking real success/error/streaming responses and private logs.", flush=True)
    start = datetime.now(timezone.utc) - timedelta(seconds=1)
    bundle = {"stream_client": (ROOT / "scripts/stream_client.py").read_text(),
              "privacy_client": (ROOT / "scripts/privacy_client.py").read_text()}
    response = kubectl("exec", "-i", client, "-c", "client", "--", "python", "-c", PRIVACY_LOADER,
                       data=json.dumps(bundle), timeout=720)
    probes = json.loads(response.stdout)
    time.sleep(1)
    logs = kubectl("logs", name, "-c", "server", "--since-time=" + start.isoformat()).stdout
    for probe in probes["probe_strings"]:
        assert probe and probe not in logs
    # Compare the actual API credential inside the server Pod; never return it.
    key_check = '''import json,os,sys; text=json.load(sys.stdin)["logs"]; print(json.dumps({"absent":os.environ["VLLM_API_KEY"] not in text}))'''
    key_absent = json.loads(kubectl("exec", "-i", name, "-c", "server", "--", "python", "-c", key_check,
                                   data=json.dumps({"logs":logs})).stdout)["absent"]
    assert key_absent
    sys.path.insert(0, str(ROOT))
    from service.events import sanitize
    events = [json.loads(line) for line in logs.splitlines() if line.strip()]
    assert all(sanitize(event) == event for event in events)
    started = {event["request_id"] for event in events if event["event"] == "request_started" and event["route"] != "/health"}
    finished = [event for event in events if event["event"] == "request_finished" and event["route"] != "/health"]
    assert len(finished) >= 9 and started == {event["request_id"] for event in finished}
    assert all(event["outcome"] == "complete" for event in finished)
    current = get_json("get", "pod", name)
    assert current["metadata"]["uid"] == server["metadata"]["uid"]
    assert all(item.get("restartCount", 0) == 0 for item in current["status"]["containerStatuses"])
    proof = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
             "unit_tests": {"status":"passed", "count":int(unit_count.group(1))},
             "kustomize_server_dry_run": "passed", "runtime": runtime, "dependency_lock": lock,
             "server_pod_uid": current["metadata"]["uid"], "server_restarts": 0,
             "pvc_uid": pvc["metadata"]["uid"], "client_control_reachable": control,
             "server_egress_blocked": blocked, "statuses": probes["statuses"],
             "completion_tokens": probes["completion_tokens"], "stream_tokens": probes["stream_tokens"],
             "request_event_pairs": len(finished), "log_status_counts": dict(Counter(str(e["status"]) for e in finished)),
             "prompt_output_key_probes_absent": True, "log_event_schema_verified": True,
             "scope": "Real final source/runtime consistency, API, streaming, private logs and enforced egress checks. Fresh download and Pod recreation have separate preserved evidence."}
    (RESULTS / "release-verification.json").write_text(json.dumps(proof, indent=2) + "\n")
    (RESULTS / "release-verification-status.txt").write_text("SUCCESS\n")
    print(json.dumps(proof, indent=2), flush=True)


if __name__ == "__main__":
    with open("/tmp/ml-vllm-lesson.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            main()
        except BaseException:
            RESULTS.mkdir(parents=True, exist_ok=True)
            (RESULTS / "release-verification-status.txt").write_text("ERROR\n")
            raise
