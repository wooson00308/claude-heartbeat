"""SQLite persistence owned by the agent runtime, not by GUI consumers."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from heartbeat.agent_contract import AgentConfiguration

AGENT_SCHEMA_VERSION = 5
DEFAULT_AGENT_DB = Path.home() / ".claude" / "heartbeat" / "agent-runtime.sqlite3"
ACTIVE_RUN_STATES = frozenset({"reserved", "queued", "running", "paused"})
ESTIMATED_MEMORY_PER_AGENT_BYTES = 1536 * 1024 * 1024
MINIMUM_RESERVED_MEMORY_BYTES = 2 * 1024 * 1024 * 1024


def _dump(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def default_agent_database_path() -> Path:
    """Resolve the runtime-owned database path when a process starts.

    Tests and packaged runtime launchers can isolate the state by setting
    ``HEARTBEAT_AGENT_HOME``. Detached workers receive the exact runtime-owned
    database through ``HEARTBEAT_AGENT_DATABASE`` so they cannot drift homes.
    """
    database_override = os.environ.get("HEARTBEAT_AGENT_DATABASE")
    if database_override:
        return Path(database_override)
    override = os.environ.get("HEARTBEAT_AGENT_HOME")
    return Path(override) / "agent-runtime.sqlite3" if override else DEFAULT_AGENT_DB


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def recommend_device_parallelism(logical_cpu_count: int, total_memory_bytes: int | None) -> dict:
    """Return a transparent first-run suggestion, never a hard limit.

    One logical CPU is left for the desktop and OS. Memory keeps the larger of
    2 GiB or 25% aside and budgets 1.5 GiB for each CLI process tree. The
    smaller allowance becomes the recommendation; users may override it.
    """
    logical_cpu_count = max(1, logical_cpu_count)
    cpu_allowance = max(1, logical_cpu_count - 1)
    reserved_memory_bytes: int | None = None
    memory_allowance: int | None = None
    if total_memory_bytes is not None and total_memory_bytes > 0:
        reserved_memory_bytes = max(MINIMUM_RESERVED_MEMORY_BYTES, total_memory_bytes // 4)
        usable = max(ESTIMATED_MEMORY_PER_AGENT_BYTES, total_memory_bytes - reserved_memory_bytes)
        memory_allowance = max(1, usable // ESTIMATED_MEMORY_PER_AGENT_BYTES)
    recommended = min(cpu_allowance, memory_allowance) if memory_allowance is not None else cpu_allowance
    return {
        "recommendedMaxParallel": max(1, recommended),
        "logicalCpuCount": logical_cpu_count,
        "totalMemoryBytes": total_memory_bytes,
        "reservedMemoryBytes": reserved_memory_bytes,
        "estimatedMemoryPerAgentBytes": ESTIMATED_MEMORY_PER_AGENT_BYTES,
        "cpuAllowance": cpu_allowance,
        "memoryAllowance": memory_allowance,
    }


def detected_device_recommendation() -> dict:
    """Read cross-platform hardware facts without making them an execution gate."""
    total_memory_bytes: int | None = None
    try:
        import psutil

        total_memory_bytes = int(psutil.virtual_memory().total)
    except (ImportError, AttributeError, OSError, ValueError):
        pass
    return recommend_device_parallelism(os.cpu_count() or 1, total_memory_bytes)


class AgentStore:
    """Small transactional store with project-id as the isolation boundary."""

    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path or default_agent_database_path()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 10
        connection: sqlite3.Connection | None = None
        while connection is None:
            candidate = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
            try:
                # WAL 전환 자체도 첫 동시 기동에서는 배타 잠금을 잡는다. 이 짧은 재시도는
                # 여러 제어 CLI가 동시에 처음 DB를 여는 경우를 직렬화한다.
                candidate.execute("PRAGMA busy_timeout=10000")
                candidate.execute("PRAGMA journal_mode=WAL")
                candidate.execute("PRAGMA foreign_keys=ON")
                self._initialize(candidate)
                connection = candidate
            except sqlite3.OperationalError as error:
                candidate.close()
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        try:
            assert connection is not None
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        previous_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_configurations (
                project_id TEXT PRIMARY KEY,
                working_directory TEXT NOT NULL,
                configuration_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_device_settings (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                max_parallel INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_queue (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                state TEXT NOT NULL,
                details_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_events (
                event_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                run_id TEXT,
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_errors (
                error_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                run_id TEXT,
                error_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_dispatcher (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                pid INTEGER NOT NULL,
                process_identity TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_automation (
                automation_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                role TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                next_check_at TEXT,
                last_check_at TEXT,
                last_result TEXT,
                last_assigned_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, role)
            );
            CREATE TABLE IF NOT EXISTS agent_watcher_state (
                project_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                last_event_at TEXT,
                error TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        if previous_version < 4:
            AgentStore._migrate_codex_model_alias(connection)
        if previous_version < 5:
            AgentStore._migrate_automation_intents(connection)
        connection.execute(f"PRAGMA user_version={AGENT_SCHEMA_VERSION}")

    @staticmethod
    def _migrate_codex_model_alias(connection: sqlite3.Connection) -> None:
        """Correct only the ChatGPT Codex alias that the CLI does not accept."""
        rows = connection.execute(
            "SELECT project_id, configuration_json FROM agent_configurations"
        ).fetchall()
        for project_id, payload in rows:
            try:
                configuration = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                continue
            roles = configuration.get("roles") if isinstance(configuration, dict) else None
            if not isinstance(roles, dict):
                continue
            changed = False
            for policy in roles.values():
                if not isinstance(policy, dict):
                    continue
                if policy.get("provider") == "codex" and policy.get("model") == "gpt-5.6":
                    policy["model"] = "gpt-5.6-sol"
                    changed = True
            if changed:
                connection.execute(
                    "UPDATE agent_configurations SET configuration_json = ?, updated_at = ? WHERE project_id = ?",
                    (_dump(configuration), utc_now(), project_id),
                )

    @staticmethod
    def _migrate_automation_intents(connection: sqlite3.Connection) -> None:
        """Move only repeat intents out of the execution-plan queue.

        A v4 intent meant that automation was actively running, so those
        projects become enabled. Projects without one remain opt-in.
        """
        rows = connection.execute(
            "SELECT run_id, project_id, payload_json, created_at FROM agent_queue"
        ).fetchall()
        enabled_projects: set[str] = set()
        migrated_ids: list[str] = []
        for run_id, project_id, payload_json, created_at in rows:
            try:
                payload = json.loads(payload_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict) or not payload.get("intentId") or not payload.get("role"):
                continue
            intent_id = str(payload["intentId"])
            now = utc_now()
            connection.execute(
                """
                INSERT OR REPLACE INTO agent_automation(
                    automation_id, project_id, role, payload_json, next_check_at,
                    last_check_at, last_result, last_assigned_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    project_id,
                    str(payload["role"]),
                    _dump({key: value for key, value in payload.items() if key != "planId"}),
                    payload.get("nextPollAt"),
                    payload.get("lastCheckAt"),
                    payload.get("lastResult"),
                    payload.get("lastAssignedAt"),
                    created_at,
                    now,
                ),
            )
            migrated_ids.append(run_id)
            enabled_projects.add(project_id)
        connection.executemany(
            "DELETE FROM agent_queue WHERE run_id = ?", [(run_id,) for run_id in migrated_ids]
        )

        configurations = connection.execute(
            "SELECT project_id, configuration_json FROM agent_configurations"
        ).fetchall()
        for project_id, configuration_json in configurations:
            try:
                configuration = json.loads(configuration_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(configuration, dict):
                continue
            configuration["automationEnabled"] = project_id in enabled_projects
            connection.execute(
                "UPDATE agent_configurations SET configuration_json = ? WHERE project_id = ?",
                (_dump(configuration), project_id),
            )

    def save_configuration(self, configuration: AgentConfiguration) -> None:
        """Atomically replace one project and the one device-wide limit."""
        payload = json.dumps(configuration.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO agent_configurations(project_id, working_directory, configuration_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        working_directory=excluded.working_directory,
                        configuration_json=excluded.configuration_json,
                        updated_at=excluded.updated_at
                    """,
                    (configuration.project_id, configuration.working_directory, payload, utc_now()),
                )
                connection.execute(
                    """
                    INSERT INTO agent_device_settings(singleton, max_parallel, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        max_parallel=excluded.max_parallel,
                        updated_at=excluded.updated_at
                    """,
                    (configuration.device_max_parallel, utc_now()),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def get_configuration(self, project_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT configuration_json FROM agent_configurations WHERE project_id = ?", (project_id,)
            ).fetchone()
            device_limit = self._device_limit(connection)
        if row is None:
            return None
        configuration = json.loads(row[0])
        configuration["deviceMaxParallel"] = device_limit
        return configuration

    @staticmethod
    def _device_limit(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT max_parallel FROM agent_device_settings WHERE singleton = 1"
        ).fetchone()
        if row is not None:
            return max(1, int(row[0]))
        # Upgrade compatibility: preserve the most recently edited legacy value
        # until the user saves the new global setting for the first time.
        legacy = connection.execute(
            "SELECT configuration_json FROM agent_configurations ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if legacy is not None:
            value = json.loads(legacy[0]).get("deviceMaxParallel")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return detected_device_recommendation()["recommendedMaxParallel"]

    def device_limit(self) -> int:
        with self._connection() as connection:
            return self._device_limit(connection)

    def device_capacity(self) -> dict:
        """Expose recommendation, global choice, project ceilings, and live use."""
        recommendation = detected_device_recommendation()
        with self._connection() as connection:
            configured = connection.execute(
                "SELECT max_parallel FROM agent_device_settings WHERE singleton = 1"
            ).fetchone()
            configuration_rows = connection.execute(
                "SELECT project_id, working_directory, configuration_json FROM agent_configurations ORDER BY project_id"
            ).fetchall()
            run_rows = connection.execute(
                "SELECT project_id, state FROM agent_runs"
            ).fetchall()
            effective = self._device_limit(connection)
        active_by_project: dict[str, int] = {}
        for project_id, state in run_rows:
            if state in ACTIVE_RUN_STATES:
                active_by_project[project_id] = active_by_project.get(project_id, 0) + 1
        projects = []
        for project_id, working_directory, payload in configuration_rows:
            configuration = json.loads(payload)
            projects.append({
                "projectId": project_id,
                "projectName": Path(working_directory).name or project_id,
                "projectMaxParallel": int(configuration.get("projectMaxParallel", 1)),
                "activeRuns": active_by_project.get(project_id, 0),
            })
        return {
            **recommendation,
            "configuredMaxParallel": int(configured[0]) if configured is not None else None,
            "effectiveMaxParallel": effective,
            "activeRuns": sum(active_by_project.values()),
            "projects": projects,
        }

    def list_project_ids(self) -> list[str]:
        """List every configured project so one tick can serve all of them."""
        with self._connection() as connection:
            rows = connection.execute("SELECT project_id FROM agent_configurations ORDER BY project_id").fetchall()
        return [row[0] for row in rows]

    def set_paused(self, project_id: str, paused: bool) -> dict | None:
        """Flip only the pause flag, leaving the rest of the configuration alone."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT configuration_json FROM agent_configurations WHERE project_id = ?", (project_id,)
                ).fetchone()
                if row is None:
                    connection.execute("ROLLBACK")
                    return None
                configuration = json.loads(row[0])
                configuration["paused"] = paused
                connection.execute(
                    "UPDATE agent_configurations SET configuration_json = ?, updated_at = ? WHERE project_id = ?",
                    (_dump(configuration), utc_now(), project_id),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return configuration

    def save_plan(self, project_id: str, plan: dict) -> None:
        """Store one execution plan so a later start can prove it is the same one."""
        self.prune_expired_plans(project_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT OR REPLACE INTO agent_queue(run_id, project_id, payload_json, created_at) VALUES (?, ?, ?, ?)",
                    (plan["planId"], project_id, _dump(plan), utc_now()),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def prune_expired_plans(self, project_id: str, *, now: str | None = None) -> int:
        """Remove spent plan previews while preserving continuous-run intents."""
        threshold = now or utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT run_id, payload_json FROM agent_queue WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
                expired = []
                for run_id, payload_json in rows:
                    payload = json.loads(payload_json)
                    expires_at = payload.get("expiresAt")
                    if "planId" in payload and isinstance(expires_at, str) and expires_at <= threshold:
                        expired.append(run_id)
                connection.executemany(
                    "DELETE FROM agent_queue WHERE run_id = ? AND project_id = ?",
                    [(run_id, project_id) for run_id in expired],
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return len(expired)

    def take_plan(self, project_id: str, plan_id: str) -> dict | None:
        """Consume one stored plan. A plan is usable once, so this deletes it."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload_json FROM agent_queue WHERE run_id = ? AND project_id = ?",
                    (plan_id, project_id),
                ).fetchone()
                if row is not None:
                    connection.execute("DELETE FROM agent_queue WHERE run_id = ?", (plan_id,))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return json.loads(row[0]) if row else None

    def save_intent(self, project_id: str, intent: dict) -> None:
        """Store what a role should keep doing when a slot frees up."""
        intent_id = intent["intentId"]
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO agent_automation(
                        automation_id, project_id, role, payload_json, next_check_at,
                        last_check_at, last_result, last_assigned_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, role) DO UPDATE SET
                        automation_id=excluded.automation_id,
                        payload_json=excluded.payload_json,
                        next_check_at=excluded.next_check_at,
                        last_check_at=excluded.last_check_at,
                        last_result=excluded.last_result,
                        last_assigned_at=excluded.last_assigned_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        intent_id,
                        project_id,
                        intent["role"],
                        _dump(intent),
                        intent.get("nextPollAt"),
                        intent.get("lastCheckAt"),
                        intent.get("lastResult"),
                        intent.get("lastAssignedAt"),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def activate_intent(self, project_id: str, intent: dict) -> None:
        """Replace the one repeat instruction for a role with a new activation."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM agent_automation WHERE project_id = ? AND role = ?",
                    (project_id, intent.get("role")),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        self.save_intent(project_id, intent)

    def list_intents(self, project_id: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM agent_automation
                WHERE project_id = ?
                ORDER BY COALESCE(last_assigned_at, ''), created_at, role
                """,
                (project_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_all_intents(self) -> list[dict]:
        """Return every persistent repeat instruction, keeping project identity."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT project_id, payload_json FROM agent_automation
                ORDER BY COALESCE(last_assigned_at, ''), created_at, project_id, role
                """
            ).fetchall()
        return [{**json.loads(payload), "projectId": project_id} for project_id, payload in rows]

    def get_intent(self, intent_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT project_id, payload_json FROM agent_automation WHERE automation_id = ?",
                (intent_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[1])
        return {**payload, "projectId": row[0]}

    def drop_intent(self, intent_id: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM agent_automation WHERE automation_id = ?", (intent_id,)
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def drop_role_intents(self, project_id: str, roles: set[str]) -> int:
        """Stop repeat instructions whose saved role policy is no longer continuous."""
        if not roles:
            return 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                placeholders = ",".join("?" for _ in roles)
                cursor = connection.execute(
                    f"DELETE FROM agent_automation WHERE project_id = ? AND role IN ({placeholders})",
                    (project_id, *sorted(roles)),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return cursor.rowcount

    def mark_automations_due(self, project_id: str) -> None:
        """Wake every participating role after a debounced workflow change."""
        for intent in self.list_intents(project_id):
            intent["nextPollAt"] = utc_now()
            self.save_intent(project_id, intent)

    def save_watcher_state(
        self,
        project_id: str,
        status: str,
        *,
        last_event_at: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO agent_watcher_state(project_id, status, last_event_at, error, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    status=excluded.status,
                    last_event_at=COALESCE(excluded.last_event_at, agent_watcher_state.last_event_at),
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (project_id, status, last_event_at, error, utc_now()),
            )

    def watcher_state(self, project_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status, last_event_at, error, updated_at FROM agent_watcher_state WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return {"status": row[0], "lastEventAt": row[1], "error": row[2], "updatedAt": row[3]}

    def get_dispatcher(self) -> dict | None:
        """Read the one device-wide repeat dispatcher process identity."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT pid, process_identity, updated_at FROM agent_dispatcher WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        return {"pid": row[0], "processIdentity": row[1], "updatedAt": row[2]}

    def claim_dispatcher(self, pid: int, identity: str | None) -> bool:
        """Let exactly one detached process own repeat scheduling on this device."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO agent_dispatcher(singleton, pid, process_identity, updated_at)
                    VALUES (1, ?, ?, ?)
                    """,
                    (pid, identity, utc_now()),
                )
                row = connection.execute(
                    "SELECT pid, process_identity FROM agent_dispatcher WHERE singleton = 1"
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return row == (pid, identity)

    def release_dispatcher(self, pid: int, identity: str | None) -> bool:
        """Release only the dispatcher identity the caller actually observed or owns."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    DELETE FROM agent_dispatcher
                    WHERE singleton = 1 AND pid = ? AND process_identity IS ?
                    """,
                    (pid, identity),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return cursor.rowcount == 1

    def save_run(self, run: dict) -> None:
        """Write one run row. The caller owns the state name and the details."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO agent_runs(run_id, project_id, state, details_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        state=excluded.state,
                        details_json=excluded.details_json,
                        updated_at=excluded.updated_at
                    """,
                    (run["runId"], run["projectId"], run["state"], _dump(run), utc_now()),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def get_run(self, run_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute("SELECT details_json FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def delete_run(self, run_id: str) -> None:
        """Remove a transient row that never became an execution."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM agent_events WHERE run_id = ?", (run_id,))
                connection.execute("DELETE FROM agent_errors WHERE run_id = ?", (run_id,))
                connection.execute("DELETE FROM agent_runs WHERE run_id = ?", (run_id,))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def prune_history(self, project_id: str, retention_days: int) -> dict[str, int]:
        """Apply the configured retention to terminal history and diagnostics."""
        threshold = (datetime.now(UTC) - timedelta(days=retention_days)).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                terminal_rows = connection.execute(
                    """
                    SELECT run_id FROM agent_runs
                    WHERE project_id = ? AND state NOT IN ('reserved','queued','running','paused')
                      AND updated_at < ?
                    """,
                    (project_id, threshold),
                ).fetchall()
                run_ids = [row[0] for row in terminal_rows]
                if run_ids:
                    placeholders = ",".join("?" for _ in run_ids)
                    events = connection.execute(
                        f"DELETE FROM agent_events WHERE run_id IN ({placeholders})", run_ids
                    ).rowcount
                    errors = connection.execute(
                        f"DELETE FROM agent_errors WHERE run_id IN ({placeholders})", run_ids
                    ).rowcount
                    runs = connection.execute(
                        f"DELETE FROM agent_runs WHERE run_id IN ({placeholders})", run_ids
                    ).rowcount
                else:
                    events = errors = runs = 0
                events += connection.execute(
                    "DELETE FROM agent_events WHERE project_id = ? AND created_at < ?",
                    (project_id, threshold),
                ).rowcount
                errors += connection.execute(
                    "DELETE FROM agent_errors WHERE project_id = ? AND created_at < ?",
                    (project_id, threshold),
                ).rowcount
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return {"runs": runs, "events": events, "errors": errors}

    def attach_supervisor(self, run_id: str, pid: int, identity: str | None) -> bool:
        """Attach the detached worker without overwriting a run it already advanced."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                stored = connection.execute(
                    "SELECT state, details_json FROM agent_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if stored is None or stored[0] != "queued":
                    connection.execute("ROLLBACK")
                    return False
                run = json.loads(stored[1])
                run["supervisorPid"] = pid
                run["supervisorIdentity"] = identity
                connection.execute(
                    "UPDATE agent_runs SET details_json = ?, updated_at = ? WHERE run_id = ?",
                    (_dump(run), utc_now(), run_id),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return True

    def list_runs(self, project_id: str | None = None, *, states: frozenset[str] | None = None) -> list[dict]:
        """List runs for one project, or for the whole device when none is given."""
        query = "SELECT details_json FROM agent_runs"
        parameters: tuple[str, ...] = ()
        if project_id is not None:
            query += " WHERE project_id = ?"
            parameters = (project_id,)
        with self._connection() as connection:
            rows = connection.execute(query + " ORDER BY updated_at DESC", parameters).fetchall()
        runs = [json.loads(row[0]) for row in rows]
        return [run for run in runs if states is None or run["state"] in states]

    def active_run_summary(self) -> dict:
        """Count active runs and name their projects, and nothing else.

        An update plan needs to say what it would disturb. It never needs the
        prompts, events or tool output behind those runs, so they do not leave
        the store through this path.
        """
        runs = self.list_runs(states=ACTIVE_RUN_STATES)
        return {"activeRuns": len(runs), "projects": sorted({run["projectId"] for run in runs})}

    def append_events(self, project_id: str, run_id: str, events: list[dict]) -> None:
        """Append normalized provider events. The caller filters them first."""
        if not events:
            return
        created_at = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    "INSERT INTO agent_events(event_id, project_id, run_id, event_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    [(uuid.uuid4().hex, project_id, run_id, _dump(event), created_at) for event in events],
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def read_events(self, project_id: str, run_id: str, *, cursor: int = 0, limit: int = 200) -> tuple[list[dict], int]:
        """Read events after a cursor and return the cursor to continue from."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT rowid, event_json FROM agent_events
                WHERE project_id = ? AND run_id = ? AND rowid > ?
                ORDER BY rowid LIMIT ?
                """,
                (project_id, run_id, cursor, limit),
            ).fetchall()
        events = [json.loads(row[1]) for row in rows]
        return events, rows[-1][0] if rows else cursor

    def record_error(self, project_id: str, run_id: str | None, error: dict) -> None:
        """Record one failure so a later reader can see what stage refused."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO agent_errors(error_id, project_id, run_id, error_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, project_id, run_id, _dump(error), utc_now()),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def get_state(self, project_id: str) -> dict:
        """Expose an empty, project-scoped runtime state until dispatch is implemented."""
        with self._connection() as connection:
            configuration = connection.execute(
                "SELECT configuration_json FROM agent_configurations WHERE project_id = ?", (project_id,)
            ).fetchone()
            queue = connection.execute(
                "SELECT payload_json FROM agent_queue WHERE project_id = ? ORDER BY created_at", (project_id,)
            ).fetchall()
            runs = connection.execute(
                "SELECT details_json, updated_at FROM agent_runs WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
            errors = connection.execute(
                "SELECT error_json FROM agent_errors WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        stored_configuration = json.loads(configuration[0]) if configuration else None
        if stored_configuration is not None:
            stored_configuration["deviceMaxParallel"] = self.device_limit()
        run_values = [_run_for_state(row[0], row[1]) for row in runs]
        active_roles = {
            run.get("role")
            for run in run_values
            if run.get("state") in ACTIVE_RUN_STATES
        }
        watcher = self.watcher_state(project_id)
        automation_roles = []
        for intent in self.list_intents(project_id):
            result = intent.get("lastResult")
            if intent.get("role") in active_roles:
                status = "running"
            elif result in {"no-target", "no_target", "waiting", None}:
                status = "waiting" if result in {"no-target", "no_target"} else "watching"
            elif result == "running":
                status = "watching"
            else:
                status = "attention"
            automation_roles.append({**intent, "status": status})
        return {
            "configuration": stored_configuration,
            "queue": [json.loads(row[0]) for row in queue],
            "automation": {
                "enabled": bool(stored_configuration and stored_configuration.get("automationEnabled")),
                "roles": automation_roles,
                "watcher": watcher,
                "dispatcher": self.get_dispatcher(),
            },
            "runs": run_values,
            "errors": [json.loads(row[0]) for row in errors],
        }


def _run_for_state(payload: str, updated_at: str) -> dict:
    """Expose a stable finish time without rewriting historical run rows."""
    run = json.loads(payload)
    if run.get("state") not in ACTIVE_RUN_STATES and not run.get("finishedAt"):
        run["finishedAt"] = updated_at
    return run
