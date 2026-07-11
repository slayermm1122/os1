from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    ok: bool
    latency_ms: float
    code: str
    message: str

    def payload(self) -> dict[str, object]:
        return asdict(self)


class ProviderProbe(Protocol):
    provider: str

    async def check(
        self,
        *,
        api_key: str | None,
        resource_id: str | None = None,
    ) -> ProviderStatus: ...


class ConnectivityService:
    def __init__(self, *, brain: ProviderProbe, voice: ProviderProbe) -> None:
        self.brain = brain
        self.voice = voice

    async def check(
        self,
        *,
        brain_api_key: str | None,
        voice_api_key: str | None,
        voice_id: str | None,
    ) -> dict[str, object]:
        brain, voice = await asyncio.gather(
            self.brain.check(api_key=brain_api_key),
            self.voice.check(api_key=voice_api_key, resource_id=voice_id),
        )
        return {
            "ready": brain.ok and voice.ok,
            "brain": brain.payload(),
            "voice": voice.payload(),
        }
