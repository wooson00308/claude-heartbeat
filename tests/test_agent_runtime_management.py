"""Device status, update planning and staged application for the runtime."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from heartbeat.agent_cli import run_agent_command
from heartbeat.agent_contract import AGENT_COMMANDS, RESERVED_COMMANDS, contract_description
from heartbeat.agent_store import AgentStore
from heartbeat.legacy_migration import (
    MANIFEST_NAME,
    activate_stable_launcher,
    write_runtime_manifest,
)
from heartbeat.runtime_management import UPDATE_STAGES, apply_update, plan_update
from heartbeat.service.base import RestartResult, ServiceStatus


def _version(version_dir: Path) -> Path:
    """Build one installed version whose manifest declares the directory's version.

    ``write_runtime_manifest`` stamps the version of the runtime doing the
    writing, which is the same for both fixtures here. The manifest is not
    hashed by itself, so restating the version keeps verification valid.
    """
    version_dir.mkdir(parents=True)
    executable = version_dir / "heartbeat"
    executable.write_bytes(b"standalone-heartbeat-" + version_dir.name.encode())
    executable.chmod(0o755)
    (version_dir / "_internal").mkdir()
    (version_dir / "_internal" / "runtime.bin").write_bytes(b"runtime")
    manifest_path = write_runtime_manifest(version_dir, target="linux-x86_64")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtimeVersion"] = version_dir.name
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return version_dir


@pytest.fixture
def device(tmp_path: Path):  # type: ignore[no-untyped-def]
    install_root = tmp_path / "install"
    current = _version(install_root / "versions" / "0.8.0")
    candidate = _version(install_root / "versions" / "0.9.0")
    launcher = activate_stable_launcher(install_root, current)
    store = AgentStore(tmp_path / "agent.sqlite3")
    return {
        "root": install_root, "current": current, "candidate": candidate,
        "launcher": launcher, "store": store, "tmp": tmp_path,
    }


def _service(**overrides) -> ServiceStatus:  # type: ignore[no-untyped-def]
    defaults = {
        "platform": "launchd", "result": "registered", "registered": True, "running": True,
        "label": "com.claude-heartbeat", "executable": "", "evidence": ("launch_agents_directory",),
    }
    return ServiceStatus(**{**defaults, **overrides})


def _reader(status: ServiceStatus):  # type: ignore[no-untyped-def]
    return lambda: status


def _run(device, **overrides) -> dict:  # type: ignore[no-untyped-def]
    run = {
        "runId": "run-1", "projectId": "project-one", "role": "developer", "provider": "claude",
        "state": "running", "targetId": "TASK-1", "leaseId": "lease-1",
    }
    device["store"].save_run({**run, **overrides})
    return run


def _digests(root: Path) -> dict[str, str]:
    """Hash every file a read must not touch.

    The SQLite file itself is excluded and checked separately: WAL bookkeeping
    rewrites its bytes even when no row changes, so the bytes would report a
    write that never happened. ``_rows`` is what proves the data stood still.
    """
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "agent.sqlite3" not in path.name
    }


def _rows(store: AgentStore) -> str:
    return json.dumps(store.list_runs(), sort_keys=True, ensure_ascii=False)


def test_plan_reports_impact_without_changing_anything(device) -> None:  # type: ignore[no-untyped-def]
    _run(device)
    _run(device, runId="run-2", projectId="project-two")
    _run(device, runId="run-3", projectId="project-two", state="succeeded")
    before, rows = _digests(device["tmp"]), _rows(device["store"])

    plan = plan_update(device["root"], device["candidate"], store=device["store"],
                       service_reader=_reader(_service(executable=str(device["launcher"]))))

    assert plan.result == "ready"
    assert plan.target_version == "0.9.0"
    assert plan.installed_version == "0.8.0"
    assert plan.launcher_switch_required is True
    assert plan.service_transition_required is True
    assert plan.active_runs == 2
    assert plan.projects == ("project-one", "project-two")
    assert _digests(device["tmp"]) == before
    assert _rows(device["store"]) == rows


def test_plan_carries_no_prompt_or_credential(device) -> None:  # type: ignore[no-untyped-def]
    secret = "role-prompt-secret-and-token"
    _run(device, detail=secret, reason=secret)

    plan = plan_update(device["root"], device["candidate"], store=device["store"],
                       service_reader=_reader(_service()))

    assert secret not in json.dumps(plan.to_dict(), ensure_ascii=False)
    assert plan.active_runs == 1


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (lambda device: device["store"].save_run({
            "runId": "late", "projectId": "project-late", "role": "developer",
            "provider": "claude", "state": "running", "targetId": "T", "leaseId": "L",
        }), "active runs"),
        (lambda device: (device["candidate"] / "_internal" / "extra.bin").write_bytes(b"x"), "manifest"),
    ],
)
def test_the_plan_identifier_is_a_fingerprint_of_what_it_assumed(device, change, field) -> None:  # type: ignore[no-untyped-def]
    reader = _reader(_service())
    first = plan_update(device["root"], device["candidate"], store=device["store"], service_reader=reader)

    change(device)
    second = plan_update(device["root"], device["candidate"], store=device["store"], service_reader=reader)

    assert first.plan_id != second.plan_id, field


def test_a_changed_service_identity_also_changes_the_plan(device) -> None:  # type: ignore[no-untyped-def]
    first = plan_update(device["root"], device["candidate"], store=device["store"],
                        service_reader=_reader(_service(label="com.claude-heartbeat")))
    second = plan_update(device["root"], device["candidate"], store=device["store"],
                         service_reader=_reader(_service(label="com.other-heartbeat")))

    assert first.plan_id != second.plan_id


def test_apply_needs_a_confirmation_even_with_no_active_run(device) -> None:  # type: ignore[no-untyped-def]
    plan = plan_update(device["root"], device["candidate"], store=device["store"],
                       service_reader=_reader(_service()))
    before, rows = _digests(device["tmp"]), _rows(device["store"])

    applied = apply_update(device["root"], device["candidate"], store=device["store"],
                           plan_id=plan.plan_id, confirmed=False, service_reader=_reader(_service()))

    assert plan.active_runs == 0
    assert applied.result == "confirmation_required"
    assert [stage.status for stage in applied.stages] == ["skipped"] * len(UPDATE_STAGES)
    assert _digests(device["tmp"]) == before
    assert _rows(device["store"]) == rows


def test_a_device_that_moved_after_the_plan_is_refused_before_stage_one(device) -> None:  # type: ignore[no-untyped-def]
    plan = plan_update(device["root"], device["candidate"], store=device["store"],
                       service_reader=_reader(_service()))
    _run(device, runId="appeared-later")
    before, rows = _digests(device["tmp"]), _rows(device["store"])

    applied = apply_update(device["root"], device["candidate"], store=device["store"],
                           plan_id=plan.plan_id, confirmed=True, service_reader=_reader(_service()))

    assert applied.result == "plan_stale"
    assert [stage.status for stage in applied.stages] == ["skipped"] * len(UPDATE_STAGES)
    assert applied.recovery_actions == ("read_a_new_plan",)
    assert _digests(device["tmp"]) == before
    assert _rows(device["store"]) == rows


def test_a_confirmed_plan_moves_the_launcher_and_reports_every_stage(device) -> None:  # type: ignore[no-untyped-def]
    status = _service()
    plan = plan_update(device["root"], device["candidate"], store=device["store"], service_reader=_reader(status))

    applied = apply_update(
        device["root"], device["candidate"], store=device["store"], plan_id=plan.plan_id, confirmed=True,
        service_reader=_reader(status), service_restart=lambda: RestartResult("ok", "restarted", "label"),
    )

    assert applied.result == "success"
    assert [stage.stage for stage in applied.stages] == list(UPDATE_STAGES)
    assert [stage.status for stage in applied.stages] == ["ok"] * len(UPDATE_STAGES)
    assert applied.runnable_version == "0.9.0"
    assert "0.9.0" in device["launcher"].read_text(encoding="utf-8")


def test_a_failed_verification_leaves_the_launcher_and_service_alone(device) -> None:  # type: ignore[no-untyped-def]
    status = _service()
    plan = plan_update(device["root"], device["candidate"], store=device["store"], service_reader=_reader(status))
    (device["candidate"] / "_internal" / "runtime.bin").write_bytes(b"tampered")
    launcher_before = device["launcher"].read_text(encoding="utf-8")
    restarts: list[str] = []

    applied = apply_update(
        device["root"], device["candidate"], store=device["store"], plan_id=plan.plan_id, confirmed=True,
        service_reader=_reader(status),
        service_restart=lambda: restarts.append("called") or RestartResult("ok", "restarted"),
    )

    # 지문이 manifest를 포함하므로 변조는 0단계에서 먼저 걸린다.
    assert applied.result == "plan_stale"
    assert device["launcher"].read_text(encoding="utf-8") == launcher_before
    assert restarts == []


def test_verification_failure_inside_a_matching_plan_is_a_stage_one_failure(device) -> None:  # type: ignore[no-untyped-def]
    status = _service()
    (device["candidate"] / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    plan = plan_update(device["root"], device["candidate"], store=device["store"], service_reader=_reader(status))
    launcher_before = device["launcher"].read_text(encoding="utf-8")

    applied = apply_update(
        device["root"], device["candidate"], store=device["store"], plan_id=plan.plan_id, confirmed=True,
        service_reader=_reader(status), service_restart=lambda: RestartResult("ok", "restarted"),
    )

    assert plan.result == "verification_failed"
    assert applied.result == "failure"
    assert applied.stages[0].status == "failed"
    assert [stage.status for stage in applied.stages[1:]] == ["skipped"] * (len(UPDATE_STAGES) - 1)
    assert applied.runnable_version == "0.8.0"
    assert device["launcher"].read_text(encoding="utf-8") == launcher_before


def test_a_failed_service_stage_is_not_reported_as_a_whole_success(device) -> None:  # type: ignore[no-untyped-def]
    status = _service()
    plan = plan_update(device["root"], device["candidate"], store=device["store"], service_reader=_reader(status))

    applied = apply_update(
        device["root"], device["candidate"], store=device["store"], plan_id=plan.plan_id, confirmed=True,
        service_reader=_reader(status),
        service_restart=lambda: RestartResult("failed", "restart-failed", "com.claude-heartbeat"),
    )

    assert applied.result == "partial_success"
    assert applied.stages[3].status == "failed"
    assert applied.runnable_version == "0.9.0"
    assert applied.recovery_actions == ("retry_service_transition",)
    assert "0.9.0" in device["launcher"].read_text(encoding="utf-8")


@pytest.mark.parametrize("platform", ["launchd", "systemd", "task_scheduler"])
def test_the_three_platforms_use_the_same_stages_and_meanings(device, platform) -> None:  # type: ignore[no-untyped-def]
    status = _service(platform=platform, detail={"native": f"{platform}-only"})
    plan = plan_update(device["root"], device["candidate"], store=device["store"], service_reader=_reader(status))

    applied = apply_update(
        device["root"], device["candidate"], store=device["store"], plan_id=plan.plan_id, confirmed=True,
        service_reader=_reader(status), service_restart=lambda: RestartResult("ok", "restarted"),
    )

    assert [stage.stage for stage in applied.stages] == list(UPDATE_STAGES)
    assert applied.result == "success"
    assert plan.to_dict()["service"]["platform"] == platform
    # 플랫폼 고유 정보는 진단 세부 정보에만 있고 계약 필드에는 새지 않는다.
    assert plan.to_dict()["service"]["detail"] == {"native": f"{platform}-only"}


def test_an_unregistered_service_skips_the_transition_instead_of_failing(device) -> None:  # type: ignore[no-untyped-def]
    status = _service(result="not_registered", registered=False, running=False)
    plan = plan_update(device["root"], device["candidate"], store=device["store"], service_reader=_reader(status))

    applied = apply_update(
        device["root"], device["candidate"], store=device["store"], plan_id=plan.plan_id, confirmed=True,
        service_reader=_reader(status),
        service_restart=lambda: pytest.fail("an unregistered service must not be restarted"),
    )

    assert plan.service_transition_required is False
    assert applied.result == "success"
    assert applied.stages[3].status == "skipped"


def test_an_unsupported_api_major_is_planned_but_not_ready(device) -> None:  # type: ignore[no-untyped-def]
    manifest_path = device["candidate"] / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["apiMajor"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    plan = plan_update(device["root"], device["candidate"], store=device["store"],
                       service_reader=_reader(_service()))

    assert plan.result in {"unsupported_version", "verification_failed"}
    assert plan.result != "ready"


# --- 계약이 알리는 명령 목록과 실제로 처리되는 명령 ---


def _invoke(command: str, payload: str = "", store: AgentStore | None = None):  # type: ignore[no-untyped-def]
    output = io.StringIO()
    code = run_agent_command(command, input_stream=io.StringIO(payload), output_stream=output, store=store)
    return code, json.loads(output.getvalue().splitlines()[0])


def test_the_command_list_is_defined_once_and_matches_what_runs(tmp_path: Path) -> None:
    store = AgentStore(tmp_path / "agent.sqlite3")
    description = contract_description()
    request = json.dumps({"apiVersion": "1", "requestId": "r"})

    handled = {
        command: _invoke(command, request, store)[1] for command in AGENT_COMMANDS if command != "contract.read"
    }
    _, unknown = _invoke("device.status", request, store)

    assert description["implementedCommands"] == list(AGENT_COMMANDS)
    assert description["reservedCommands"] == list(RESERVED_COMMANDS) == []
    # 목록에 있는 이름은 어떤 이유로든 미구현으로 거절되지 않는다.
    assert [
        command for command, response in handled.items()
        if response.get("error", {}).get("code") == "unsupported_command"
    ] == []
    assert unknown["error"]["code"] == "unsupported_command"
    assert _invoke("contract.read")[1]["data"]["implementedCommands"] == list(AGENT_COMMANDS)


def test_the_device_query_is_not_duplicated_as_an_agent_command() -> None:
    """같은 사실을 돌려주는 조회는 runtime 명령군 하나뿐이어야 한다."""
    description = contract_description()

    assert "runtime.inspect" in description["runtimeCommands"]
    assert [command for command in AGENT_COMMANDS if "inspect" in command or "device" in command] == []


def test_update_commands_travel_through_the_json_envelope(tmp_path: Path, device) -> None:  # type: ignore[no-untyped-def]
    store = device["store"]
    request = json.dumps({
        "apiVersion": "1", "requestId": "r",
        "installRoot": str(device["root"]), "versionDir": str(device["candidate"]),
    })

    code, planned = _invoke("update.plan", request, store)
    unconfirmed = _invoke("update.apply", json.dumps({
        "apiVersion": "1", "requestId": "r", "installRoot": str(device["root"]),
        "versionDir": str(device["candidate"]), "planId": planned["data"]["planId"],
    }), store)[1]

    assert code == 0
    assert planned["outcome"] == "success"
    assert planned["data"]["targetVersion"] == "0.9.0"
    assert planned["data"]["stages"] == list(UPDATE_STAGES)
    assert unconfirmed["outcome"] == "failure"
    assert unconfirmed["data"]["result"] == "confirmation_required"


PLAN_FIELDS = {
    "planId", "result", "targetVersion", "target", "checkedAt", "manifestVerified",
    "launcherSwitchRequired", "serviceTransitionRequired", "recoverableOnFailure",
    "installedVersion", "runningVersion", "activeRuns", "projects", "service", "detail", "stages",
}
SERVICE_FIELDS = {
    "platform", "result", "registered", "running", "label", "executable",
    "recoverable", "checkedAt", "evidence", "detail",
}


@pytest.mark.parametrize(
    ("status", "expected_running", "expected_transition"),
    [
        (_service(result="not_registered", registered=False, running=False), False, False),
        (_service(result="executable_missing", running=None), None, True),
        (_service(result="ambiguous_registration", running=None), None, True),
        (_service(result="permission_denied", registered=None, running=None), None, False),
        (_service(result="unsupported_platform", registered=None, running=None), None, False),
        (_service(), True, True),
    ],
)
def test_each_service_situation_stays_distinct_inside_the_plan(
    device, status, expected_running, expected_transition
) -> None:  # type: ignore[no-untyped-def]
    plan = plan_update(device["root"], device["candidate"], store=device["store"],
                       service_reader=_reader(status))

    described = plan.to_dict()["service"]
    assert set(described) == SERVICE_FIELDS
    assert described["result"] == status.result
    assert described["running"] is expected_running
    assert plan.service_transition_required is expected_transition
    # 확인하지 못한 상태에서 실행 중 버전을 만들어내지 않는다.
    assert plan.running_version is None or status.running is True


def test_the_plan_answers_every_fact_the_app_would_otherwise_parse(device) -> None:  # type: ignore[no-untyped-def]
    """앱이 SQLite·plist·unit·schtasks 출력을 직접 읽지 않아도 되는지 확인한다."""
    _run(device)

    described = plan_update(device["root"], device["candidate"], store=device["store"],
                            service_reader=_reader(_service(executable=str(device["launcher"])))).to_dict()

    assert set(described) == PLAN_FIELDS
    assert described["installedVersion"] == "0.8.0"
    assert described["targetVersion"] == "0.9.0"
    assert described["activeRuns"] == 1 and described["projects"] == ["project-one"]
    assert described["service"]["label"] and described["service"]["executable"]
    assert json.loads(json.dumps(described)) == described
