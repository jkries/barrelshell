"""Optional read-only Chromium renderer.

All HTTP(S) requests are fulfilled via the DNS-pinned Python transport. The
browser is placed behind a dead proxy as a fail-closed backstop: requests that
escape interception cannot connect directly. No user profile, cookies, service
workers, downloads, real WebSockets, or mutating HTTP methods are permitted.
This is defense in depth, not a substitute for OS/container isolation.
"""
import asyncio
import time
from urllib.parse import urlsplit

from .transport import ByteBudget, FetchError, get, remaining, validate_url


async def _render(url: str, *, timeout: float, max_bytes: int,
                  max_requests: int) -> tuple[bytes, str, list[str]]:
    try:
        from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
    except ImportError as exc:
        raise FetchError("browser support missing; install requirements-browser.txt and run python -m playwright install chromium") from exc
    url, _, _ = validate_url(url)
    deadline = time.monotonic() + timeout
    count = blocked = 0
    budget = ByteBudget(max_bytes)
    fatal = None
    main_final = url
    navigation_seen = False
    warnings = []
    # Serialize transport calls so requests share a real aggregate byte budget.
    # Keep the event loop free for Playwright callbacks and timeout cancellation.
    network_lock = asyncio.Lock()

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                headless=True, timeout=int(remaining(deadline) * 1000),
                proxy={"server": "http://127.0.0.1:9", "bypass": "<-loopback>"},
                args=["--disable-quic", "--disable-background-networking",
                      "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
        except Exception as exc:
            raise FetchError("Chromium could not start; run python -m playwright install chromium. "
                             f"Details: {type(exc).__name__}") from exc
        try:
            context = await browser.new_context(service_workers="block", accept_downloads=False)
            # Remove APIs that can create traffic outside normal HTTP routing.
            # Apply in every new frame before site scripts execute.
            await context.add_init_script("""(() => {
              for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection',
                                  'WebTransport', 'Worker', 'SharedWorker']) {
                Object.defineProperty(globalThis, name, {
                  value: undefined, writable: false, configurable: false
                });
              }
            })();""")

            async def block_websocket(ws):
                nonlocal blocked
                blocked += 1
                # Routed sockets never reach the network unless connect_to_server
                # is called. Leave a disconnected mock instead of initiating a
                # close handshake that some Playwright versions can hang on.

            await context.route_web_socket("**/*", block_websocket)
            page = await context.new_page()

            async def handle(route):
                nonlocal count, blocked, fatal, main_final, navigation_seen
                async with network_lock:
                    req = route.request
                    is_main = req.is_navigation_request() and req.frame == page.main_frame
                    if is_main and navigation_seen:
                        blocked += 1
                        await route.abort()
                        return
                    try:
                        count += 1
                        if count > max_requests or budget.left <= 0:
                            raise FetchError("browser resource budget exhausted")
                        remaining(deadline)
                        if (req.method not in ("GET", "HEAD")
                                or urlsplit(req.url).scheme not in ("http", "https")
                                or req.resource_type in {"image", "media", "font"}
                                or (req.is_navigation_request() and not is_main)):
                            blocked += 1
                            await route.abort()
                            return
                        response = await asyncio.to_thread(
                            get, req.url, method=req.method, headers=req.headers,
                            max_bytes=budget.left, deadline=deadline, budget=budget,
                            timeout=remaining(deadline))
                        if is_main:
                            navigation_seen = True
                            main_final = response.url
                            if "html" not in response.headers.get("content-type", ""):
                                raise FetchError("browser navigation did not return HTML")
                        # No cookies/auth or stale transfer encodings reach Chromium.
                        # Bodies have already been bounded and decompressed.
                        allowed = {"content-type", "access-control-allow-origin",
                                   "access-control-allow-headers", "access-control-expose-headers"}
                        headers = {k: v for k, v in response.headers.items() if k in allowed}
                        headers["cache-control"] = "no-store"
                        body = response.body
                        if is_main and response.url != req.url:
                            from html import escape
                            base = f'<base href="{escape(response.url, quote=True)}">'.encode()
                            body = base + body
                        await route.fulfill(status=response.status, headers=headers, body=body)
                    except FetchError as exc:
                        blocked += 1
                        if is_main:
                            fatal = str(exc)
                        await route.abort()

            await context.route("**/*", handle)
            page.set_default_timeout(max(1, int(remaining(deadline) * 1000)))
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=max(1, int(remaining(deadline) * 1000)))
                # Avoid networkidle: many sites hold connections open forever.
                end = min(deadline, time.monotonic() + 3)
                while time.monotonic() < end:
                    await page.wait_for_timeout(min(250, max(1, int((end - time.monotonic()) * 1000))))
            except BrowserTimeout:
                warnings.append("Browser settling timed out; rendered content may be partial.")
            except Exception as exc:
                raise FetchError(f"browser navigation failed: {fatal or type(exc).__name__}") from exc
            if fatal:
                raise FetchError(fatal)
            if not navigation_seen:
                raise FetchError("browser did not load a document")
            if blocked:
                warnings.append(f"Browser blocked {blocked} resource(s) under read-only/network policy; content may be partial.")
            html = (await page.content()).encode("utf-8")
            if len(html) > max_bytes:
                raise FetchError("rendered HTML exceeds byte limit")
            return html, main_final, warnings
        finally:
            try:
                await asyncio.wait_for(browser.close(), timeout=3)
            except Exception:
                pass  # Playwright's context exit also disconnects the driver.


def render(url: str, *, timeout: float = 25, max_bytes: int = 20_000_000,
           max_requests: int = 60) -> tuple[bytes, str, list[str]]:
    async def bounded():
        try:
            return await asyncio.wait_for(_render(url, timeout=timeout, max_bytes=max_bytes,
                                                 max_requests=max_requests), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise FetchError("browser rendering deadline exceeded") from exc
    return asyncio.run(bounded())
