# Host Metering

Report a Linux host's logical CPU count, subscription identity, and cloud billing
labels to a Prometheus remote write endpoint.

## Requirements

- Python 3.6 or later, with the standard `ssl` and `sqlite3` modules.
- Linux with `/proc/cpuinfo` and `subscription-manager`.
- A registered host's consumer certificate and private key for mutual TLS.

The normal runtime uses only the standard library. The minimum version keeps
compatibility with the Python 3 packages in the existing RHEL 7/8 environments.

## Run and install

Run directly from the checkout:

```sh
python3 -m host_metering --config /etc/host-metering.conf once
python3 -m host_metering --config /etc/host-metering.conf daemon
```

Or install into a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
host-metering daemon
```

The commands are `daemon`, `once`, `migrate-wal`, and `help`. `--version` prints
the release version. Both `--config` and `-config` are accepted.

Build a standalone executable zip archive using only Python's standard library:

```sh
make build
python3 dist/host-metering help
python3 dist/host-metering daemon
```

The archive contains the application and needs Python on the target machine.
Its executable shebang is `/usr/bin/python3`, as used by the RPM installation.

## Configuration

Settings keep their existing names and defaults. Values in the `[host-metering]`
section override defaults; nonempty `HOST_METERING_*` environment variables
override file values. See [host-metering.conf(5)](contrib/man/host-metering.conf.5)
and [host-metering(1)](contrib/man/host-metering.1) for the complete list.

```ini
[host-metering]
write_url=https://receiver.example.com/api/v1/write
write_interval_sec=600
host_cert_path=/etc/pki/consumer/cert.pem
host_cert_key_path=/etc/pki/consumer/key.pem
metrics_wal_path=/var/run/host-metering/metrics
send_hostname=yes
log_level=INFO
```

For example, `HOST_METERING_WRITE_URL` overrides `write_url`. Numeric settings are
unsigned integer seconds, except `write_retry_attempts`, which is a count of
total attempts. Invalid numeric overrides are logged and leave the previous
value in place. Missing configuration files are logged and defaults remain usable.

`collect_interval_sec=0` collects at each write. `label_refresh_interval_sec=0`
disables periodic label refresh. `metrics_max_age_sec=0` disables expiration.
The write interval must exceed the configured maximum retry duration.

`HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` are supported, including CIDR,
subdomain, and port entries. The systemd service starts Python directly with
`--rhsm-config /etc/rhsm/rhsm.conf` to fill missing proxy values from the `[server]`
section's `proxy_hostname` and `proxy_port`. Each nonempty HTTP/HTTPS environment
setting, including lowercase variants, takes precedence independently.
Server certificates are verified using the system trust
store; `SSL_CERT_FILE` and `SSL_CERT_DIR` can select an additional deployment's
trust configuration through Python/OpenSSL.

HTTP proxies, including CONNECT tunnels to HTTPS receivers, work with the
standard library. If the proxy itself uses TLS (an `https://` proxy URL), install
the optional transport with `python -m pip install '.[https-proxy]'` from the
checkout, or install `urllib3>=1.26` alongside the executable archive. This keeps
both TLS connections verified and avoids silently downgrading proxy encryption.

## Daemon behavior

The service collects and sends a sample immediately, then follows the configured
intervals. The metric remains `system_cpu_logical_count`; label names, product
ordering, optional hostname suppression, and AWS/Azure/GCP billing fields are
preserved. Labels are sorted and empty values are omitted.

Samples are committed to `samples.sqlite3` in `metrics_wal_path` before sending.
Success and permanent HTTP errors remove only the batch that was sent. Network
errors, HTTP 429, and HTTP 5xx keep samples for later delivery. HTTP 429 and 5xx
also retry with capped exponential backoff. Old samples expire even while a host
is unregistered. Only one Python process can own a queue directory.

SIGHUP refreshes host information and credentials. Certificate and key changes,
including removal and atomic replacement, are checked once per second. SIGINT
and SIGTERM stop the daemon; retry waits are interruptible. Subscription-manager
commands have a 30 second timeout.

