import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/configure_cluster_dns.py"
spec = importlib.util.spec_from_file_location("cluster_dns", SOURCE)
dns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dns)


class DnsTests(unittest.TestCase):
    def test_routable_resolver_selected_and_loopback_excluded(self):
        self.assertEqual(dns.upstreams_from_resolv("nameserver 127.0.0.53\nnameserver 172.31.144.1\nnameserver 172.31.144.1\n"), ["172.31.144.1"])

    def test_corefile_plugins_and_block_are_preserved(self):
        text = ".:53 {\n kubernetes cluster.local\n forward . /etc/resolv.conf {\n  max_concurrent 1000\n }\n cache 30\n}\n"
        fixed = dns.rewrite_corefile(text, ["172.31.144.1"])
        self.assertEqual(fixed, text.replace("/etc/resolv.conf", "172.31.144.1"))
        self.assertEqual(dns.rewrite_corefile(fixed, ["172.31.144.1"]), fixed)

    def test_unknown_corefile_is_not_replaced(self):
        with self.assertRaises(ValueError):
            dns.rewrite_corefile(".:53 {\n kubernetes cluster.local\n}\n", ["172.31.144.1"])

    def test_tcp_setting_is_idempotent_and_can_be_removed(self):
        original = ".:53 {\n forward . /etc/resolv.conf {\n  max_concurrent 1000\n }\n}\n"
        fixed = dns.rewrite_corefile(original, ["1.1.1.1"], force_tcp=True)
        self.assertEqual(fixed.count("force_tcp"), 1)
        self.assertEqual(dns.rewrite_corefile(fixed, ["1.1.1.1"], force_tcp=True), fixed)
        self.assertNotIn("force_tcp", dns.rewrite_corefile(fixed, ["1.1.1.1"]))

    def test_selection_uses_pod_results_and_avoids_forwarding_to_itself(self):
        result = {"wsl": {"dns": [{"server": "172.31.144.1", "ok": True}]},
                  "pod": {"resolvers": ["10.96.0.10"], "dns": [
                      {"server": "10.96.0.10", "ok": True},
                      {"server": "172.31.144.1", "ok": False},
                      {"server": "1.1.1.1", "ok": True}]}}
        self.assertEqual(dns.select_upstream(result), ("1.1.1.1", "udp"))

    def test_no_reachable_pod_dns_is_not_accepted(self):
        with self.assertRaises(RuntimeError):
            dns.select_upstream({"pod": {"resolvers": ["10.96.0.10"], "dns": [{"server": "10.96.0.10", "ok": True}]}})

    def test_real_tcp_dns_response_can_be_selected(self):
        result = {"pod": {"resolvers": ["10.96.0.10"], "dns": [], "dns_tcp": [{"server": "8.8.8.8", "ok": True}]}}
        self.assertEqual(dns.select_upstream(result), ("8.8.8.8", "tcp"))

    def test_https_fallback_requires_successful_dns_not_just_open_port(self):
        result = {"pod": {"dns_https": [{"server": "1.1.1.1", "ok": True}],
                          "tcp": [{"host": "1.1.1.1", "port": 443, "ok": True}]}}
        self.assertEqual(dns.select_upstream(result), ("1.1.1.1", "https"))
        result["pod"]["dns_https"][0]["ok"] = False
        with self.assertRaises(RuntimeError):
            dns.select_upstream(result)

    def test_https_change_keeps_internal_dns_and_can_be_reverted(self):
        original = ".:53 {\n kubernetes cluster.local\n forward . /etc/resolv.conf {\n  max_concurrent 1000\n }\n cache 30\n}\n"
        fixed = dns.rewrite_corefile(original, ["1.1.1.1"], https=True)
        self.assertIn("forward . https://1.1.1.1 {", fixed)
        self.assertIn("tls_servername 1.1.1.1", fixed)
        self.assertIn("kubernetes cluster.local", fixed)
        self.assertIn("cache 30", fixed)
        self.assertEqual(dns.rewrite_corefile(fixed, ["1.1.1.1"], https=True), fixed)
        plain = dns.rewrite_corefile(fixed, ["172.31.144.1"])
        self.assertEqual(plain, original.replace("/etc/resolv.conf", "172.31.144.1"))

    def test_unverified_https_endpoint_and_mixed_modes_are_rejected(self):
        corefile = ".:53 {\n forward . /etc/resolv.conf {\n }\n}\n"
        with self.assertRaises(ValueError):
            dns.rewrite_corefile(corefile, ["192.0.2.1"], https=True)
        with self.assertRaises(ValueError):
            dns.rewrite_corefile(corefile, ["1.1.1.1"], https=True, force_tcp=True)


if __name__ == "__main__":
    unittest.main()
