from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    ok: bool
    latency_ms: float
    code: str
    message: str
    upstream_status: int | None = None
    provider_detail: dict[str, str] = field(default_factory=dict)

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
    def __init__(
        self,
        *,
        brain: ProviderProbe,
        stt: ProviderProbe,
        tts: ProviderProbe | Iterable[ProviderProbe],
        default_tts_provider: str | None = None,
    ) -> None:
        self.brain = brain
        self.stt = stt
        probes = [tts] if hasattr(tts, "check") else list(tts)
        self.tts = {probe.provider.strip().lower(): probe for probe in probes}
        if not self.tts:
            raise ValueError("At least one TTS connectivity probe is required.")
        self.default_tts_provider = (
            default_tts_provider or next(iter(self.tts))
        ).strip().lower()
        if self.default_tts_provider not in self.tts:
            raise ValueError(
                f"Default TTS connectivity provider is not registered: {self.default_tts_provider}"
            )

    async def check(
        self,
        *,
        brain_api_key: str | None,
        stt_api_key: str | None,
        tts_api_key: str | None,
        tts_voice_id: str | None,
        tts_provider: str | None = None,
    ) -> dict[str, object]:
        selected = (tts_provider or self.default_tts_provider).strip().lower()
        tts_probe = self.tts.get(selected)
        if tts_probe is None:
            tts = ProviderStatus(
                provider=selected or "unknown",
                ok=False,
                latency_ms=0,
                code="unsupported_provider",
                message="The selected voice provider is not available.",
            )
            brain, stt = await asyncio.gather(
                self.brain.check(api_key=brain_api_key),
                self.stt.check(api_key=stt_api_key),
            )
            return _report(brain, stt, tts)
        brain, stt, tts = await asyncio.gather(
            self.brain.check(api_key=brain_api_key),
            self.stt.check(api_key=stt_api_key),
            tts_probe.check(api_key=tts_api_key, resource_id=tts_voice_id),
        )
        return _report(brain, stt, tts)


def _report(
    brain: ProviderStatus,
    stt: ProviderStatus,
    tts: ProviderStatus,
) -> dict[str, object]:
    return {
        "ready": brain.ok and stt.ok and tts.ok,
        "brain": brain.payload(),
        "stt": stt.payload(),
        "tts": tts.payload(),
        "voice": tts.payload(),  # Backward-compatible alias for older clients.
    }
