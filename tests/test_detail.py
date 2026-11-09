"""Response-detail commands and prompt integration; no live inference or writes."""
import copy
import re
import unittest
from unittest.mock import patch

import barrel_v1 as core


class DetailTests(unittest.TestCase):
    def setUp(self):
        for item in (
            patch.object(core, "detail_overrides", {}),
            patch.object(core, "thinking_overrides", {}),
            patch.object(core, "conversations", {}),
            patch.object(core, "active_tasks", {}),
            patch.object(core, "sidebar_state", {}),
            patch.object(core, "trace_chats", set()),
            patch.object(core, "stats", copy.deepcopy(core.stats)),
            patch.object(core, "last_contact_at", core.last_contact_at),
            patch.object(core, "log_event"),
            patch.object(core, "track_tokens"),
            patch.object(core, "build_system_prompt", return_value="Original persona and protocol"),
        ):
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def response(content="Answer."):
        return {"message": {"content": content}}

    def test_default_preserves_prompt_exactly(self):
        self.assertEqual(core.detail_instruction(42), "")
        with patch.object(core, "model_chat", return_value=self.response()) as model:
            core.handle_turn(42, "Hello")
        self.assertEqual(model.call_args.kwargs["messages"][0]["content"], "Original persona and protocol")

    def test_all_modes_set_idempotently_without_model_or_disk_writes(self):
        with patch.object(core, "model_chat") as model, patch("builtins.open") as file_open:
            for mode in ("brief", "balanced", "full"):
                self.assertIn(mode.upper(), core.handle_command("/detail " + mode, 42))
                core.handle_command("/detail " + mode, 42)
                self.assertEqual(core.detail_overrides[42], mode)
            model.assert_not_called()
            file_open.assert_not_called()

    def test_chat_isolation_and_default_removes_override(self):
        core.handle_command("/detail brief", 42)
        core.handle_command("/detail full", core.WEB_CHAT_ID)
        self.assertIn("preference: brief", core.detail_instruction(42))
        self.assertIn("preference: full", core.detail_instruction(core.WEB_CHAT_ID))
        self.assertEqual(core.detail_instruction(99), "")
        core.handle_command("/detail default", 42)
        self.assertNotIn(42, core.detail_overrides)
        self.assertEqual(core.detail_instruction(42), "")
        self.assertEqual(core.detail_overrides[core.WEB_CHAT_ID], "full")

    def test_bare_command_and_status_are_read_only(self):
        self.assertIn("DEFAULT", core.handle_command("/detail", 42))
        self.assertEqual(core.detail_overrides, {})
        core.handle_command("/detail BRIEF", 42)
        self.assertIn("BRIEF", core.handle_command("/detail status", 42))
        self.assertIn("BRIEF", core.handle_command("/detail", 42))
        self.assertEqual(core.detail_overrides, {42: "brief"})

    def test_invalid_command_does_not_mutate(self):
        core.handle_command("/detail brief", 42)
        for arg in ("short", "brief extra", "status extra"):
            self.assertIn("Usage:", core.handle_command("/detail " + arg, 42))
        self.assertEqual(core.detail_overrides, {42: "brief"})
        self.assertIsNone(core.handle_command("/details", 42))

    def test_instructions_preserve_explicit_requests_and_safety(self):
        for mode in ("brief", "balanced", "full"):
            core.handle_command("/detail " + mode, 42)
            instruction = core.detail_instruction(42)
            self.assertIn("explicit request", instruction)
            self.assertIn("complete code", instruction)
            self.assertIn("Never omit essential warnings", instruction)
            self.assertIn("permission gates", instruction)
            self.assertIn("no fixed word or paragraph limit", instruction)
        self.assertIn("do not pad", core.detail_instruction(42))
        core.handle_command("/detail brief", 42)
        self.assertIn("do not routinely end every reply", core.detail_instruction(42))

    def test_main_and_scheduled_turns_use_instructions_not_token_cutoffs(self):
        for mode, kind in (("brief", "chat"), ("balanced", "pulse"), ("full", "chat")):
            with self.subTest(mode=mode, kind=kind):
                core.handle_command("/detail " + mode, 42)
                with patch.object(core, "model_chat", return_value=self.response()) as model:
                    core.handle_turn(42, "Give me complete instructions", kind=kind)
                call = model.call_args.kwargs
                system = call["messages"][0]["content"]
                self.assertTrue(system.startswith("Original persona and protocol"))
                self.assertIn("Response detail preference: " + mode, system)
                self.assertEqual(call["options"]["num_predict"], -1)

    def test_ask_and_sidebar_share_preference_without_changing_role(self):
        core.handle_command("/detail full", 42)
        core._sidebar(42)["context"] = "You are an expert botanist."
        with patch.object(core, "model_chat", return_value=self.response()) as model:
            core.handle_command("/ask Explain roots", 42)
            system = model.call_args.kwargs["messages"][0]["content"]
            self.assertIn("expert botanist", system)
            self.assertIn("no tools available", system)
            self.assertIn("preference: full", system)
            core._sidebar(42)["on"] = True
            core.route_turn(42, "Explain leaves")
            self.assertIn("preference: full", model.call_args.kwargs["messages"][0]["content"])
            core.sidebar_answer(99, "Explain roots")
            self.assertNotIn("Response detail preference", model.call_args.kwargs["messages"][0]["content"])

    def test_detail_and_thinking_are_independent(self):
        core.handle_command("/think off", 42)
        core.handle_command("/detail full", 42)
        with patch.object(core, "model_chat", return_value=self.response()) as model:
            core.handle_turn(42, "Hello")
        self.assertIs(model.call_args.kwargs["think"], False)
        self.assertIn("preference: full", model.call_args.kwargs["messages"][0]["content"])
        core.handle_command("/think on", 42)
        self.assertEqual(core.detail_overrides[42], "full")
        core.handle_command("/detail default", 42)
        self.assertEqual(core.thinking_overrides[42], True)

    def test_preference_is_snapshotted_across_tool_rounds(self):
        core.handle_command("/detail brief", 42)
        def tool(arg, chat):
            core.handle_command("/detail full", chat)  # simulate concurrent user command
            return "Tool result"
        with patch.object(core, "TOOLS", {"probe": {"handler": tool, "desc": "test"}}), \
             patch.object(core, "TOOL_RE", re.compile(r"<(probe)>(.*?)</\1>", re.S)), \
             patch.object(core, "model_chat", side_effect=[self.response("<probe>test</probe>"), self.response()]) as model:
            core.handle_turn(42, "Check something")
        for call in model.call_args_list:
            self.assertIn("preference: brief", call.kwargs["messages"][0]["content"])
        with patch.object(core, "model_chat", return_value=self.response()) as model:
            core.handle_turn(42, "Next question")
        self.assertIn("preference: full", model.call_args.kwargs["messages"][0]["content"])

    def test_model_output_cannot_change_mode(self):
        with patch.object(core, "model_chat", return_value=self.response("/detail full")):
            self.assertEqual(core.handle_turn(42, "Hello"), "/detail full")
        self.assertEqual(core.detail_overrides, {})


if __name__ == "__main__":
    unittest.main()
