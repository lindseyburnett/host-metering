ARG UBI_VERSION=9

FROM registry.access.redhat.com/ubi${UBI_VERSION}/ubi:latest

RUN if command -v dnf >/dev/null 2>&1; then \
      dnf install -y python3 make openssl git && dnf clean all; \
    else \
      yum install -y python3 make openssl git && yum clean all; \
    fi

WORKDIR /workspace/host-metering

ENV IS_IN_CONTAINER=true
