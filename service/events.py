"""No prompts, response bodies, headers, raw URLs or exception messages in logs."""

import json
import logging
import os
import re
import socket

ROUTES = {"/health", "/v1/models", "/v1/chat/completions", "/v1/completions", "/metrics", "other"}
METHODS = {"GET", "POST", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE", "other"}
LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def valid_int(value, low, high):
    return type(value) is int and low <= value <= high


def sanitize(value):
    """Reconstruct output from approved fields; never echo arbitrary input."""
    if not isinstance(value, dict):
        return None
    event = value.get("event")
    if not isinstance(event, str):
        return None
    if event in {"request_started", "request_finished"}:
        request_id = value.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            return None
        route, method = value.get("route"), value.get("method")
        if not isinstance(route, str) or route not in ROUTES or not isinstance(method, str) or method not in METHODS:
            return None
        result = {"event": event, "request_id": request_id, "route": route, "method": method}
        if event == "request_finished":
            status, duration, outcome = value.get("status"), value.get("duration_ms"), value.get("outcome")
            if not valid_int(status, 100, 599) or type(duration) not in (int, float) or not 0 <= duration <= 86400000:
                return None
            if not isinstance(outcome, str) or outcome not in {"complete", "error", "disconnect"}:
                return None
            result.update(status=status, duration_ms=round(duration, 3), outcome=outcome)
        return result
    if event == "runtime_log":
        level, source = value.get("level"), value.get("source")
        if not isinstance(level, str) or level not in LEVELS or not isinstance(source, str) or source not in {"vllm", "uvicorn", "other"}:
            return None
        return {"event": event, "level": level, "source": source}
    return None


def emit(event):
    clean = sanitize(event)
    address = os.environ.get("SAFE_LOG_SOCKET")
    if clean is None or not address:
        return
    # One small Unix datagram, independent of stdout/stderr and HTTP bodies.
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.settimeout(0.2)
            channel.sendto(json.dumps(clean).encode(), address)
    except OSError:
        # Never let a logging failure echo a record or an exception to stderr.
        return


class SafeEventHandler(logging.Handler):
    def emit(self, record):
        name = record.name
        source = "vllm" if name == "vllm" or name.startswith("vllm.") else "uvicorn" if name.startswith("uvicorn") else "other"
        emit({"event": "runtime_log", "level": record.levelname, "source": source})
