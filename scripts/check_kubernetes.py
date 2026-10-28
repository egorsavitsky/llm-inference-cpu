"""First model start, enforced egress denial, and recreation with the same PVC."""

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results/local"
NAMESPACE = "ml-inference"
KUBECONFIG = Path("/home/mlops/.kube/config")


def kubectl_command(*args, namespace=NAMESPACE):
    # Use the cluster credentials without switching the host process to mlops.
    return ["kubectl", "--kubeconfig=" + str(KUBECONFIG), "--context=ml-inference",
            "-n", namespace, *args]


def kubectl(*args, data=None, timeout=120, check=True):
    command = kubectl_command(*args)
    result = subprocess.run(command, input=data, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    if check and result.returncode:
        # Secret input and HTTP response text must not enter setup logs.
        raise RuntimeError(f"kubectl {args[0]} failed (exit {result.returncode})")
    return result


def get_json(*args):
    return json.loads(kubectl(*args, "-o", "json").stdout)


def secret(name, value=None):
    existing = kubectl("get", "secret", name, check=False)
    if not existing.returncode and value is None:
        return
    if value is None:
        value = secrets.token_urlsafe(32) if name == "api-credentials" else ""
    manifest = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": NAMESPACE},
                "type": "Opaque", "data": {"token": base64.b64encode(value.encode()).decode()}}
    kubectl("apply", "-f", "-", data=json.dumps(manifest))


def ready_pod(label, *, old_uid=None, timeout=1800):
    deadline, progress = time.monotonic() + timeout, 0
    while time.monotonic() < deadline:
        pods = get_json("get", "pods", "-l", label)["items"]
        for pod in pods:
            if pod["metadata"].get("deletionTimestamp") or pod["metadata"]["uid"] == old_uid:
                continue
            status = pod.get("status", {})
            if any(item["type"] == "Ready" and item["status"] == "True" for item in status.get("conditions", [])):
                return pod
            for item in status.get("initContainerStatuses", []) + status.get("containerStatuses", []):
                reason = item.get("state", {}).get("waiting", {}).get("reason")
                if reason in {"ErrImageNeverPull", "InvalidImageName", "CreateContainerConfigError"}:
                    raise RuntimeError("Pod configuration/image error; inspect saved diagnostics")
                if item.get("restartCount", 0) >= 3:
                    raise RuntimeError("Repeated container failures; inspect saved diagnostics")
        if time.monotonic() >= progress:
            print(f"Waiting for {label}: image preparation, initContainer or startup probe.", flush=True)
            progress = time.monotonic() + 30
        time.sleep(5)
    raise TimeoutError("Pod did not become ready within the allowed time")


def exec_python(pod, container, code, *args, timeout=120):
    return kubectl("exec", pod, "-c", container, "--", "python", "-c", code, *args, timeout=timeout)


CLIENT = '''
import json, os, urllib.error, urllib.request
base = "http://cpu-llm:8000"
unauthorized = []
for headers in ({}, {"Authorization": "Bearer invalid-fixed-test-key"}):
    try:
        urllib.request.urlopen(urllib.request.Request(base + "/v1/models", headers=headers), timeout=10)
    except urllib.error.HTTPError as error:
        unauthorized.append(error.code)
        error.close()
assert unauthorized == [401, 401]
payload = {"model":"learning-model","messages":[{"role":"user","content":"Say hello in one short sentence."}],"max_tokens":8,"temperature":0}
request = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json","Authorization":"Bearer " + os.environ["VLLM_API_KEY"]})
with urllib.request.urlopen(request, timeout=300) as response:
    status, result = response.status, json.load(response)
assert result["choices"][0]["message"]["content"].strip()
assert result["usage"]["completion_tokens"] > 0
print(json.dumps({"http_status":status,"unauthorized_statuses":unauthorized,"usage":result["usage"],"nonempty_answer":True}))
'''

TCP_PROBE = '''
import json, socket, sys
try:
    with socket.create_connection((sys.argv[1], 8080), timeout=3):
        pass
except OSError:
    reachable = False
else:
    reachable = True
print(json.dumps({"reachable":reachable}))
'''

