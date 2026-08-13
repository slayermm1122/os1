from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from backend.core.chat_history import JSONLChatHistory
from backend.core.messages import build_messages
from backend.core.sessions import KVConversationStore


class MessageBuilderTests(unittest.TestCase):
    def test_persona_precedes_base_prompt_and_language_is_hidden_on_user(self) -> None:
        messages = build_messages(
            system_prompt="stable-system",
            user_text="Hi",
            history=[],
            assistant_name="Samantha",
            user_name="小丽",
            persona="conversational",
            response_language="zh",
        )

        prompt = messages[0]["content"]
        self.assertLess(prompt.index('Your name is "Samantha"'), prompt.index("stable-system"))
        self.assertIn('user\'s name is "小丽"', prompt)
        self.assertIn("ask one relevant follow-up question", prompt)
        self.assertTrue(prompt.endswith("stable-system"))
        self.assertNotIn("简体中文", prompt)
        self.assertEqual(messages[-1]["content"], "Hi\n\n请用简体中文回答。")

    def test_english_mode_keeps_system_stable_and_enriches_final_user(self) -> None:
        messages = build_messages(
            system_prompt="stable-system",
            user_text="Hi",
            history=[],
            response_language="en",
        )

        self.assertEqual(messages[0]["content"], "stable-system")
        self.assertEqual(messages[-1]["content"], "Hi\n\nPlease respond in English.")

    def test_language_changes_do_not_change_system_message(self) -> None:
        english = build_messages(
            system_prompt="stable-system",
            user_text="hello",
            history=[],
            response_language="en",
        )
        chinese = build_messages(
            system_prompt="stable-system",
            user_text="你好",
            history=[],
            response_language="zh",
        )
        self.assertEqual(english[0], chinese[0])

    def test_history_is_preserved_without_turn_truncation(self) -> None:
        store = KVConversationStore(max_turns=1, max_sessions=5, ttl_seconds=3600)
        for index in range(3):
            store.append_turn("one", f"U{index}", f"A{index}")

        self.assertEqual(
            [message["content"] for message in store.get_history("one")],
            ["U0", "A0", "U1", "A1", "U2", "A2"],
        )

    def test_interrupted_history_can_store_user_without_empty_assistant(self) -> None:
        store = KVConversationStore(max_turns=1, max_sessions=5, ttl_seconds=3600)
        store.append_exchange("one", "hello\n\nPlease respond in English.", None)
        self.assertEqual(
            store.get_history("one"),
            [{"role": "user", "content": "hello\n\nPlease respond in English."}],
        )

    def test_names_are_unicode_letters_without_spaces_and_pronunciation_is_prompted(self) -> None:
        messages = build_messages(
            system_prompt="stable-system",
            user_text="hello",
            history=[],
            assistant_name="ソフィー",
            assistant_name_pronunciation="so fee",
            user_name="moi",
            user_name_pronunciation="mou e",
            persona="empathetic",
        )
        prompt = messages[0]["content"]
        self.assertIn('Your name is "ソフィー"', prompt)
        self.assertIn('approximate pronunciation is "so fee"', prompt)
        self.assertIn('user\'s name is "moi"', prompt)
        self.assertIn('approximate pronunciation is "mou e"', prompt)
        self.assertIn("empathetic AI companion", prompt)

    def test_jsonl_history_keeps_full_interrupted_reply_and_filters_by_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            history = JSONLChatHistory(Path(temp) / "chat.jsonl")
            history.append_turn(
                session_id="one",
                turn_id="turn-one",
                user_text="hello",
                assistant_text="A complete response.",
                interrupted=True,
                playback_started=True,
                spoken_text="A complete",
            )
            history.append_turn(
                session_id="two",
                turn_id="turn-two",
                user_text="ignored",
                assistant_text="ignored",
            )
            record = history.session("one")[0]
            self.assertEqual(record["user"], "hello")
            self.assertEqual(
                record["assistant"],
                "A complete(interrupted by user) response.",
            )

    def test_jsonl_thinking_interruption_marks_both_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            history = JSONLChatHistory(Path(temp) / "chat.jsonl")
            history.append_turn(
                session_id="one",
                turn_id="turn-one",
                user_text="hello",
                assistant_text="A complete response.",
                interrupted=True,
            )
            record = history.session("one")[0]
            self.assertEqual(record["user"], "hello(interrupted)")
            self.assertEqual(record["assistant"], "(interrupted)A complete response.")

    def test_jsonl_user_phase_is_replaced_by_completed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            history = JSONLChatHistory(Path(temp) / "chat.jsonl")
            history.append_user(
                session_id="one",
                turn_id="turn-one",
                user_text="hello",
            )
            self.assertEqual(history.session("one")[0]["phase"], "user")
            history.append_turn(
                session_id="one",
                turn_id="turn-one",
                user_text="hello",
                assistant_text="hi there",
            )
            records = history.session("one")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["assistant"], "hi there")
            self.assertEqual(records[0]["phase"], "complete")


if __name__ == "__main__":
    unittest.main()
