"""Device-level update planning and staged application for the installed runtime.

The app never touches a plist, a unit file, a launcher or the runtime database
itself.  It asks here, and gets three things: what the device currently is, what
an update would affect, and — after its own confirmation — what each stage of
that update actually did.

Planning is read-only and says so by construction: it verifies, reads and
counts, and every write lives in ``apply_update``.  The two are held together by
a fingerprint of everything the plan assumed, so a device that moved between the
two calls is refused before the first byte is written.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from heartbeat.agent_contract import API_VERSION
from heartbeat.agent_store import AgentStore
from heartbeat.legacy_migration import (
    RuntimeIntegrityError,
    activate_stable_launcher,
    installed_runtime,
    runtime_target,
    runtime_version_of,
    verify_runtime_manifest,
)
from heartbeat.service import inspect_service, restart_service
from heartbeat.service.base import ServiceStatus, checked_at

# 적용 결과의 단계 이름과 순서. 세 운영체제가 같은 이름을 쓰고 앱은 이 이름을
# 합치거나 새로 만들지 않는다.
UPDATE_STAGES = (
    "manifest_verification",
    "version_install",
    "launcher_switch",
    "service_transition",
    "running_version_check",
)


@dataclass(frozen=True)
class StageResult:
    """One update stage and what it actually did."""

    stage: str
    status: str  # ok | failed | skipped
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class UpdatePlan:
    """What an update would change, and the fingerprint it assumed."""

    plan_id: str
    result: str  # ready | verification_failed | unsupported_version | candidate_missing
    target_version: str | None
    target: str
    checked_at: str
    manifest_verified: bool
    launcher_switch_required: bool
    service_transition_required: bool
    recoverable_on_failure: bool
    installed_version: str | None
    running_version: str | None
    active_runs: int
    projects: tuple[str, ...]
    service: ServiceStatus
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "planId": self.plan_id,
            "result": self.result,
            "targetVersion": self.target_version,
            "target": self.target,
            "checkedAt": self.checked_at,
            "manifestVerified": self.manifest_verified,
            "launcherSwitchRequired": self.launcher_switch_required,
            "serviceTransitionRequired": self.service_transition_required,
            "recoverableOnFailure": self.recoverable_on_failure,
            "installedVersion": self.installed_version,
            "runningVersion": self.running_version,
            "activeRuns": self.active_runs,
            "projects": list(self.projects),
            "service": self.service.to_dict(),
            "detail": self.detail,
            "stages": list(UPDATE_STAGES),
        }


@dataclass(frozen=True)
class UpdateApplication:
    """The staged outcome of one apply, never collapsed into one boolean."""

    plan_id: str
    result: str  # success | partial_success | failure | plan_stale | confirmation_required
    checked_at: str
    stages: tuple[StageResult, ...]
    runnable_version: str | None = None
    recovery_actions: tuple[str, ...] = ()
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "planId": self.plan_id,
            "result": self.result,
            "checkedAt": self.checked_at,
            "stages": [stage.to_dict() for stage in self.stages],
            "runnableVersion": self.runnable_version,
            "recoveryActions": list(self.recovery_actions),
            "detail": self.detail,
        }


@dataclass
class DeviceFacts:
    """Everything a plan reads once, so the plan and its fingerprint agree."""

    installed: dict[str, Any]
    service: ServiceStatus
    running_version: str | None
    active_runs: int
    projects: tuple[str, ...]
    manifest: dict[str, Any] | None = None
    manifest_error: str | None = None
    candidate: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Hash exactly what the plan assumed, and nothing that drifts on its own.

        The checked-at timestamp is deliberately outside: it changes on every
        read and would make every plan stale.
        """
        payload = json.dumps(
            {
                "candidate": self.candidate,
                "manifest": self.manifest,
                "manifestError": self.manifest_error,
                "installedVersion": self.installed.get("installedVersion"),
                "installResult": self.installed.get("result"),
                "runningVersion": self.running_version,
                "service": [
                    self.service.result, self.service.label, self.service.executable,
                    self.service.registered, self.service.running,
                ],
                "activeRuns": self.active_runs,
                "projects": list(self.projects),
                **self.extra,
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def read_device_facts(
    install_root: Path,
    version_dir: Path,
    *,
    store: AgentStore,
    service_reader: Callable[[], ServiceStatus] = inspect_service,
) -> DeviceFacts:
    """Read the device once. Nothing here writes, and nothing is guessed."""
    installed = installed_runtime(install_root)
    service = service_reader()
    running_version = (
        runtime_version_of(Path(service.executable))
        if service.running and service.executable
        else None
    )
    summary = store.active_run_summary()
    facts = DeviceFacts(
        installed=installed,
        service=service,
        running_version=running_version,
        active_runs=summary["activeRuns"],
        projects=tuple(summary["projects"]),
        candidate=version_dir.name,
    )
    try:
        facts.manifest = verify_runtime_manifest(version_dir)
    except RuntimeIntegrityError as error:
        facts.manifest_error = str(error)
    return facts


def plan_update(
    install_root: Path,
    version_dir: Path,
    *,
    store: AgentStore,
    service_reader: Callable[[], ServiceStatus] = inspect_service,
) -> UpdatePlan:
    """Describe the update without performing any part of it."""
    facts = read_device_facts(install_root, version_dir, store=store, service_reader=service_reader)
    manifest = facts.manifest
    if manifest is None:
        result = "candidate_missing" if not version_dir.is_dir() else "verification_failed"
        target_version: str | None = None
    elif manifest.get("apiMajor") != int(API_VERSION):
        result, target_version = "unsupported_version", manifest.get("runtimeVersion")
    else:
        result, target_version = "ready", manifest.get("runtimeVersion")

    return UpdatePlan(
        plan_id=facts.fingerprint(),
        result=result,
        target_version=target_version,
        target=(manifest or {}).get("target") or runtime_target(),
        checked_at=checked_at(),
        manifest_verified=manifest is not None,
        launcher_switch_required=facts.installed.get("installedVersion") != target_version,
        service_transition_required=facts.service.registered is True,
        # 되돌릴 곳이 있으려면 지금 설치본을 읽을 수 있어야 한다.
        recoverable_on_failure=facts.installed.get("result") in {"installed", "unsupported_version"},
        installed_version=facts.installed.get("installedVersion"),
        running_version=facts.running_version,
        active_runs=facts.active_runs,
        projects=facts.projects,
        service=facts.service,
        detail=facts.manifest_error,
    )


def apply_update(
    install_root: Path,
    version_dir: Path,
    *,
    store: AgentStore,
    plan_id: str,
    confirmed: bool,
    service_reader: Callable[[], ServiceStatus] = inspect_service,
    service_restart: Callable[[], Any] = restart_service,
) -> UpdateApplication:
    """Apply one confirmed plan, stage by stage, and report each stage.

    The device is read again first.  A fingerprint that no longer matches means
    the world moved under the plan the user confirmed, so nothing is written and
    a new plan is required.
    """
    skipped = tuple(StageResult(stage, "skipped") for stage in UPDATE_STAGES)
    if not confirmed:
        return UpdateApplication(
            plan_id=plan_id, result="confirmation_required", checked_at=checked_at(),
            stages=skipped, detail="an explicit confirmation is required",
        )

    facts = read_device_facts(install_root, version_dir, store=store, service_reader=service_reader)
    runnable = facts.installed.get("installedVersion")
    if facts.fingerprint() != plan_id:
        return UpdateApplication(
            plan_id=plan_id, result="plan_stale", checked_at=checked_at(), stages=skipped,
            runnable_version=runnable, recovery_actions=("read_a_new_plan",),
            detail="the device changed after the plan was confirmed",
        )

    stages: list[StageResult] = []
    if facts.manifest is None:
        stages.append(StageResult("manifest_verification", "failed", facts.manifest_error))
        return _stopped_before_writing(plan_id, stages, runnable)
    stages.append(StageResult("manifest_verification", "ok", facts.manifest.get("runtimeVersion")))

    if version_dir.parent != (install_root / "versions").resolve() and version_dir.parent.name != "versions":
        stages.append(StageResult("version_install", "failed", "version directory is not installed under the root"))
        return _stopped_before_writing(plan_id, stages, runnable)
    stages.append(StageResult("version_install", "ok", version_dir.name))

    try:
        activate_stable_launcher(install_root, version_dir)
    except (RuntimeIntegrityError, OSError) as error:
        stages.append(StageResult("launcher_switch", "failed", str(error)))
        return _stopped_before_writing(plan_id, stages, runnable)
    stages.append(StageResult("launcher_switch", "ok"))
    switched = facts.manifest.get("runtimeVersion")

    if not facts.service.registered:
        stages.append(StageResult("service_transition", "skipped", "no registration to restart"))
    else:
        restarted = service_restart()
        status = getattr(restarted, "status", "failed")
        stages.append(StageResult("service_transition", "ok" if status == "ok" else status,
                                  getattr(restarted, "detail", None)))

    after = installed_runtime(install_root)
    if after.get("installedVersion") == switched:
        stages.append(StageResult("running_version_check", "ok", switched))
    else:
        stages.append(StageResult("running_version_check", "failed", after.get("result")))

    failed = [stage for stage in stages if stage.status == "failed"]
    if not failed:
        keep = {version_dir.name}
        if runnable:
            keep.add(str(runnable))
        _prune_old_versions(install_root, keep)
        return UpdateApplication(plan_id=plan_id, result="success", checked_at=checked_at(),
                                 stages=tuple(stages), runnable_version=switched)
    return UpdateApplication(
        plan_id=plan_id, result="partial_success", checked_at=checked_at(), stages=tuple(stages),
        # launcher는 이미 새 버전을 가리키므로 지금 실행 가능한 버전은 그쪽이다.
        runnable_version=switched,
        recovery_actions=tuple(f"retry_{stage.stage}" for stage in failed),
        detail="the launcher moved but a later stage did not finish",
    )


def _prune_old_versions(install_root: Path, keep: set[str]) -> None:
    """Remove version directories the retention policy no longer needs.

    Every runtime release used to stay on disk forever — about 90MB each — so
    a user machine grew without bound (observed 2026-08-13: five generations).
    Only the newly activated version and the one it replaced (the rollback
    target) remain. This runs only after a fully successful update, and a
    directory that fails to delete is left for the next update to retry —
    retention is hygiene, never a reason to fail an update that already worked.
    """
    versions_root = install_root / "versions"
    try:
        entries = [path for path in versions_root.iterdir() if path.is_dir()]
    except OSError:
        return
    for path in entries:
        if path.name in keep:
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            continue


def _stopped_before_writing(plan_id: str, stages: list[StageResult], runnable: str | None) -> UpdateApplication:
    """A failure before the launcher moved leaves the device exactly as it was."""
    done = {stage.stage for stage in stages}
    stages.extend(StageResult(stage, "skipped") for stage in UPDATE_STAGES if stage not in done)
    return UpdateApplication(
        plan_id=plan_id, result="failure", checked_at=checked_at(), stages=tuple(stages),
        runnable_version=runnable, recovery_actions=("keep_current_version",),
        detail="nothing was changed",
    )
