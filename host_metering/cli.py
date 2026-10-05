"""Command line entry point."""

import argparse
import logging
import os
import sys

from . import __version__
from .config import Config, DEFAULT_CONFIG_PATH
from .daemon import Daemon
from .proxy import apply_rhsm_proxy


def configure_logging(config):
    logger = logging.getLogger("host_metering")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    if config.log_path:
        fd = os.open(config.log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(fd)
        handler = logging.FileHandler(config.log_path, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)
    # Instance IDs are literal data, including any '%' characters.
    class Formatter(logging.Formatter):
        def format(self, record):
            prefix = "[{}] ".format(config.instance_id) if config.instance_id else ""
            return prefix + super().format(record)
    handler.setFormatter(Formatter("%(asctime)s %(message)s", "%Y/%m/%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(config.log_level.upper())
    logger.propagate = False
    return logger


def main(argv=None):
    parser = argparse.ArgumentParser(prog="host-metering")
    parser.add_argument("--config", "-config", default=DEFAULT_CONFIG_PATH,
                        help="configuration file path (default: %(default)s)")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--rhsm-config", help="fill missing proxy settings from this RHSM config file")
    parser.add_argument("command", choices=("daemon", "once", "migrate-wal", "help"))
    args = parser.parse_args(argv)
    if args.command == "help":
        parser.print_help()
        return 0
    config = Config()
    messages = []
    try:
        messages.extend(config.update_from_file(args.config))
    except (OSError, UnicodeError) as exc:
        messages.append("Failed to process config file: {}".format(exc))
    messages.extend(config.update_from_env())
    try:
        if args.command != "migrate-wal":
            config.validate()
        elif not config.metrics_wal_path:
            raise ValueError("metrics_wal_path must be defined")
    except ValueError as exc:
        print("Invalid configuration: {}".format(exc), file=sys.stderr)
        return 2
    try:
        logger = configure_logging(config)
    except ValueError as exc:
        print("Invalid logging configuration: {}".format(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print("Failed to initialize logger: {}".format(exc), file=sys.stderr)
        return 1
    if args.rhsm_config and args.command != "migrate-wal":
        messages.extend(apply_rhsm_proxy(args.rhsm_config))
    for message in messages:
        logger.warning(message)
    logger.info("%s", config)
    try:
        if args.command == "migrate-wal":
            from .migrate import migrate_metrics
            count = migrate_metrics(config.metrics_wal_path)
            if count is None:
                logger.info("WAL migration already completed; no samples imported")
            else:
                logger.info("WAL migration completed: imported %d sample(s)", count)
            return 0
        with Daemon(config) as daemon:
            if args.command == "once":
                daemon.run_once()
            else:
                daemon.run()
    except Exception as exc:
        logger.error("Host metering failed: %s", exc)
        return 1
    return 0
