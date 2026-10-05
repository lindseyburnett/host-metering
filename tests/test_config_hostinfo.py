import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from host_metering.cli import configure_logging, main
from host_metering.config import Config
from host_metering.hostinfo import HostInfo, SubManInfoProvider, get_cpu_count, parse_output

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = Config()
        config.validate()
        self.assertEqual(config.write_interval_sec, 600)
        self.assertEqual(config.metrics_max_age_sec, 5400)
        self.assertEqual(config.write_retry_attempts, 8)

    def test_file_environment_precedence_and_partial_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text("""ignored=value
; comment
[elsewhere]
write_interval_sec=1
[host-metering]
write_interval_sec=700
write_url=https://localhost/a%2Fb?token=a=b
instance_id=file
write_timeout_sec=oops
collect_interval_sec=9
log_path=
unknown=ignored
""")
            config = Config()
            self.assertEqual(len(config.update_from_file(str(path))), 1)
        self.assertEqual(config.write_interval_sec, 700)
        self.assertEqual(config.write_timeout_sec, 60)
        self.assertEqual(config.collect_interval_sec, 9)
        self.assertEqual(config.write_url, "https://localhost/a%2Fb?token=a=b")
        errors = config.update_from_env({"HOST_METERING_INSTANCE_ID": "env",
                                         "HOST_METERING_WRITE_INTERVAL_SEC": "800",
                                         "HOST_METERING_LOG_PATH": ""})
        self.assertEqual(errors, [])
        self.assertEqual(config.instance_id, "env")
        self.assertEqual(config.write_interval_sec, 800)

    def test_all_environment_settings(self):
        values = {}
        for name, default in Config.DEFAULTS.items():
            values["HOST_METERING_" + name.upper()] = "12" if isinstance(default, int) else "example"
        config = Config()
        self.assertEqual(config.update_from_env(values), [])
        for name, default in Config.DEFAULTS.items():
            self.assertEqual(getattr(config, name), 12 if isinstance(default, int) else "example")

    def test_unsigned_integer_parsing(self):
        for value in ("-1", "+1", "1.5", "nan", "4294967296", " 10", "10 "):
            with self.subTest(value=value):
                config = Config()
                self.assertTrue(config.update_from_env({"HOST_METERING_COLLECT_INTERVAL_SEC": value}))
                self.assertEqual(config.collect_interval_sec, 0)

    def test_invalid_configurations(self):
        for overrides in ({"write_url": ""}, {"write_url": "ftp://server/path"},
                          {"write_url": "https://server:99999"}, {"write_interval_sec": 560},
                          {"write_retry_min_int_sec": 10}, {"metrics_wal_path": ""},
                          {"write_retry_attempts": 0}, {"write_timeout_sec": 0},
                          {"send_hostname": "maybe"}, {"log_level": "invalid"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                Config(**overrides).validate()

    def test_log_permissions_and_literal_instance_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "log")
            config = Config(log_path=path, instance_id="100%", log_level="WARN")
            logger = configure_logging(config)
            logger.info("hidden")
            logger.warning("visible")
            handler = logger.handlers[0]
            handler.flush()
            text = Path(path).read_text()
            self.assertIn("[100%]", text)
            self.assertIn("visible", text)
            self.assertNotIn("hidden", text)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            configure_logging(Config())

    def test_cli_help_and_error_codes(self):
        with patch("sys.stdout"), patch("sys.stderr"):
            self.assertEqual(main(["help"]), 0)
            with self.assertRaises(SystemExit) as exc:
                main(["unknown"])
            self.assertEqual(exc.exception.code, 2)
            with patch.dict(os.environ, {"HOST_METERING_SEND_HOSTNAME": "invalid"}, clear=True):
                self.assertEqual(main(["--config", "/nonexistent", "once"]), 2)


class HostInfoTests(unittest.TestCase):
    def test_cpuinfo_counts_logical_processors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cpuinfo"
            path.write_text("processor\t: 0\ncpu cores: 2\n\nprocessor : 3\n")
            self.assertEqual(get_cpu_count(str(path)), 2)
            path.write_text("model name: not a CPU entry\n")
            with self.assertRaises(ValueError):
                get_cpu_count(str(path))
        with self.assertRaises(OSError):
            get_cpu_count("/does/not/exist")

    def test_parser(self):
        self.assertEqual(parse_output("# a:b\n; c:d\nProduct ID: 69\nproduct id: 394\n"
                                      "IPv6: a:b:c\ninvalid\n"),
                         {"product id": ["69", "394"], "ipv6": ["a:b:c"]})

    def test_mock_subscription_manager_for_all_clouds(self):
        expected = {
            "aws": ("000000000000", "1-11111111111111111"),
            "azure": ("00000000-0000-0000-0000-000000000000",
                      "11111111-1111-1111-1111-111111111111"),
            "gcp": ("000000000000", "1111111111111111111"),
        }
        for cloud, billing in expected.items():
            with self.subTest(cloud=cloud), patch.dict(os.environ, {"CLOUD_PROVIDER": cloud}):
                provider = SubManInfoProvider(lambda: 8, str(ROOT / "mocks/subscription-manager"))
                host = provider.load()
                self.assertEqual(host.host_id, "01234567-89ab-cdef-0123-456789abcdef")
                self.assertEqual(host.host_name, "host.mock.test")
                self.assertEqual(host.external_organization, "12345678")
                self.assertEqual(host.socket_count, "3")
                self.assertEqual(host.product, ["394", "69"])
                self.assertEqual((host.usage, host.support), ("Production", "Premium"))
                self.assertEqual(host.conversions_success, "true")
                self.assertEqual(host.billing.marketplace, cloud)
                self.assertEqual((host.billing.account, host.billing.instance_id), billing)
                provider.cpu_counter = lambda: 4
                provider.refresh_cpu_count(host)
                self.assertEqual(host.cpu_count, 4)

    def test_missing_subscription_manager_leaves_identity_empty(self):
        with self.assertLogs("host_metering", logging.WARNING):
            host = SubManInfoProvider(lambda: 8, "/does/not/exist").load()
        self.assertEqual(host.cpu_count, 8)
        self.assertEqual(host.host_id, "")

    def test_locale_is_forced_even_when_lc_all_is_set(self):
        with patch.dict(os.environ, {"LC_ALL": "fr_FR.UTF-8"}), patch(
            "host_metering.hostinfo.subprocess.run"
        ) as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            SubManInfoProvider(lambda: 1)._command("identity")
            self.assertEqual(run.call_args[1]["env"]["LC_ALL"], "C.UTF-8")

    def test_labels_are_sorted_nonempty_and_hostname_optional(self):
        host = HostInfo(host_id="id", host_name="name", product=["69", "394"])
        labels = host.labels()
        self.assertEqual(labels, sorted(labels))
        self.assertTrue(all(name and value for name, value in labels))
        self.assertEqual(dict(labels)["product"], "69,394")
        self.assertNotIn("display_name", dict(host.labels(False)))
