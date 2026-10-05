"""Prometheus remote write with host mTLS, environment proxies and retries."""

import ipaddress
import base64
from contextlib import contextmanager
from http.client import HTTPException
import logging
import os
import ssl
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import __version__
from .wire import encode_write_request, snappy_block

LOG = logging.getLogger("host_metering")


class NotifyError(Exception):
    def __init__(self, message, recoverable=True):
        super().__init__(message)
        self.recoverable = recoverable


def should_notify(samples, host):
    if not samples:
        return "no samples to send"
    if host is None:
        return "missing internal HostInfo"
    if not host.host_id:
        return "missing HostId"
    if not host.external_organization:
        return "missing ExternalOrganization"
    return None


def proxy_for_url(url, environ=None):
    """Keep Go's NO_PROXY CIDR, port and subdomain matching behavior."""
    environ = os.environ if environ is None else environ
    target = urlsplit(url)
    host = (target.hostname or "").lower().rstrip(".")
    port = target.port or (443 if target.scheme == "https" else 80)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if host == "localhost" or (address is not None and address.is_loopback):
        return None
    no_proxy = environ.get("NO_PROXY") or environ.get("no_proxy", "")
    for raw_entry in no_proxy.split(","):
        entry = raw_entry.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return None
        if "/" in entry:
            try:
                if address is not None and address in ipaddress.ip_network(entry, strict=False):
                    return None
            except ValueError:
                pass
            continue
        try:
            # Bare IPv6 addresses do not carry a port.
            entry_address = ipaddress.ip_address(entry)
            if address == entry_address:
                return None
            continue
        except ValueError:
            pass
        try:
            parsed = urlsplit("//" + entry)
            entry_host = (parsed.hostname or "").rstrip(".")
            if parsed.port is not None and parsed.port != port:
                continue
        except ValueError:
            continue
        if entry_host.startswith("*."):
            entry_host = entry_host[1:]
        if not entry_host:
            continue
        if entry_host.startswith("."):
            if host.endswith(entry_host):
                return None
        elif host == entry_host or host.endswith("." + entry_host):
            return None
    key = target.scheme.upper() + "_PROXY"
    proxy = environ.get(key) or environ.get(key.lower())
    if proxy and "://" not in proxy:
        proxy = "http://" + proxy
    return proxy


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A 302 followed as GET can look like a successful write and lose data.
        # Remote write must finish at the configured POST endpoint.
        return None


class ExplicitProxyHandler(ProxyHandler):
    """The proxy was already selected using our NO_PROXY rules.

    urllib's second environment lookup would otherwise let lowercase variables
    or platform proxy settings override that decision.
    """

    def proxy_open(self, request, proxy, scheme):
        parsed = urlsplit(proxy)
        if parsed.scheme != "http":
            raise ValueError("An HTTP proxy URL is required by this transport")
        if parsed.username is not None:
            credentials = "{}:{}".format(unquote(parsed.username), unquote(parsed.password or ""))
            request.add_unredirected_header(
                "Proxy-Authorization", "Basic " + base64.b64encode(credentials.encode()).decode("ascii"))
        request.set_proxy(parsed.netloc.rsplit("@", 1)[-1], parsed.scheme)
        return None


def tls_context():
    return ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None,
                                      capath=os.environ.get("SSL_CERT_DIR") or None)


class TLSProxyClient:
    """TLS to both the proxy and receiver needs urllib3's TLS-in-TLS support."""

    def __init__(self, proxy, context):
        try:
            import urllib3
        except ImportError as exc:
            raise ValueError("HTTPS proxy URLs require the host-metering[https-proxy] extra") from exc
        if tuple(int(part) for part in urllib3.__version__.split(".")[:2]) < (1, 26):
            raise ValueError("HTTPS proxy URLs require urllib3 >= 1.26")
        self.urllib3 = urllib3
        parsed = urlsplit(proxy)
        proxy_headers = {}
        if parsed.username is not None:
            credentials = "{}:{}".format(unquote(parsed.username), unquote(parsed.password or ""))
            proxy_headers = urllib3.make_headers(proxy_basic_auth=credentials)
        self.pool = urllib3.ProxyManager(proxy, ssl_context=context, proxy_ssl_context=tls_context(),
                                         proxy_headers=proxy_headers)

    @contextmanager
    def open(self, request, timeout):
        try:
            response = self.pool.request(
                "POST", request.full_url, body=request.data, headers=dict(request.header_items()),
                timeout=self.urllib3.Timeout(total=timeout), retries=False, redirect=False,
                preload_content=False,
            )
        except self.urllib3.exceptions.HTTPError as exc:
            raise URLError(str(exc)) from exc
        try:
            yield response
        finally:
            response.close()
            response.release_conn()

    def close(self):
        self.pool.clear()


class PrometheusNotifier:
    def __init__(self, config, stop_event=None):
        self.config = config
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self._client = None

    def host_changed(self):
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()
        self._client = None

    def close(self):
        self.host_changed()

    def _create_client(self):
        context = tls_context()
        context.load_cert_chain(self.config.host_cert_path, self.config.host_cert_key_path)
        proxy = proxy_for_url(self.config.write_url)
        if proxy and urlsplit(proxy).scheme == "https":
            return TLSProxyClient(proxy, context)
        proxies = {urlsplit(self.config.write_url).scheme: proxy} if proxy else {}
        return build_opener(ExplicitProxyHandler(proxies), HTTPSHandler(context=context), NoRedirect())

    def notify(self, samples, host):
        cfg = self.config
        try:
            if self._client is None:
                self._client = self._create_client()
            payload = snappy_block(encode_write_request(
                host.labels(cfg.send_hostname != "no"), samples,
            ))
        except (OSError, ValueError) as exc:
            raise NotifyError(str(exc)) from exc
        headers = {
            "Content-Encoding": "snappy",
            "Content-Type": "application/x-protobuf",
            "X-Prometheus-Remote-Write-Version": "0.1.0",
            "User-Agent": "host-metering/" + __version__,
        }
        retry_wait = cfg.write_retry_min_int_sec
        for attempt in range(cfg.write_retry_attempts):
            if self.stop_event.is_set():
                raise NotifyError("Notification cancelled")
            # Construct a fresh request for every retry so its body is replayed.
            request = Request(cfg.write_url, data=payload, headers=headers, method="POST")
            try:
                with self._client.open(request, timeout=cfg.write_timeout_sec) as response:
                    status = response.status
            except HTTPError as exc:
                status = exc.code
                exc.close()
            except (OSError, URLError, HTTPException, ValueError) as exc:
                raise NotifyError(str(exc)) from exc
            if 200 <= status < 300:
                return
            if status != 429 and not 500 <= status < 600:
                raise NotifyError("HTTP error: {}".format(status), recoverable=False)
            if attempt + 1 < cfg.write_retry_attempts:
                retry_wait = min(retry_wait * 2, cfg.write_retry_max_int_sec)
                LOG.info("HTTP error: %d; retrying in %s seconds", status, retry_wait)
                if self.stop_event.wait(retry_wait):
                    raise NotifyError("Notification cancelled")
        raise NotifyError("Failed after {} attempts".format(cfg.write_retry_attempts))
