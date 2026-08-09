from __future__ import annotations

import unittest

from backend.core.messages import build_messages
from backend.core.sessions import KVConversationStore


class MessageBuilderTests(unittest.TestCase):
    def test_persona_precedes_base_prompt_and_language_is_last(self) -> None:
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
        self.assertTrue(prompt.endswith("请你用简体中文回答。"))

    def test_english_voice_requires_english_as_the_final_instruction(self) -> None:
        messages = build_messages(
            system_prompt="stable-system",
            user_text="Hi",
            history=[],
            response_language="en",
        )

        self.assertTrue(messages[0]["content"].endswith("Please respond in English."))

    def test_history_is_preserved_without_turn_truncation(self) -> None:
        store = KVConversationStore(max_turns=1, max_sessions=5, ttl_seconds=3600)
        for index in range(3):
            store.append_turn("one", f"U{index}", f"A{index}")

        self.assertEqual(
            [message["content"] for message in store.get_history("one")],
            ["U0", "A0", "U1", "A1", "U2", "A2"],
        )


if __name__ == "__main__":
    unittest.main()
