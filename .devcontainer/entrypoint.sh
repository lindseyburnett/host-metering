#!/bin/bash
cd "${WORKDIR:-/workspace/host-metering}" || exit 1
exec "$@"
