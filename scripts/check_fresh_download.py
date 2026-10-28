"""Download and verify the model in a temporary PVC."""

import argparse
from datetime import datetime, timezone
import json
import secrets
import time

from check_kubernetes import ROOT, RESULTS, get_json, kubectl


INITIAL_DOWNLOAD = '''
import json, runpy, sys
from pathlib import Path
path = Path("/models/qwen-0.5b")
if path.exists() and any(path.iterdir()):
    print(json.dumps({"event":"fresh_precondition_failed"}), flush=True)
    sys.exit(1)
print(json.dumps({"event":"fresh_model_directory_empty"}), flush=True)
runpy.run_path("/app/download.py", run_name="__main__")
'''

VERIFY = '''
import json, os
from pathlib import Path
import download
manifest = json.loads(Path("/app/model-manifest.json").read_text())
directory = Path("/models/qwen-0.5b")
assert os.getuid() == 10001
assert all(download.valid_file(directory / name, expected) for name, expected in manifest["files"].items())
assert json.loads((directory / ".ready.json").read_text()) == manifest
print(json.dumps({"event":"fresh_files_verified", "uid":os.getuid(), "files":len(manifest["files"]),
                  "revision":manifest["revision"], "all_sizes_and_sha256_match":True}))
'''


def resources(name):
    labels = {"app": "model-download-check", "check-id": name}
    container_security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                          "capabilities": {"drop": ["ALL"]}}
    limits = {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "256Mi"}}
    common = {"image": "ml-model-downloader:0.1", "imagePullPolicy": "Never",
              "securityContext": container_security, "resources": limits}
    pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": name, "labels": labels},
           "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "3Gi"}}}}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "labels": labels},
           "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                        "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                    "initContainers": [{**common, "name": "download-model", "command": ["python", "-c", INITIAL_DOWNLOAD],
                                        "env": [{"name": "HF_TOKEN", "valueFrom": {"secretKeyRef": {"name": "hf-credentials", "key": "token"}}}],
                                        "volumeMounts": [{"name": "models", "mountPath": "/models"}]}],
                    "containers": [{**common, "name": "verify", "command": ["python", "-c", VERIFY],
                                    "volumeMounts": [{"name": "models", "mountPath": "/models", "readOnly": True}]}],
                    "volumes": [{"name": "models", "persistentVolumeClaim": {"claimName": name}}]}}
    return pvc, pod


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="Keep only this experiment's Pod/PVC after success")
    args = parser.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    name = "model-download-check-" + secrets.token_hex(4)
    pvc, pod = resources(name)
    # An existing claim would invalidate the empty-cache test.
    kubectl("create", "-f", "-", data=json.dumps(pvc))
    created = get_json("get", "pvc", name)
    kubectl("create", "-f", "-", data=json.dumps(pod))
    print("Fresh download experiment: " + name + ". Serving PVC is retained.", flush=True)
    deadline, next_progress = time.monotonic() + 2400, 0
    while time.monotonic() < deadline:
        current = get_json("get", "pod", name)
        phase = current.get("status", {}).get("phase")
        if phase == "Succeeded":
            break
        if phase == "Failed":
            raise RuntimeError("Fresh download Pod failed; its Pod and PVC are retained for inspection")
        if time.monotonic() >= next_progress:
            print("Waiting for fresh HTTPS acquisition and independent file verification.", flush=True)
            next_progress = time.monotonic() + 30
        time.sleep(5)
    else:
        raise TimeoutError("Fresh download timed out; its Pod and PVC are retained")
    events = [json.loads(line) for line in kubectl("logs", name, "-c", "download-model").stdout.splitlines() if line.strip()]
    verification = json.loads(kubectl("logs", name, "-c", "verify").stdout)
    manifest = json.loads((ROOT / "downloader/model-manifest.json").read_text())
    assert events[0]["event"] == "fresh_model_directory_empty"
    downloaded = [event["file"] for event in events if event["event"] == "model_file_ready"]
    assert sorted(downloaded) == sorted(manifest["files"])
    assert events[-1]["event"] == "model_download_complete"
    assert verification["all_sizes_and_sha256_match"] and verification["uid"] == 10001
    proof = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "pod_uid": current["metadata"]["uid"],
             "temporary_pvc_uid": created["metadata"]["uid"], "initial_model_directory_empty": True,
             "init_events": events, "verification": verification,
             "serving_pvc_modified": False, "scope": "Fresh initContainer HTTPS download into an isolated PVC and independent SHA-256 verification; vLLM startup/restart proven separately."}
    output = RESULTS / (name + ".json")
    output.write_text(json.dumps(proof, indent=2) + "\n")
    if not args.keep:
        # Delete only our newly created experimental resources after checking IDs.
        current_pvc = get_json("get", "pvc", name)
        assert current_pvc["metadata"]["uid"] == created["metadata"]["uid"]
        assert current_pvc["metadata"]["labels"]["check-id"] == name
        current_pod = get_json("get", "pod", name)
        assert current_pod["metadata"]["uid"] == current["metadata"]["uid"]
        kubectl("delete", "pod", name, "--wait=true", "--timeout=60s")
        kubectl("delete", "pvc", name, "--wait=true", "--timeout=60s")
    print(json.dumps({"fresh_download_verified": True, "files": len(downloaded),
                      "result_file": str(output), "experimental_resources_retained": args.keep}), flush=True)


if __name__ == "__main__":
    main()
