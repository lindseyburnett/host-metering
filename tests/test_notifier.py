from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import ssl
import select
import socket
from socketserver import ThreadingMixIn
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from host_metering import __version__
from host_metering.config import Config
from host_metering.hostinfo import HostInfo
from host_metering.notifier import NotifyError, PrometheusNotifier, proxy_for_url, should_notify
from host_metering.wire import Sample


class Receiver:
    def __init__(self, statuses, context=None):
        self.statuses = list(statuses)
        self.requests = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                receiver.requests.append((self.path, self.headers,
                                          self.rfile.read(int(self.headers["Content-Length"])),
                                          self.connection.getpeercert() if context else None))
                status = receiver.statuses.pop(0) if receiver.statuses else 204
                self.send_response(status)
                self.send_header("Content-Length", "0")
                if 300 <= status < 400:
                    self.send_header("Location", "/redirected")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        if context:
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.url = "{}://localhost:{}/api/v1/write".format(
            "https" if context else "http", self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


class ConnectProxy:
    def __init__(self, context=None):
        proxy = self
        self.connections = []

        class Handler(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                proxy.connections.append(self.path)
                host, port = self.path.rsplit(":", 1)
                with socket.create_connection((host, int(port)), timeout=2) as upstream:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.flush()
                    self.connection.settimeout(2)
                    try:
                        while True:
                            ready, _, _ = select.select([self.connection, upstream], [], [], 2)
                            if not ready:
                                break
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    return
                                target = upstream if source is self.connection else self.connection
                                target.sendall(data)
                    except OSError:
                        pass

            def log_message(self, *args):
                pass

        class Server(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        if context:
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.url = "{}://localhost:{}".format("https" if context else "http", self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


class CertificateTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.cert = str(Path(cls.temp.name) / "cert.pem")
        cls.key = str(Path(cls.temp.name) / "key.pem")
        config = Path(cls.temp.name) / "openssl.cnf"
        config.write_text("""[req]
distinguished_name=dn
prompt=no
x509_extensions=extensions
[dn]
CN=localhost
[extensions]
basicConstraints=critical,CA:TRUE
keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=DNS:localhost,IP:127.0.0.1
""")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", cls.key, "-out", cls.cert, "-config", str(config)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.env = patch.dict(os.environ, {"SSL_CERT_FILE": self.cert, "NO_PROXY": "*"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.host = HostInfo(host_id="host-id", external_organization="org", host_name="host")
        self.samples = [Sample(8, 1700000000000)]

    def notifier(self, statuses, tls=False, **overrides):
        context = None
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.cert, self.key)
            context.load_verify_locations(self.cert)
            context.verify_mode = ssl.CERT_REQUIRED
        receiver = Receiver(statuses, context)
        self.addCleanup(receiver.close)
        cfg = Config(write_url=receiver.url, host_cert_path=self.cert, host_cert_key_path=self.key,
                     write_retry_attempts=3, write_retry_min_int_sec=0.001,
                     write_retry_max_int_sec=0.004, write_timeout_sec=1)
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return PrometheusNotifier(cfg), receiver


class NotifierTests(CertificateTestCase):
    def test_mutual_tls_headers_and_certificate_reload(self):
        notifier, receiver = self.notifier([204, 204, 204], tls=True)
        notifier.notify(self.samples, self.host)
        path, headers, body, peer_cert = receiver.requests[0]
        self.assertEqual(path, "/api/v1/write")
        self.assertEqual(headers["Content-Encoding"], "snappy")
        self.assertEqual(headers["Content-Type"], "application/x-protobuf")
        self.assertEqual(headers["X-Prometheus-Remote-Write-Version"], "0.1.0")
        self.assertEqual(headers["User-Agent"], "host-metering/" + __version__)
        self.assertTrue(peer_cert)
        self.assertIn(b"system_cpu_logical_count", body)
        client = notifier._client
        notifier.notify(self.samples, self.host)
        self.assertIs(notifier._client, client)
        notifier.host_changed()
        notifier.notify(self.samples, self.host)
        self.assertIsNot(notifier._client, client)

    def test_untrusted_server_is_rejected(self):
        notifier, receiver = self.notifier([], tls=True)
        with patch.dict(os.environ, {"SSL_CERT_FILE": "/nonexistent"}):
            with self.assertRaises(NotifyError) as caught:
                notifier.notify(self.samples, self.host)
        self.assertTrue(caught.exception.recoverable)
        self.assertEqual(receiver.requests, [])

    def test_missing_client_certificate_is_recoverable(self):
        notifier, receiver = self.notifier([], host_cert_path="/nonexistent")
        with self.assertRaises(NotifyError) as caught:
            notifier.notify(self.samples, self.host)
        self.assertTrue(caught.exception.recoverable)
        self.assertEqual(receiver.requests, [])

    def test_retries_replay_body_and_apply_capped_backoff(self):
        notifier, receiver = self.notifier([500, 429, 204])
        with patch.object(notifier.stop_event, "wait", return_value=False) as wait:
            notifier.notify(self.samples, self.host)
        self.assertEqual([call[0][0] for call in wait.call_args_list], [0.002, 0.004])
        self.assertEqual(len(receiver.requests), 3)
        self.assertEqual(len({request[2] for request in receiver.requests}), 1)
        self.assertTrue(receiver.requests[0][2])

    def test_retry_exhaustion_is_recoverable(self):
        notifier, receiver = self.notifier([500, 503, 429])
        with self.assertRaises(NotifyError) as caught:
            notifier.notify(self.samples, self.host)
        self.assertTrue(caught.exception.recoverable)
        self.assertEqual(len(receiver.requests), 3)

    def test_permanent_errors_and_redirects_are_not_retried(self):
        for status in (400, 401, 403, 404, 302, 307):
            with self.subTest(status=status):
                notifier, receiver = self.notifier([status])
                with self.assertRaises(NotifyError) as caught:
                    notifier.notify(self.samples, self.host)
                self.assertFalse(caught.exception.recoverable)
                self.assertEqual(len(receiver.requests), 1)

    def test_network_errors_keep_samples_recoverable(self):
        notifier = PrometheusNotifier(Config())
        notifier._client = Mock()
        notifier._client.open.side_effect = URLError("unavailable")
        with self.assertRaises(NotifyError) as caught:
            notifier.notify(self.samples, self.host)
        self.assertTrue(caught.exception.recoverable)
        self.assertEqual(notifier._client.open.call_count, 1)

    def test_stop_cancels_backoff(self):
        notifier, receiver = self.notifier([500, 204])
        with patch.object(notifier.stop_event, "wait", return_value=True):
            with self.assertRaises(NotifyError):
                notifier.notify(self.samples, self.host)
        self.assertEqual(len(receiver.requests), 1)

    def test_http_proxy_receives_absolute_target_url(self):
        receiver = Receiver([204])
        self.addCleanup(receiver.close)
        config = Config(write_url="http://target.example.test/api/v1/write", host_cert_path=self.cert,
                        host_cert_key_path=self.key, write_retry_attempts=1, write_timeout_sec=1)
        with patch.dict(os.environ, {"HTTP_PROXY": receiver.url, "NO_PROXY": "", "no_proxy": "",
                                     "http_proxy": receiver.url}):
            PrometheusNotifier(config).notify(self.samples, self.host)
        self.assertEqual(receiver.requests[0][0], config.write_url)

    def test_https_receiver_through_http_connect_proxy(self):
        notifier, receiver = self.notifier([204], tls=True)
        proxy = ConnectProxy()
        self.addCleanup(proxy.close)
        with patch("host_metering.notifier.proxy_for_url", return_value=proxy.url):
            notifier.notify(self.samples, self.host)
        self.assertEqual(len(proxy.connections), 1)
        self.assertTrue(receiver.requests[0][3])

    def test_lowercase_no_proxy_cannot_override_uppercase(self):
        receiver = Receiver([204])
        self.addCleanup(receiver.close)
        config = Config(write_url="http://target.example.test/api/v1/write", host_cert_path=self.cert,
                        host_cert_key_path=self.key, write_retry_attempts=1, write_timeout_sec=1)
        with patch.dict(os.environ, {"HTTP_PROXY": receiver.url, "NO_PROXY": "unrelated.test",
                                     "no_proxy": "*"}):
            PrometheusNotifier(config).notify(self.samples, self.host)
        self.assertEqual(len(receiver.requests), 1)


def supports_tls_proxy():
    try:
        import urllib3
    except ImportError:
        return False
    return tuple(int(part) for part in urllib3.__version__.split(".")[:2]) >= (1, 26)


@unittest.skipUnless(supports_tls_proxy(), "optional HTTPS proxy extra not installed")
class TLSProxyTests(CertificateTestCase):
    def test_https_receiver_through_https_proxy(self):
        notifier, receiver = self.notifier([204], tls=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        proxy = ConnectProxy(context)
        self.addCleanup(proxy.close)
        with patch("host_metering.notifier.proxy_for_url", return_value=proxy.url):
            notifier.notify(self.samples, self.host)
        self.assertEqual(len(proxy.connections), 1)
        self.assertTrue(receiver.requests[0][3])
        notifier.host_changed()


class ProxyPolicyTests(unittest.TestCase):
    def test_environment_and_bypass_rules(self):
        cases = [("https://service.example.test/write", "", "https://proxy:3128"),
                 ("http://service.example.test/write", "", "http://proxy:3128"),
                 ("http://localhost/write", "", None), ("http://127.0.0.2/write", "", None),
                 ("https://[::1]/write", "", None),
                 ("http://10.2.3.4/write", "10.0.0.0/8", None),
                 ("http://[2001:db8::1]/write", "2001:db8::/32", None),
                 ("http://[2001:db8::1]/write", "[2001:db8::1]:80", None),
                 ("http://a.example.test/write", ".example.test", None),
                 ("http://example.test/write", ".example.test", "http://proxy:3128"),
                 ("http://a.example.test:81/write", "example.test:81", None),
                 ("http://example.test:80/write", "example.test:81", "http://proxy:3128"),
                 ("https://anything.test/write", "*", None)]
        for url, bypass, expected in cases:
            with self.subTest(url=url, bypass=bypass):
                env = {"HTTP_PROXY": "http://proxy:3128", "HTTPS_PROXY": "https://proxy:3128",
                       "NO_PROXY": bypass}
                self.assertEqual(proxy_for_url(url, env), expected)

    def test_notification_policy(self):
        samples = [Sample(1, 1)]
        self.assertIsNotNone(should_notify([], HostInfo()))
        self.assertIsNotNone(should_notify(samples, None))
        self.assertIsNotNone(should_notify(samples, HostInfo()))
        self.assertIsNotNone(should_notify(samples, HostInfo(host_id="id")))
        self.assertIsNone(should_notify(samples, HostInfo(host_id="id", external_organization="org")))
