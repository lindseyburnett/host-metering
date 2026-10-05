"""Collect, queue and report metrics; all scheduled work runs serially."""

import logging
import signal
import threading
import time

from .certwatcher import CertWatcher
from .hostinfo import SubManInfoProvider
from .metricslog import MetricsLog
from .notifier import NotifyError, PrometheusNotifier, should_notify

LOG = logging.getLogger("host_metering")


class Daemon:
    def __init__(self, config, provider=None, notifier=None, metrics_log=None):
        self.config = config
        self.stop_event = threading.Event()
        self.reload_event = threading.Event()
        self.provider = provider if provider is not None else SubManInfoProvider()
        self.notifier = notifier if notifier is not None else PrometheusNotifier(config, self.stop_event)
        self.watcher = CertWatcher(config.host_cert_path, config.host_cert_key_path)
        self.metrics_log = metrics_log if metrics_log is not None else MetricsLog(config.metrics_wal_path)
        self.host_info = None
        self.started = False

    def load_host_info(self):
        # Invalidate credentials even when reloading subscription info fails.
        self.notifier.host_changed()
        self.host_info = self.provider.load()
        LOG.info("HostInfo loaded: %s", dict(self.host_info.labels()))

    def collect_metrics(self):
        if self.host_info is None:
            self.load_host_info()
        self.provider.refresh_cpu_count(self.host_info)
        self.metrics_log.write_sample(self.host_info.cpu_count)

    def notify(self):
        # Prune before policy checks too, so unregistered hosts stay bounded.
        self.metrics_log.remove_expired(self.config.metrics_max_age_sec)
        samples, checkpoint = self.metrics_log.get_samples()
        reason = should_notify(samples, self.host_info)
        if reason:
            LOG.warning("Cannot notify: %s", reason)
            return
        try:
            self.notifier.notify(samples, self.host_info)
        except NotifyError as exc:
            if not exc.recoverable:
                self.metrics_log.remove_samples(checkpoint)
            raise
        self.metrics_log.remove_samples(checkpoint)
        LOG.info("Notification successful: sent %d sample(s)", len(samples))

    def run_once(self):
        self.load_host_info()
        self.collect_metrics()
        self.notify()

    def stop(self):
        self.stop_event.set()

    def reload(self):
        self.reload_event.set()

    @staticmethod
    def _attempt(action):
        try:
            action()
        except Exception:
            LOG.exception("Metering operation failed; will retry on the next interval")

    def _write(self):
        if self.config.collect_interval_sec == 0:
            self._attempt(self.collect_metrics)
        self.notify()

    def run(self):
        previous_handlers = {}
        if threading.current_thread() is threading.main_thread():
            for signum, callback in ((signal.SIGINT, self.stop), (signal.SIGTERM, self.stop),
                                     (signal.SIGHUP, self.reload)):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda *_args, cb=callback: cb())
        try:
            self._attempt(self.run_once)
            now = time.monotonic()
            timers = [
                [self.config.write_interval_sec, now + self.config.write_interval_sec, self._write],
                [1, now + 1, self._check_credentials],
            ]
            for interval, action in ((self.config.collect_interval_sec, self.collect_metrics),
                                     (self.config.label_refresh_interval_sec, self.load_host_info)):
                if interval > 0:
                    timers.append([interval, now + interval, action])
            self.started = True
            LOG.info("Server fully started")
            while not self.stop_event.is_set():
                if self.reload_event.is_set():
                    self.reload_event.clear()
                    self._attempt(self.load_host_info)
                now = time.monotonic()
                for timer in timers:
                    if self.stop_event.is_set():
                        break
                    interval, deadline, action = timer
                    if now >= deadline:
                        self._attempt(action)
                        # Skip missed ticks instead of starting an overdue burst.
                        elapsed = time.monotonic() - deadline
                        timer[1] = deadline + (int(elapsed // interval) + 1) * interval
                delay = min(timer[1] for timer in timers) - time.monotonic()
                self.stop_event.wait(max(0, min(delay, 0.2)))
        finally:
            self.started = False
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            LOG.info("Server stopped")

    def _check_credentials(self):
        if self.watcher.changed():
            LOG.info("Host certificate or key changed")
            self.load_host_info()

    def close(self):
        try:
            if hasattr(self.notifier, "close"):
                self.notifier.close()
        finally:
            self.metrics_log.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
