"""Opt-in real Chromium wizard tests. Network backends are mocked.

RUN_ONBOARDING_BROWSER_TESTS=1 python -m unittest discover -s tests -p test_onboarding_browser.py -v
Requires optional Playwright + Chromium, not needed to run BarrelShell.
"""
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import barrel_v1 as core
import onboarding


@unittest.skipUnless(os.environ.get("RUN_ONBOARDING_BROWSER_TESTS") == "1", "opt-in Chromium onboarding tests")
class WizardBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        for item in (
            patch.object(core, "CONFIG_FILE", str(root / "config.json")),
            patch.object(core, "IDENTITY_FILE", str(root / "identity.md")),
            patch.object(core, "DASHBOARD_HTML", str(Path(core.__file__).parent / "dashboard.html")),
            patch.object(core, "DASHBOARD_HOST", "127.0.0.1"),
            patch.object(core, "DASHBOARD_TOKEN", ""),
            patch.object(core, "gather_status", return_value={"uptime": "0", "started": "now", "stats": {"tools": {}}, "tokens": {}}),
            patch.object(core, "TELEGRAM_ENABLED", False),
            patch.object(core, "OLLAMA_URL", "http://127.0.0.1:11434"),
            patch.object(core, "MODEL", "qwen3:8b"), patch.object(core, "NUM_CTX", 16384),
            patch.object(core, "_cfg", dict(core._cfg)),
            patch.object(onboarding, "STATE_FILE", str(root / "onboarding.local.json")),
            patch.object(onboarding, "SECRETS_FILE", str(root / "secrets.local.json")),
            patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in
                                   ("OLLAMA_HOST", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_IDS")}, clear=True),
            patch.object(core.ollama, "Client"),
        ):
            item.start()
            self.addCleanup(item.stop)
        core.ollama.Client.return_value.chat.return_value = {"message": {"content": "Hello, I am Moss. How can I help?"}}
        self.service = onboarding.Setup(core)
        p = patch.object(core, "setup_service", self.service)
        p.start(); self.addCleanup(p.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.DashboardHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch()
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={"width": 1100, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def next(self):
        finishing = self.page.locator("#next").inner_text() == "Start chatting"
        self.page.locator("#next").click()
        if finishing:
            self.page.wait_for_url(self.url + "/")
            return
        self.page.locator("#busy").wait_for(state="hidden")
        self.assertFalse(self.page.locator("#error").is_visible(), self.page.locator("#error").inner_text())

    def advance_to_telegram(self):
        self.page.goto(self.url + "/setup")
        self.page.locator("#busy").wait_for(state="hidden")
        self.next()
        self.page.locator("#name").fill("Moss")
        self.page.locator("#owner").fill("Jo")
        self.next()
        self.assertIn("You are Moss", self.page.locator("#identity").input_value())
        self.next()
        # Verify a failed premature save keeps the user on the brain step.
        self.page.locator("#next").click()
        self.page.locator("#busy").wait_for(state="hidden")
        self.assertIn("Test this model", self.page.locator("#error").inner_text())
        self.page.locator("#test").click()
        self.page.locator("#busy").wait_for(state="hidden")
        self.assertIn("Model returned", self.page.locator("#test-result").inner_text())
        self.next()
        self.page.locator("#preview-send").click()
        self.page.locator("#busy").wait_for(state="hidden")
        self.assertIn("I am Moss", self.page.locator("#preview-result").inner_text())
        self.next()

    def test_fresh_local_only_setup(self):
        self.advance_to_telegram()
        self.next()
        self.assertIn("not linked (optional)", self.page.locator("#summary").inner_text())
        self.next()
        self.page.wait_for_url(self.url + "/")
        self.assertFalse(self.page.locator("#setup-banner").is_visible())
        self.assertIn("You are Moss", (self.root / "identity.md").read_text())
        self.assertEqual(json.loads((self.root / "config.json").read_text())["model"], "qwen3:8b")
        self.assertFalse((self.root / "secrets.local.json").exists())
        self.assertFalse((self.root / "history.md").exists())
        self.assertEqual(self.errors, [])

    def test_telegram_pairing_ui_confirm_and_finish(self):
        self.advance_to_telegram()
        def telegram(token, method, **params):
            if method == "getMe":
                return {"username": "MossTestBot"}
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getUpdates":
                return []
            return {}
        def candidate(p):
            with self.service.lock:
                p.update(phase="candidate", candidate={"id": 42, "name": "Jo", "username": "jo"})
        with patch.object(self.service, "telegram", side_effect=telegram), patch.object(self.service, "poll_pair", side_effect=candidate):
            self.page.locator("#token").fill("12345:" + "A" * 30)
            self.page.locator("#tg-start").click()
            self.page.locator("#tg-confirm").wait_for(state="visible")
            self.assertIn("Numeric user ID: 42", self.page.locator("#tg-status").inner_text())
            self.assertEqual(self.page.locator("#token").input_value(), "")
            self.assertFalse((self.root / "secrets.local.json").exists())
            self.page.locator("#tg-confirm").click()
            self.page.locator("#restart-note").wait_for(state="visible")
            self.assertIn("Test reply sent", self.page.locator("#tg-status").inner_text())
        self.next()
        self.assertIn("restart required", self.page.locator("#summary").inner_text())
        self.assertEqual(json.loads((self.root / "secrets.local.json").read_text())["telegram_allowed_ids"], "42")
        self.next()
        self.assertEqual(self.errors, [])

    def test_save_later_resumes_without_overwriting_existing_identity(self):
        (self.root / "identity.md").write_text("You are Original, keep me.")
        self.page.goto(self.url + "/setup")
        self.page.locator("#busy").wait_for(state="hidden")
        self.next(); self.next()
        self.assertEqual(self.page.locator("#identity").input_value(), "You are Original, keep me.")
        self.page.locator("#extra").fill("Draft preferences")
        self.page.locator("#later").click()
        self.page.wait_for_url(self.url + "/")
        self.page.goto(self.url + "/setup")
        self.page.locator("#busy").wait_for(state="hidden")
        self.assertEqual(self.page.locator("#extra").input_value(), "Draft preferences")
        self.assertTrue(self.page.locator('section[data-step="2"]').is_visible())
        self.assertEqual((self.root / "identity.md").read_text(), "You are Original, keep me.")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
