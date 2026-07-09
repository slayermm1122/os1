from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from .config import Settings


Message = dict[str, str]


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _require_api_key(self, api_key: str | None = None) -> str:
        resolved_api_key = (api_key or self.settings.llm_api_key).strip()
        if not resolved_api_key:
            raise RuntimeError("LLM_API_KEY, DEEPSEEK_API_KEY, or XAI_API_KEY is not configured.")
        return resolved_api_key

    async def stream_reply(
        self,
        messages: list[Message],
        *,
        api_key: str | None = None,
    ) -> AsyncIterator[str]:
        resolved_api_key = self._require_api_key(api_key)
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
            "stream": True,
        }
        if self.settings.llm_reasoning_effort:
            payload["reasoning_effort"] = self.settings.llm_reasoning_effort

        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=self.settings.upstream_read_timeout_seconds,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                self.settings.llm_chat_url,
                headers={
                    "Authorization": f"Bearer {resolved_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                response.raise_for_status()
                async with asyncio.timeout(self.settings.upstream_stream_timeout_seconds):
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue

                        data = line.removeprefix("data:").strip()
                        if data == "[DONE]":
                            break

                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue

                        for choice in event.get("choices", []):
                            delta = choice.get("delta") or {}
                            content = delta.get("content")
                            if content:
                                yield content


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
