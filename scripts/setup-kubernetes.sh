#!/usr/bin/env bash
# Prepare Minikube, import local images and verify the service.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
result_dir="$project_dir/results/local"
mkdir -p -- "$result_dir"
exec 9>/tmp/ml-vllm-lesson.lock
flock -n 9 || { printf 'Another project experiment is running.\n' >&2; exit 1; }
exec > >(tee -a "$result_dir/setup-kubernetes.log") 2>&1
printf 'RUNNING\n' > "$result_dir/setup-kubernetes-status.txt"
finish() {
    local code=$?
    if [ "$code" -eq 0 ]; then
        printf 'SUCCESS: Kubernetes verification completed.\n' | tee "$result_dir/setup-kubernetes-status.txt"
    else
        printf 'ERROR: Kubernetes setup stopped with exit code %s. Keep the logs.\n' "$code" | tee "$result_dir/setup-kubernetes-status.txt"
    fi
    printf 'Logs and status: %s\n' "$result_dir"
}
trap finish EXIT
printf '\nKubernetes setup started at %s\n' "$(date -Is)"
[ "$(id -u)" -eq 0 ] || { printf 'Run this setup as root in Ubuntu.\n' >&2; exit 1; }
[ "$(uname -m)" = x86_64 ] || { printf 'This setup targets Linux amd64.\n' >&2; exit 1; }
systemctl start docker
python3 - "$project_dir" <<'PY'
import json, pathlib, shutil, subprocess, sys
available = int(next(line.split()[1] for line in pathlib.Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))
running = subprocess.run(['docker', 'inspect', '--type=container', 'ml-inference'], capture_output=True, text=True)
resuming = running.returncode == 0 and json.loads(running.stdout)[0]['State']['Running']
if not resuming and available < 8 * 1024**2:
    raise SystemExit('Less than 8 GiB currently available in WSL; close unused applications and retry.')
if shutil.disk_usage(sys.argv[1]).free < 40 * 1024**3:
    raise SystemExit('Less than 40 GiB free on the project drive.')
PY
minikube_version=v1.39.0
kubernetes_version=v1.37.0
task_downloads="$result_dir/tool-downloads"
mkdir -p "$task_downloads"
install_checked() {
    local name=$1 url=$2 checksum_url=$3
    curl --fail --location --retry 2 --connect-timeout 20 --max-time 900 "$url" -o "$task_downloads/$name"
    curl --fail --location --retry 2 --connect-timeout 20 --max-time 120 "$checksum_url" -o "$task_downloads/$name.sha256"
    python3 - "$task_downloads/$name" "$task_downloads/$name.sha256" <<'PY'
import hashlib, pathlib, re, sys
target, checksum = map(pathlib.Path, sys.argv[1:])
expected = checksum.read_text().split()[0]
if not re.fullmatch('[0-9a-fA-F]{64}', expected) or hashlib.sha256(target.read_bytes()).hexdigest().lower() != expected.lower():
    raise SystemExit('Downloaded tool checksum mismatch')
PY
    install -m 0755 "$task_downloads/$name" "/usr/local/bin/$name"
}
if ! command -v minikube >/dev/null || [ "$(minikube version --short)" != "$minikube_version" ]; then
    install_checked minikube "https://github.com/kubernetes/minikube/releases/download/$minikube_version/minikube-linux-amd64" "https://github.com/kubernetes/minikube/releases/download/$minikube_version/minikube-linux-amd64.sha256"
fi
if ! command -v kubectl >/dev/null || ! kubectl version --client -o json | python3 -c "import json,sys; sys.exit(json.load(sys.stdin)['clientVersion']['gitVersion'] != '$kubernetes_version')"; then
    install_checked kubectl "https://dl.k8s.io/release/$kubernetes_version/bin/linux/amd64/kubectl" "https://dl.k8s.io/release/$kubernetes_version/bin/linux/amd64/kubectl.sha256"
fi
if ! id mlops >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash --user-group mlops
fi
[ "$(id -u mlops)" -ne 0 ] || { printf 'mlops must be non-root.\n' >&2; exit 1; }
usermod -aG docker mlops
runuser -u mlops -- docker info >/dev/null
if ! docker image inspect ml-inference-service:0.1 >/dev/null 2>&1; then
    docker build --network=none --pull=false -t ml-inference-service:0.1 "$project_dir"
fi
if ! docker image inspect ml-model-downloader:0.1 >/dev/null 2>&1; then
    docker build --network=none --pull=false -t ml-model-downloader:0.1 "$project_dir/downloader"
fi
runuser -u mlops -- minikube start -p ml-inference --driver=docker --container-runtime=containerd --cni=calico --kubernetes-version="$kubernetes_version" --cpus=4 --memory=8704 --wait=all --wait-timeout=15m
# These are versioned development images; do not re-import gigabytes on retry.
# After rebuilding an existing tag, load it explicitly with --overwrite=true.
runuser -u mlops -- minikube -p ml-inference image load ml-inference-service:0.1 --daemon --overwrite=false
runuser -u mlops -- minikube -p ml-inference image load ml-model-downloader:0.1 --daemon --overwrite=false
python3 -u "$project_dir/scripts/check_kubernetes.py"
