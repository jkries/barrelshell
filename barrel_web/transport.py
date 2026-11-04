"""Bounded, credential-free HTTP reads with DNS-pinned connections.

No environment proxies, cookie jar, automatic redirects, or second DNS lookup.
HTTPS still verifies the original hostname and uses it for SNI. All addresses
returned for a hostname must be public. Each redirect repeats this policy.
"""
from dataclasses import dataclass
import http.client
import ipaddress
import queue
import socket
import ssl
import threading
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
import zlib

import certifi


class FetchError(ValueError):
    """A safe, user-facing retrieval failure."""


@dataclass
class ByteBudget:
    """Shared body budget; callers serialize reads when sharing this object."""
    left: int

    def consume(self, amount: int):
        self.left -= amount
        if self.left < 0:
            self.left = 0
            raise FetchError("aggregate response byte limit exceeded")


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


_DNS_SLOTS = threading.BoundedSemaphore(4)


def remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise FetchError("request deadline exceeded")
    return left


def validate_url(url: str):
    if len(url) > 8192 or any(ord(c) < 33 for c in url) or "\\" in url:
        raise FetchError("invalid URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host:
            raise FetchError("only absolute http/https URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise FetchError("URLs containing credentials are not allowed")
        if "%" in host:
            raise FetchError("scoped/encoded hostnames are not allowed")
        host = host.encode("idna").decode("ascii")
        port = port if port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise FetchError("invalid port")
    except (ValueError, UnicodeError) as exc:
        raise FetchError(f"invalid URL: {exc}") from exc
    netloc = f"[{host}]" if ":" in host else host
    if port != (443 if parsed.scheme == "https" else 80):
        netloc += f":{port}"
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
    clean = urlunsplit((parsed.scheme, netloc, path, query, ""))
    return clean, host, port


def resolve_public(host: str, port: int, deadline: float) -> list:
    """Bound resolver wait and concurrency, including stuck OS DNS calls."""
    if not _DNS_SLOTS.acquire(timeout=remaining(deadline)):
        raise FetchError("DNS resolver busy")
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception as exc:
            result.put(exc)
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=resolve, daemon=True).start()
    try:
        infos = result.get(timeout=remaining(deadline))
    except queue.Empty as exc:
        raise FetchError("DNS lookup timed out") from exc
    if isinstance(infos, Exception):
        raise FetchError("host is unresolvable") from infos
    if not infos:
        raise FetchError("host is unresolvable")
    for family, _, _, _, address in infos:
        ip = ipaddress.ip_address(address[0])
        # IPv4-mapped IPv6 must also pass the IPv4 policy.
        mapped = getattr(ip, "ipv4_mapped", None)
        if (family not in (socket.AF_INET, socket.AF_INET6)
                or not ip.is_global or ip.is_multicast
                or (mapped and (not mapped.is_global or mapped.is_multicast))
                or (ip.version == 6 and (ip.sixtofour is not None or ip.teredo is not None
                                        or ip in ipaddress.ip_network("64:ff9b::/96")))):
            raise FetchError("host resolves to a non-public address")
    return infos


def _connection(scheme: str, host: str, port: int, infos: list,
                deadline: float):
    def pinned_connect(_address, timeout=None, source_address=None):
        last = None
        for family, socktype, proto, _, address in infos:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(min(10, remaining(deadline)))
                sock.connect(address)  # numeric sockaddr; no second resolution
                return sock
            except OSError as exc:
                last = exc
                sock.close()
            except FetchError:
                sock.close()
                raise
        raise FetchError(f"connection failed: {last}")

    if scheme == "https":
        context = ssl.create_default_context()
        # Python's OpenSSL CA file may be absent on Windows. Add Mozilla roots
        # without dropping OS/SSL_CERT_FILE roots or disabling verification.
        context.load_verify_locations(cafile=certifi.where())
        conn = http.client.HTTPSConnection(host, port, timeout=remaining(deadline),
                                           context=context)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=remaining(deadline))
    conn._create_connection = pinned_connect
    return conn


