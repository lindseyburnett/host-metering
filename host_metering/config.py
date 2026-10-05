"""Existing INI settings, overridden by HOST_METERING_* variables."""

import os
import re
from urllib.parse import urlsplit

DEFAULT_CONFIG_PATH = "/etc/host-metering.conf"


class Config:
    DEFAULTS = {
        "write_url": "http://localhost:9090/api/v1/write",
        "write_interval_sec": 600,
        "host_cert_path": "/etc/pki/consumer/cert.pem",
        "host_cert_key_path": "/etc/pki/consumer/key.pem",
        "collect_interval_sec": 0,
        "label_refresh_interval_sec": 86400,
        "send_hostname": "yes",
        "write_retry_attempts": 8,
        "write_retry_min_int_sec": 1,
        "write_retry_max_int_sec": 10,
        "write_timeout_sec": 60,
        "metrics_max_age_sec": 5400,
        "metrics_wal_path": "/var/run/host-metering/metrics",
        "log_level": "INFO",
        "log_path": "",
        "instance_id": "",
    }

    def __init__(self, **overrides):
        unknown = overrides.keys() - self.DEFAULTS.keys()
        if unknown:
            raise TypeError("Unknown configuration: " + ", ".join(sorted(unknown)))
        self.__dict__.update(self.DEFAULTS)
        self.__dict__.update(overrides)

    def _update(self, values):
        errors = []
        for name, default in self.DEFAULTS.items():
            if name not in values:
                continue
            value = values[name]
            if isinstance(default, int):
                if not re.fullmatch(r"[0-9]+", value) or int(value) > 0xFFFFFFFF:
                    errors.append("Invalid value for {}: {!r}".format(name, value))
                    continue
                value = int(value)
            setattr(self, name, value)
        return errors

    def update_from_file(self, path):
        # Keep the original parser's handling of comments, unknown lines and
        # literal '%' characters; configparser interpolation would change it.
        sections = {}
        section = None
        with open(path, encoding="utf-8") as stream:
            for raw_line in stream:
                line = raw_line.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1]
                    sections[section] = {}
                elif section is not None and "=" in line:
                    name, value = line.split("=", 1)
                    sections[section][name.strip()] = value.strip()
        return self._update(sections.get("host-metering", {}))

    def update_from_env(self, environ=None):
        environ = os.environ if environ is None else environ
        values = {}
        for name in self.DEFAULTS:
            value = environ.get("HOST_METERING_" + name.upper(), "")
            if value:
                values[name] = value
        return self._update(values)

    def validate(self):
        url = urlsplit(self.write_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("write_url must be an absolute HTTP or HTTPS URL")
        # Accessing port also validates its range and syntax.
        url.port
        if self.write_retry_attempts < 1:
            raise ValueError("write_retry_attempts must be at least 1")
        if self.write_timeout_sec <= 0:
            raise ValueError("write_timeout_sec must be positive")
        if self.write_interval_sec <= self.write_retry_attempts * (
            self.write_retry_max_int_sec + self.write_timeout_sec
        ):
            raise ValueError(
                "write_interval_sec must exceed write_retry_attempts * "
                "(write_retry_max_int_sec + write_timeout_sec)"
            )
        if self.write_retry_min_int_sec >= self.write_retry_max_int_sec:
            raise ValueError("write_retry_min_int_sec must be less than write_retry_max_int_sec")
        if not self.metrics_wal_path:
            raise ValueError("metrics_wal_path must be defined")
        if self.send_hostname not in ("yes", "no"):
            raise ValueError("send_hostname must be yes or no")
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARN", "WARNING", "ERROR"):
            raise ValueError("log_level must be DEBUG, INFO, WARN or ERROR")

    def __str__(self):
        return "Config:\n" + "\n".join(
            "|  {}: {}".format(name, getattr(self, name)) for name in self.DEFAULTS
        )
