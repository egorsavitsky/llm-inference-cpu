"""Check that the complete installed distribution set equals requirements.txt."""

import importlib.metadata
import json
from pathlib import Path
import re
import sys


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def main():
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/requirements.txt")
    expected = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)", line)
        if not match or canonical(match[1]) in expected:
            raise SystemExit("Invalid or duplicate exact requirement")
        expected[canonical(match[1])] = match[2]
    actual = {canonical(dist.metadata["Name"]): dist.version for dist in importlib.metadata.distributions()}
    if actual != expected:
        print(json.dumps({"missing": sorted(expected.keys() - actual.keys()),
                          "extra": sorted(actual.keys() - expected.keys()),
                          "different": sorted(name for name in expected.keys() & actual.keys() if expected[name] != actual[name])}))
        raise SystemExit(1)
    print(json.dumps({"event": "dependency_lock_verified", "packages": len(actual)}))


if __name__ == "__main__":
    main()
