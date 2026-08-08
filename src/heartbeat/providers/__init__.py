"""Provider-neutral agent runtime interfaces.

Provider process execution is added by the next runtime task.  This package
already owns the names that may be persisted in the agent configuration so
configuration validation does not depend on a provider executable.
"""

from heartbeat.providers.base import ProviderDescriptor, ProviderName, SUPPORTED_PROVIDERS
from heartbeat.providers.claude import ClaudeProvider
from heartbeat.providers.codex import CodexProvider
from heartbeat.providers.process import (
    AgentProvider,
    ProviderDiagnostic,
    ProviderEvent,
    ProviderExecutionRequest,
    ProviderExecutionResult,
)

__all__ = [
    "AgentProvider",
    "ClaudeProvider",
    "CodexProvider",
    "ProviderDescriptor",
    "ProviderDiagnostic",
    "ProviderEvent",
    "ProviderExecutionRequest",
    "ProviderExecutionResult",
    "ProviderName",
    "SUPPORTED_PROVIDERS",
]
