import importlib.util
from pathlib import Path
import socket
import struct
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "scripts/network_diagnostics.py"
spec = importlib.util.spec_from_file_location("dns_probe", SOURCE)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class FakeChannel:
    def __init__(self, *, tcp=False, rcode=0, mismatch=False):
        self.tcp, self.rcode, self.mismatch = tcp, rcode, mismatch
        self.buffer = b""
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def settimeout(self, value):
        pass
    def prepare(self, query):
        transaction = struct.unpack("!H", query[:2])[0] ^ int(self.mismatch)
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("203.0.113.7")
        payload = struct.pack("!6H", transaction, 0x8180 | self.rcode, 1, int(self.rcode == 0), 0, 0) + query[12:]
        if self.rcode == 0:
            payload += answer
        self.buffer = struct.pack("!H", len(payload)) + payload if self.tcp else payload
    def sendto(self, query, address):
        self.prepare(query)
    def sendall(self, query):
        self.prepare(query[2:])
    def recv(self, size):
        amount = min(size, 3) if self.tcp else size
        result, self.buffer = self.buffer[:amount], self.buffer[amount:]
        return result


class DnsProbeTests(unittest.TestCase):
    def test_udp_a_response_with_compressed_name(self):
        with patch.object(probe.socket, "socket", return_value=FakeChannel()):
            result = probe.dns_query("1.1.1.1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["addresses"], ["203.0.113.7"])

    def test_tcp_frames_can_arrive_in_partial_reads(self):
        with patch.object(probe.socket, "create_connection", return_value=FakeChannel(tcp=True)):
            result = probe.dns_tcp("1.1.1.1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "dns_tcp")

    def test_servfail_is_not_success(self):
        with patch.object(probe.socket, "socket", return_value=FakeChannel(rcode=2)):
            result = probe.dns_query("1.1.1.1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["rcode"], 2)

    def test_wrong_transaction_is_not_success(self):
        with patch.object(probe.socket, "socket", return_value=FakeChannel(mismatch=True)):
            result = probe.dns_query("1.1.1.1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "ValueError")

    def https_probe(self, content_type):
        response = Mock()
        response.status = 200
        response.headers.get_content_type.return_value = content_type
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        def open_request(request, timeout):
            self.assertEqual(request.full_url, "https://1.1.1.1/dns-query")
            self.assertEqual(request.get_header("Content-type"), "application/dns-message")
            channel = FakeChannel()
            channel.prepare(request.data)
            response.read.return_value = channel.buffer
            return response
        opener.open.side_effect = open_request
        with patch.object(probe.urllib.request, "build_opener", return_value=opener):
            return probe.dns_https("1.1.1.1")

    def test_https_requires_valid_dns_wire_response(self):
        result = self.https_probe("application/dns-message")
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "dns_https")
        self.assertEqual(result["addresses"], ["203.0.113.7"])

    def test_http_200_html_is_not_a_dns_success(self):
        result = self.https_probe("text/html")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "ValueError")


if __name__ == "__main__":
    unittest.main()
