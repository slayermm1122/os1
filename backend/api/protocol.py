from __future__ import annotations

import json

from ..core.orchestrator import PipelineEvent


def sse(event: PipelineEvent) -> str:
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


def websocket_message(event: PipelineEvent) -> str:
    return json.dumps({"event": event.event, "data": event.data}, ensure_ascii=False)
