from __future__ import annotations

Message = dict[str, str]

_PERSONAS = {"default", "concise", "conversational"}


def sanitize_person_name(value: str | None) -> str:
    name = " ".join(str(value or "").split()).strip()
    if not name or len(name) > 40 or not name[0].isalpha():
        return ""
    if not all(character.isalpha() or character in " -'’" for character in name):
        return ""
    return name


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
    user_name: str = "",
    persona: str = "default",
    response_language: str = "en",
) -> list[Message]:
    """Build a stable system prompt plus the exact chronological conversation."""
    identity_lines: list[str] = []
    ai_name = sanitize_person_name(assistant_name)
    human_name = sanitize_person_name(user_name)
    style = normalize_persona(persona)
    if ai_name:
        identity_lines.append(
            f'Your name is "{ai_name}". Do not identify yourself by any company or model name.'
        )
    if human_name:
        identity_lines.append(
            f'The user\'s name is "{human_name}". Use it naturally and sparingly when appropriate.'
        )
    if style == "concise":
        identity_lines.append(
            "Answer concisely and directly. Fully answer the question, then stop without filler."
        )
    elif style == "conversational":
        identity_lines.append(
            "Respond conversationally, connect each reply to what the user just said, and after "
            "answering ask one relevant follow-up question."
        )

    system_parts: list[str] = []
    if identity_lines:
        system_parts.append(
            "Identity and conversation style:\n"
            + "\n".join(f"- {line}" for line in identity_lines)
        )
    system_parts.append(system_prompt.strip())
    if str(response_language or "en").strip().lower().startswith("zh"):
        system_parts.append("请你用简体中文回答。")
    else:
        system_parts.append("Please respond in English.")
    system_content = "\n\n".join(part for part in system_parts if part)
    return [
        {"role": "system", "content": system_content},
        *[message.copy() for message in history],
        {"role": "user", "content": user_text.strip()},
    ]
