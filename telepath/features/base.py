from __future__ import annotations

from typing import Any, Protocol


class Feature(Protocol):
    name: str

    def can_handle(self, event: Any) -> bool: ...

    async def handle(self, event: Any, context: Any) -> Any: ...


class FeatureRegistry:
    def __init__(self, features: list[Feature]):
        self.features = features

    async def dispatch(self, event: Any, context: Any) -> Any:
        for feature in self.features:
            if feature.can_handle(event):
                return await feature.handle(event, context)
        return None
