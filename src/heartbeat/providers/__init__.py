"""Provider-neutral agent runtime interfaces.

Provider process execution is added by the next runtime task.  This package
already owns the names that may be persisted in the agent configuration so
configuration validation does not depend on a provider executable.
"""

from heartbeat.providers.base import ProviderDescriptor, ProviderName, SUPPORTED_PROVIDERS
from heartbeat.providers.claude import ClaudeProvider
from heartbeat.providers.codex import CodexProvider
from heartbeat.providers.lifecycle import (
    PROVIDER_LIFECYCLE_CONTRACT_VERSION,
    RESERVATION_CONTRACT_VERSION,
    LifecycleFailure,
    Reservation,
    RunConclusion,
    RunRecovery,
    cancel_run,
    conclude_run,
    read_reservation,
    recover_run,
    start_reserved_run,
)
from heartbeat.providers.process import (
    PROVIDER_RUN_CONTRACT_VERSION,
    AgentProvider,
    ProviderCancellation,
    ProviderDiagnostic,
    ProviderEvent,
    ProviderEventBatch,
    ProviderExecutionRequest,
    ProviderExecutionResult,
    ProviderRunHandle,
    ProviderStartFailure,
)

__all__ = [
    "PROVIDER_LIFECYCLE_CONTRACT_VERSION",
    "PROVIDER_RUN_CONTRACT_VERSION",
    "RESERVATION_CONTRACT_VERSION",
    "AgentProvider",
    "ClaudeProvider",
    "CodexProvider",
    "LifecycleFailure",
    "ProviderCancellation",
    "ProviderDescriptor",
    "ProviderDiagnostic",
    "ProviderEvent",
    "ProviderEventBatch",
    "ProviderExecutionRequest",
    "ProviderExecutionResult",
    "ProviderName",
    "ProviderRunHandle",
    "ProviderStartFailure",
    "Reservation",
    "RunConclusion",
    "RunRecovery",
    "SUPPORTED_PROVIDERS",
    "cancel_run",
    "conclude_run",
    "read_reservation",
    "recover_run",
    "start_reserved_run",
]
