"""Send a question through the in-cluster API client."""

import argparse
import fcntl
import json
from pathlib import Path

from check_kubernetes import ROOT, kubectl, ready_pod


CLIENT = '''
import json, os, sys, urllib.error, urllib.request
try:
    payload = json.load(sys.stdin)
    request = urllib.request.Request("http://cpu-llm:8000/v1/chat/completions",
        data=json.dumps(payload).encode(), headers={"Content-Type":"application/json",
        "Authorization":"Bearer " + os.environ["VLLM_API_KEY"]})
    with urllib.request.urlopen(request, timeout=360) as response:
        result = json.load(response)
    print(json.dumps({"ok":True,"answer":result["choices"][0]["message"]["content"],"usage":result["usage"]}))
except Exception as error:
    result={"ok":False,"error_type":type(error).__name__}
    if isinstance(error, urllib.error.HTTPError):
        result["http_status"]=error.code
    print(json.dumps(result))
    sys.exit(1)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--max-tokens", type=int, default=64, choices=range(1, 129))
    args = parser.parse_args()
    question = args.prompt_file.read_text(encoding="utf-8") if args.prompt_file else input("Вопрос: ")
    if not question.strip():
        raise ValueError("Question is empty")
    ready_pod("app=cpu-llm", timeout=30)
    kubectl("apply", "-f", str(ROOT / "k8s/benchmark-client.yaml"))
    client = ready_pod("app=benchmark-client", timeout=120)["metadata"]["name"]
    payload = {"model": "learning-model", "messages": [{"role": "user", "content": question}],
               "max_tokens": args.max_tokens, "temperature": 0}
    response = kubectl("exec", "-i", client, "-c", "client", "--", "python", "-c", CLIENT,
                       data=json.dumps(payload), timeout=420, check=False)
    result = json.loads(response.stdout)
    if response.returncode or not result["ok"]:
        raise RuntimeError("Request failed; inspect HTTP status with the service checks")

    print(result["answer"])
    print(json.dumps({"usage": result["usage"]}))


if __name__ == "__main__":
    with open("/tmp/ml-vllm-lesson.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
