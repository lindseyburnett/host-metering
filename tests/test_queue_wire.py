from pathlib import Path
import tempfile
import unittest

from host_metering.metricslog import MetricsLog
from host_metering.wire import (
    Sample, decode_sample, encode_sample, encode_varint, encode_write_request, read_varint, snappy_block,
)


class WireTests(unittest.TestCase):
    def test_known_protobuf_encoding(self):
        # Independently specified protobuf: Label(name="a", value="b") and
        # Sample(value=1.0, timestamp=150), wrapped in TimeSeries/WriteRequest.
        expected = bytes.fromhex("0a160a060a0161120162120c09000000000000f03f109601")
        self.assertEqual(encode_write_request([("a", "b")], [Sample(1, 150)]), expected)

    def test_snappy_literal_boundaries(self):
        fixtures = [(0, "00"), (1, "0100"), (60, "3cec"),
                    (61, "3df03c"), (256, "8002f0ff"), (257, "8102f40001"),
                    (65536, "808004f4ffff"), (65537, "818004f8000001")]
        for size, header in fixtures:
            with self.subTest(size=size):
                self.assertEqual(snappy_block(b"x" * size), bytes.fromhex(header) + b"x" * size)

    def test_varints_and_signed_timestamps(self):
        for value in (0, 1, 127, 128, 150, 0xFFFFFFFFFFFFFFFF):
            encoded = encode_varint(value)
            self.assertEqual(read_varint(encoded), (value, len(encoded)))
        sample = Sample(8.0, -1)
        self.assertEqual(decode_sample(encode_sample(sample)), sample)

    def test_corrupt_samples_are_rejected(self):
        for data in (b"\x09\x00", b"\x10\x80", b"\x00", b"\x0a\x05abc",
                     b"\x08\x01", b"\x10" + b"\xff" * 10):
            with self.subTest(data=data), self.assertRaises(ValueError):
                decode_sample(data)

    def test_unknown_protobuf_fields_are_skipped(self):
        sample = Sample(2, 3)
        self.assertEqual(decode_sample(encode_sample(sample) + b"\x1a\x03abc"), sample)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "metrics"

    def open_log(self):
        log = MetricsLog(str(self.path))
        self.addCleanup(log.close)
        return log

    def test_persistence_and_snapshot_deletion(self):
        log = self.open_log()
        log.write_sample(4, 1000)
        samples, checkpoint = log.get_samples()
        self.assertEqual(samples, [Sample(4, 1000)])
        log.write_sample(8, 2000)
        log.remove_samples(checkpoint)
        log.close()
        log = self.open_log()
        self.assertEqual(log.get_samples()[0], [Sample(8, 2000)])
        _, checkpoint = log.get_samples()
        log.remove_samples(checkpoint)
        log.write_sample(16, 3000)
        log.remove_samples(checkpoint)
        self.assertEqual(log.get_samples()[0], [Sample(16, 3000)])

    def test_expiration_boundary_and_clock_rollback(self):
        log = self.open_log()
        for timestamp in (1000, 500, 1500, 999):
            log.write_sample(4, timestamp)
        log.remove_expired(1, now=2)
        self.assertEqual(log.get_samples()[0], [Sample(4, 1000), Sample(4, 1500)])
        log.remove_expired(0, now=100)
        self.assertEqual(len(log.get_samples()[0]), 2)
        log.remove_expired(1, now=100)
        self.assertEqual(log.get_samples(), ([], 0))

    def test_concurrent_daemons_cannot_use_same_queue(self):
        log = self.open_log()
        with self.assertRaises(OSError):
            MetricsLog(str(self.path))
        log.close()
        self.open_log()

    def test_legacy_import_skips_checkpoints_and_never_reimports(self):
        self.path.mkdir()
        # Go gogo/protobuf Sample(value=8, timestamp=150), length-prefixed by
        # tidwall/wal; its checkpoint is the single zero byte.
        data = bytes.fromhex("0c09000000000000204010960100")
        segment = self.path / "00000000000000000004"
        segment.write_bytes(data)
        (self.path / "00000000000000000006").write_bytes(bytes.fromhex("0b0900000000000010401078"))
        log = self.open_log()
        self.assertEqual(log.get_samples()[0], [Sample(4, 120), Sample(8, 150)])
        self.assertEqual(segment.read_bytes(), data)
        log.remove_samples(log.get_samples()[1])
        log.close()
        self.assertEqual(self.open_log().get_samples(), ([], 0))

    def test_legacy_import_rolls_back_on_corruption(self):
        self.path.mkdir()
        segment = self.path / "00000000000000000001"
        segment.write_bytes(bytes.fromhex("0b0900000000000010401078") + b"\x7f")
        with self.assertRaises(ValueError):
            MetricsLog(str(self.path))
        segment.write_bytes(bytes.fromhex("0b0900000000000010401078"))
        self.assertEqual(self.open_log().get_samples()[0], [Sample(4, 120)])

    def test_legacy_start_marker_honors_committed_truncation(self):
        self.path.mkdir()
        (self.path / "00000000000000000001").write_bytes(b"discarded data")
        (self.path / "00000000000000000002.START").write_bytes(
            bytes.fromhex("0b0900000000000010401078"))
        self.assertEqual(self.open_log().get_samples()[0], [Sample(4, 120)])

    def test_legacy_end_marker_honors_committed_truncation(self):
        self.path.mkdir()
        (self.path / "00000000000000000001").write_bytes(b"replaced data")
        (self.path / "00000000000000000001.END").write_bytes(
            bytes.fromhex("0b0900000000000010401078"))
        (self.path / "00000000000000000002").write_bytes(b"truncated data")
        self.assertEqual(self.open_log().get_samples()[0], [Sample(4, 120)])

    def test_legacy_gap_fails_without_silently_dropping_data(self):
        self.path.mkdir()
        (self.path / "00000000000000000001").write_bytes(b"\x00")
        (self.path / "00000000000000000003").write_bytes(b"\x00")
        with self.assertRaises(ValueError):
            MetricsLog(str(self.path))
