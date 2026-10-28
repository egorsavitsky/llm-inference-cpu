"""Exit status for the local /health endpoint."""

import urllib.request


def main():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
