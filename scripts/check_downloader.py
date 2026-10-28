"""Exercise the small init image against real cached weights with no network."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=300).stdout


def main():
    image = json.loads(docker("image", "inspect", "ml-model-downloader:0.1"))[0]
    server = json.loads(docker("image", "inspect", "ml-inference-service:0.1"))[0]
    uid = int(docker("run", "--rm", "--pull=never", "--network=none", "--entrypoint", "python",
                     "ml-model-downloader:0.1", "-c", "import os; print(os.getuid())"))
    assert uid == 10001
    container = docker("create", "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges", "--memory=256m",
                       "--mount", "type=volume,src=ml-vllm-lesson-models,dst=/models",
                       "ml-model-downloader:0.1", "--check-only").strip()
    try:
        output = docker("start", "--attach", container)
        details = json.loads(docker("inspect", container))[0]
        assert details["State"]["ExitCode"] == 0
        events = [json.loads(line) for line in output.splitlines() if line.strip()]
        assert len(events) == 1 and events[0]["event"] == "model_cache_verified"
        proof = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "uid": uid,
                 "image_id": image["Id"], "image_bytes": image["Size"], "server_image_bytes": server["Size"],
                 "network_mode": details["HostConfig"]["NetworkMode"], "read_only_root": details["HostConfig"]["ReadonlyRootfs"],
                 "memory_limit_bytes": details["HostConfig"]["Memory"], "events": events,
                 "scope": "Real pinned model-file hashes in a non-root, network-disabled downloader container. Fresh online download is checked during Kubernetes setup."}
        path = ROOT / "results/local/downloader-result.json"
        path.write_text(json.dumps(proof, indent=2) + "\n")
        print(json.dumps(proof, indent=2))
    finally:
        docker("rm", "--force", container)


if __name__ == "__main__":
    main()
