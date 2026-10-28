"""Configure CoreDNS with an explicitly selected, verified upstream resolver."""

import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def upstreams_from_resolv(text):
    result = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            address = ipaddress.ip_address(parts[1])
            if not address.is_loopback and not address.is_unspecified and not address.is_multicast:
                if str(address) not in result:
                    result.append(str(address))
    if not result:
        raise ValueError("No routable upstream DNS found in WSL")
    return result


def rewrite_corefile(corefile, upstreams, *, force_tcp=False, https=False):
    checked = upstreams_from_resolv("\n".join("nameserver " + item for item in upstreams))
    if https and force_tcp:
        raise ValueError("Choose either TCP DNS or DNS over HTTPS")
    if https and (len(checked) != 1 or checked[0] not in {"1.1.1.1", "1.0.0.1"}):
        raise ValueError("DNS over HTTPS requires one verified Cloudflare IP endpoint")
    targets = ["https://" + item if https else item for item in checked]
    pattern = r"(?m)^([ \t]*forward[ \t]+\.)[ \t]+[^\n{]+"
    updated, count = re.subn(pattern, lambda match: match[1] + " " + " ".join(targets) + " ", corefile, count=1)
    if count != 1:
        raise ValueError("Expected one default DNS forward rule")
    updated = re.sub(r"(?m)^[ \t]*force_tcp[ \t]*\n", "", updated)
    updated = re.sub(r"(?m)^[ \t]*tls_servername[ \t]+(?:1\.1\.1\.1|1\.0\.0\.1)[ \t]*\n", "", updated)
    if force_tcp or https:
        option = "force_tcp" if force_tcp else "tls_servername " + checked[0]
        updated, count = re.subn(r"(?m)^([ \t]*forward[ \t]+\.[^\n]*\{)[ \t]*$",
                                lambda match: match[1] + "\n       " + option, updated, count=1)
        if count != 1:
            raise ValueError("TCP DNS requires a forward block")
    return updated


def select_upstream(diagnostics):
    pod = diagnostics.get("pod", {})
    cluster_resolvers = set(pod.get("resolvers", []))
    for field, transport in (("dns", "udp"), ("dns_tcp", "tcp"), ("dns_https", "https")):
        for probe in pod.get(field, []):
            address = probe.get("server")
            if probe.get("ok") and address not in cluster_resolvers:
                upstreams_from_resolv("nameserver " + address)
                if transport == "https" and address not in {"1.1.1.1", "1.0.0.1"}:
                    continue
                return address, transport
    raise RuntimeError("No external DNS answered directly from the Pod; see network-diagnostics.json")


def kubectl(*args, timeout=60):
    from check_kubernetes import kubectl_command
    result = subprocess.run(kubectl_command(*args, namespace="kube-system"),
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("CoreDNS configuration operation failed")
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", action="append")
    parser.add_argument("--force-tcp", action="store_true")
    parser.add_argument("--https", action="store_true")
    args = parser.parse_args()
    if "microsoft" not in Path("/proc/sys/kernel/osrelease").read_text().lower():
        print(json.dumps({"event": "cluster_dns_default", "reason": "not_wsl"}))
        return
    upstreams = args.upstream or upstreams_from_resolv(Path("/etc/resolv.conf").read_text())
    if args.https:
        # Native DoH was checked for this release.
        deployment = json.loads(kubectl("get", "deployment", "coredns", "-o", "json"))
        image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        if not re.search(r":v1\.14\.6(?:@sha256:[0-9a-f]+)?$", image):
            raise RuntimeError("Verify native DoH support for this CoreDNS image before changing its configuration")
    config = json.loads(kubectl("get", "configmap", "coredns", "-o", "json"))
    before = config["data"]["Corefile"]
    after = rewrite_corefile(before, upstreams, force_tcp=args.force_tcp, https=args.https)
    results = ROOT / "results/local"
    results.mkdir(parents=True, exist_ok=True)
    backup = results / "kubernetes-coredns-before.json"
    if not backup.exists():
        backup.write_text(json.dumps(config, indent=2) + "\n")
    if before != after:
        patch = json.dumps({"data": {"Corefile": after}})
        kubectl("patch", "configmap", "coredns", "--type=merge", "-p", patch)
        kubectl("rollout", "restart", "deployment/coredns")
        kubectl("rollout", "status", "deployment/coredns", "--timeout=180s", timeout=200)
    proof = {"event": "cluster_dns_configured", "upstreams": upstreams,
             "transport": "https" if args.https else "tcp" if args.force_tcp else "udp", "changed": before != after,
             "scope": "Only CoreDNS in the named local Minikube cluster; no Windows/WSL resolver settings changed."}
    (results / "kubernetes-dns.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
