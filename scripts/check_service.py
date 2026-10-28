"""Exercise the built service offline; retain only safe proof and server events."""

import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results/local"
IMAGE = "ml-inference-service:0.1"


def docker(*args, timeout=120, check=True, env=None):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, env=env)
    if check and result.returncode:
        # Do not echo command payloads or client bodies on failures.
        raise RuntimeError(f"Docker operation {args[0]} failed with exit code {result.returncode}")
    return result


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    status_file = RESULTS / "service-check-status.txt"
    status_file.write_text("RUNNING\n")
    container = None
    key = secrets.token_urlsafe(32)
    try:
        print("Checking complete installed dependency lock.", flush=True)
        lock = docker("run", "--rm", "--pull=never", "--network=none", "--entrypoint", "python", IMAGE, "/app/scripts/check_environment.py")
        lock_result = json.loads(lock.stdout)
        print("Checking raw-output suppression and SIGTERM forwarding.", flush=True)
        boundary = docker("run", "--rm", "--pull=never", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                          "--mount", f"type=bind,src={ROOT / 'scripts/check_log_boundary.py'},dst=/checks/check.py,readonly",
                          "--entrypoint", "python", IMAGE, "/checks/check.py", timeout=60)
        boundary_result = json.loads(boundary.stdout)
        print("Starting real vLLM with restricted logs; loading model locally.", flush=True)
        env = {**os.environ, "VLLM_API_KEY": key}
        container = docker("create", "--pull=never", "--network=none", "--cpus=3", "--memory=7g", "--shm-size=1g",
                           "--cap-drop=ALL", "--security-opt=no-new-privileges", "--env", "VLLM_API_KEY",
                           "--mount", "type=volume,src=ml-vllm-lesson-models,dst=/models,readonly", IMAGE, env=env).stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container):
            container = None
            raise RuntimeError("Unexpected container ID")
        for source, destination in ((ROOT / "scripts/health_client.py", "health_client.py"),
                                    (ROOT / "scripts/stream_client.py", "stream_client.py"),
                                    (ROOT / "scripts/privacy_client.py", "privacy_client.py")):
            docker("cp", str(source), f"{container}:/tmp/{destination}")
        docker("start", container)
        deadline, next_message = time.monotonic() + 900, time.monotonic() + 30
        while time.monotonic() < deadline:
            state = json.loads(docker("inspect", container).stdout)[0]["State"]
            if not state["Running"]:
                raise RuntimeError("Service exited before readiness; see service-check-server.log")
            health = docker("exec", container, "python", "/tmp/health_client.py", timeout=15, check=False)
            if not health.returncode:
                break
            if time.monotonic() >= next_message:
                print("Waiting for model readiness (up to 15 minutes).", flush=True)
                next_message = time.monotonic() + 30
            time.sleep(2)
        else:
            raise TimeoutError("Service did not become ready")
        print("Testing missing/wrong keys, invalid input, unknown route/model, ordinary and streaming answers.", flush=True)
        client = json.loads(docker("exec", container, "python", "/tmp/privacy_client.py", timeout=720).stdout)
        # Allow the asynchronous event socket to drain, then stop gracefully.
        time.sleep(1)
        details = json.loads(docker("inspect", container).stdout)[0]
        docker("stop", "--time", "35", container, timeout=45)
        logs = docker("logs", container)
        combined = logs.stdout + logs.stderr
        for text in [key, *client["probe_strings"]]:
            if text and text in combined:
                raise RuntimeError("A sensitive probe appeared in server logs")
        events = [json.loads(line) for line in combined.splitlines() if line.strip()]
        finished = [event for event in events if event["event"] == "request_finished"]
        started_ids = {event["request_id"] for event in events if event["event"] == "request_started"}
        assert started_ids == {event["request_id"] for event in finished}
        assert len(started_ids) == len(finished)
        assert sum(event["status"] == 401 for event in finished) >= 2
        assert any(event["status"] in (400, 422) for event in finished)
        assert any(event["status"] == 404 for event in finished)
        assert sum(event["status"] == 200 and event["route"] == "/v1/chat/completions" for event in finished) == 2
        assert all(event["outcome"] == "complete" for event in finished if event["status"] == 200)
        assert any(event["event"] == "service_stopping" for event in events)
        assert any(event["event"] == "service_exited" for event in events)
        assert any(event["event"] == "service_exited" and event["exit_code"] in (0, -15) for event in events)
        proof = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "image_id": details["Image"],
                 "network_mode": details["HostConfig"]["NetworkMode"], "user": details["Config"]["User"],
                 "dependency_lock": lock_result, "log_boundary": boundary_result,
                 "statuses": client["statuses"], "completion_tokens": client["completion_tokens"],
                 "stream_tokens": client["stream_tokens"], "finished_events": len(finished),
                 "probe_strings_absent": True, "graceful_stop_events": True,
                 "scope": "Real CPU API error/success paths plus synthetic native-output/traceback and shutdown checks. Kubernetes and load testing remain."}
        (RESULTS / "service-check-result.json").write_text(json.dumps(proof, indent=2) + "\n")
        status_file.write_text("SUCCESS\n")
        print(json.dumps(proof, indent=2), flush=True)
    except BaseException:
        status_file.write_text("ERROR\n")
        raise
    finally:
        if container:
            try:
                logs = docker("logs", container, check=False)
                (RESULTS / "service-check-server.log").write_text(logs.stdout + logs.stderr)
            finally:
                docker("rm", "--force", container)


if __name__ == "__main__":
    with open("/tmp/ml-vllm-lesson.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
