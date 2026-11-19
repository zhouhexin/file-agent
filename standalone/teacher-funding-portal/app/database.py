"""Shared PostgreSQL database, isolated portal table names; SQLite is for local test fixtures."""

import os
import re
import sqlite3
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import DictCursor

TABLES = ("users", "staff", "roster_imports", "roster_entries", "funding_snapshots",
          "funding_people", "funding_details", "portal_state", "audit_events", "rate_limits",
          "research_snapshots", "research_people", "research_items")
ID_TABLES = {"roster_imports", "funding_snapshots", "audit_events", "research_snapshots"}
PREFIX = "teacher_funding_"
IntegrityErrors = (sqlite3.IntegrityError, psycopg2.IntegrityError)
TOKEN = re.compile(r"'(?:''|[^'])*'|\b[A-Za-z_][A-Za-z_0-9]*\b|\?")


def postgres_sql(sql):
    """Translate only unquoted identifiers/placeholders, never parameter values or literals."""
    def convert(match):
        value = match.group()
        if value == "?":
            return "%s"
        if value in TABLES or value.startswith("ix_funding_"):
            return PREFIX + value
        return value
    result = TOKEN.sub(convert, sql)
    return result.replace("MIN(attempts+1,", "LEAST(attempts+1,")


class Cursor:
    def __init__(self, cursor, lastrowid=None):
        self.cursor, self.lastrowid = cursor, lastrowid

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)


class PostgresConnection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        cursor = self.raw.cursor(cursor_factory=DictCursor)
        if sql.strip() == "BEGIN IMMEDIATE":
            cursor.execute("SELECT pg_advisory_xact_lock(742876601)")
            return Cursor(cursor)
        insert = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
        with_id = insert and insert.group(1) in ID_TABLES
        query = postgres_sql(sql)
        if with_id:
            query += " RETURNING id"
        cursor.execute(query, params)
        return Cursor(cursor, cursor.fetchone()[0] if with_id else None)

    def executescript(self, script):
        script = script.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        script = re.sub(r"\b(created_at|imported_at) TEXT", r"\1 TIMESTAMPTZ", script)
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)
        self.commit()

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def database_url():
    value = os.getenv("PORTAL_DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("请设置 PORTAL_DATABASE_URL，使用现有 PostgreSQL 数据库及账号。")
    if not value.startswith(("postgresql://", "postgresql+psycopg2://")):
        raise RuntimeError("PORTAL_DATABASE_URL 必须是 PostgreSQL 连接地址。")
    return value.replace("postgresql+psycopg2://", "postgresql://", 1)


@contextmanager
def connect_postgres(url=None):
    db = PostgresConnection(psycopg2.connect(url or database_url(), connect_timeout=10))
    try:
        yield db
    finally:
        db.close()


def initialize_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            employee_no TEXT UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','admin')),
            active INTEGER NOT NULL DEFAULT 1,
            token_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS staff (
            employee_no TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            roster_import_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS roster_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            imported_count INTEGER NOT NULL,
            imported_by TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS roster_entries (
            import_id INTEGER NOT NULL REFERENCES roster_imports(id),
            employee_no TEXT NOT NULL,
            name TEXT NOT NULL,
            PRIMARY KEY(import_id, employee_no)
        );
        CREATE TABLE IF NOT EXISTS funding_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            imported_by TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            person_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS funding_people (
            snapshot_id INTEGER NOT NULL REFERENCES funding_snapshots(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            research_total TEXT,
            other_total TEXT,
            total_spent TEXT,
            remaining TEXT,
            needs_review INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(snapshot_id, name)
        );
        CREATE TABLE IF NOT EXISTS funding_details (
            snapshot_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            registration TEXT NOT NULL,
            date_text TEXT NOT NULL,
            usage_amount TEXT,
            note TEXT NOT NULL,
            needs_review INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(snapshot_id, name) REFERENCES funding_people(snapshot_id, name) ON DELETE CASCADE,
            PRIMARY KEY(snapshot_id, name, sequence)
        );
        CREATE TABLE IF NOT EXISTS portal_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            subject TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rate_limits (
            bucket TEXT PRIMARY KEY,
            window_start INTEGER NOT NULL,
            attempts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_funding_people_name ON funding_people(name, snapshot_id);
        CREATE INDEX IF NOT EXISTS ix_funding_details_owner ON funding_details(snapshot_id, name, sequence);
        CREATE TABLE IF NOT EXISTS research_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            source_version_id TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS research_people (
            snapshot_id INTEGER NOT NULL REFERENCES research_snapshots(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            research_total TEXT,
            needs_review INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(snapshot_id,name)
        );
        CREATE TABLE IF NOT EXISTS research_items (
            snapshot_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            year TEXT NOT NULL,
            kind TEXT NOT NULL,
            venue TEXT NOT NULL,
            title TEXT NOT NULL,
            paper_type TEXT NOT NULL,
            funding_type TEXT NOT NULL,
            published_at TEXT NOT NULL,
            category TEXT NOT NULL,
            amount TEXT,
            location TEXT NOT NULL,
            note TEXT NOT NULL,
            needs_review INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(snapshot_id,name,sequence),
            FOREIGN KEY(snapshot_id,name) REFERENCES research_people(snapshot_id,name) ON DELETE CASCADE
        );
        """)
