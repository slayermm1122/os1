from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .core.connectivity import ConnectivityService
from .core.orchestrator import TurnOrchestrator
from .core.rate_limit import SlidingWindowRateLimiter
from .gateways.knowledge import KnowledgeBrowser, KnowledgeGateway
from .telemetry import SQLiteTelemetryRecorder


@dataclass(frozen=True)
class ApplicationServices:
    settings: Settings
    orchestrator: TurnOrchestrator
    knowledge: KnowledgeGateway
    telemetry: SQLiteTelemetryRecorder
    rate_limiter: SlidingWindowRateLimiter
    connectivity: ConnectivityService | None = None
    knowledge_browser: KnowledgeBrowser | None = None
