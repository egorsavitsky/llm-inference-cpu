"""Closed-loop load client for the in-cluster vLLM API."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import signal
import statistics
import sys
import time
import urllib.error
import urllib.request

BASE = "http://cpu-llm:8000"
MODEL = "learning-model"


def messages(workload, identity):
    # Vary prefixes between cases, but keep inputs identical across configs.
    nonce = hashlib.sha256(identity.encode()).hexdigest()[:16]
    repetitions = {"short": 1, "long": 45}[workload]
    text = nonce + ": " + "A computer reads data, follows instructions, and produces results. " * repetitions
    return [{"role": "user", "content": text + "Explain this process in simple English."}]


def request(path, payload):
    return urllib.request.Request(BASE + path, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + os.environ["VLLM_API_KEY"],
    })


def token_count(chat):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request("/tokenize", {"model": MODEL, "messages": chat,
                                          "add_generation_prompt": True}), timeout=30) as response:
        count = json.load(response)["count"]
    if type(count) is not int or count <= 0:
        raise ValueError("Invalid tokenizer count")
    return count


def read_stream(lines, started, clock=time.monotonic):
    first, finished, usage, reason, done = None, None, None, None, False
    parts = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            parts.append(line[5:].lstrip(" "))
            continue
        if line or not parts:
            continue
        data, parts = "\n".join(parts), []
        if done:
            raise ValueError("Unexpected data after stream completion")
        elapsed = clock() - started
        if data == "[DONE]":
            done, finished = True, elapsed
            # Drain HTTP to EOF so the server records a completed response.
            continue
        chunk = json.loads(data)
        if "error" in chunk:
            raise ValueError("Streaming error")
        if chunk.get("usage") is not None:
            usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                raise ValueError("Expected one completion")
            content = choice.get("delta", {}).get("content")
            if content:
                if not isinstance(content, str):
                    raise ValueError("Unexpected content type")
                if first is None:
                    first = elapsed
            if choice.get("finish_reason") is not None:
                reason = choice["finish_reason"]
    if parts or not done or first is None or reason is None or not usage:
        raise ValueError("Incomplete stream")
    for field in ("prompt_tokens", "completion_tokens"):
        if type(usage.get(field)) is not int or usage[field] <= 0:
            raise ValueError("Invalid token usage")
    if not 0 <= first <= finished or finished <= 0:
        raise ValueError("Invalid duration")
    return {"ok": True, "ttft_seconds": first, "latency_seconds": finished,
            "prompt_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"],
            "finish_reason": reason}


def generate(item):
    payload = {"model": MODEL, "messages": item["messages"], "max_tokens": 32,
               "temperature": 0, "ignore_eos": True, "stream": True,
               "stream_options": {"include_usage": True}}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    try:
        with opener.open(request("/v1/chat/completions", payload), timeout=360) as response:
            if response.headers.get_content_type() != "text/event-stream":
                raise ValueError("Expected SSE")
            result = read_stream(response, started)
        if result["prompt_tokens"] != item["expected_prompt_tokens"] or result["output_tokens"] != 32:
            raise ValueError("Token usage differs from controlled workload")
    except Exception as error:
        result = {"ok": False, "error_type": type(error).__name__}
        if isinstance(error, urllib.error.HTTPError):
            result["http_status"] = error.code
    return {"request_id": item["request_id"], **result}


def percentile(values, fraction):
    """Nearest-rank percentile; small samples do not estimate population tails."""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summarize(rows, seconds):
    successful = [row for row in rows if row["ok"]]
    summary = {"requests": len(rows), "successful": len(successful), "errors": len(rows) - len(successful),
               "wall_seconds": seconds, "requests_per_second": len(successful) / seconds,
               "output_tokens_per_second": sum(row["output_tokens"] for row in successful) / seconds,
               "input_tokens_per_second": sum(row["prompt_tokens"] for row in successful) / seconds}
    for field in ("ttft_seconds", "latency_seconds", "prompt_tokens"):
        values = [row[field] for row in successful]
        summary[field] = {"min": min(values) if values else None,
                          "median": statistics.median(values) if values else None,
                          "p95_nearest_rank": percentile(values, .95),
                          "max": max(values) if values else None}
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=8)
    args = parser.parse_args()
    if args.requests < 4 or args.requests % 4:
        parser.error("--requests must be a positive multiple of 4, at least 4")
    prepared = {}
    for workload in ("short", "long"):
        for concurrency in (1, 2, 4):
            items = []
            for number in range(args.requests):
                identity = f"cpu-benchmark-v1/{workload}/{concurrency}/{number}"
                chat = messages(workload, identity)
                count = token_count(chat)
                if count + 32 > 1024:
                    raise ValueError("Workload exceeds model context")
                items.append({"request_id": identity, "messages": chat, "expected_prompt_tokens": count})
            prepared[workload, concurrency] = items
    warmups = []
    for workload in ("short", "long"):
        chat = messages(workload, "cpu-benchmark-v1/warmup/" + workload)
        warmup = generate({"request_id": "warmup/" + workload, "messages": chat,
                           "expected_prompt_tokens": token_count(chat)})
        warmups.append(warmup)
        if not warmup["ok"]:
            raise RuntimeError("Warmup failed")
    cases = []
    for concurrency in (1, 2, 4):

        order = ("long", "short") if concurrency == 2 else ("short", "long")
        for workload in order:
            print(f"Benchmark: {workload}, clients={concurrency}, requests={args.requests}", file=sys.stderr, flush=True)
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                rows = list(executor.map(generate, prepared[workload, concurrency]))
            wall = time.monotonic() - started
            case = {"workload": workload, "concurrency": concurrency,
                    "summary": summarize(rows, wall), "requests": rows}
            cases.append(case)
            print(json.dumps({"event": "benchmark_case", "case": case}), flush=True)
            print(f"Completed: errors={case['summary']['errors']}, wall_seconds={wall:.2f}", file=sys.stderr, flush=True)
    result = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
                      "max_output_tokens": 32, "warmups_excluded": warmups, "cases": cases,
                      "method": {
                          "load": "Closed loop: up to C worker threads, each immediately sends its next request; no client think time. Worker queue time excluded from per-request latency, included in case wall time.",
                          "ttft": "Client time before HTTP call to first nonempty content delta, a first-text-chunk proxy for TTFT; role-only chunks excluded.",
                          "latency": "Client time to SSE [DONE]; server startup and tokenizer calls excluded.",
                          "throughput": "Sum of actual output tokens divided by full case wall time, including all workers and drain; input tokens reported separately.",
                          "tokens": "Inputs counted with the serving tokenizer/chat template, checked against usage. ignore_eos fixes output to 32 tokens. SSE chunks are not counted as tokens.",
                          "cache": "Early unique deterministic IDs vary across measured cases and warmups. Long prompt cache reuse reduced, not fully isolated; runtime defaults retained.",
                          "limits": "Small sample, one laptop and one run per configuration; p95 is nearest rank of this sample, not a production-tail estimate. Results concern speed, not answer quality.",
                      }}
    print(json.dumps({"event": "benchmark_result", "result": result}), flush=True)
    if any(case["summary"]["errors"] for case in cases):
        raise SystemExit(1)


if __name__ == "__main__":
    # Killing kubectl alone can leave the remote client running.
    signal.signal(signal.SIGALRM, lambda signum, frame: os._exit(124))
    signal.alarm(3500)
    try:
        main()
    except Exception as error:
        # No exception message, prompt, completion or auth header in terminal logs.
        print(json.dumps({"event": "benchmark_failed", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
