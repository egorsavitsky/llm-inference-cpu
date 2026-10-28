"""Linux/container integration checks for raw output suppression and signals."""

import json
import os
import signal
import subprocess
import sys


def launch(code):
    wrapper = "import sys; from service.launch import supervise; sys.exit(supervise([sys.executable, '-c', sys.argv[1]]))"
    return [sys.executable, "-c", wrapper, code]


marker = "PRIVATE_NATIVE_AND_TRACEBACK_CANARY"
code = f"""
import logging, os, sys
from service.events import emit, SafeEventHandler
print('{marker}', flush=True)
os.write(2, b'{marker}\\n')
logger = logging.getLogger('vllm.test')
logger.addHandler(SafeEventHandler())
logger.error('{marker}')
emit({{'event':'request_finished','request_id':'a'*32,'method':'POST','route':'/v1/chat/completions','status':500,'duration_ms':1,'outcome':'error','prompt':'{marker}'}})
raise RuntimeError('{marker}')
"""
result = subprocess.run(launch(code), capture_output=True, text=True, timeout=20)
assert result.returncode == 1
assert marker not in result.stdout + result.stderr
records = [json.loads(line) for line in result.stdout.splitlines()]
assert any(item["event"] == "request_finished" and item["status"] == 500 for item in records)
assert any(item["event"] == "runtime_log" for item in records)
assert records[-1] == {"event": "service_exited", "exit_code": 1}

process = subprocess.Popen(launch("import time; time.sleep(60)"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
try:
    first = process.stdout.readline()
    assert json.loads(first)["event"] == "service_started"
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)
    assert not stderr
    events = [json.loads(line) for line in stdout.splitlines()]
    assert any(item["event"] == "service_stopping" for item in events)
    assert process.returncode == 143
finally:
    if process.poll() is None:
        process.kill()
        process.wait()
print(json.dumps({"raw_output_and_traceback_hidden": True, "safe_error_event_preserved": True,
                  "sigterm_forwarded": True, "uid": os.getuid()}))