def _read_body(response, sock, deadline: float, max_bytes: int,
               budget: ByteBudget | None = None) -> bytes:
    length = response.getheader("Content-Length")
    if length:
        try:
            if int(length) < 0 or int(length) > max_bytes:
                raise FetchError("response exceeds byte limit")
        except ValueError as exc:
            raise FetchError("invalid or excessive Content-Length") from exc
    encoding = response.getheader("Content-Encoding", "identity").lower().strip()
    if encoding not in ("identity", "gzip", ""):
        raise FetchError(f"unsupported content encoding: {encoding}")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    chunks, size, wire_size, charged = [], 0, 0, 0
    while True:
        if response.isclosed():
            break
        sock.settimeout(min(10, remaining(deadline)))
        chunk = response.read1(min(65536, max_bytes - wire_size + 1))
        if not chunk:
            break
        wire_size += len(chunk)
        wire_charge = max(wire_size, size)
        if budget:
            budget.consume(wire_charge - charged)
        charged = wire_charge
        if wire_size > max_bytes:
            raise FetchError("response exceeds byte limit")
        if decoder:
            chunk = decoder.decompress(chunk, max_bytes - size + 1)
        size += len(chunk)
        charge = max(wire_size, size)
        if budget:
            budget.consume(charge - charged)
        charged = charge
        if size > max_bytes or (decoder and decoder.unconsumed_tail):
            raise FetchError("decompressed response exceeds byte limit")
        chunks.append(chunk)
    if response.length is not None and response.length > 0:
        raise FetchError("incomplete response body")
    if decoder and (not decoder.eof or decoder.unused_data):
        raise FetchError("incomplete or concatenated gzip response")
    return b"".join(chunks)


def get(url: str, *, max_bytes: int = 20_000_000, timeout: float = 30,
        max_redirects: int = 5, deadline: float | None = None,
        method: str = "GET", headers: dict | None = None,
        budget: ByteBudget | None = None) -> Response:
    """Read one public resource; limits cover redirects and decompression.

    The absolute deadline also allows browser subrequests to share a budget.
    Only GET/HEAD are supported. Caller headers are deliberately whitelisted.
    """
    if method not in ("GET", "HEAD"):
        raise FetchError("only GET/HEAD requests are allowed")
    if max_bytes < 1 or timeout <= 0:
        raise FetchError("invalid request limits")
    if budget:
        max_bytes = min(max_bytes, budget.left)
        if max_bytes < 1:
            raise FetchError("aggregate response byte limit exceeded")
    deadline = min(deadline or float("inf"), time.monotonic() + timeout)
    for hop in range(max_redirects + 1):
        url, host, port = validate_url(url)
        parsed = urlsplit(url)
        infos = resolve_public(host, port, deadline)
        conn = _connection(parsed.scheme, host, port, infos, deadline)
        watchdog = None
        try:
            request_headers = {"User-Agent": "Mozilla/5.0 (BarrelShell document reader)",
                               "Accept-Encoding": "gzip", "Connection": "close"}
            for key, value in (headers or {}).items():
                if key.lower() in {"accept", "accept-language", "origin", "range"}:
                    request_headers[key] = value
            path = urlunsplit(("", "", parsed.path, parsed.query, ""))
            conn.connect()
            sock = conn.sock

            def expire_socket(target=sock):
                # A socket timeout alone is an inactivity limit. A peer that
                # trickles header bytes could otherwise evade the total budget.
                try:
                    target.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                target.close()

            watchdog = threading.Timer(remaining(deadline), expire_socket)
            watchdog.daemon = True
            watchdog.start()
            sock.settimeout(min(10, remaining(deadline)))
            conn.request(method, path, headers=request_headers)
            sock.settimeout(min(10, remaining(deadline)))
            response = conn.getresponse()
            try:
                response_headers = {k.lower(): v for k, v in response.getheaders()}
                if response.status in (301, 302, 303, 307, 308):
                    if hop == max_redirects:
                        raise FetchError("too many redirects")
                    location = response_headers.get("location")
                    if not location:
                        raise FetchError("redirect has no Location")
                    url = urljoin(url, location)
                    continue
                if response.status >= 400:
                    raise FetchError(f"HTTP {response.status} from {host}")
                body = b"" if method == "HEAD" else _read_body(response, sock, deadline, max_bytes, budget)
                remaining(deadline)
                return Response(url, response.status, response_headers, body)
            finally:
                response.close()
        except FetchError:
            raise
        except (OSError, http.client.HTTPException, zlib.error, ValueError) as exc:
            if time.monotonic() >= deadline:
                raise FetchError("request deadline exceeded") from exc
            raise FetchError(f"HTTP read failed: {type(exc).__name__}: {exc}") from exc
        finally:
            if watchdog:
                watchdog.cancel()
            conn.close()
    raise FetchError("too many redirects")
