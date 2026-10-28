"""Parse SSE responses and record client-visible timings."""

import json
import time


def sse_events(lines):
    """Dispatch data only at an SSE event boundary; ignore comments/other fields."""
    parts = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if parts:
                yield "\n".join(parts)
                parts = []
        elif line.startswith("data:"):
            value = line[5:]
            parts.append(value[1:] if value.startswith(" ") else value)
    if parts:
        raise RuntimeError("Incomplete SSE event at end of connection")


def collect_stream(lines, started, clock=time.monotonic):
    answer = []
    arrivals = []
    usage = None
    finish_reason = None
    completed = False
    for event in sse_events(lines):
        elapsed = clock() - started
        if event == "[DONE]":
            completed = True
            total = elapsed
            break
        chunk = json.loads(event)
        if "error" in chunk:
            raise RuntimeError("Server returned a streaming error")
        if chunk.get("usage") is not None:
            usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                raise RuntimeError("Expected one completion")
            content = choice.get("delta", {}).get("content")
            if content:
                if not isinstance(content, str):
                    raise RuntimeError("Unexpected content type")
                answer.append(content)
                arrivals.append(elapsed)
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]
    if not completed or finish_reason is None:
        raise RuntimeError("Stream ended without a completed response")
    if not arrivals or not "".join(answer).strip():
        raise RuntimeError("No generated text in stream")
    if not usage or not isinstance(usage.get("completion_tokens"), int) or usage["completion_tokens"] <= 0:
        raise RuntimeError("Missing positive server token usage")
    if total <= 0:
        raise RuntimeError("Invalid elapsed time")
    return {
        "answer": "".join(answer),
        "ttft_seconds": round(arrivals[0], 6),
        "total_seconds": round(total, 6),
        "after_first_content_seconds": round(total - arrivals[0], 6),
        "last_content_seconds": round(arrivals[-1], 6),
        "content_chunks": len(arrivals),
        "chunk_arrival_seconds": [round(value, 6) for value in arrivals],
        "usage": usage,
        "finish_reason": finish_reason,
        "e2e_output_tokens_per_second": round(usage["completion_tokens"] / total, 6),
    }
