"""DNS over UDP/TCP/HTTPS in WSL and a Pod; no credentials or HTTP bodies."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def resolvers():
    return [line.split()[1] for line in Path("/etc/resolv.conf").read_text().splitlines()
            if line.startswith("nameserver ")]


def skip_name(packet, offset):
    while packet[offset]:
        if packet[offset] & 0xC0 == 0xC0:
            return offset + 2
        offset += packet[offset] + 1
    return offset + 1


def receive_exact(channel, size):
    parts = bytearray()
    while len(parts) < size:
        block = channel.recv(size - len(parts))
        if not block:
            raise OSError("DNS TCP response ended early")
        parts.extend(block)
    return bytes(parts)


def dns_query(server, transport="udp"):
    result = {"server": server, "kind": "dns_" + transport}
    transaction = secrets.randbits(16)
    labels = b"".join(bytes([len(part)]) + part.encode() for part in "huggingface.co".split(".")) + b"\x00"
    query = struct.pack("!6H", transaction, 0x0100, 1, 0, 0, 0) + labels + struct.pack("!2H", 1, 1)
    try:
        family = socket.AF_INET6 if ipaddress.ip_address(server).version == 6 else socket.AF_INET
        if transport == "https":
            # Literal IP avoids depending on the DNS service being diagnosed.
            # urllib verifies the HTTPS certificate against this IP using system CAs.
            request = urllib.request.Request("https://" + server + "/dns-query", data=query,
                                             headers={"Content-Type": "application/dns-message",
                                                      "Accept": "application/dns-message"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=5) as response:
                if response.status != 200 or response.headers.get_content_type() != "application/dns-message":
                    raise ValueError("Unexpected DNS HTTPS response")
                packet = response.read(65536)
                if len(packet) > 65535:
                    raise ValueError("DNS HTTPS response too large")
        elif transport == "tcp":
            with socket.create_connection((server, 53), timeout=3) as channel:
                channel.sendall(struct.pack("!H", len(query)) + query)
                size = struct.unpack("!H", receive_exact(channel, 2))[0]
                packet = receive_exact(channel, size)
        else:
            with socket.socket(family, socket.SOCK_DGRAM) as channel:
                channel.settimeout(3)
                channel.sendto(query, (server, 53))
                packet = channel.recv(4096)
        ident, flags, questions, answers, _, _ = struct.unpack("!6H", packet[:12])
        if ident != transaction or not flags & 0x8000:
            raise ValueError("Unexpected DNS response")
        offset = 12
        for _ in range(questions):
            offset = skip_name(packet, offset) + 4
        addresses = []
        for _ in range(answers):
            offset = skip_name(packet, offset)
            kind, group, ttl, size = struct.unpack("!HHIH", packet[offset:offset + 10])
            offset += 10
            if kind == 1 and group == 1 and size == 4:
                addresses.append(socket.inet_ntoa(packet[offset:offset + 4]))
            offset += size
        result.update(ok=bool(addresses) and flags & 15 == 0, rcode=flags & 15, addresses=addresses)
    except Exception as error:
        result.update(ok=False, error_type=type(error).__name__)
        if isinstance(error, urllib.error.URLError) and isinstance(error.reason, Exception):
            result["reason_type"] = type(error.reason).__name__
        if isinstance(error, urllib.error.HTTPError):
            result["http_status"] = error.code
    return result


def dns_tcp(server):
    return dns_query(server, "tcp")


def dns_https(server):
    return dns_query(server, "https")


def tcp_probe(target):
    host, port = target
    result = {"kind": "tcp", "host": host, "port": port}
    try:
        with socket.create_connection(target, timeout=3):
            pass
        result["ok"] = True
    except OSError as error:
        result.update(ok=False, error_type=type(error).__name__, errno=error.errno)
    return result


def matrix(extra):
    servers = list(dict.fromkeys([*resolvers(), *extra, "1.1.1.1", "8.8.8.8", "9.9.9.9"]))
    with ThreadPoolExecutor(max_workers=8) as executor:
        dns = list(executor.map(dns_query, servers))
        dns_over_tcp = list(executor.map(dns_tcp, servers))
        dns_over_https = list(executor.map(dns_https, ["1.1.1.1", "1.0.0.1"]))
        tcp = list(executor.map(tcp_probe, [("1.1.1.1", 443), ("1.1.1.1", 53), ("8.8.8.8", 53)]))
    return {"resolvers": resolvers(), "dns": dns, "dns_tcp": dns_over_tcp,
            "dns_https": dns_over_https, "tcp": tcp}


def diagnose():
    from check_kubernetes import kubectl_command
    root = Path(__file__).resolve().parents[1]
    host_dns = resolvers()
    program = Path(__file__).read_text()
    command = kubectl_command("exec", "egress-probe", "--", "python", "-c", program, "--inside")
    for resolver in host_dns:
        command.extend(["--host-dns", resolver])
    host_result = matrix([])
    pod = subprocess.run(command, capture_output=True, text=True, timeout=45)
    result = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "wsl": host_result,
              "pod": json.loads(pod.stdout) if pod.returncode == 0 else {"probe_exit_code": pod.returncode}}
    folder = root / "results/local"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "network-diagnostics.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--host-dns", action="append", default=[])
    args = parser.parse_args()
    print(json.dumps(matrix(args.host_dns) if args.inside else diagnose(), indent=2))


if __name__ == "__main__":
    main()
