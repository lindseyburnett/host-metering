import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from host_metering.certwatcher import CertWatcher
from host_metering.config import Config
from host_metering.daemon import Daemon
from host_metering.hostinfo import HostInfo
from host_metering.notifier import NotifyError


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(metrics_wal_path=self.temp.name, metrics_max_age_sec=0,
                          host_cert_path=str(Path(self.temp.name) / "cert.pem"),
                          host_cert_key_path=str(Path(self.temp.name) / "key.pem"),
                          write_interval_sec=0.025, label_refresh_interval_sec=0)
        self.provider = Mock()
        self.provider.load.return_value = HostInfo(cpu_count=4, host_id="id", external_organization="org")
        self.notifier = Mock()
        self.daemon = Daemon(self.cfg, provider=self.provider, notifier=self.notifier)
        self.addCleanup(self.daemon.close)

    def test_run_once(self):
        self.daemon.run_once()
        self.assertEqual(self.notifier.notify.call_count, 1)
        samples, host = self.notifier.notify.call_args[0]
        self.assertEqual(samples[0].value, 4)
        self.assertEqual(host.host_id, "id")
        self.assertEqual(self.daemon.metrics_log.get_samples()[0], [])

    def test_recoverable_and_unknown_errors_preserve_queue(self):
        for error in (NotifyError("unavailable"), RuntimeError("unknown failure")):
            with self.subTest(error=error):
                self.notifier.notify.side_effect = error
                with self.assertRaises(type(error)):
                    self.daemon.run_once()
                self.assertTrue(self.daemon.metrics_log.get_samples()[0])
        self.notifier.notify.side_effect = None
        self.daemon.notify()
        self.assertEqual(len(self.notifier.notify.call_args[0][0]), 2)
        self.assertEqual(self.daemon.metrics_log.get_samples()[0], [])

    def test_permanent_errors_discard_rejected_batch_only(self):
        def reject(samples, host):
            self.daemon.metrics_log.write_sample(9)
            raise NotifyError("bad request", recoverable=False)
        self.notifier.notify.side_effect = reject
        with self.assertRaises(NotifyError):
            self.daemon.run_once()
        self.assertEqual([s.value for s in self.daemon.metrics_log.get_samples()[0]], [9])

    def test_unregistered_hosts_still_prune_expired_samples(self):
        self.cfg.metrics_max_age_sec = 1
        self.provider.load.return_value = HostInfo(cpu_count=4)
        self.daemon.metrics_log.write_sample(9, 1000)
        self.daemon.run_once()
        self.notifier.notify.assert_not_called()
        self.assertEqual([s.value for s in self.daemon.metrics_log.get_samples()[0]], [4])

    def run_in_thread(self):
        thread = threading.Thread(target=self.daemon.run, daemon=True)
        thread.start()
        def stop():
            self.daemon.stop()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        return thread

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while not condition():
            if time.monotonic() > deadline:
                self.fail("Condition was not met")
            time.sleep(0.005)

    def test_daemon_collect_write_reload_and_stop(self):
        thread = self.run_in_thread()
        self.wait_for(lambda: self.notifier.notify.call_count >= 2)
        self.daemon.reload()
        self.wait_for(lambda: self.provider.load.call_count >= 2)
        self.assertTrue(self.daemon.started)
        self.daemon.stop()
        thread.join(timeout=3)
        self.assertFalse(self.daemon.started)

    def test_independent_collection_timer_batches_samples(self):
        self.cfg.collect_interval_sec = 0.01
        self.cfg.write_interval_sec = 0.1
        self.run_in_thread()
        self.wait_for(lambda: self.notifier.notify.call_count >= 2)
        self.assertGreater(len(self.notifier.notify.call_args[0][0]), 1)

    def test_label_refresh_timer(self):
        self.cfg.label_refresh_interval_sec = 0.01
        self.run_in_thread()
        self.wait_for(lambda: self.provider.load.call_count >= 3)

    def test_recovery_after_initial_host_load_failure(self):
        self.provider.load.side_effect = [OSError("temporary failure"),
                                          HostInfo(cpu_count=4, host_id="id", external_organization="org")]
        self.run_in_thread()
        self.wait_for(lambda: self.notifier.notify.call_count >= 1)
        self.assertEqual(self.provider.load.call_count, 2)

    def test_cert_reload_invalidates_client_even_if_host_loading_fails(self):
        self.provider.load.side_effect = OSError("temporary")
        with self.assertRaises(OSError):
            self.daemon.load_host_info()
        self.notifier.host_changed.assert_called_once_with()


class WatcherAndSignalTests(unittest.TestCase):
    def test_certificate_and_key_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / "cert", Path(directory) / "key"
            watcher = CertWatcher(str(cert), str(key))
            self.assertFalse(watcher.changed())
            cert.write_text("cert")
            self.assertTrue(watcher.changed())
            self.assertFalse(watcher.changed())
            cert.write_text("new cert")
            self.assertTrue(watcher.changed())
            key.write_text("key")
            self.assertTrue(watcher.changed())
            replacement = Path(directory) / "replacement"
            replacement.write_text("new cert")
            replacement.replace(cert)
            self.assertTrue(watcher.changed())
            cert.unlink()
            self.assertTrue(watcher.changed())
            cert.write_text("recreated")
            self.assertTrue(watcher.changed())

    def test_real_sighup_and_sigterm(self):
        with tempfile.TemporaryDirectory() as directory:
            script = r'''
import pathlib, sys
from host_metering.config import Config
from host_metering.daemon import Daemon
from host_metering.hostinfo import HostInfo
from unittest.mock import Mock
root = pathlib.Path(sys.argv[1])
def load():
    with (root / "loads").open("a") as stream:
        stream.write("loaded\n")
    return HostInfo(cpu_count=1, host_id="id", external_organization="org")
provider = Mock()
provider.load.side_effect = load
with Daemon(Config(metrics_wal_path=str(root / "queue")), provider=provider, notifier=Mock()) as daemon:
    daemon.run()
'''
            process = subprocess.Popen([sys.executable, "-c", script, directory],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                path = Path(directory) / "loads"
                deadline = time.monotonic() + 5
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(path.exists(), "Daemon did not start")
                process.send_signal(signal.SIGHUP)
                while len(path.read_text().splitlines()) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertGreaterEqual(len(path.read_text().splitlines()), 2)
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr.decode())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