NETWORK_PROBE = '''
import hashlib, json, socket, urllib.error, urllib.request
stage = "dns"
try:
    socket.getaddrinfo("huggingface.co", 443)
    stage = "https"
    url = "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct/resolve/7ae557604adf67be50417f59c2c2f167def9a775/config.json"
    with urllib.request.urlopen(url, timeout=20) as response:
        data = response.read(2048)
    stage = "checksum"
    assert hashlib.sha256(data).hexdigest() == "18e18afcaccafade98daf13a54092927904649e1dd4eba8299ab717d5d94ff45"
    result = {"ok":True,"dns":True,"https":True,"config_checksum":True}
except Exception as error:
    result = {"ok":False,"stage":stage,"error_type":type(error).__name__}
    if isinstance(error, urllib.error.HTTPError):
        result["http_status"] = error.code
    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, Exception):
        result["reason_type"] = type(error.reason).__name__
    if isinstance(error, OSError) and isinstance(error.errno, int):
        result["errno"] = error.errno
print(json.dumps(result))
'''


def reachable(pod_name, address):
    return json.loads(exec_python(pod_name, "server", TCP_PROBE, address, timeout=15).stdout)["reachable"]


def wait_network(pod_name, address, expected):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if reachable(pod_name, address) is expected:
            return
        time.sleep(2)
    raise RuntimeError("NetworkPolicy reachability check did not match the expected state")