Remote writes use the existing protobuf schema and Snappy block format. The
small encoder uses literal Snappy blocks, which are compatible with receivers
but trade compression savings for zero native dependencies. See the
[Prometheus wire specification](https://prometheus.io/docs/specs/prw/remote_write_spec/)
and [Snappy format](https://github.com/google/snappy/blob/main/format_description.txt).

## Upgrade from Go

1. Stop the old service and back up its configuration and metrics directory.
2. Install the Python build with the same configuration and metrics directory.
3. Run the migration command with the service's configuration:

   ```sh
   host-metering --config /etc/host-metering.conf migrate-wal
   ```

4. Start the service after migration completes.

The command imports tidwall/wal binary segments into SQLite in one transaction,
including committed START/END truncations. It does not contact the receiver or
require subscription-manager or TLS credentials. It uses `metrics_wal_path`,
including a `HOST_METERING_METRICS_WAL_PATH` override if set.

Original segments are retained, and a database marker prevents reimport on later
command invocations. Existing markers from the earlier automatic importer are
honored. The daemon does not load the legacy decoder or automatically migrate
files; if it detects an unmigrated queue, it stops with migration instructions.
A corrupt queue rolls back the import so the files can be repaired and the
command retried. Fresh installations need no migration. Stop all metering
processes before importing, and do not run the Go and Python services together.
The new SQLite queue is not readable by Go; a rollback requires an explicit queue
migration or draining the Python queue first, and must avoid replaying the
retained legacy segments.

Some behavior is deliberately tightened: zero attempts/timeouts, invalid URLs,
invalid log levels and invalid `send_hostname` values fail validation; `once`
returns a nonzero exit status on a send failure. Redirect responses are rejected,
so `write_url` must name the final POST endpoint. File-based logging keeps the
timestamp and optional instance ID format.

## Tests and development

```sh
make test       # requires openssl for generated TLS test certificates
make test-ff    # stop at the first failure
```

The suite covers configuration, subscription parsing, all cloud mappings, queue
persistence and Go migration, wire fixtures, real HTTP/mutual TLS, proxy use,
retry body replay, daemon scheduling, and real Unix signals. Tests use temporary
directories, mock subscription-manager output, and local HTTP servers.

CI also decodes our payloads with independent protobuf and Snappy libraries:

```sh
python -m pip install protobuf python-snappy
python -m tools.verify_wire
```

For a Linux development session with a local Prometheus receiver:

```sh
make test-daemon
```

This generates test credentials, uses `mocks/subscription-manager`, and starts
Prometheus through Podman Compose. The UBI dev container defaults to version 9;
`make podman-containers UBI_VERSION=8` builds the older environment.
Run `.devcontainer/commands/prepare_containers.sh` before opening the VS Code
dev container. The editor configuration now uses the Python debugger.

`mocks/mocked_run.sh --config .devcontainer/host-metering.conf once` is a shortcut
for running the Python CLI with mocked subscription data. Run `make cert` first.
To capture actual host subscription output, run `mocks/mock_from_host.sh` on a
registered development host.

## RPMs and releases

```sh
make tarball
make rpm
make mock      # EPEL 7
make mock-8
make mock-9
```

The RPM installs the executable Python archive, systemd service, man pages, and
SELinux policy. Proxy setup runs inside Python. It requires Python 3 and
subscription-manager. The service commands are unchanged:

```sh
systemctl enable --now host-metering
journalctl -feu host-metering
```

Custom unit overrides that invoke `host-metering-proxy-setup.sh` should use
`/usr/bin/host-metering --rhsm-config /etc/rhsm/rhsm.conf daemon` instead.

`make version-update NEXT_VERSION=x.y.z` updates `host_metering/__init__.py`.
Semantic release uses that version for source archives and RPMs. See
[CONTRIBUTING.md](CONTRIBUTING.md) for contribution and release conventions.
