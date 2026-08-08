"""SQLite persistence owned by the agent runtime, not by GUI consumers."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from heartbeat.agent_contract import AgentConfiguration

AGENT_SCHEMA_VERSION = 1
DEFAULT_AGENT_DB = Path.home() / ".claude" / "heartbeat" / "agent-runtime.sqlite3"


def default_agent_database_path() -> Path:
    """Resolve the runtime-owned database path when a process starts.

    Tests and packaged runtime launchers can isolate the state by setting
    ``HEARTBEAT_AGENT_HOME`` without changing legacy Heartbeat paths.
    """
    override = os.environ.get("HEARTBEAT_AGENT_HOME")
    return Path(override) / "agent-runtime.sqlite3" if override else DEFAULT_AGENT_DB


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_configurations (
                project_id TEXT PRIMARY KEY,
                working_directory TEXT NOT NULL,
                configuration_json TEXT NOT NULL,
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
            """
        )
        connection.execute(f"PRAGMA user_version={AGENT_SCHEMA_VERSION}")

    def save_configuration(self, configuration: AgentConfiguration) -> None:
        """Atomically replace one project's complete configuration."""
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
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def get_configuration(self, project_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT configuration_json FROM agent_configurations WHERE project_id = ?", (project_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

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
                "SELECT details_json FROM agent_runs WHERE project_id = ? ORDER BY updated_at DESC", (project_id,)
            ).fetchall()
            errors = connection.execute(
                "SELECT error_json FROM agent_errors WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        return {
            "configuration": json.loads(configuration[0]) if configuration else None,
            "queue": [json.loads(row[0]) for row in queue],
            "runs": [json.loads(row[0]) for row in runs],
            "errors": [json.loads(row[0]) for row in errors],
        }
