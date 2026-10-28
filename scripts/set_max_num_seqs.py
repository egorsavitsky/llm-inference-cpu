"""Change one ConfigMap setting, recreate the Pod, and verify the offline service."""

import argparse
from datetime import datetime, timezone
import fcntl
import json

from check_kubernetes import ROOT, RESULTS, CLIENT, get_json, kubectl, exec_python, ready_pod, init_events, wait_network


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("value", type=int, choices=(1, 2))
    args = parser.parse_args()
    source = ROOT / "config/server.json"
    original_text = source.read_text()
    before = json.loads(original_text)
    if before["max_num_seqs"] == args.value:
        raise ValueError("Requested setting is already in the source; refusing an ambiguous experiment")
    RESULTS.mkdir(parents=True, exist_ok=True)
    old = ready_pod("app=cpu-llm", timeout=30)
    old_name = old["metadata"]["name"]
    read_config = 'from pathlib import Path; print(Path("/app/config/server.json").read_text())'
    live = json.loads(exec_python(old_name, "server", read_config).stdout)
    if live != before:
        raise ValueError("Source and current Pod config differ; reconcile before comparing")
    pvc = get_json("get", "pvc", "model-files")
    policy = get_json("get", "networkpolicy", "model-offline")
    after = {**before, "max_num_seqs": args.value}
    applied = False
    try:
        source.write_text(json.dumps(after, indent=2) + "\n")
        kubectl("apply", "--dry-run=server", "-k", str(ROOT))
        kubectl("apply", "-k", str(ROOT))
        applied = True
        print(f"ConfigMap updated: max_num_seqs {before['max_num_seqs']} -> {args.value}. Waiting for offline startup.", flush=True)
        current = ready_pod("app=cpu-llm", old_uid=old["metadata"]["uid"], timeout=1200)
        name = current["metadata"]["name"]
        actual = json.loads(exec_python(name, "server", read_config).stdout)
        assert actual == after
        assert get_json("get", "pvc", "model-files")["metadata"]["uid"] == pvc["metadata"]["uid"]
        assert get_json("get", "networkpolicy", "model-offline")["metadata"]["uid"] == policy["metadata"]["uid"]
        assert current["status"]["containerStatuses"][0]["imageID"] == old["status"]["containerStatuses"][0]["imageID"]
        events = init_events(name)
        assert events and all(event["event"] == "model_cache_verified" for event in events)
        address = get_json("get", "service", "egress-probe")["spec"]["clusterIP"]
        ready_pod("app=egress-probe", timeout=30)
        wait_network(name, address, False)
        helper = ready_pod("app=egress-probe", timeout=30)["metadata"]["name"]
        print("New configuration ready. Checking a real Service response.", flush=True)
        answer = json.loads(exec_python(helper, "http", CLIENT, timeout=420).stdout)
        proof = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "before": before, "after": actual,
                 "old_pod_uid": old["metadata"]["uid"], "new_pod_uid": current["metadata"]["uid"],
                 "pvc_uid": pvc["metadata"]["uid"], "same_pvc": True, "same_image": True,
                 "same_egress_policy": True, "egress_control": "blocked", "init_events": events, "answer": answer}
        output = RESULTS / (f"config-transition-seqs{before['max_num_seqs']}-to-{args.value}.json")
        output.write_text(json.dumps(proof, indent=2) + "\n")
        print("Configuration change verified. Saved " + str(output), flush=True)
    finally:
        if not applied:
            source.write_text(original_text)


if __name__ == "__main__":
    with open("/tmp/ml-vllm-lesson.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
