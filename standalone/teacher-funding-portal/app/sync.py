"""Pull only published workbooks through FileAgent's existing authenticated read APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from .snapshots import save_snapshot, set_state, state
from .workbooks import WorkbookImportError, parse_funding_workbook
from .research import parse_research_workbook, save_research


class SyncError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the service credential to a redirect destination.


@dataclass(frozen=True)
class SyncConfig:
    base_url: str
    token: str
    filename: str
    interval: int = 60
    max_bytes: int = 20 * 1024 * 1024
    timeout: int = 30
    research_filename: str = ""

    @classmethod
    def from_env(cls):
        if os.getenv("PORTAL_SYNC_ENABLED", "false").lower() != "true":
            return None
        base = os.getenv("PORTAL_FILEAGENT_BASE_URL", "").rstrip("/")
        parsed = urlsplit(base)
        token = os.getenv("PORTAL_FILEAGENT_ACCESS_TOKEN", "").strip()
        filename = os.getenv("PORTAL_SYNC_FILENAME", "教师成果资助使用登记余额表.xlsx").strip()
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"} or not token or not filename.endswith(".xlsx")):
            raise RuntimeError("自动同步配置无效，请填写 FileAgent 服务根地址、访问令牌和余额表完整文件名。")
        research_filename = os.getenv("PORTAL_RESEARCH_FILENAME", "教师成果明细汇总（2019-2026）.xlsx").strip()
        if research_filename and not research_filename.endswith(".xlsx"):
            raise RuntimeError("PORTAL_RESEARCH_FILENAME 必须是成果总表的完整 .xlsx 文件名。")
        return cls(base, token, filename,
                   max(10, int(os.getenv("PORTAL_SYNC_INTERVAL_SECONDS", "60"))),
                   int(os.getenv("PORTAL_MAX_UPLOAD_MB", "20")) * 1024 * 1024,
                   research_filename=research_filename)


class FileAgentReader:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(NoRedirect())

    def get(self, path, *, binary=False):
        request = Request(self.config.base_url + path,
                          headers={"Authorization": "Bearer " + self.config.token})
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                limit = self.config.max_bytes if binary else (32 * 1024 * 1024 if path == "/api/working-copies" else 2 * 1024 * 1024)
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise SyncError("来源文件或响应超过大小限制。")
                return data if binary else json.loads(data)
        except HTTPError as exc:
            raise SyncError(f"FileAgent 返回 HTTP {exc.code}，请检查凭证、文件状态和服务地址。") from None
        except (URLError, TimeoutError, OSError):
            raise SyncError("无法连接 FileAgent，稍后自动重试。") from None
        except (ValueError, UnicodeError):
            raise SyncError("FileAgent 返回的内容无法解析。") from None

    def resolve(self):
        data = self.get("/api/structured-datasets/targets/by-filename?" +
                        urlencode({"filename": self.config.filename}))
        try:
            if data.get("status") != "ACTIVE" or data.get("filename") != self.config.filename:
                raise ValueError()
            return tuple(str(UUID(data[key])) for key in
                         ("dataset_id", "working_copy_id", "document_version_id"))
        except (TypeError, ValueError, KeyError, AttributeError):
            raise SyncError("来源不是唯一的活动已发布余额表。") from None

    def download(self, working_copy_id):
        meta = self.get(f"/api/working-copies/{working_copy_id}")
        content = self.get(f"/api/working-copies/{working_copy_id}/download", binary=True)
        if hashlib.sha256(content).hexdigest() != meta.get("content_sha256"):
            raise SyncError("来源文件校验不一致，等待下次同步。")
        return content, meta.get("current_version_id")

    def resolve_research(self):
        files = self.get("/api/working-copies")
        if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
            raise SyncError("FileAgent 文件列表无法读取。")
        matches = [item for item in files if item.get("filename") == self.config.research_filename
                   and item.get("status") == "ACTIVE"]
        if not matches:
            raise SyncError(f"未找到活动成果总表：{self.config.research_filename}。请核对完整文件名。")

        def created_at(item):
            try:
                instant = datetime.fromisoformat(item["created_at"])
                if instant.utcoffset() is None:
                    raise ValueError()
                return instant.astimezone(timezone.utc)
            except (TypeError, KeyError, ValueError):
                raise SyncError("同名成果总表缺少有效的带时区创建时间，无法确定最新上传文件。") from None

        # Choose the newest upload first. Never fall back to an older published
        # file while the newest upload is still publishing or needs repair.
        ranked = sorted(((created_at(item), item) for item in matches),
                        key=lambda pair: pair[0], reverse=True)
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            raise SyncError("最新的同名成果总表创建时间相同，无法确定同步来源，已保留上一版数据。")
        item = ranked[0][1]
        if "publication_status" not in item:
            raise SyncError("无法确认成果总表的正式发布状态，等待下次同步。")
        if item["publication_status"] is not None:
            raise SyncError("成果总表尚未发布完成，等待下次同步。")
        try:
            scope = tuple(str(UUID(item[key])) for key in ("id", "current_version_id"))
            self.research_resolved_filename = item["filename"]
            return scope
        except (TypeError, KeyError, ValueError):
            raise SyncError("成果总表缺少有效发布版本。") from None


class Synchronizer:
    def __init__(self, config, connection, reader=None):
        self.config, self.connection = config, connection
        self.reader = reader or FileAgentReader(config)

    def once(self):
        try:
            scope = self.reader.resolve()  # Existing resolver rejects unpublished candidates.
            source_key = json.dumps([self.config.base_url, *scope], separators=(",", ":"))
            with self.connection() as db:
                if state(db, "funding_source_key") == source_key:
                    self.record(db, "UP_TO_DATE", "已是最新发布版本。")
                    db.commit()
                    return
            data, version = self.reader.download(scope[1])
            if version != scope[2] or self.reader.resolve() != scope:
                raise SyncError("下载期间余额表版本发生变化，等待下次同步。")
            people = parse_funding_workbook(data)
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if state(db, "funding_source_key") != source_key:
                    save_snapshot(db, filename=self.config.filename,
                                  digest=hashlib.sha256(data).hexdigest(), actor="FILEAGENT_SYNC",
                                  people=people, source_key=source_key)
                self.record(db, "SYNCED", "金额和明细已同步。")
                set_state(db, "sync_last_success_at", datetime.now(timezone.utc).isoformat())
                db.commit()
        except (SyncError, WorkbookImportError) as exc:
            self.failed(str(exc))
        except Exception:
            # Neither URLs, credentials nor downloaded workbook contents enter logs.
            self.failed("同步失败，已保留上一份有效数据，下次自动重试。")

    def record(self, db, status, message):
        set_state(db, "sync_status", status)
        set_state(db, "sync_message", message)
        set_state(db, "sync_last_checked_at", datetime.now(timezone.utc).isoformat())

    def research_once(self):
        if not self.config.research_filename:
            return
        try:
            scope = self.reader.resolve_research()
            key = json.dumps([self.config.base_url, *scope], separators=(",", ":"))
            with self.connection() as db:
                if state(db, "research_source_key") == key:
                    self.research_record(db, "UP_TO_DATE", "资助总额明细已是最新版本。")
                    db.commit()
                    return
            data, version = self.reader.download(scope[0])
            if version != scope[1] or self.reader.resolve_research() != scope:
                raise SyncError("成果总表下载期间版本变化，等待下次同步。")
            people = parse_research_workbook(data)
            actual_filename = getattr(self.reader, "research_resolved_filename", None)
            if not isinstance(actual_filename, str):
                actual_filename = self.config.research_filename
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if state(db, "research_source_key") != key:
                    save_research(db, filename=actual_filename, data=data,
                                  version_id=scope[1], source_key=key, people=people)
                self.research_record(db, "SYNCED", "资助总额及逐项成果明细已同步。")
                db.commit()
        except (SyncError, WorkbookImportError) as exc:
            self.research_failed(str(exc))
        except Exception:
            self.research_failed("资助总额明细同步失败，已保留上一版数据。")

    def research_record(self, db, status, message):
        set_state(db, "research_sync_status", status)
        set_state(db, "research_sync_message", message)
        set_state(db, "research_sync_checked_at", datetime.now(timezone.utc).isoformat())

    def research_failed(self, message):
        with self.connection() as db:
            self.research_record(db, "FAILED", message)
            db.commit()
        logging.getLogger(__name__).warning("资助总额明细同步未完成：%s", message)

    def failed(self, message):
        with self.connection() as db:
            self.record(db, "FAILED", message)
            db.commit()
        logging.getLogger(__name__).warning("自动同步未完成：%s", message)

    async def run(self, stop):
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.once)
                await asyncio.to_thread(self.research_once)
            except Exception:
                # A transient local DB failure must not terminate the polling task.
                logging.getLogger(__name__).error("自动同步状态暂时无法保存，稍后继续重试。")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.config.interval)
            except asyncio.TimeoutError:
                pass
