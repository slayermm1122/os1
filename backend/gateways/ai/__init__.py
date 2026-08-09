"""Provider-neutral AI gateway surface.

The legacy ``gateways.llm`` package remains as a compatibility import for v0.02
integrations. New composition code should import from this package.
"""

from ..llm.base import AIGateway, LLMRequest as AIRequest
from ..llm.base import LLMStreamEvent as AIStreamEvent
from ..llm.base import LLMUsage as AIUsage
from ..llm.xai import XAILLMGateway as XAIGateway

__all__ = [
    "AIGateway", "AIRequest", "AIStreamEvent", "AIUsage", "XAIGateway",
]
