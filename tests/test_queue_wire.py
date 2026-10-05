from pathlib import Path
import tempfile
import unittest

from host_metering.metricslog import MetricsLog
from host_metering.wire import (
    Sample, encode_sample, encode_varint, encode_write_request, snappy_block,
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
        fixtures = [(0, "00"), (1, "01"), (127, "7f"), (128, "8001"),
                    (150, "9601"), (0xFFFFFFFFFFFFFFFF, "ffffffffffffffffff01")]
        for value, encoded in fixtures:
            self.assertEqual(encode_varint(value), bytes.fromhex(encoded))
        self.assertEqual(encode_sample(Sample(8, -1)),
                         bytes.fromhex("09000000000000204010ffffffffffffffffff01"))


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
