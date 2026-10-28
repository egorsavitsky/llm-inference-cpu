"""API probes with unique markers for checking server logs."""

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/tmp")
from stream_client import collect_stream

BASE = "http://127.0.0.1:8000"


def call(path, payload=None, token=True, raw=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + (os.environ["VLLM_API_KEY"] if token is True else token)
    data = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read()
    with response:
        return response.status, response.read()


def main():
    marker = "INPUT_CANARY_" + secrets.token_hex(12)
    wrong = "WRONG_KEY_" + secrets.token_hex(12)
    results = {}
    results["no_key"] = call("/v1/models", token=False)[0]
    results["wrong_key"] = call("/v1/models", token=wrong)[0]
    results["models"] = call("/v1/models")[0]
    results["unknown_path"] = call("/v1/" + marker + "?query=" + marker)[0]
    results["invalid_json"] = call("/v1/chat/completions", raw=("{" + marker).encode())[0]
    results["invalid_messages"] = call("/v1/chat/completions", {"model": "learning-model", "messages": marker})[0]
    results["unknown_model"] = call("/v1/chat/completions", {"model": marker, "messages": [{"role": "user", "content": marker}]})[0]
    assert results["no_key"] == results["wrong_key"] == 401
    assert results["models"] == 200 and results["unknown_path"] == 404
    assert results["invalid_json"] in (400, 422) and results["invalid_messages"] in (400, 422)
    assert results["unknown_model"] == 404
    payload = {"model": "learning-model", "messages": [{"role": "user", "content": "Say hello in one short sentence. Reference: " + marker}],
               "max_tokens": 12, "temperature": 0, "stream": False}
    status, body = call("/v1/chat/completions", payload)
    assert status == 200
    response = json.loads(body)
    answer = response["choices"][0]["message"]["content"]
    assert isinstance(answer, str) and len(answer.strip()) >= 4
    results["completion"] = status
    payload.update(stream=True, stream_options={"include_usage": True}, max_tokens=8)
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["VLLM_API_KEY"]})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as stream:
        streamed = collect_stream(stream, started)
        # Drain HTTP too, so the server records a completed response.
        stream.read()
    results["stream"] = 200
    print(json.dumps({"statuses": results, "probe_strings": [marker, wrong, answer, streamed["answer"]],
                      "completion_tokens": response["usage"]["completion_tokens"],
                      "stream_tokens": streamed["usage"]["completion_tokens"]}))


if __name__ == "__main__":
    main()
