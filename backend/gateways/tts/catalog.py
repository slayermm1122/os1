from __future__ import annotations

from typing import Protocol


class VoiceCatalog(Protocol):
    provider: str

    async def list_voices(self, *, api_key: str | None = None) -> list[dict[str, object]]: ...

    async def preview(
        self,
        voice_id: str,
        *,
        api_key: str | None = None,
    ) -> tuple[bytes, str]: ...
