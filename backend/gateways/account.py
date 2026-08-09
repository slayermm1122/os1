from __future__ import annotations

from typing import Protocol


class AccountGateway(Protocol):
    provider: str

    async def summary(self) -> dict[str, object]: ...
