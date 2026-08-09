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
        brain: ProviderProbe | Iterable[ProviderProbe],
        stt: ProviderProbe,
        tts: ProviderProbe | Iterable[ProviderProbe],
        default_brain_provider: str | None = None,
        default_tts_provider: str | None = None,
    ) -> None:
        brain_probes = [brain] if hasattr(brain, "check") else list(brain)
        self.brains = {probe.provider.strip().lower(): probe for probe in brain_probes}
        if not self.brains:
            raise ValueError("At least one brain connectivity probe is required.")
        self.default_brain_provider = (
            default_brain_provider or next(iter(self.brains))
        ).strip().lower()
        if self.default_brain_provider not in self.brains:
            raise ValueError(
                f"Default brain connectivity provider is not registered: {self.default_brain_provider}"
            )
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
        brain_provider: str | None = None,
        tts_provider: str | None = None,
    ) -> dict[str, object]:
        selected_brain = (brain_provider or self.default_brain_provider).strip().lower()
        brain_probe = self.brains.get(selected_brain)
        selected = (tts_provider or self.default_tts_provider).strip().lower()
        tts_probe = self.tts.get(selected)
        if brain_probe is None:
            brain = ProviderStatus(
                provider=selected_brain or "unknown",
                ok=False,
                latency_ms=0,
                code="unsupported_provider",
                message="The selected brain provider is not available.",
            )
            stt, tts = await asyncio.gather(
                self.stt.check(api_key=stt_api_key),
                tts_probe.check(api_key=tts_api_key, resource_id=tts_voice_id) if tts_probe else _unsupported_tts(selected),
            )
            return _report(brain, stt, tts)
        if tts_probe is None:
            tts = ProviderStatus(
                provider=selected or "unknown",
                ok=False,
                latency_ms=0,
                code="unsupported_provider",
                message="The selected voice provider is not available.",
            )
            brain, stt = await asyncio.gather(
                brain_probe.check(api_key=brain_api_key),
                self.stt.check(api_key=stt_api_key),
            )
            return _report(brain, stt, tts)
        brain, stt, tts = await asyncio.gather(
            brain_probe.check(api_key=brain_api_key),
            self.stt.check(api_key=stt_api_key),
            tts_probe.check(api_key=tts_api_key, resource_id=tts_voice_id),
        )
        return _report(brain, stt, tts)


async def _unsupported_tts(provider: str) -> ProviderStatus:
    return ProviderStatus(
        provider=provider or "unknown",
        ok=False,
        latency_ms=0,
        code="unsupported_provider",
        message="The selected voice provider is not available.",
    )


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
