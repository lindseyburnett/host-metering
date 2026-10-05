"""The small subset of protobuf used by Prometheus remote write 1.0.

Schema: https://prometheus.io/docs/specs/prw/remote_write_spec/
Snappy: https://github.com/google/snappy/blob/main/format_description.txt
Literal-only Snappy blocks avoid platform dependencies; these small batches do
not need a compression library. This is block format, never framed Snappy.
"""

import struct
from collections import namedtuple

Sample = namedtuple("Sample", "value timestamp")


def encode_varint(value):
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("varint outside uint64 range")
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


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


def _message(field, data):
    return encode_varint((field << 3) | 2) + encode_varint(len(data)) + data


def encode_sample(sample):
    return (b"\x09" + struct.pack("<d", sample.value) + b"\x10"
            + encode_varint(sample.timestamp & 0xFFFFFFFFFFFFFFFF))


def decode_sample(data):
    """Read old Go WAL samples, rejecting truncated/invalid protobuf fields."""
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


def encode_write_request(labels, samples):
    series = bytearray()
    for name, value in sorted(labels):
        label = _message(1, name.encode("utf-8")) + _message(2, value.encode("utf-8"))
        series.extend(_message(1, label))
    for sample in sorted(samples, key=lambda item: item.timestamp):
        series.extend(_message(2, encode_sample(sample)))
    return _message(1, bytes(series))


def snappy_block(data):
    size = len(data)
    if size > 0xFFFFFFFF:
        raise ValueError("Snappy block exceeds 4 GiB")
    prefix = encode_varint(size)
    if not size:
        return prefix
    if size <= 60:
        return prefix + bytes([(size - 1) << 2]) + data
    length_size = ((size - 1).bit_length() + 7) // 8
    return (prefix + bytes([(59 + length_size) << 2])
            + (size - 1).to_bytes(length_size, "little") + data)
