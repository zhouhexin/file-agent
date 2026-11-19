"""教师资助独立查询服务：工号认证、个人查询及受控管理员维护入口。"""

from __future__ import annotations

import hashlib
import asyncio
import hmac
import json
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .database import initialize_schema, connect_postgres, IntegrityErrors
from .security import configured_secret, hash_password, issue_token, read_token, verify_password
from .workbooks import WorkbookImportError, parse_funding_workbook, parse_roster
from .snapshots import save_snapshot, state
from .sync import SyncConfig, Synchronizer
from .research import read_research


DATA_DIR = Path(os.getenv("PORTAL_DATA_DIR", "./data")).resolve()
DB_PATH = Path(os.getenv("PORTAL_DATABASE_PATH", str(DATA_DIR / "portal.sqlite3")))
MAX_UPLOAD = int(os.getenv("PORTAL_MAX_UPLOAD_MB", "20")) * 1024 * 1024
TOKEN_TTL = max(1, min(24, int(os.getenv("PORTAL_TOKEN_TTL_HOURS", "12"))))
JWT_SECRET = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connection():
    if os.getenv("PORTAL_TEST_SQLITE", "false").lower() != "true":
        with connect_postgres() as db:
            yield db
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """仅初始化独立服务业务表，校验管理员配置，不改 FileAgent 账号。"""
    global JWT_SECRET
    JWT_SECRET = configured_secret()
    with connection() as db:
        initialize_schema(db)
        admin_name = os.getenv("PORTAL_ADMIN_USERNAME", "").strip()
        admin_password = os.getenv("PORTAL_ADMIN_PASSWORD", "")
        if not admin_name or len(admin_password) < 8:
            raise RuntimeError("PORTAL_ADMIN_USERNAME is required and PORTAL_ADMIN_PASSWORD must be at least 8 characters")
        existing = db.execute("SELECT username,role FROM users WHERE username=?", (admin_name,)).fetchone()
        if existing and existing["role"] != "admin":
            raise RuntimeError("管理员账号与已注册教师工号冲突，请使用独立管理员账号。")
        if existing is None:
            db.execute("INSERT INTO users(username, employee_no, password_hash, role, created_at) VALUES(?,?,?,?,?)",
                       (admin_name, None, hash_password(admin_password), "admin", utc_now()))
        db.commit()


def audit(db: sqlite3.Connection, actor: str, action: str, subject: str) -> None:
    db.execute("INSERT INTO audit_events(actor,action,subject,created_at) VALUES(?,?,?,?)",
               (actor, action, subject, utc_now()))


def rate_limit(db: sqlite3.Connection, request: Request, action: str,
               *, limit: int, window_seconds: int) -> None:
    import time
    address = request.client.host if request.client else "unknown"
    bucket = hmac.new(JWT_SECRET.encode(), f"{action}:{address}".encode(), hashlib.sha256).hexdigest()
    now = int(time.time())
    db.execute("INSERT INTO rate_limits(bucket,window_start,attempts) VALUES(?,?,1) "
               "ON CONFLICT(bucket) DO UPDATE SET "
               "attempts=CASE WHEN rate_limits.window_start<=? THEN 1 "
               "WHEN rate_limits.attempts>=? THEN ? ELSE rate_limits.attempts+1 END, "
               "window_start=CASE WHEN rate_limits.window_start<=? "
               "THEN excluded.window_start ELSE rate_limits.window_start END",
               (bucket, now, now-window_seconds, limit+1, limit+1, now-window_seconds))
    attempts = db.execute("SELECT attempts FROM rate_limits WHERE bucket=?", (bucket,)).fetchone()["attempts"]
    db.execute("DELETE FROM rate_limits WHERE window_start<?", (now - 86400,))
    db.commit()
    if attempts > limit:
        raise HTTPException(429, "操作过于频繁，请稍后再试。")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    config = SyncConfig.from_env()
    stop = asyncio.Event()
    task = asyncio.create_task(Synchronizer(config, connection).run(stop)) if config else None
    try:
        yield
    finally:
        stop.set()
        if task:
            await task


