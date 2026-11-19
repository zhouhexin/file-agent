"""Local administrator commands; no FileAgent access or account creation."""

import argparse
import hashlib
import json
import sqlite3
import os
from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .database import initialize_schema, connect_postgres, TABLES, ID_TABLES, PREFIX
from .workbooks import parse_roster


@contextmanager
def roster_connection(database=None):
    if database is None:
        with connect_postgres() as db:
            yield db
    else:
        database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(database, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


def import_roster_file(source: Path, database: Path | None = None):
    data = source.read_bytes()
    entries = parse_roster(data)
    digest = hashlib.sha256(data).hexdigest()
    with roster_connection(database) as db:
        initialize_schema(db)
        db.execute("BEGIN IMMEDIATE")
        imported = db.execute("SELECT id FROM roster_imports WHERE sha256=? AND imported_by='LOCAL_ADMIN'",
                              (digest,)).fetchone()
        if imported:
            db.rollback()
            return {"status": "ALREADY_IMPORTED", "count": len(entries), "sha256": digest,
                    "import_id": imported["id"]}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cursor = db.execute("INSERT INTO roster_imports(filename,sha256,imported_count,imported_by,imported_at) VALUES(?,?,?,?,?)",
                            (source.name, digest, len(entries), "LOCAL_ADMIN", now))
        import_id = cursor.lastrowid
        created, updated, unchanged = 0, 0, 0
        for entry in entries:
            employee, name = entry["employee_no"], entry["name"]
            if db.execute("SELECT 1 FROM users WHERE username=? AND role='admin'", (employee,)).fetchone():
                raise ValueError("名单工号与管理员账号冲突，已停止导入。")
            old = db.execute("SELECT name,active FROM staff WHERE employee_no=?", (employee,)).fetchone()
            if old is None:
                created += 1
            elif old["name"] != name or not old["active"]:
                updated += 1
                db.execute("UPDATE users SET token_version=token_version+1 WHERE employee_no=?", (employee,))
            else:
                unchanged += 1
            db.execute("INSERT INTO roster_entries(import_id,employee_no,name) VALUES(?,?,?)",
                       (import_id, employee, name))
            db.execute("INSERT INTO staff(employee_no,name,active,roster_import_id) VALUES(?,?,1,?) "
                       "ON CONFLICT(employee_no) DO UPDATE SET name=excluded.name,active=1,roster_import_id=excluded.roster_import_id",
                       (employee, name, import_id))
        db.execute("INSERT INTO audit_events(actor,action,subject,created_at) VALUES(?,?,?,?)",
                   ("LOCAL_ADMIN", "ROSTER_MERGED", str(import_id), now))
        db.commit()
        groups = defaultdict(list)
        for row in db.execute("SELECT employee_no,name FROM staff WHERE active=1"):
            groups[row["name"]].append(row["employee_no"])
        return {"status": "IMPORTED", "count": len(entries), "created": created, "updated": updated,
                "unchanged": unchanged, "sha256": digest, "import_id": import_id,
                "duplicate_names": {name: numbers for name, numbers in groups.items() if len(numbers) > 1}}


def migrate_sqlite(source: Path):
    """Copy the old portal DB to empty prefixed tables; never write any FileAgent table."""
    origin = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    origin.row_factory = sqlite3.Row
    try:
        origin.execute("BEGIN")
        with connect_postgres() as db:
            initialize_schema(db)
            db.execute("BEGIN IMMEDIATE")
            marker = db.execute("SELECT value FROM portal_state WHERE key='sqlite_migration'").fetchone()
            if marker:
                db.rollback()
                return {"status": "ALREADY_MIGRATED"}
            if any(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES):
                raise ValueError("目标查询服务表已有记录，已停止迁移，避免覆盖现有数据。")
            counts = {}
            source_tables = {row[0] for row in origin.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in TABLES:
                rows = origin.execute(f"SELECT * FROM {table}").fetchall() if table in source_tables else []
                counts[table] = len(rows)
                for row in rows:
                    columns = row.keys()
                    quoted_columns = ','.join('"' + column.replace('"', '""') + '"' for column in columns)
                    db.execute(f"INSERT INTO {table} ({quoted_columns}) VALUES ({','.join('?' for _ in columns)})",
                               tuple(row))
            for table in ID_TABLES:
                # Names are fixed module constants, not supplied by a caller.
                db.raw.cursor().execute(
                    f"SELECT setval(pg_get_serial_sequence('{PREFIX}{table}','id'), "
                    f"COALESCE((SELECT MAX(id) FROM {PREFIX}{table}),1), "
                    f"EXISTS(SELECT 1 FROM {PREFIX}{table}))")
            db.execute("INSERT INTO portal_state(key,value) VALUES('sqlite_migration',?)",
                       (datetime.now(timezone.utc).isoformat(),))
            db.commit()
            return {"status": "MIGRATED", "counts": counts}
    finally:
        origin.close()


def main():
    parser = argparse.ArgumentParser(description="维护教师资助查询服务的本地名单表。")
    parser.add_argument("command", choices=["import-roster", "migrate-sqlite"])
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--database", type=Path, help="仅供 SQLite 测试使用；正常使用 PostgreSQL。")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--fileagent-env", type=Path)
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=False)
    if args.fileagent_env:
        from dotenv import dotenv_values
        url = dotenv_values(args.fileagent_env).get("DATABASE_URL")
        if not url:
            raise RuntimeError("FileAgent 配置中没有 DATABASE_URL。")
        os.environ["PORTAL_DATABASE_URL"] = url
    result = (migrate_sqlite(args.file) if args.command == "migrate-sqlite"
              else import_roster_file(args.file, args.database))
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
