"""Opt-in real Chromium tests with fixture responses, never public websites.

Run with BARREL_BROWSER_TESTS=1 after installing requirements-browser.txt and
Chromium. The default suite mocks the renderer at the document-service boundary
and tests network policy separately, without requiring a browser installation.
"""
import os
import time
import unittest
from unittest.mock import patch

from barrel_web import browser
from barrel_web.transport import FetchError, Response, get


@unittest.skipUnless(os.environ.get("BARREL_BROWSER_TESTS") == "1", "opt-in Chromium integration tests")
class BrowserTests(unittest.TestCase):
    def test_real_js_hydration_and_private_request_refusal(self):
        requests = []
        html = b'''<!doctype html><html><head><title>JS fixture</title></head>
<body><main id="app">Loading</main><script src="/app.js"></script></body></html>'''
        script = b'''async function run() {
const data = await (await fetch('/data.json')).json();
document.querySelector('#app').textContent = data.article;
try { await fetch('http://127.0.0.1:1234/probe'); } catch(e) {
 document.body.append(' PRIVATE BLOCKED');
}
fetch('/mutate', {method: 'POST', body: 'unsafe'}).catch(()=>{});
new WebSocket('wss://fixture.test/socket');
document.body.append(typeof RTCPeerConnection === 'undefined' ? ' RTC DISABLED' : ' RTC AVAILABLE');
document.body.append(typeof Worker === 'undefined' ? ' WORKER DISABLED' : ' WORKER AVAILABLE');
} run();'''

        def fixtures(url, **kwargs):
            requests.append(url)
            if url == "https://fixture.test/":
                return Response(url, 200, {"content-type": "text/html"}, html)
            if url.endswith("/app.js"):
                return Response(url, 200, {"content-type": "application/javascript"}, script)
            if url.endswith("/data.json"):
                return Response(url, 200, {"content-type": "application/json"}, b'{"article":"Hydrated article from JavaScript"}')
            # Real policy should reject the private address before a connection.
            return get(url, **kwargs)

        with patch.object(browser, "get", side_effect=fixtures), patch("barrel_web.transport._connection") as connection:
            body, final_url, warnings = browser.render("https://fixture.test/", timeout=20)
            connection.assert_not_called()
        text = body.decode()
        self.assertIn("Hydrated article from JavaScript", text)
        self.assertIn("PRIVATE BLOCKED", text)
        self.assertIn("RTC DISABLED", text)
        self.assertIn("WORKER DISABLED", text)
        self.assertNotIn("https://fixture.test/mutate", requests)
        self.assertTrue(any("blocked" in w for w in warnings))
        self.assertEqual(final_url, "https://fixture.test/")

    def test_unresponsive_javascript_obeys_render_deadline(self):
        html = b"<html><script>while(true) {}</script></html>"
        fixture = Response("https://fixture.test/", 200, {"content-type": "text/html"}, html)
        started = time.monotonic()
        with patch.object(browser, "get", return_value=fixture):
            with self.assertRaises(FetchError):
                browser.render("https://fixture.test/", timeout=3)
        self.assertLess(time.monotonic() - started, 8)

    def test_private_main_navigation_is_refused(self):
        with self.assertRaises(FetchError):
            browser.render("http://127.0.0.1/", timeout=10)


if __name__ == "__main__":
    unittest.main()
