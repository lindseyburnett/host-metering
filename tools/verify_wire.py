"""Verify our wire encoder with independent protobuf and Snappy libraries.

Development only: python -m pip install protobuf python-snappy
Run from the repository root: python -m tools.verify_wire
"""

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
import snappy

from host_metering.hostinfo import BillingInfo, HostInfo
from host_metering.wire import Sample, encode_write_request, snappy_block


def write_request_type():
    schema = descriptor_pb2.FileDescriptorProto(name="remote.proto", package="prometheus", syntax="proto3")
    definitions = {
        "Label": [("name", 1, 9, None, False), ("value", 2, 9, None, False)],
        "Sample": [("value", 1, 1, None, False), ("timestamp", 2, 3, None, False)],
        "TimeSeries": [("labels", 1, 11, "Label", True), ("samples", 2, 11, "Sample", True)],
        "WriteRequest": [("timeseries", 1, 11, "TimeSeries", True)],
    }
    for name, fields in definitions.items():
        message = schema.message_type.add(name=name)
        for field_name, number, kind, referenced, repeated in fields:
            field = message.field.add(name=field_name, number=number, type=kind,
                                      label=3 if repeated else 1)
            if referenced:
                field.type_name = ".prometheus." + referenced
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    descriptor = pool.FindMessageTypeByName("prometheus.WriteRequest")
    if hasattr(message_factory, "GetMessageClass"):
        return message_factory.GetMessageClass(descriptor)
    return message_factory.MessageFactory(pool).GetPrototype(descriptor)


def main():
    request_type = write_request_type()
    host = HostInfo(host_id="id", host_name="höst.example.test", external_organization="org",
                    product=["394", "69"], billing=BillingInfo("marketplace", "aws", "123", "i-1"))
    for count in (1, 60, 256, 5000):
        samples = [Sample(8, 1700000000000 + i * 1000) for i in range(count)]
        payload = snappy_block(encode_write_request(host.labels(), samples))
        decoded = request_type.FromString(snappy.decompress(payload))
        assert len(decoded.timeseries) == 1
        series = decoded.timeseries[0]
        assert [(label.name, label.value) for label in series.labels] == host.labels()
        assert [(sample.value, sample.timestamp) for sample in series.samples] == samples
    for size in (0, 1, 60, 61, 256, 257, 65536, 65537):
        data = b"x" * size
        assert snappy.decompress(snappy_block(data)) == data
    print("Independent protobuf and Snappy verification passed")


if __name__ == "__main__":
    main()
