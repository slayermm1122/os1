from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .core.connectivity import ConnectivityService
from .core.local_settings import LocalSettingsService
from .core.live_sessions import LiveSessionRegistry
from .core.orchestrator import TurnOrchestrator
from .core.rate_limit import SlidingWindowRateLimiter
from .gateways.account import AccountGateway
from .gateways.pronunciation import ElevenLabsPronunciationGateway
from .gateways.tts.catalog import VoiceCatalog
from .telemetry import SQLiteTelemetryRecorder


@dataclass(frozen=True)
class ApplicationServices:
    settings: Settings
    orchestrator: TurnOrchestrator
    telemetry: SQLiteTelemetryRecorder
    rate_limiter: SlidingWindowRateLimiter
    connectivity: ConnectivityService | None = None
    voice_catalog: VoiceCatalog | None = None
    account: AccountGateway | None = None
    local_settings: LocalSettingsService | None = None
    pronunciation: ElevenLabsPronunciationGateway | None = None
    live_sessions: LiveSessionRegistry | None = None
