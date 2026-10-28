"""PID 1: supervise vLLM and publish only validated events from a private socket."""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

from service.events import sanitize


def output(event):
    print(json.dumps(event, separators=(",", ":")), flush=True)


def server_command(config):
    if set(config) != {"model_path", "served_name", "host", "port", "dtype", "max_model_len", "max_num_seqs", "max_num_batched_tokens"}:
        raise ValueError("Unexpected config fields")
    if not Path(config["model_path"]).is_absolute() or not Path(config["model_path"]).is_dir():
        raise ValueError("A local model directory is required")
    if config["dtype"] not in ("float32", "bfloat16", "float16"):
        raise ValueError("Invalid dtype")
    for name in ("port", "max_model_len", "max_num_seqs", "max_num_batched_tokens"):
        if type(config[name]) is not int or not 1 <= config[name] <= 65535:
            raise ValueError("Invalid numeric setting")
    return [
        "vllm", "serve", config["model_path"], "--served-model-name", config["served_name"],
        "--host", config["host"], "--port", str(config["port"]), "--dtype", config["dtype"],
        "--max-model-len", str(config["max_model_len"]),
        "--max-num-seqs", str(config["max_num_seqs"]),
        "--max-num-batched-tokens", str(config["max_num_batched_tokens"]),
        "--enforce-eager", "--no-enable-log-requests", "--no-enable-log-outputs",
        "--disable-uvicorn-access-log", "--middleware", "service.middleware.RequestEvents",
    ]


def supervise(command):
    with tempfile.TemporaryDirectory(prefix="safe-events-") as directory:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            address = str(Path(directory) / "events.sock")
            channel.bind(address)
            channel.settimeout(0.25)
            environment = {
                **os.environ, "SAFE_LOG_SOCKET": address,
                "VLLM_LOGGING_CONFIG_PATH": "/app/config/logging.json",
                "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
            }
            # Library/native prints and tracebacks cannot reach container logs.
            # Safe logging uses the dedicated socket, never these descriptors.
            child = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = None

            def stop(signum, frame):
                nonlocal deadline
                if deadline is None:
                    deadline = time.monotonic() + 25
                    output({"event": "service_stopping"})
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

            previous = {signum: signal.signal(signum, stop) for signum in (signal.SIGTERM, signal.SIGINT)}
            output({"event": "service_started", "uid": os.getuid()})
            try:
                while True:
                    try:
                        data = channel.recv(8192)
                    except socket.timeout:
                        if child.poll() is not None:
                            break
                    else:
                        try:
                            clean = sanitize(json.loads(data))
                        except (ValueError, TypeError, RecursionError):
                            clean = None
                        if clean is not None:
                            output(clean)
                    if deadline is not None and time.monotonic() >= deadline:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                code = child.wait()
                output({"event": "service_exited", "exit_code": code})
                return code if code >= 0 else 128 - code
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
                # Also clean worker descendants after an unexpected parent exit.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()


def main():
    try:
        if os.getuid() == 0 or not os.environ.get("VLLM_API_KEY"):
            raise ValueError("Non-root user and an API key are required")
        config = json.loads(Path(os.environ.get("SERVICE_CONFIG", "/app/config/server.json")).read_text())
        command = server_command(config)
    except Exception:
        output({"event": "configuration_error"})
        return 2
    try:
        return supervise(command)
    except Exception:
        output({"event": "supervisor_error"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
