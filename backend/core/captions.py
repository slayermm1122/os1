from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CaptionAlignment:
    """Provider-neutral character timing used by the caption transport."""

    chars: tuple[str, ...]
    char_start_times_ms: tuple[float, ...]
    char_durations_ms: tuple[float, ...]


def caption_payload(alignment: CaptionAlignment) -> dict[str, list[str] | list[float]]:
    """Serialize caption timing without coupling the orchestrator to a TTS provider."""

    return {
        "chars": list(alignment.chars),
        "char_start_times_ms": list(alignment.char_start_times_ms),
        "char_durations_ms": list(alignment.char_durations_ms),
    }
