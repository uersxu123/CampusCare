from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import redis
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.services.tool_result_store import ToolResultStore


REQUIRED_TABLES = {"tool_execution_records", "tool_result_views", "model_context_manifests"}


def _alembic(root: Path, database_url: str, *arguments: str) -> dict:
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=root,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(arguments)} failed: {completed.stderr or completed.stdout}")
    return {"command": list(arguments), "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Context WorkItems V7 MySQL/Redis 隔离存储探针")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--admin-url",
        default="mysql+pymysql://root:root@127.0.0.1:13306/mysql?charset=utf8mb4",
    )
    parser.add_argument("--redis-url", default="redis://127.0.0.1:16379/15")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    database_name = "mindbridge_eval_ctxv7_" + uuid.uuid4().hex[:12]
    if not database_name.startswith("mindbridge_eval_ctxv7_"):
        raise RuntimeError("隔离数据库名称校验失败")
    admin_url = make_url(args.admin_url).set(database="mysql")
    database_url = make_url(args.admin_url).set(database=database_name).render_as_string(hide_password=False)
    admin = create_engine(admin_url, pool_pre_ping=True)
    evidence = {
        "schemaVersion": 1,
        "startedAt": datetime.now(UTC).isoformat(),
        "databaseName": database_name,
        "redisDatabase": 15,
        "commands": [],
        "checks": {},
        "cleanup": {},
    }
    created = False
    try:
        with admin.begin() as connection:
            connection.execute(text(
                f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            ))
        created = True
        evidence["commands"].append(_alembic(root, database_url, "upgrade", "head"))
        engine = create_engine(database_url, pool_pre_ping=True)
        tables_after_upgrade = set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        evidence["checks"]["upgradeHead"] = revision == "0016_context_workitems_v7"
        evidence["checks"]["tablesCreated"] = REQUIRED_TABLES.issubset(tables_after_upgrade)

        sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        payload = {"items": [{"evidenceId": "ev-storage", "content": "隔离数据库中的完整证据原文。"}]}
        writer = ToolResultStore(sessions, namespace="probe")
        writer.persist(
            "exec-storage-probe",
            payload,
            user_id="user-a",
            session_id="session-a",
            persist_reason="STORAGE_PROBE",
        )
        reader = ToolResultStore(sessions, namespace="probe")
        allowed = reader.read(
            "exec-storage-probe", user_id="user-a", session_id="session-a", evidence_ids=["ev-storage"]
        )
        denied = reader.read(
            "exec-storage-probe", user_id="user-b", session_id="session-a", evidence_ids=["ev-storage"]
        )
        evidence["checks"]["mysqlExactReread"] = (
            allowed.get("status") == "OK"
            and allowed.get("excerpts", [{}])[0].get("text") == payload["items"][0]["content"]
        )
        evidence["checks"]["crossUserDenied"] = denied.get("status") == "NOT_FOUND_OR_NOT_AUTHORIZED"
        engine.dispose()

        evidence["commands"].append(_alembic(root, database_url, "downgrade", "0015_three_layer_memory_v3"))
        engine = create_engine(database_url, pool_pre_ping=True)
        tables_after_downgrade = set(inspect(engine).get_table_names())
        evidence["checks"]["downgradeRemovedTables"] = REQUIRED_TABLES.isdisjoint(tables_after_downgrade)
        engine.dispose()

        evidence["commands"].append(_alembic(root, database_url, "upgrade", "head"))
        engine = create_engine(database_url, pool_pre_ping=True)
        evidence["checks"]["reupgradeCreatedTables"] = REQUIRED_TABLES.issubset(inspect(engine).get_table_names())
        engine.dispose()

        redis_client = redis.Redis.from_url(args.redis_url, socket_timeout=2.0, decode_responses=True)
        redis_key = "context-workitems-v7:probe:" + uuid.uuid4().hex
        redis_client.set(redis_key, "隔离探针", ex=60)
        evidence["checks"]["redisRoundTrip"] = redis_client.get(redis_key) == "隔离探针"
        redis_client.delete(redis_key)
        evidence["checks"]["redisKeyCleaned"] = redis_client.get(redis_key) is None
        redis_client.close()
        evidence["passed"] = all(evidence["checks"].values())
    except Exception as exc:
        evidence["passed"] = False
        evidence["errorType"] = type(exc).__name__
        evidence["error"] = str(exc)
    finally:
        if created:
            with admin.begin() as connection:
                existing = connection.execute(
                    text("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME=:name"),
                    {"name": database_name},
                ).scalar_one_or_none()
                if existing == database_name:
                    connection.execute(text(f"DROP DATABASE `{database_name}`"))
            evidence["cleanup"]["databaseDropped"] = True
        admin.dispose()
        evidence["finishedAt"] = datetime.now(UTC).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print(args.output)
    print(json.dumps({"passed": evidence.get("passed"), "checks": evidence.get("checks")}, ensure_ascii=False))
    return 0 if evidence.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
