"""Offline setup tests: all Ollama/Telegram network calls are mocked."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import onboarding as setup
import barrel_v1 as core

TOKEN = "123456:" + "A" * 30


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        self.core = SimpleNamespace(
            CONFIG_FILE=str(root / "config.json"), IDENTITY_FILE=str(root / "identity.md"),
            read_file=lambda p: Path(p).read_text(encoding="utf-8").strip() if Path(p).exists() else "",
            TELEGRAM_ENABLED=False, OLLAMA_URL="http://127.0.0.1:11434", MODEL="qwen3:8b",
            NUM_CTX=16384, ollama=SimpleNamespace(Client=Mock()), agent_lock=threading.Lock(), _cfg={})
        self.service = setup.Setup(self.core)
        self.patches = [patch.object(setup, "STATE_FILE", str(root / "onboarding.local.json")),
                        patch.object(setup, "SECRETS_FILE", str(root / "secrets.local.json")),
                        patch.dict(os.environ, {}, clear=True)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_fresh_status_and_resumable_draft_drops_secrets(self):
        self.assertTrue(self.service.status()["offer_setup"])
        self.assertFalse(self.service.status()["existing"])
        self.service.dispatch("draft", {"step": 3, "draft": {"name": "Moss", "token": TOKEN, "code": "secret"}})
        state = setup.read_object(setup.STATE_FILE)
        self.assertEqual(state["draft"], {"name": "Moss"})
        self.assertEqual(state["step"], 3)
        self.assertNotIn(TOKEN, Path(setup.STATE_FILE).read_text())
        self.service.dispatch("dismiss", {})
        self.assertFalse(self.service.status()["offer_setup"])
        self.assertEqual(self.service.status()["state"]["step"], 3)

    def test_identity_generation_is_deterministic_without_model(self):
        result = self.service.dispatch("identity-preview", {"name": "Moss", "owner": "Jo", "preset": "organizer"})
        self.assertIn("You are Moss", result["identity"])
        self.assertIn("Call your user Jo", result["identity"])
        self.assertIn("Boundaries", result["identity"])
        self.core.ollama.Client.assert_not_called()

    def test_existing_identity_requires_confirmation_and_has_backup(self):
        path = Path(self.core.IDENTITY_FILE)
        path.write_text("My existing persona")
        with self.assertRaisesRegex(ValueError, "Confirm replacement"):
            self.service.dispatch("save-identity", {"identity": "New persona"})
        self.assertEqual(path.read_text(), "My existing persona")
        self.service.dispatch("save-identity", {"identity": "New persona", "replace": True})
        self.assertEqual(path.read_text(), "New persona\n")
        self.assertEqual(Path(str(path) + ".setup-backup.local.md").read_text(), "My existing persona\n")

    def test_corrupt_config_not_silently_replaced(self):
        Path(self.core.CONFIG_FILE).write_text("broken")
        self.service.last_test = ("http://127.0.0.1:11434", "qwen3:8b")
        with self.assertRaises(ValueError):
            self.service.dispatch("save-backend", {})
        self.assertEqual(Path(self.core.CONFIG_FILE).read_text(), "broken")

    def test_model_check_returns_installed_names(self):
        response = Mock()
        response.json.return_value = {"models": [{"name": "small:latest"}]}
        with patch.object(setup.requests, "get", return_value=response):
            result = self.service.dispatch("models", {})
        self.assertEqual(result, {"reachable": True, "models": ["small:latest"]})

    def test_generation_is_isolated_and_backend_save_merges_config(self):
        Path(self.core.CONFIG_FILE).write_text('{"custom_key": "keep", "dashboard_port": 8888}')
        self.core.ollama.Client.return_value.chat.return_value = {"message": {"content": "Hello!"}}
        data = {"host": "http://localhost:11434", "model": "small:latest", "identity": "Be kind", "num_ctx": 4096}
        self.assertEqual(self.service.dispatch("test-model", data)["reply"], "Hello!")
        call = self.core.ollama.Client.return_value.chat.call_args.kwargs
        self.assertEqual(call["messages"][0]["content"], "Be kind")
        self.assertNotIn("tools", call)
        self.assertFalse((self.root / "history.md").exists())
        self.service.dispatch("save-backend", data)
        config = setup.read_object(self.core.CONFIG_FILE)
        self.assertEqual(config["custom_key"], "keep")
        self.assertEqual(config["dashboard_port"], 8888)
        self.assertEqual((self.core.MODEL, self.core.NUM_CTX, self.core.OLLAMA_URL),
                         ("small:latest", 4096, "http://localhost:11434"))
        with self.assertRaisesRegex(ValueError, "Test this model"):
            self.service.dispatch("save-backend", {**data, "model": "another"})

    def test_generation_error_is_redacted(self):
        self.core.ollama.Client.return_value.chat.side_effect = RuntimeError(TOKEN)
        with self.assertRaises(ValueError) as ctx:
            self.service.dispatch("test-model", {})
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_empty_response_does_not_pass_test(self):
        self.core.ollama.Client.return_value.chat.return_value = {"message": {"content": ""}}
        with self.assertRaises(ValueError):
            self.service.dispatch("test-model", {})
        self.assertIsNone(self.service.last_test)

    def test_invalid_backend_urls_and_environment_override(self):
        for host in ("file:///etc/passwd", "http://user:secret@localhost", "http://localhost/path", "http://localhost:bad"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                setup.backend({"host": host})
        self.service.last_test = ("http://localhost:11434", "qwen3:8b")
        with patch.dict(os.environ, {"OLLAMA_HOST": "http://other:11434"}):
            with self.assertRaisesRegex(ValueError, "overrides"):
                self.service.dispatch("save-backend", {"host": "http://localhost:11434"})

    def start_pair(self):
        with patch.object(self.service, "telegram", side_effect=[{"username": "MossBot"}, {"url": ""}]), \
             patch.object(setup.threading, "Thread"):
            return self.service.dispatch("telegram-start", {"token": TOKEN})

    def test_pair_token_only_in_memory_link_has_strong_challenge(self):
        result = self.start_pair()
        self.assertIn("https://t.me/MossBot?start=", result["link"])
        self.assertGreaterEqual(len(self.service.pair["code"]), 32)
        self.assertFalse(Path(setup.SECRETS_FILE).exists())
        status = self.service.dispatch("telegram-status", {})
        self.assertNotIn(TOKEN, json.dumps(status))
        self.assertNotIn("code", status)
        with self.assertRaisesRegex(ValueError, "Cancel"):
            self.service.dispatch("telegram-start", {"token": TOKEN})

    def test_existing_webhook_is_not_removed(self):
        with patch.object(self.service, "telegram", side_effect=[{"username": "MossBot"}, {"url": "https://existing.test"}]) as tg:
            with self.assertRaisesRegex(ValueError, "existing webhook"):
                self.service.dispatch("telegram-start", {"token": TOKEN})
        self.assertEqual([c.args[1] for c in tg.call_args_list], ["getMe", "getWebhookInfo"])
        self.assertIsNone(self.service.pair)

    def test_active_bot_and_environment_cannot_pair(self):
        self.core.TELEGRAM_ENABLED = True
        with self.assertRaisesRegex(ValueError, "already running"):
            self.service.dispatch("telegram-start", {"token": TOKEN})
        self.core.TELEGRAM_ENABLED = False
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "old"}):
            with self.assertRaisesRegex(ValueError, "environment"):
                self.service.dispatch("telegram-start", {"token": TOKEN})

    def update(self, text, chat_type="private", uid=42, update_id=1):
        return {"update_id": update_id, "message": {"text": text, "chat": {"type": chat_type, "id": uid},
                "from": {"id": uid, "first_name": "Jo", "username": "jo"}}}

    def test_only_matching_private_challenge_can_be_confirmed(self):
        self.start_pair()
        p = self.service.pair
        updates = [self.update("hello"), self.update("/start wrong", update_id=2),
                   self.update("/start " + p["code"], "group", update_id=3),
                   self.update("/start " + p["code"], update_id=4)]
        with patch.object(self.service, "telegram", side_effect=[updates, []]) as tg:
            self.service.poll_pair(p)
        self.assertEqual(p["phase"], "candidate")
        self.assertEqual(tg.call_args_list[-1].kwargs["offset"], 5)
        self.assertFalse(Path(setup.SECRETS_FILE).exists())
        with self.assertRaisesRegex(ValueError, "Account changed"):
            self.service.dispatch("telegram-confirm", {"user_id": 99})
        with patch.object(self.service, "telegram", return_value={}):
            result = self.service.dispatch("telegram-confirm", {"user_id": 42})
        self.assertTrue(result["delivery"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(setup.read_object(setup.SECRETS_FILE),
                         {"telegram_bot_token": TOKEN, "telegram_allowed_ids": "42"})
        with self.assertRaises(ValueError):
            self.service.dispatch("telegram-confirm", {"user_id": 42})

    def test_expired_pair_and_cancel_never_authorize(self):
        self.start_pair()
        self.service.pair.update(phase="candidate", candidate={"id": 42}, expires=time.time() - 1)
        with self.assertRaises(ValueError):
            self.service.dispatch("telegram-confirm", {"user_id": 42})
        self.assertEqual(self.service.dispatch("telegram-status", {})["phase"], "expired")
        self.service.dispatch("telegram-cancel", {})
        self.assertFalse(Path(setup.SECRETS_FILE).exists())

    def test_delivery_failure_is_distinct_from_authorization(self):
        self.start_pair()
        self.service.pair.update(phase="candidate", candidate={"id": 42})
        with patch.object(self.service, "telegram", side_effect=ValueError("offline")):
            result = self.service.dispatch("telegram-confirm", {"user_id": 42})
        self.assertFalse(result["delivery"])
        self.assertTrue(Path(setup.SECRETS_FILE).exists())

    def test_telegram_network_error_does_not_expose_token(self):
        with patch.object(setup.requests, "post", side_effect=setup.requests.ConnectionError(TOKEN)):
            with self.assertRaises(ValueError) as ctx:
                self.service.telegram(TOKEN, "getMe")
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_finish_persists_completed_flag(self):
        self.service.dispatch("finish", {})
        self.assertTrue(self.service.status()["state"]["completed"])
        self.assertFalse(self.service.status()["offer_setup"])


class RuntimeIntegrationTests(unittest.TestCase):
    def test_runtime_inference_uses_configured_host(self):
        with patch.object(core, "OLLAMA_URL", "http://my-ollama:11434"), patch.object(core.ollama, "Client") as client:
            core.model_chat(model="small", messages=[])
        client.assert_called_once_with(host="http://my-ollama:11434")
        client.return_value.chat.assert_called_once_with(model="small", messages=[])

    def test_startup_reads_local_credentials_and_environment_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text('{"ollama_host":"http://saved:11434"}')
            (root / "secrets.local.json").write_text(json.dumps({"telegram_bot_token": TOKEN, "telegram_allowed_ids": "42"}))
            probe = ("import sys; sys.path.insert(0, " + repr(str(Path(core.__file__).parent)) + "); "
                     "import barrel_v1 as c; import json; "
                     "print('PROBE:' + json.dumps([c.BOT_TOKEN == " + repr(TOKEN) + ", sorted(c.ALLOWED_IDS), c.TELEGRAM_ENABLED, c.OLLAMA_URL]))")
            env = {k: v for k, v in os.environ.items() if k not in
                   ("OLLAMA_HOST", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_IDS")}
            result = subprocess.run([sys.executable, "-c", probe], cwd=root, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('PROBE:[true, [42], true, "http://saved:11434"]', result.stdout)
            result = subprocess.run([sys.executable, "-c", probe], cwd=root,
                                    env={**env, "TELEGRAM_BOT_TOKEN": "environment-token", "TELEGRAM_ALLOWED_IDS": "77", "OLLAMA_HOST": "http://env:11434"},
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('PROBE:[false, [77], true, "http://env:11434"]', result.stdout)

    def test_runtime_log_redacts_bot_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.jsonl"
            with patch.object(core, "LOG_FILE", str(path)), patch.object(core, "BOT_TOKEN", TOKEN):
                core.log_event("test", error=f"https://api.telegram.org/bot{TOKEN}/sendMessage")
            self.assertNotIn(TOKEN, path.read_text())
            self.assertIn("REDACTED_BOT_TOKEN", path.read_text())


class SetupHTTPTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.service.status.return_value = {"offer_setup": True}
        self.service.dispatch.return_value = {"saved": True}
        for item in (patch.object(core, "setup_service", self.service),
                     patch.object(core, "DASHBOARD_HOST", "127.0.0.1"),
                     patch.object(core, "DASHBOARD_TOKEN", "")):
            item.start()
            self.addCleanup(item.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.DashboardHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            result = conn.getresponse()
            return result.status, dict(result.getheaders()), result.read()
        finally:
            conn.close()

    def test_setup_assets_and_status(self):
        for path in ("/setup", "/setup.js", "/setup.css", "/setup/status"):
            with self.subTest(path=path):
                code, headers, body = self.request("GET", path)
                self.assertEqual(code, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertTrue(body)

    def test_setup_is_origin_and_token_protected(self):
        for method, path, body in (("GET", "/setup/status", None), ("POST", "/setup/draft", "{}")):
            code, _, _ = self.request(method, path, body, {"Origin": "https://evil.test", "Content-Type": "application/json"})
            self.assertEqual(code, 403)
        with patch.object(core, "DASHBOARD_TOKEN", "password"):
            self.assertEqual(self.request("GET", "/setup/status")[0], 401)
            self.assertEqual(self.request("POST", "/setup/draft", "{}", {"Content-Type": "application/json"})[0], 401)
            self.assertEqual(self.request("GET", "/setup/status", headers={"Cookie": "barrel_token=password"})[0], 200)
        self.service.dispatch.assert_not_called()

    def test_json_type_size_and_dispatch(self):
        self.assertEqual(self.request("POST", "/setup/draft", "{}", {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/setup/draft", "[]", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/setup/draft", None, {"Content-Type": "application/json", "Content-Length": "32001"})[0], 413)
        self.assertEqual(self.request("POST", "/setup/draft", '{}', {"Content-Type": "application/json"})[0], 200)
        self.service.dispatch.assert_called_once_with("draft", {})

    def test_unexpected_errors_are_redacted(self):
        self.service.dispatch.side_effect = RuntimeError(TOKEN)
        code, _, body = self.request("POST", "/setup/telegram-start", '{}', {"Content-Type": "application/json"})
        self.assertEqual(code, 500)
        self.assertNotIn(TOKEN.encode(), body)


if __name__ == "__main__":
    unittest.main()
