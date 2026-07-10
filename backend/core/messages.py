from __future__ import annotations


Message = dict[str, str]


def build_messages(
    *,
    system_prompt: str,
    user_text: str,
    history: list[Message],
    knowledge_context: str = "",
) -> list[Message]:
    system_content = system_prompt.strip()
    if knowledge_context:
        system_content = (
            f"{system_content}\n\n"
            "下面是关键词搜索命中的参考资料片段。只在相关时使用；不要编造片段之外的事实。\n"
            f"{knowledge_context}"
        )

    return [
        {"role": "system", "content": system_content},
        *history,
        {"role": "user", "content": user_text},
    ]