app = FastAPI(title="教师资助查询", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def private_responses(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


class RegisterBody(BaseModel):
    employee_no: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class LoginBody(BaseModel):
    employee_no: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class AdminResetPasswordBody(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class StaffBody(BaseModel):
    employee_no: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    active: bool = True

    @field_validator("employee_no", "name")
    @classmethod
    def clean_text(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("工号和姓名不能为空或包含控制字符。")
        return value


def db_dep():
    with connection() as db:
        yield db


Db = Annotated[sqlite3.Connection, Depends(db_dep)]


def current_user(db: Db, authorization: Annotated[str | None, Header()] = None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "请先登录。")
    try:
        claims = read_token(authorization[7:].strip(), JWT_SECRET)
    except ValueError as exc:
        raise HTTPException(401, "登录已过期，请重新登录。") from exc
    user = db.execute("SELECT username,employee_no,role,active,token_version FROM users WHERE username=?",
                      (claims["sub"],)).fetchone()
    if (user is None or not user["active"] or user["role"] != claims["role"]
            or int(claims.get("ver", -1)) != user["token_version"]):
        raise HTTPException(401, "账号状态已变化，请重新登录。")
    return dict(user)


UserDep = Annotated[dict, Depends(current_user)]


def admin_user(user: UserDep):
    if user["role"] != "admin":
        raise HTTPException(403, "仅管理员可以执行此操作。")
    return user


AdminDep = Annotated[dict, Depends(admin_user)]


def _make_token(db: sqlite3.Connection, user: sqlite3.Row) -> str:
    audit(db, user["username"], "LOGIN", user["username"])
    db.commit()
    return issue_token(user["username"], user["role"], JWT_SECRET, TOKEN_TTL, user["token_version"])


@app.get("/health")
def health():
    return {"status": "ok", "service": "teacher-funding-portal",
            "version": os.getenv("PORTAL_RELEASE_VERSION", "development")}


@app.get("/", response_class=FileResponse)
def home():
    return FileResponse(Path(__file__).parent / "static" / "index.html", media_type="text/html", headers={"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'",
                                             "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"})


@app.get("/static/portal.js", include_in_schema=False)
def portal_script():
    return FileResponse(Path(__file__).parent / "static" / "portal.js", media_type="application/javascript")


@app.get("/static/portal.css", include_in_schema=False)
def portal_styles():
    return FileResponse(Path(__file__).parent / "static" / "portal.css", media_type="text/css")


@app.post("/api/register", status_code=201)
def register(body: RegisterBody, request: Request, db: Db):
    rate_limit(db, request, "register:" + body.employee_no.strip(), limit=8, window_seconds=3600)
    employee_no = body.employee_no.strip()
    staff = db.execute("SELECT name,active FROM staff WHERE employee_no=?", (employee_no,)).fetchone()
    if staff is None or not staff["active"]:
        raise HTTPException(404, "名单中没有这个有效工号，请联系管理员核对。")
    try:
        db.execute("INSERT INTO users(username,employee_no,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                   (employee_no, employee_no, hash_password(body.password), "user", utc_now()))
    except IntegrityErrors as exc:
        db.rollback()
        raise HTTPException(409, "该工号已注册。请直接登录；如无法登录，请联系管理员重置密码。") from exc
    audit(db, employee_no, "REGISTER", employee_no)
    db.commit()
    return {"employee_no": employee_no, "name": staff["name"], "message": "注册成功，请登录。"}


@app.post("/api/login")
def login(body: LoginBody, request: Request, db: Db):
    rate_limit(db, request, "login:" + body.employee_no.strip(), limit=30, window_seconds=900)
    username = body.employee_no.strip()
    user = db.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
    if user is None or not verify_password(body.password, user["password_hash"]):
        audit(db, username[:64], "LOGIN_FAILED", "")
        db.commit()
        raise HTTPException(401, "工号或密码不正确。")
    staff = db.execute("SELECT name,active FROM staff WHERE employee_no=?", (user["employee_no"],)).fetchone() if user["employee_no"] else None
    if user["role"] == "user" and (staff is None or not staff["active"]):
        raise HTTPException(403, "该工号当前已停用。")
    token = _make_token(db, user)
    return {"access_token": token, "token_type": "bearer", "role": user["role"],
            "employee_no": user["employee_no"], "name": staff["name"] if staff else "管理员"}


@app.get("/api/me")
def me(user: UserDep, db: Db):
    if user["role"] == "admin":
        return {"role": "admin", "username": user["username"], "name": "管理员"}
    staff = db.execute("SELECT name,active FROM staff WHERE employee_no=?", (user["employee_no"],)).fetchone()
    if staff is None or not staff["active"]:
        raise HTTPException(403, "该工号当前已停用，无法查询资助信息。")
    return {"role": "user", "employee_no": user["employee_no"], "name": staff["name"]}


@app.post("/api/password/change")
def change_password(body: PasswordBody, request: Request, user: UserDep, db: Db):
    rate_limit(db, request, "password:" + user["username"], limit=10, window_seconds=900)
    row = db.execute("SELECT password_hash FROM users WHERE username=?", (user["username"],)).fetchone()
    if row is None or not verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(400, "当前密码不正确。")
    changed = db.execute("UPDATE users SET password_hash=?,token_version=token_version+1 WHERE username=? AND token_version=?",
                         (hash_password(body.new_password), user["username"], user["token_version"]))
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "账号已发生变化，请重新登录后修改密码。")
    audit(db, user["username"], "PASSWORD_CHANGED", user["username"])
    db.commit()
    return {"message": "密码已修改，请重新登录。"}


@app.get("/api/my/funding")
def my_funding(user: UserDep, db: Db):
    if user["role"] != "user" or not user["employee_no"]:
        raise HTTPException(403, "此页面仅供教师账号使用。")
    staff = db.execute("SELECT name,active FROM staff WHERE employee_no=?", (user["employee_no"],)).fetchone()
    if staff is None or not staff["active"]:
        raise HTTPException(403, "该工号当前已停用，无法查询资助信息。")
    same_name = db.execute("SELECT COUNT(*) AS n FROM staff WHERE name=? AND active=1",
                           (staff["name"],)).fetchone()["n"]
    if same_name != 1:
        raise HTTPException(409, "名单中存在重名教师，当前无法安全关联资助明细，请联系管理员核对。")
    current = db.execute("SELECT value FROM portal_state WHERE key='funding_snapshot_id'").fetchone()
    if current is None:
        return {"status": "NO_FUNDING_DATA", "employee_no": user["employee_no"], "name": staff["name"],
                "research": read_research(db, staff["name"], None),
                "message": "尚未成功同步或导入余额表，请稍后再试。"}
    people = db.execute("SELECT * FROM funding_people WHERE snapshot_id=? AND name=?",
                        (int(current["value"]), staff["name"])).fetchall()
    if len(people) != 1:
        if len(people) > 1:
            raise HTTPException(409, "余额表中存在同名教师记录，已停止展示金额，请联系管理员核对。")
        return {"status": "NO_FUNDING_RECORD", "employee_no": user["employee_no"], "name": staff["name"],
                "research": read_research(db, staff["name"], None),
                "message": "当前余额表没有找到你的资助记录。"}
    person = people[0]
    details = db.execute("SELECT sequence,registration,date_text,usage_amount,note,needs_review FROM funding_details WHERE snapshot_id=? AND name=? ORDER BY sequence",
                         (person["snapshot_id"], staff["name"])).fetchall()
    snap = db.execute("SELECT imported_at,filename FROM funding_snapshots WHERE id=?", (person["snapshot_id"],)).fetchone()
    review = bool(person["needs_review"] or any(row["needs_review"] for row in details))
    audit(db, user["username"], "FUNDING_VIEW", user["employee_no"])
    db.commit()
    return {
        "status": "NEEDS_REVIEW" if review else "READY",
        "employee_no": user["employee_no"], "name": staff["name"],
        "research_total": person["research_total"], "other_total": person["other_total"],
        "total_funding": (str((Decimal(person["research_total"] or "0") +
                                Decimal(person["other_total"] or "0")).quantize(Decimal("0.01")))
                          if person["research_total"] is not None or person["other_total"] is not None else None),
        "total_spent": person["total_spent"], "remaining": None if review else person["remaining"],
        "updated_at": snap["imported_at"], "source_filename": snap["filename"],
        "needs_review": review,
        "research": read_research(db, staff["name"], person["research_total"]),
        "sync_status": state(db, "sync_status") if SyncConfig.from_env() else None,
        "details": [{"sequence": row["sequence"], "registration": row["registration"],
                     "date": row["date_text"], "usage_amount": row["usage_amount"],
                     "note": row["note"], "needs_review": bool(row["needs_review"])} for row in details],
    }


async def _read_upload(file: UploadFile) -> bytes:
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(415, "只接受 .xlsx 文件。")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"文件不能超过 {MAX_UPLOAD // (1024 * 1024)} MB。")
    return data


@app.post("/api/admin/roster/import")
async def import_roster(user: AdminDep, db: Db, file: UploadFile = File(...)):
    data = await _read_upload(file)
    try:
        entries = parse_roster(data)
    except WorkbookImportError as exc:
        raise HTTPException(422, str(exc)) from exc
    digest = hashlib.sha256(data).hexdigest()
    db.execute("BEGIN IMMEDIATE")
    # Invalidate existing sessions when a roster update changes or disables a binding.
    incoming = {entry["employee_no"]: entry["name"] for entry in entries}
    for old in db.execute("SELECT employee_no,name,active FROM staff").fetchall():
        if old["name"] != incoming.get(old["employee_no"]) or not old["active"]:
            db.execute("UPDATE users SET token_version=token_version+1 WHERE employee_no=?", (old["employee_no"],))
    cursor = db.execute("INSERT INTO roster_imports(filename,sha256,imported_count,imported_by,imported_at) VALUES(?,?,?,?,?)",
                        (Path(file.filename).name, digest, len(entries), user["username"], utc_now()))
    import_id = cursor.lastrowid
    db.execute("UPDATE staff SET active=0")
    for entry in entries:
        db.execute("INSERT INTO roster_entries(import_id,employee_no,name) VALUES(?,?,?)",
                   (import_id, entry["employee_no"], entry["name"]))
        db.execute("INSERT INTO staff(employee_no,name,active,roster_import_id) VALUES(?,?,1,?) "
                   "ON CONFLICT(employee_no) DO UPDATE SET name=excluded.name,active=1,roster_import_id=excluded.roster_import_id",
                   (entry["employee_no"], entry["name"], import_id))
    db.execute("INSERT INTO portal_state(key,value) VALUES('roster_import_id',?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(import_id),))
    audit(db, user["username"], "ROSTER_IMPORTED", str(import_id))
    db.commit()
    return {"import_id": import_id, "count": len(entries), "sha256": digest,
            "message": f"已导入 {len(entries)} 位教师。"}


@app.post("/api/admin/funding/import")
async def import_funding(user: AdminDep, db: Db, file: UploadFile = File(...)):
    if SyncConfig.from_env():
        raise HTTPException(409, "自动同步已开启，余额表以 FileAgent 正式发布版本为准。")
    data = await _read_upload(file)
    try:
        people = parse_funding_workbook(data)
    except WorkbookImportError as exc:
        raise HTTPException(422, str(exc)) from exc
    digest = hashlib.sha256(data).hexdigest()
    snapshot_id = save_snapshot(db, filename=Path(file.filename).name, digest=digest,
                                actor=user["username"], people=people)
    db.commit()
    return {"snapshot_id": snapshot_id, "count": len(people), "sha256": digest,
            "message": f"已导入余额表，共 {len(people)} 位教师。"}


@app.get("/api/admin/status")
def admin_status(user: AdminDep, db: Db):
    roster = db.execute("SELECT COUNT(*) AS n FROM staff WHERE active=1").fetchone()["n"]
    registered = db.execute("SELECT COUNT(*) AS n FROM users WHERE role='user' AND active=1").fetchone()["n"]
    current = db.execute("SELECT value FROM portal_state WHERE key='funding_snapshot_id'").fetchone()
    funding = (db.execute("SELECT imported_at,filename,person_count FROM funding_snapshots WHERE id=?",
                          (int(current["value"]),)).fetchone() if current else None)
    return {"active_staff_count": roster, "registered_count": registered,
            "funding_import": dict(funding) if funding else None,
            "research_sync": {"status": state(db, "research_sync_status"),
                              "message": state(db, "research_sync_message"),
                              "checked_at": state(db, "research_sync_checked_at")},
            "sync": {"enabled": SyncConfig.from_env() is not None,
                     **{key: state(db, "sync_" + key) for key in
                        ("status", "message", "last_checked_at", "last_success_at")}}}


@app.get("/api/admin/staff")
def list_staff(user: AdminDep, db: Db, offset: int = Query(default=0, ge=0),
               limit: int = Query(default=50, ge=1, le=100)):
    total = db.execute("SELECT COUNT(*) FROM staff").fetchone()[0]
    rows = db.execute("SELECT s.employee_no,s.name,s.active, "
                      "EXISTS(SELECT 1 FROM users u WHERE u.employee_no=s.employee_no) AS registered "
                      "FROM staff s ORDER BY s.employee_no LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return {"total": total, "offset": offset, "limit": limit,
            "staff": [{**dict(row), "active": bool(row["active"]),
                       "registered": bool(row["registered"])} for row in rows]}


def _save_staff(body, user, db, *, create):
    db.execute("BEGIN IMMEDIATE")
    old = db.execute("SELECT employee_no,name,active FROM staff WHERE employee_no=?",
                     (body.employee_no,)).fetchone()
    if create and old:
        raise HTTPException(409, "该工号已存在，请修改已有记录。")
    if not create and old is None:
        raise HTTPException(404, "该工号不存在。")
    reserved = db.execute("SELECT 1 FROM users WHERE username=? AND role='admin'", (body.employee_no,)).fetchone()
    if reserved:
        raise HTTPException(409, "该工号与管理员账号冲突，请核对后再填写。")
    if create:
        db.execute("INSERT INTO staff(employee_no,name,active) VALUES(?,?,?)",
                   (body.employee_no, body.name, int(body.active)))
    else:
        db.execute("UPDATE staff SET name=?,active=?,roster_import_id=NULL WHERE employee_no=?",
                   (body.name, int(body.active), body.employee_no))
        if old["name"] != body.name or bool(old["active"]) != body.active:
            db.execute("UPDATE users SET token_version=token_version+1 WHERE employee_no=?", (body.employee_no,))
    audit(db, user["username"], "STAFF_CREATED" if create else "STAFF_UPDATED",
          json.dumps({"employee_no": body.employee_no, "before": dict(old) if old else None,
                      "after": {"name": body.name, "active": body.active}}, ensure_ascii=False))
    db.commit()
    return {"employee_no": body.employee_no, "name": body.name, "active": body.active,
            "message": "教师已新增。" if create else "教师信息已保存。"}


@app.post("/api/admin/staff", status_code=201)
def create_staff(body: StaffBody, user: AdminDep, db: Db):
    return _save_staff(body, user, db, create=True)


@app.put("/api/admin/staff")
def update_staff(body: StaffBody, user: AdminDep, db: Db):
    return _save_staff(body, user, db, create=False)


@app.post("/api/admin/users/{employee_no}/reset-password")
def reset_password(employee_no: str, body: AdminResetPasswordBody, user: AdminDep, db: Db):
    result = db.execute("UPDATE users SET password_hash=?,token_version=token_version+1 WHERE employee_no=? AND role='user'",
                        (hash_password(body.new_password), employee_no))
    if result.rowcount != 1:
        raise HTTPException(404, "该工号尚未注册。")
    audit(db, user["username"], "PASSWORD_RESET", employee_no)
    db.commit()
    return {"message": "密码已重置。"}
