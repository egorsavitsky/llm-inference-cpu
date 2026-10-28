"""Pinned, hash-verified model download using only Python's standard library."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request


def event(name, **fields):
    print(json.dumps({"event": name, **fields}), flush=True)


def validate_manifest(manifest):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", manifest["model_id"]):
        raise ValueError("Invalid model ID")
    if not re.fullmatch(r"[0-9a-f]{40}", manifest["revision"]):
        raise ValueError("An immutable revision is required")
    if not manifest["files"]:
        raise ValueError("Empty file list")
    for name, item in manifest["files"].items():
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", name) or name in (".", ".."):
            raise ValueError("Invalid filename")
        if type(item["size"]) is not int or item["size"] <= 0 or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise ValueError("Invalid fingerprint")


def valid_file(path, expected):
    try:
        if path.is_symlink() or path.stat().st_size != expected["size"]:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest() == expected["sha256"]
    except OSError:
        return False


class SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https":
            raise ValueError("HTTPS redirect required")
        redirected = super().redirect_request(request, fp, code, message, headers, newurl)
        if redirected is not None and target.hostname != urllib.parse.urlsplit(request.full_url).hostname:
            redirected.remove_header("Authorization")
        return redirected


def ensure_model(manifest, directory, *, offline=False, opener=None, retries=3, sleep=time.sleep):
    validate_manifest(manifest)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # Locking is performed by main on Linux; tests call this function directly.
    files_ok = {name: valid_file(directory / name, expected) for name, expected in manifest["files"].items()}
    if all(files_ok.values()):
        marker = directory / ".ready.json"
        try:
            marker_ok = json.loads(marker.read_text()) == manifest
        except (OSError, ValueError):
            marker_ok = False
        if not marker_ok:
            temporary = directory / ".ready.json.tmp"
            temporary.write_text(json.dumps(manifest, indent=2) + "\n")
            temporary.replace(marker)
        event("model_cache_verified", revision=manifest["revision"])
        return
    if offline:
        raise RuntimeError("Offline cache verification failed")
    marker = directory / ".ready.json"
    marker.unlink(missing_ok=True)
    opener = opener or urllib.request.build_opener(SafeRedirects())
    token = os.environ.get("HF_TOKEN", "")
    headers = {"User-Agent": "cpu-llm-model-downloader/1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    for name, expected in manifest["files"].items():
        if files_ok[name]:
            continue
        destination = directory / name
        temporary = directory / (name + ".part")
        if temporary.is_symlink():
            raise ValueError("Unexpected temporary symlink")
        url = f"https://huggingface.co/{manifest['model_id']}/resolve/{manifest['revision']}/{name}"
        for attempt in range(retries):
            try:
                request = urllib.request.Request(url, headers=headers)
                with opener.open(request, timeout=120) as source, temporary.open("wb") as target:
                    downloaded = 0
                    while True:
                        block = source.read(4 * 1024 * 1024)
                        if not block:
                            break
                        downloaded += len(block)
                        if downloaded > expected["size"]:
                            raise ValueError("Unexpected download size")
                        target.write(block)
                if not valid_file(temporary, expected):
                    raise ValueError("Downloaded fingerprint mismatch")
                temporary.replace(destination)
                event("model_file_ready", file=name)
                break
            except Exception:
                if attempt + 1 == retries:
                    raise RuntimeError("Model file download failed") from None
                event("model_download_retry", file=name, attempt=attempt + 1)
                sleep(attempt + 1)
    temporary = directory / ".ready.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(marker)
    event("model_download_complete", revision=manifest["revision"])


def main():
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name("model-manifest.json").read_text())
    directory = Path(os.environ.get("MODEL_DIR", "/models/qwen-0.5b"))
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".download.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ensure_model(manifest, directory, offline=args.check_only)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # HTTP exceptions may contain signed URLs or credentials: never echo.
        event("model_download_failed")
        sys.exit(1)
