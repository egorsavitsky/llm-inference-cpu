#!/usr/bin/env bash
# Verify a fresh download without changing the serving PVC.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
result_dir="$project_dir/results/local"
mkdir -p -- "$result_dir"
exec 9>/tmp/ml-vllm-lesson.lock
flock -n 9 || { printf 'Another project experiment is running.\n' >&2; exit 1; }
exec > >(tee -a "$result_dir/fresh-download.log") 2>&1
printf 'RUNNING\n' > "$result_dir/fresh-download-status.txt"
finish() {
    local code=$?
    if [ "$code" -eq 0 ]; then
        printf 'SUCCESS: Fresh model download verified.\n' | tee "$result_dir/fresh-download-status.txt"
    else
        printf 'ERROR: Fresh download check stopped with exit code %s.\n' "$code" | tee "$result_dir/fresh-download-status.txt"
    fi
    printf 'Logs and status: %s\n' "$result_dir"
}
trap finish EXIT
printf '\nFresh download check started at %s\n' "$(date -Is)"
[ "$(id -u)" -eq 0 ] || { printf 'Run this check as root in Ubuntu.\n' >&2; exit 1; }
python3 -u "$project_dir/scripts/check_fresh_download.py"
