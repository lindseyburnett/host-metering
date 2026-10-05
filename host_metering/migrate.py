"""Explicit migration of tidwall/wal queues; never imported by the daemon.

Format: https://github.com/tidwall/wal/blob/v1.1.7/wal.go
"""

from pathlib import Path
import struct

from .metricslog import MetricsLog, legacy_segments
from .wire import Sample


def read_varint(data, offset=0):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Truncated varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError("Varint overflow")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("Invalid varint")


def decode_sample(data):
    """Decode legacy protobuf samples and reject malformed fields."""
    offset, value, timestamp = 0, 0.0, 0
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        field, kind = tag >> 3, tag & 7
        if field == 0:
            raise ValueError("Invalid protobuf field")
        if kind == 0:
            number, offset = read_varint(data, offset)
            if field == 2:
                timestamp = number if number < (1 << 63) else number - (1 << 64)
        elif kind in (1, 5):
            size = 8 if kind == 1 else 4
            if offset + size > len(data):
                raise ValueError("Truncated protobuf field")
            if field == 1 and kind == 1:
                value = struct.unpack_from("<d", data, offset)[0]
            offset += size
        elif kind == 2:
            size, offset = read_varint(data, offset)
            offset += size
            if offset > len(data):
                raise ValueError("Truncated protobuf field")
        else:
            raise ValueError("Unsupported protobuf wire type")
        if (field == 1 and kind != 1) or (field == 2 and kind != 0):
            raise ValueError("Invalid sample field type")
    return Sample(value, timestamp)


def legacy_samples(directory):
    """Read segments without altering them, honoring committed truncations.

    Records are varint lengths followed by protobuf samples. Empty records are
    checkpoints. START/END files identify committed truncations after a crash.
    """
    segments = legacy_segments(directory)
    if not segments:
        raise ValueError("No legacy WAL segments found in " + str(directory))
    starts = [i for i, p in enumerate(segments) if p.suffix == ".START"]
    ends = [i for i, p in enumerate(segments) if p.suffix == ".END"]
    if len(starts) > 1 or len(ends) > 1 or (starts and ends):
        raise ValueError("Inconsistent legacy WAL truncation markers")
    if starts:
        segments = segments[starts[0]:]
    elif ends:
        segments = segments[:ends[0] + 1]
        if len(segments) > 1 and segments[-2].name[:20] == segments[-1].name[:20]:
            del segments[-2]
    expected = None
    for segment in segments:
        index = int(segment.name[:20])
        if expected is not None and index != expected:
            raise ValueError("Non-contiguous legacy WAL at " + str(segment))
        data = segment.read_bytes()
        offset = 0
        while offset < len(data):
            size, offset = read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ValueError("Truncated legacy WAL at " + str(segment))
            if size:
                yield decode_sample(data[offset:end])
            offset = end
            index += 1
        expected = index


def migrate_metrics(path):
    """Import this directory once, preserving earlier automatic-import markers."""
    if not path:
        raise ValueError("metrics log path cannot be empty")
    directory = Path(path)
    if not directory.is_dir():
        raise ValueError("Metrics directory does not exist: " + str(directory))
    with MetricsLog(str(directory), allow_legacy=True) as queue:
        return queue.import_samples_once("legacy_imported", legacy_samples(directory))
