#!/usr/bin/env bash
# Install Docker Engine on Ubuntu.
# Source: https://docs.docker.com/engine/install/ubuntu/
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run this setup as root inside Ubuntu WSL.' >&2
    exit 1
fi

. /etc/os-release
if [ "$ID" != ubuntu ] || [ "$VERSION_CODENAME" != noble ] || [ "$(dpkg --print-architecture)" != amd64 ]; then
    printf '%s\n' 'This setup is limited to the inspected Ubuntu 24.04 amd64 environment.' >&2
    exit 1
fi

if command -v docker >/dev/null 2>&1; then
    printf '%s\n' 'Docker already exists; leaving the installation unchanged.'
    systemctl start docker
    docker version
    exit 0
fi

for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -qx 'install ok installed'; then
        printf 'Conflicting installed package: %s. Inspect it before proceeding.\n' "$package" >&2
        exit 1
    fi
done

docker_source='Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc'
if [ -e /etc/apt/sources.list.d/docker.list ]; then
    printf '%s\n' 'A different Docker apt configuration exists. Inspect it before proceeding.' >&2
    exit 1
fi
if [ -e /etc/apt/sources.list.d/docker.sources ] && ! cmp -s /etc/apt/sources.list.d/docker.sources <(printf '%s\n' "$docker_source"); then
    printf '%s\n' 'The existing Docker apt source differs from this setup. Leaving it unchanged.' >&2
    exit 1
fi

apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 -o APT::Update::Error-Mode=any update
apt-get install -y --no-install-recommends ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
docker_key_tmp=$(mktemp /etc/apt/keyrings/docker.asc.XXXXXX)
trap 'rm -f -- "$docker_key_tmp"' EXIT
curl --fail --show-error --silent --location --connect-timeout 20 --max-time 120 \
    https://download.docker.com/linux/ubuntu/gpg -o "$docker_key_tmp"
mv -- "$docker_key_tmp" /etc/apt/keyrings/docker.asc
chmod 0644 /etc/apt/keyrings/docker.asc

printf '%s\n' "$docker_source" > /etc/apt/sources.list.d/docker.sources

apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 -o APT::Update::Error-Mode=any update
apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin
systemctl start docker
docker version

