import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from host_metering.cli import main
from host_metering.proxy import apply_rhsm_proxy


class RHSMProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "rhsm.conf"
        self.path.write_text("""[server]
proxy_hostname = proxy.example.test
proxy_port = 3128
[other]
proxy_hostname = ignored.example.test
proxy_port = 9999
""")
        self.proxy = "http://proxy.example.test:3128"

    def test_populates_missing_proxies(self):
        env = {"NO_PROXY": "localhost,10.0.0.0/8"}
        self.assertEqual(apply_rhsm_proxy(str(self.path), env), [])
        self.assertEqual(env, {"HTTP_PROXY": self.proxy, "HTTPS_PROXY": self.proxy,
                               "NO_PROXY": "localhost,10.0.0.0/8"})

    def test_preserves_each_explicit_proxy_independently(self):
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            with self.subTest(key=key):
                env = {key: "http://explicit.example.test:8080"}
                self.assertEqual(apply_rhsm_proxy(str(self.path), env), [])
                self.assertEqual(env[key], "http://explicit.example.test:8080")
                missing = "HTTPS_PROXY" if key.upper() == "HTTP_PROXY" else "HTTP_PROXY"
                self.assertEqual(env[missing], self.proxy)
                self.assertEqual(len(env), 2)

    def test_explicit_proxies_do_not_read_rhsm_file(self):
        env = {"HTTP_PROXY": "http://explicit", "https_proxy": "http://secure"}
        with patch("host_metering.proxy.open", side_effect=AssertionError("unnecessary read")):
            self.assertEqual(apply_rhsm_proxy(str(self.path), env), [])
        self.assertEqual(env, {"HTTP_PROXY": "http://explicit", "https_proxy": "http://secure"})

    def test_empty_variables_use_rhsm_defaults(self):
        env = {"HTTP_PROXY": "", "HTTPS_PROXY": ""}
        self.assertEqual(apply_rhsm_proxy(str(self.path), env), [])
        self.assertEqual(env, {"HTTP_PROXY": self.proxy, "HTTPS_PROXY": self.proxy})

    def test_missing_or_unconfigured_rhsm_is_normal(self):
        env = {"HTTP_PROXY": "http://explicit"}
        self.path.unlink()
        self.assertEqual(apply_rhsm_proxy(str(self.path), env), [])
        for content in ("[server]\n", "[server]\nproxy_hostname=proxy.test\n",
                        "[other]\nproxy_hostname=proxy.test\nproxy_port=3128\n"):
            with self.subTest(content=content):
                self.path.write_text(content)
                self.assertEqual(apply_rhsm_proxy(str(self.path), env), [])
                self.assertEqual(env, {"HTTP_PROXY": "http://explicit"})

    def test_bad_proxy_config_leaves_environment_intact(self):
        for content in ("not an ini file", "[server]\nproxy_hostname=proxy.test\nproxy_port=oops\n",
                        "[server]\nproxy_hostname=proxy.test\nproxy_port=0\n",
                        "[server]\nproxy_hostname=proxy.test\nproxy_port=65536\n",
                        "[server]\nproxy_hostname=http://proxy.test\nproxy_port=3128\n",
                        "[server]\nproxy_hostname=proxy.test:3128\nproxy_port=3128\n"):
            with self.subTest(content=content):
                env = {"HTTPS_PROXY": "http://explicit"}
                self.path.write_text(content)
                self.assertEqual(len(apply_rhsm_proxy(str(self.path), env)), 1)
                self.assertEqual(env, {"HTTPS_PROXY": "http://explicit"})

    def test_comments_and_ipv6_addresses(self):
        for hostname in ("2001:db8::1", "[2001:db8::1]"):
            with self.subTest(hostname=hostname):
                self.path.write_text("[server]\nproxy_hostname={} # host\nproxy_port=3128 ; port\n".format(hostname))
                env = {}
                self.assertEqual(apply_rhsm_proxy(str(self.path), env), [])
                self.assertEqual(env["HTTPS_PROXY"], "http://[2001:db8::1]:3128")

    def test_cli_applies_rhsm_defaults_before_starting_daemon(self):
        config = Path(self.temp.name) / "host-metering.conf"
        config.write_text("[host-metering]\n")
        with patch.dict(os.environ, {}, clear=True), patch("host_metering.cli.Daemon") as daemon, \
                patch("host_metering.cli.configure_logging", return_value=Mock()):
            def started():
                self.assertEqual(os.environ["HTTP_PROXY"], self.proxy)
                self.assertEqual(os.environ["HTTPS_PROXY"], self.proxy)
            daemon.return_value.__enter__.return_value.run_once.side_effect = started
            self.assertEqual(main(["--config", str(config), "--rhsm-config", str(self.path), "once"]), 0)
            daemon.return_value.__enter__.return_value.run_once.assert_called_once_with()
