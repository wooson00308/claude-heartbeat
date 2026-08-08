"""Stable provider descriptors shared by the agent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProviderName = Literal["claude", "codex"]
SUPPORTED_PROVIDERS = frozenset({"claude", "codex"})


@dataclass(frozen=True)
class ProviderDescriptor:
    """A provider name and the executable it will eventually use.

    Keeping this descriptive prevents configuration code from starting a
    provider process merely to decide whether its value is valid.
    """

    name: ProviderName
    executable: str