def init_events(name):
    output = kubectl("logs", name, "-c", "download-model").stdout
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    print("Preparing namespace, private Secrets and Kustomize resources.", flush=True)
    kubectl("apply", "-f", str(ROOT / "k8s/namespace.yaml"))
    secret("api-credentials")
    hf_file = os.environ.get("HF_TOKEN_FILE")
    secret("hf-credentials", Path(hf_file).read_text().strip() if hf_file else None)
    # Allow the download and positive network-control check before denying egress.
    kubectl("delete", "networkpolicy", "model-offline", "--ignore-not-found")
    previous_pvc = kubectl("get", "pvc", "model-files", check=False)
    rendered = kubectl("kustomize", str(ROOT)).stdout
    (RESULTS / "kubernetes-rendered.yaml").write_text(rendered)
    kubectl("apply", "--dry-run=server", "-k", str(ROOT))
    kubectl("apply", "-k", str(ROOT))
    kubectl("apply", "-f", str(ROOT / "k8s/egress-probe.yaml"))
    client_pod = ready_pod("app=egress-probe", timeout=300)["metadata"]["name"]
    print("Comparing DNS over UDP, TCP and HTTPS in WSL and in the Pod.", flush=True)
    from network_diagnostics import diagnose
    from configure_cluster_dns import select_upstream
    diagnostics = diagnose()
    address, transport = select_upstream(diagnostics)
    print(f"Verified DNS from Pod: {address}, transport={transport}.", flush=True)
    dns_command = ["python3", str(ROOT / "scripts/configure_cluster_dns.py"), "--upstream", address]
    if transport == "tcp":
        dns_command.append("--force-tcp")
    elif transport == "https":
        dns_command.append("--https")
    subprocess.run(dns_command, check=True, timeout=240)
    print("Checking public model DNS/HTTPS from a Pod before waiting for the model.", flush=True)
    for attempt in range(3):
        network = json.loads(exec_python(client_pod, "http", NETWORK_PROBE, timeout=60).stdout)
        network["attempts"] = attempt + 1
        if network["ok"] or network.get("stage") != "dns":
            break
        if attempt < 2:
            print("Waiting for the updated DNS service endpoints.", flush=True)
            time.sleep(2)
    (RESULTS / "kubernetes-network-preflight.json").write_text(json.dumps(network, indent=2) + "\n")
    if not network["ok"]:
        print(json.dumps(network), flush=True)
        raise RuntimeError("Pod cannot reach the public model; see kubernetes-network-preflight.json")
    existing = get_json("get", "pods", "-l", "app=cpu-llm")["items"]
    failed = [pod for pod in existing if not pod["metadata"].get("deletionTimestamp")
                 and not any(item["type"] == "Ready" and item["status"] == "True" for item in pod.get("status", {}).get("conditions", []))
                 and any(item.get("restartCount", 0) > 0 for item in pod.get("status", {}).get("initContainerStatuses", []) + pod.get("status", {}).get("containerStatuses", []))
                 ]
    ignored_uid = None
    if failed:
        print("Restarting failed project Pod; keeping the existing PVC.", flush=True)
        ignored_uid = failed[0]["metadata"]["uid"]
        kubectl("rollout", "restart", "deployment/cpu-llm")
    first = ready_pod("app=cpu-llm", old_uid=ignored_uid)
    first_name = first["metadata"]["name"]
    pvc = get_json("get", "pvc", "model-files")
    first_init = init_events(first_name)
    (RESULTS / "kubernetes-first-start.json").write_text(json.dumps({
        "checked_at_utc": datetime.now(timezone.utc).isoformat(), "pod_uid": first["metadata"]["uid"],
        "pvc_uid": pvc["metadata"]["uid"], "pvc_created_this_run": previous_pvc.returncode != 0,
        "init_events": first_init,
    }, indent=2))
    print("First Pod ready. Checking a real answer through the Service.", flush=True)
    first_answer = json.loads(exec_python(client_pod, "http", CLIENT, timeout=420).stdout)
    probe_service = get_json("get", "service", "egress-probe")
    address = probe_service["spec"]["clusterIP"]
    print("Checking reachable control destination, then enforcing deny-all egress.", flush=True)
    wait_network(first_name, address, True)
    kubectl("apply", "-f", str(ROOT / "k8s/offline-policy.yaml"))
    wait_network(first_name, address, False)
    # Confirm the destination is still running after egress is denied.
    ready_pod("app=egress-probe", timeout=30)
    print("Recreating the model Pod with the same PVC and egress denial in force.", flush=True)
    kubectl("rollout", "restart", "deployment/cpu-llm")
    second = ready_pod("app=cpu-llm", old_uid=first["metadata"]["uid"], timeout=1200)
    second_name = second["metadata"]["name"]
    second_init = init_events(second_name)
    if not second_init or any(item["event"] != "model_cache_verified" for item in second_init):
        raise RuntimeError("Restart did not use only the verified local cache")
    second_pvc = get_json("get", "pvc", "model-files")
    assert second_pvc["metadata"]["uid"] == pvc["metadata"]["uid"]
    wait_network(second_name, address, False)
    ready_pod("app=egress-probe", timeout=30)
    second_answer = json.loads(exec_python(client_pod, "http", CLIENT, timeout=420).stdout)
    uid = int(exec_python(second_name, "server", "import os; print(os.getuid())").stdout)
    assert uid == 10001
    nodes = get_json("get", "nodes")
    events = kubectl("logs", second_name, "-c", "server").stdout
    (RESULTS / "kubernetes-server.log").write_text(events)
    (RESULTS / "kubernetes-init.log").write_text("\n".join(json.dumps(item) for item in second_init) + "\n")
    first_fresh = previous_pvc.returncode != 0 and sum(item["event"] == "model_file_ready" for item in first_init) == 8
    proof = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
             "node_versions": [node["status"]["nodeInfo"]["kubeletVersion"] for node in nodes["items"]],
             "first_pod_uid": first["metadata"]["uid"], "recreated_pod_uid": second["metadata"]["uid"],
             "pvc_uid": pvc["metadata"]["uid"], "same_pvc": True,
             "first_fresh_download_verified": first_fresh, "first_init_events": first_init,
             "all_eight_files_downloaded_this_run": sum(item["event"] == "model_file_ready" for item in first_init) == 8,
             "restart_init_events": second_init, "uid": uid,
             "egress_control_before_policy": "reachable", "egress_control_after_policy": "blocked",
             "egress_after_recreation": "blocked", "first_answer": first_answer, "offline_answer": second_answer,
             "secret_references": {"api": "api-credentials", "hf": "hf-credentials"},
             "hf_token_required_for_public_model": False,
             "scope": "Real Service requests, non-root init/server, same-PVC recreation, and enforced egress denial. Load testing remains."}
    (RESULTS / "kubernetes-result.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(json.dumps({key: value for key, value in proof.items() if key not in ("first_init_events", "restart_init_events")}, indent=2), flush=True)
    print("Kubernetes checks passed. Model Pod, PVC and offline policy are retained.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        for label, args in (("kubernetes-failure-pods.json", ("get", "pods", "-o", "json")),
                            ("kubernetes-failure-events.txt", ("get", "events", "--sort-by=.lastTimestamp"))):
            try:
                result = kubectl(*args, check=False)
                (RESULTS / label).write_text(result.stdout)
            except Exception:
                pass
        raise
