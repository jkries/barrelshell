import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time
import unittest
from unittest.mock import patch, Mock

from barrel_web import transport as http


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/trickle":
            try:
                for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok":
                    self.connection.sendall(bytes([byte]))
                    time.sleep(0.03)
            except OSError:
                pass
            return
        if self.path == "/private":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1/private")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/broken":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"short")
            return
        if self.path == "/gzip":
            body = gzip.compress(b"a" * 10000)
        elif self.path == "/big":
            body = b"a" * 10000
        else:
            body = b"public content"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        if self.path == "/close":
            self.send_header("Connection", "close")
        if self.path == "/gzip":
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, OSError):
            pass


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def fetch_fixture(self, path, **kwargs):
        original = http.resolve_public
        port = self.server.server_port

        def fixture_only(host, requested_port, deadline):
            if host == "fixture.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            return original(host, requested_port, deadline)

        # This explicit test-only exception is never present in production.
        with patch.object(http, "resolve_public", side_effect=fixture_only):
            return http.get(f"http://fixture.test:{port}{path}", **kwargs)

    def test_public_response_and_gzip(self):
        self.assertEqual(self.fetch_fixture("/").body, b"public content")
        self.assertEqual(self.fetch_fixture("/gzip").body, b"a" * 10000)
        self.assertEqual(self.fetch_fixture("/close").body, b"public content")

    def test_byte_caps_apply_to_wire_and_decompressed_content(self):
        for path in ("/big", "/gzip"):
            with self.subTest(path=path), self.assertRaises(http.FetchError):
                self.fetch_fixture(path, max_bytes=500)

    def test_truncated_response_is_not_reported_as_complete(self):
        with self.assertRaisesRegex(http.FetchError, "incomplete"):
            self.fetch_fixture("/broken")

    def test_shared_budget_accounts_for_failed_and_successful_reads(self):
        budget = http.ByteBudget(200)
        self.fetch_fixture("/", budget=budget)
        self.assertEqual(budget.left, 186)
        with self.assertRaisesRegex(http.FetchError, "incomplete"):
            self.fetch_fixture("/broken", budget=budget, max_bytes=1000)
        # Even bytes from a failed, truncated read count against the budget.
        self.assertEqual(budget.left, 181)
        self.fetch_fixture("/", budget=budget)
        self.assertEqual(budget.left, 167)
        budget.left = 12
        with self.assertRaises(http.FetchError):
            self.fetch_fixture("/", budget=budget)

    def test_redirect_private_and_loop_refused(self):
        with self.assertRaisesRegex(http.FetchError, "non-public"):
            self.fetch_fixture("/private")
        with self.assertRaisesRegex(http.FetchError, "too many redirects"):
            self.fetch_fixture("/loop", max_redirects=2)

    def test_invalid_urls_and_credentials(self):
        for url in ("file:///tmp/a", "http://user:password@example.com/", "http://[::1%25lo]/",
                    "http://example.com/\nheader", "http://example.com\\@127.0.0.1/", "http://example.com:99999/", "http://example.com:0/"):
            with self.subTest(url=url), self.assertRaises(http.FetchError):
                http.validate_url(url)

    def test_private_ipv4_ipv6_and_mixed_dns_refused(self):
        for addresses in (["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["::1"],
                          ["::ffff:127.0.0.1"], ["64:ff9b::7f00:1"], ["2002:7f00:1::"],
                          ["224.0.0.1"], ["93.184.216.34", "192.168.1.1"]):
            infos = [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))
                     for ip in addresses]
            with self.subTest(addresses=addresses), patch.object(socket, "getaddrinfo", return_value=infos):
                with self.assertRaisesRegex(http.FetchError, "non-public"):
                    http.resolve_public("example.org", 80, time.monotonic() + 5)

    def test_connection_uses_validated_numeric_address_without_resolving_again(self):
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        sock = Mock()
        with patch.object(socket, "socket", return_value=sock), patch.object(socket, "getaddrinfo") as dns:
            conn = http._connection("http", "example.org", 80, infos, time.monotonic() + 5)
            conn.connect()
            sock.connect.assert_called_once_with(("93.184.216.34", 80))
            dns.assert_not_called()
            conn.close()

    def test_https_preserves_hostname_verification_and_sni(self):
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        sock, context = Mock(), Mock()
        with patch.object(socket, "socket", return_value=sock), patch.object(http.ssl, "create_default_context", return_value=context):
            conn = http._connection("https", "example.org", 443, infos, time.monotonic() + 5)
            conn.connect()
            context.wrap_socket.assert_called_once_with(sock, server_hostname="example.org")
            conn.close()

    def test_trickled_headers_cannot_extend_overall_deadline(self):
        start = time.monotonic()
        with self.assertRaises(http.FetchError):
            self.fetch_fixture("/trickle", timeout=0.15)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_unicode_urls_are_encoded_without_changing_host(self):
        url, host, _ = http.validate_url("https://example.org/caf\u00e9?q=\u4e2d\u6587")
        self.assertEqual(host, "example.org")
        self.assertIn("caf%C3%A9", url)
        self.assertIn("%E4%B8%AD", url)

    def test_expired_deadline_and_mutating_requests(self):
        with self.assertRaises(http.FetchError):
            http.get("https://example.org", deadline=time.monotonic() - 1)
        with self.assertRaises(http.FetchError):
            http.get("https://example.org", method="POST")


if __name__ == "__main__":
    unittest.main()
