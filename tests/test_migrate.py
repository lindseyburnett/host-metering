import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from host_metering.cli import main
from host_metering.metricslog import MetricsLog
from host_metering.migrate import decode_sample, migrate_metrics, read_varint
from host_metering.wire import Sample


class LegacyDecoderTests(unittest.TestCase):
    def test_varints_and_signed_timestamps(self):
        self.assertEqual(read_varint(bytes.fromhex("9601")), (150, 2))
        sample = bytes.fromhex("09000000000000204010ffffffffffffffffff01")
        self.assertEqual(decode_sample(sample), Sample(8, -1))

    def test_corrupt_samples_are_rejected(self):
        for data in (b"\x09\x00", b"\x10\x80", b"\x00", b"\x0a\x05abc",
                     b"\x08\x01", b"\x10" + b"\xff" * 10):
            with self.subTest(data=data), self.assertRaises(ValueError):
                decode_sample(data)

    def test_unknown_protobuf_fields_are_skipped(self):
        sample = bytes.fromhex("0900000000000010401078")
        self.assertEqual(decode_sample(sample + b"\x1a\x03abc"), Sample(4, 120))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "metrics"
        self.path.mkdir()
        # One tidwall/wal record: length-prefixed Sample(value=4, timestamp=120).
        self.record = bytes.fromhex("0b0900000000000010401078")

    def test_startup_requires_explicit_migration(self):
        segment = self.path / "00000000000000000001"
        segment.write_bytes(self.record)
        with self.assertRaisesRegex(ValueError, "migrate-wal"):
            MetricsLog(str(self.path))
        self.assertEqual(segment.read_bytes(), self.record)
        self.assertEqual(migrate_metrics(str(self.path)), 1)
        with MetricsLog(str(self.path)) as queue:
            self.assertEqual(queue.get_samples()[0], [Sample(4, 120)])

    def test_import_skips_checkpoints_and_never_reimports(self):
        # Sample(value=8, timestamp=150) followed by an empty checkpoint record.
        data = bytes.fromhex("0c09000000000000204010960100")
        segment = self.path / "00000000000000000004"
        segment.write_bytes(data)
        (self.path / "00000000000000000006").write_bytes(self.record)
        self.assertEqual(migrate_metrics(str(self.path)), 2)
        with MetricsLog(str(self.path)) as queue:
            self.assertEqual(queue.get_samples()[0], [Sample(4, 120), Sample(8, 150)])
            queue.remove_samples(queue.get_samples()[1])
        self.assertEqual(segment.read_bytes(), data)
        self.assertIsNone(migrate_metrics(str(self.path)))
        with MetricsLog(str(self.path)) as queue:
            self.assertEqual(queue.get_samples(), ([], 0))

    def test_import_rolls_back_on_corruption(self):
        with MetricsLog(str(self.path)) as queue:
            queue.write_sample(16, 90)
        segment = self.path / "00000000000000000001"
        segment.write_bytes(self.record + b"\x7f")
        with self.assertRaises(ValueError):
            migrate_metrics(str(self.path))
        with MetricsLog(str(self.path), allow_legacy=True) as queue:
            self.assertEqual(queue.get_samples()[0], [Sample(16, 90)])
        with self.assertRaisesRegex(ValueError, "migrate-wal"):
            MetricsLog(str(self.path))
        segment.write_bytes(self.record)
        self.assertEqual(migrate_metrics(str(self.path)), 1)
        with MetricsLog(str(self.path)) as queue:
            self.assertEqual(queue.get_samples()[0], [Sample(16, 90), Sample(4, 120)])

    def test_start_marker_honors_committed_truncation(self):
        (self.path / "00000000000000000001").write_bytes(b"discarded data")
        (self.path / "00000000000000000002.START").write_bytes(self.record)
        self.assertEqual(migrate_metrics(str(self.path)), 1)
        with MetricsLog(str(self.path)) as queue:
            self.assertEqual(queue.get_samples()[0], [Sample(4, 120)])

    def test_end_marker_honors_committed_truncation(self):
        (self.path / "00000000000000000001").write_bytes(b"replaced data")
        (self.path / "00000000000000000001.END").write_bytes(self.record)
        (self.path / "00000000000000000002").write_bytes(b"truncated data")
        self.assertEqual(migrate_metrics(str(self.path)), 1)
        with MetricsLog(str(self.path)) as queue:
            self.assertEqual(queue.get_samples()[0], [Sample(4, 120)])

    def test_gap_and_conflicting_markers_fail_without_dropping_data(self):
        for suffix in ("", ".END"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                (path / ("00000000000000000001" + suffix)).write_bytes(b"\x00")
                (path / ("00000000000000000003" + (".START" if suffix else ""))).write_bytes(b"\x00")
                with self.assertRaises(ValueError):
                    migrate_metrics(directory)

    def test_cannot_migrate_while_daemon_owns_queue(self):
        with MetricsLog(str(self.path)):
            with self.assertRaises(OSError):
                migrate_metrics(str(self.path))

    def test_prior_automatic_migration_marker_remains_valid(self):
        (self.path / "00000000000000000001").write_bytes(b"retained archive, never read again")
        with closing(sqlite3.connect(str(self.path / "samples.sqlite3"))) as db:
            with db:
                db.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                           "value REAL NOT NULL, timestamp INTEGER NOT NULL)")
                db.execute("INSERT INTO samples(value, timestamp) VALUES (4, 120)")
                db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("INSERT INTO metadata VALUES ('legacy_imported', '1')")
        self.assertIsNone(migrate_metrics(str(self.path)))
        with MetricsLog(str(self.path)) as queue:
            self.assertEqual(queue.get_samples()[0], [Sample(4, 120)])

    def test_missing_directory_fails_without_creating_it(self):
        path = self.path / "missing"
        with self.assertRaisesRegex(ValueError, "does not exist"):
            migrate_metrics(str(path))
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            migrate_metrics("")

    def test_no_segments_does_not_mark_migration_completed(self):
        with self.assertRaisesRegex(ValueError, "No legacy WAL segments"):
            migrate_metrics(str(self.path))
        (self.path / "00000000000000000001").write_bytes(self.record)
        self.assertEqual(migrate_metrics(str(self.path)), 1)
        with MetricsLog(str(self.path)) as queue:
            self.assertEqual(queue.get_samples()[0], [Sample(4, 120)])

    def test_empty_checkpoint_queue_can_be_migrated(self):
        (self.path / "00000000000000000001").write_bytes(b"\x00")
        self.assertEqual(migrate_metrics(str(self.path)), 0)
        self.assertIsNone(migrate_metrics(str(self.path)))
        with MetricsLog(str(self.path)) as queue:
            self.assertEqual(queue.get_samples(), ([], 0))

    def test_cli_reports_migration_failure_and_allows_retry(self):
        segment = self.path / "00000000000000000001"
        segment.write_bytes(self.record + b"\xff")
        config = Path(self.temp.name) / "metering.conf"
        config.write_text("[host-metering]\nmetrics_wal_path={}\n".format(self.path))
        with patch.dict(os.environ, {}, clear=True), patch("host_metering.cli.configure_logging") as logging:
            self.assertEqual(main(["--config", str(config), "migrate-wal"]), 1)
            logging.return_value.error.assert_called_once()
            segment.write_bytes(self.record)
            self.assertEqual(main(["--config", str(config), "migrate-wal"]), 0)
        with MetricsLog(str(self.path)) as queue:
            self.assertEqual(queue.get_samples()[0], [Sample(4, 120)])

    def test_cli_migrates_without_daemon_or_valid_network_settings(self):
        (self.path / "00000000000000000001").write_bytes(self.record)
        config = Path(self.temp.name) / "metering.conf"
        config.write_text("[host-metering]\nmetrics_wal_path={}\nwrite_url=invalid\n".format(self.path))
        with patch.dict(os.environ, {}, clear=True), patch("host_metering.cli.Daemon") as daemon:
            self.assertEqual(main(["--config", str(config), "migrate-wal"]), 0)
            self.assertEqual(main(["--config", str(config), "migrate-wal"]), 0)
            daemon.assert_not_called()
        with MetricsLog(str(self.path)) as queue:
            self.assertEqual(queue.get_samples()[0], [Sample(4, 120)])

    def test_daemon_imports_do_not_load_legacy_decoder(self):
        result = subprocess.run([sys.executable, "-c",
                                 "import host_metering.cli, sys; "
                                 "assert 'host_metering.migrate' not in sys.modules"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
