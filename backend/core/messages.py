from __future__ import annotations

import json

Message = dict[str, str]

_PERSONAS = {"default", "concise", "conversational", "empathetic"}


def sanitize_person_name(value: str | None) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 40:
        return ""
    if not all(character.isalpha() for character in name):
        return ""
    return name


def sanitize_pronunciation(value: str | None) -> str:
    pronunciation = " ".join(str(value or "").split()).strip()
    return pronunciation if len(pronunciation) <= 120 else ""


def sanitize_assistant_name(value: str | None) -> str:
    """Compatibility alias for callers that only configure the AI name."""
    return sanitize_person_name(value)


def normalize_persona(value: str | None) -> str:
    persona = str(value or "default").strip().lower()
    return persona if persona in _PERSONAS else "default"


def build_messages(
    *,
    system_prompt: str,
    user_text: str,
    history: list[Message],
    assistant_name: str = "",
    assistant_name_pronunciation: str = "",
    user_name: str = "",
    user_name_pronunciation: str = "",
    persona: str = "default",
    response_language: str = "en",
) -> list[Message]:
    """Build a stable system prompt plus the exact chronological conversation."""
    identity_lines: list[str] = []
    ai_name = sanitize_person_name(assistant_name)
    human_name = sanitize_person_name(user_name)
    ai_pronunciation = sanitize_pronunciation(assistant_name_pronunciation)
    human_pronunciation = sanitize_pronunciation(user_name_pronunciation)
    style = normalize_persona(persona)
    if ai_name:
        line = f'Your name is {json.dumps(ai_name, ensure_ascii=False)}.'
        if ai_pronunciation:
            line += (
                " Its approximate pronunciation is "
                f"{json.dumps(ai_pronunciation, ensure_ascii=False)}."
            )
        identity_lines.append(line)
    if human_name:
        line = f"The user's name is {json.dumps(human_name, ensure_ascii=False)}."
        if human_pronunciation:
            line += (
                " Its approximate pronunciation is "
                f"{json.dumps(human_pronunciation, ensure_ascii=False)}."
            )
        line += " Use the name naturally and sparingly when appropriate."
        identity_lines.append(line)
    if style == "concise":
        identity_lines.append(
            "Answer concisely and directly. Fully answer the question, then stop without filler."
        )
    elif style == "conversational":
        identity_lines.append(
            "Respond conversationally, connect each reply to what the user just said, and after "
            "answering ask one relevant follow-up question."
        )
    elif style == "empathetic":
        identity_lines.append(
            "Respond as a deeply caring and empathetic AI companion. Notice the user's feelings, "
            "validate them sincerely without overdoing it, and prioritize warmth, reassurance, and "
            "attentive support while remaining honest that you are AI."
        )

    system_parts: list[str] = []
    if identity_lines:
        system_parts.append(
            "Identity and conversation style:\n"
            + "\n".join(f"- {line}" for line in identity_lines)
        )
    system_parts.append(system_prompt.strip())
    system_content = "\n\n".join(part for part in system_parts if part)
    language = str(response_language or "en").strip().lower()
    language_instruction = (
        "请用简体中文回答。"
        if language.startswith("zh")
        else "Please respond in English."
    )
    model_user_text = f"{user_text.strip()}\n\n{language_instruction}"
    return [
        {"role": "system", "content": system_content},
        *[message.copy() for message in history],
        {"role": "user", "content": model_user_text},
    ]
