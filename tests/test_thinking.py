"""Per-chat Ollama thinking requests; no live model or file writes."""
import copy
import re
import unittest
from unittest.mock import patch

import barrel_v1 as core


class ThinkingTests(unittest.TestCase):
    def setUp(self):
        for item in (
            patch.object(core, "thinking_overrides", {}),
            patch.object(core, "conversations", {}),
            patch.object(core, "active_tasks", {}),
            patch.object(core, "sidebar_state", {}),
            patch.object(core, "trace_chats", set()),
            patch.object(core, "stats", copy.deepcopy(core.stats)),
            patch.object(core, "last_contact_at", core.last_contact_at),
            patch.object(core, "log_event"),
            patch.object(core, "track_tokens"),
            patch.object(core, "build_system_prompt", return_value="Test assistant"),
        ):
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def response(content="Done."):
        return {"message": {"content": content, "thinking": "Private model reasoning"}}

    def test_default_is_omitted_not_forced(self):
        self.assertEqual(core.thinking_options(42), {})
        with patch.object(core, "model_chat", return_value=self.response()) as model:
            self.assertEqual(core.handle_turn(42, "Hello"), "Done.")
        self.assertNotIn("think", model.call_args.kwargs)
        self.assertIn("MODEL DEFAULT", core.handle_command("/think status", 42))
        self.assertEqual(core.thinking_overrides, {})

    def test_bare_command_toggles_off_then_on(self):
        self.assertIn("OFF", core.handle_command("/think", 42))
        self.assertEqual(core.thinking_options(42), {"think": False})
        self.assertIn("ON", core.handle_command("/think", 42))
        self.assertEqual(core.thinking_options(42), {"think": True})

    def test_explicit_commands_idempotent_and_chat_scoped(self):
        core.handle_command("/think on", 42)
        core.handle_command("/think on", 42)
        core.handle_command("/think off", core.WEB_CHAT_ID)
        self.assertEqual(core.thinking_options(42), {"think": True})
        self.assertEqual(core.thinking_options(core.WEB_CHAT_ID), {"think": False})
        self.assertEqual(core.thinking_options(99), {})
        core.handle_command("/think default", 42)
        self.assertEqual(core.thinking_options(42), {})
        self.assertEqual(core.thinking_options(core.WEB_CHAT_ID), {"think": False})

    def test_invalid_command_does_not_mutate_or_call_model(self):
        with patch.object(core, "model_chat") as model:
            self.assertIn("Usage:", core.handle_command("/think maybe", 42))
            self.assertIsNone(core.handle_command("/thinking", 42))
            model.assert_not_called()
        self.assertEqual(core.thinking_overrides, {})

    def test_status_is_read_only_and_case_insensitive_argument(self):
        core.handle_command("/think ON", 42)
        self.assertIn("ON", core.handle_command("/think status", 42))
        self.assertEqual(core.thinking_options(42), {"think": True})
        self.assertIn("Resets on restart", core.handle_command("/think off", 42))

    def test_normal_and_pulse_calls_forward_boolean(self):
        for enabled, kind in ((False, "chat"), (True, "pulse")):
            with self.subTest(enabled=enabled, kind=kind):
                core.thinking_overrides[42] = enabled
                with patch.object(core, "model_chat", return_value=self.response()) as model:
                    core.handle_turn(42, "Hello", kind=kind)
                self.assertIs(model.call_args.kwargs["think"], enabled)

    def test_sidebar_and_ask_use_same_chat_preference(self):
        core.handle_command("/think off", 42)
        with patch.object(core, "model_chat", return_value=self.response()) as model:
            self.assertEqual(core.handle_command("/ask Hello", 42), "Done.")
            self.assertIs(model.call_args.kwargs["think"], False)
            core.sidebar_answer(99, "Hello")
            self.assertNotIn("think", model.call_args.kwargs)

    def test_setting_is_snapshotted_for_all_tool_rounds(self):
        core.handle_command("/think off", 42)
        def tool(arg, chat):
            core.handle_command("/think on", chat)  # simulate a concurrent user toggle
            return "Result"
        with patch.object(core, "TOOLS", {"probe": {"handler": tool, "desc": "test"}}), \
             patch.object(core, "TOOL_RE", re.compile(r"<(probe)>(.*?)</\1>", re.S)), \
             patch.object(core, "model_chat", side_effect=[self.response("<probe>test</probe>"), self.response()]) as model:
            core.handle_turn(42, "Check something")
        self.assertEqual([call.kwargs["think"] for call in model.call_args_list], [False, False])
        with patch.object(core, "model_chat", return_value=self.response()) as model:
            core.handle_turn(42, "Next question")
        self.assertIs(model.call_args.kwargs["think"], True)

    def test_model_output_cannot_toggle_setting_or_expose_thinking_field(self):
        with patch.object(core, "model_chat", return_value=self.response("/think on")):
            self.assertEqual(core.handle_turn(42, "Hello"), "/think on")
        self.assertEqual(core.thinking_overrides, {})
        self.assertNotIn("Private model reasoning", str(core.conversations))


if __name__ == "__main__":
    unittest.main()
