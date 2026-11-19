import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from openpyxl import Workbook

from app import main
from app.snapshots import state
from app.sync import FileAgentReader, SyncConfig, SyncError, Synchronizer
from app.workbooks import (SUMMARY_HEADERS, DETAIL_HEADERS, parse_funding_workbook,
                           parse_roster, WorkbookImportError)


def workbook(amount=20):
    book = Workbook()
    summary = book.active
    summary.title = "使用登记汇总"
    summary.append(SUMMARY_HEADERS)
    summary.append([1, "张老师", 100, 0, amount, 100 - amount])
    sheet = book.create_sheet("张老师")
    sheet.append(DETAIL_HEADERS)
    sheet.append([1, "差旅费", "2026-11-05", amount, "已核对"])
    sheet.append(["总科研成果资助金额", 100])
    data = io.BytesIO()
    book.save(data)
    book.close()
    return data.getvalue()


class Reader:
    scope = ("dataset", "working", "version-1")

    def __init__(self):
        self.content = workbook()
        self.calls = 0
        self.fail = False
        self.changed = False

    def resolve(self):
        if self.fail:
            raise SyncError("FileAgent 返回 HTTP 423，正在发布。")
        return self.scope

    def download(self, _):
        self.calls += 1
        if self.changed:
            self.scope = ("dataset", "working", "version-2")
        return self.content, "version-1"


class SyncTests(unittest.TestCase):
    def seed_staff(self):
        with main.connection() as db:
            db.executemany("INSERT INTO staff(employee_no,name,active) VALUES(?,?,1)",
                           [("001", "张老师"), ("002", "李老师")])
            db.commit()

    def teacher_token(self, client, employee="001"):
        result = client.post("/api/register", json={"employee_no": employee, "password": "test-password"})
        self.assertEqual(result.status_code, 201)
        login = client.post("/api/login", json={"employee_no": employee, "password": "test-password"})
        self.assertEqual(login.status_code, 200)
        return {"Authorization": "Bearer " + login.json()["access_token"]}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.multiple(main, DATA_DIR=Path(self.temp.name),
                                    DB_PATH=Path(self.temp.name) / "query.sqlite3")
        self.paths.start()
        self.env = patch.dict(os.environ, {"PORTAL_JWT_SECRET": "a" * 40,
                                           "PORTAL_ADMIN_USERNAME": "admin",
                                           "PORTAL_ADMIN_PASSWORD": "test-password-12345",
                                           "PORTAL_TEST_SQLITE": "true",
                                           "PORTAL_SYNC_ENABLED": "false"})
        self.env.start()
        main.init_db()
        self.reader = Reader()
        self.sync = Synchronizer(SyncConfig("http://fileagent", "secret", "余额表.xlsx"),
                                 main.connection, self.reader)

    def tearDown(self):
        self.paths.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_first_sync_and_same_version_does_not_download_again(self):
        self.sync.once()
        self.sync.once()
        with main.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM funding_snapshots").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT remaining FROM funding_people").fetchone()[0], "80.00")
            self.assertEqual(db.execute("SELECT registration FROM funding_details").fetchone()[0], "差旅费")
        self.assertEqual(self.reader.calls, 1)

    def test_new_version_replaces_amounts_and_details_together(self):
        self.sync.once()
        self.reader.scope = ("dataset", "working", "version-2")
        self.reader.content = workbook(35)
        self.reader.download = lambda _: (self.reader.content, "version-2")
        self.sync.once()
        with main.connection() as db:
            current = state(db, "funding_snapshot_id")
            self.assertEqual(db.execute("SELECT remaining FROM funding_people WHERE snapshot_id=?", (current,)).fetchone()[0], "65.00")
            self.assertEqual(db.execute("SELECT usage_amount FROM funding_details WHERE snapshot_id=?", (current,)).fetchone()[0], "35.00")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM funding_snapshots").fetchone()[0], 2)

    def test_unpublished_source_preserves_old_data_and_retries(self):
        self.sync.once()
        self.reader.fail = True
        self.sync.once()
        with main.connection() as db:
            self.assertEqual(state(db, "sync_status"), "FAILED")
            self.assertEqual(state(db, "funding_snapshot_id"), "1")
        self.reader.fail = False
        self.sync.once()
        with main.connection() as db:
            self.assertEqual(state(db, "sync_status"), "UP_TO_DATE")

    def test_invalid_workbook_preserves_old_pointer(self):
        self.sync.once()
        self.reader.scope = ("dataset", "working", "version-2")
        self.reader.download = lambda _: (b"not a zip", "version-2")
        self.sync.once()
        with main.connection() as db:
            self.assertEqual(state(db, "funding_snapshot_id"), "1")
            self.assertEqual(state(db, "sync_status"), "FAILED")

    def test_version_change_during_download_is_rejected(self):
        self.reader.changed = True
        self.sync.once()
        with main.connection() as db:
            self.assertIsNone(state(db, "funding_snapshot_id"))
            self.assertEqual(state(db, "sync_status"), "FAILED")

    def test_snapshot_write_failure_rolls_back_all_rows(self):
        self.sync.once()
        self.reader.scope = ("dataset", "working", "version-2")
        self.reader.download = lambda _: (workbook(30), "version-2")
        original = main.connection
        from app.snapshots import save_snapshot
        def fail_after_write(*args, **kwargs):
            save_snapshot(*args, **kwargs)
            raise RuntimeError("injected failure")
        with patch("app.sync.save_snapshot", side_effect=fail_after_write):
            self.sync.once()
        with original() as db:
            self.assertEqual(state(db, "funding_snapshot_id"), "1")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM funding_snapshots").fetchone()[0], 1)

    def test_hash_mismatch_rejected(self):
        reader = FileAgentReader(self.sync.config)
        with patch.object(reader, "get", side_effect=[{"content_sha256": "wrong"}, workbook()]):
            with self.assertRaises(SyncError):
                reader.download("working")

    def test_ambiguous_or_nonactive_resolve_cannot_be_used(self):
        reader = FileAgentReader(self.sync.config)
        with patch.object(reader, "get", return_value={"status": "ARCHIVED"}):
            with self.assertRaises(SyncError):
                reader.resolve()

    def test_api_exposes_sync_status_and_blocks_manual_override(self):
        self.sync.once()
        with TestClient(main.app) as client:
            login = client.post("/api/login", json={"employee_no": "admin", "password": "test-password-12345"})
            self.assertEqual(login.status_code, 200)
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            status = client.get("/api/admin/status", headers=headers)
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["sync"]["status"], "SYNCED")
            self.assertNotIn("secret", status.text)
            with patch("app.main.SyncConfig.from_env", return_value=self.sync.config):
                result = client.post("/api/admin/funding/import", headers=headers,
                                     files={"file": ("balance.xlsx", workbook())})
                self.assertEqual(result.status_code, 409)
            self.assertEqual(client.get("/api/admin/status").status_code, 401)

    def test_registration_duplicates_and_self_only_query(self):
        self.seed_staff()
        self.sync.once()
        with TestClient(main.app) as client:
            headers = self.teacher_token(client)
            duplicate = client.post("/api/register", json={"employee_no": "001", "password": "different-password"})
            self.assertEqual(duplicate.status_code, 409)
            unknown = client.post("/api/register", json={"employee_no": "999", "password": "test-password"})
            self.assertEqual(unknown.status_code, 404)
            result = client.get("/api/my/funding?employee_no=002&name=other", headers=headers)
            self.assertEqual(result.json()["name"], "张老师")
            self.assertEqual(result.json()["remaining"], "80.00")
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/api/admin/status", headers=headers).status_code, 403)

    def test_password_changes_and_admin_reset_revoke_old_sessions(self):
        self.seed_staff()
        with TestClient(main.app) as client:
            headers = self.teacher_token(client)
            invalid = client.post("/api/password/change", headers=headers,
                                  json={"current_password": "wrong", "new_password": "new-password"})
            self.assertEqual(invalid.status_code, 400)
            changed = client.post("/api/password/change", headers=headers,
                                  json={"current_password": "test-password", "new_password": "new-password"})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(client.get("/api/me", headers=headers).status_code, 401)
            login = client.post("/api/login", json={"employee_no": "001", "password": "new-password"})
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            admin = client.post("/api/login", json={"employee_no": "admin", "password": "test-password-12345"})
            admin_headers = {"Authorization": "Bearer " + admin.json()["access_token"]}
            reset = client.post("/api/admin/users/001/reset-password", headers=admin_headers,
                                json={"new_password": "reset-password"})
            self.assertEqual(reset.status_code, 200)
            self.assertEqual(client.get("/api/me", headers=headers).status_code, 401)

    def test_roster_refresh_invalidates_old_binding_sessions(self):
        self.seed_staff()
        with TestClient(main.app) as client:
            teacher = self.teacher_token(client)
            admin = client.post("/api/login", json={"employee_no": "admin", "password": "test-password-12345"})
            admin_headers = {"Authorization": "Bearer " + admin.json()["access_token"]}
            book = Workbook()
            book.active.append(["工号", "姓名"])
            book.active.append(["002", "李老师"])
            stream = io.BytesIO()
            book.save(stream)
            book.close()
            result = client.post("/api/admin/roster/import", headers=admin_headers,
                                 files={"file": ("roster.xlsx", stream.getvalue())})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(client.get("/api/my/funding", headers=teacher).status_code, 401)
            self.assertEqual(client.post("/api/login", json={"employee_no": "001", "password": "test-password"}).status_code, 403)

    def test_review_balance_is_not_exposed_by_api(self):
        self.seed_staff()
        book = Workbook()
        book.active.title = "使用登记汇总"
        book.active.append(SUMMARY_HEADERS)
        book.active.append([1, "张老师", 100, 0, 20, 80])
        sheet = book.create_sheet("张老师")
        sheet.append(DETAIL_HEADERS)
        sheet.append([1, "差旅", "2026-11-05", None, "金额待核对"])
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        self.reader.content = stream.getvalue()
        self.sync.once()
        with TestClient(main.app) as client:
            headers = self.teacher_token(client)
            data = client.get("/api/my/funding", headers=headers).json()
            self.assertTrue(data["needs_review"])
            self.assertIsNone(data["remaining"])

    def test_roster_uses_first_sheet_and_preserves_leading_zeros(self):
        book = Workbook()
        book.active.append(["工号", "姓名"])
        book.active.append([123, "张老师"])
        book.active.cell(2, 1).number_format = "000000"
        book.create_sheet("other")
        book.active = 1
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        self.assertEqual(parse_roster(stream.getvalue()), [{"employee_no": "000123", "name": "张老师"}])

    def test_invalid_amounts_and_uncached_formulas_never_become_valid_balance(self):
        for amount in [-1, 1.234, "=1+1", "NaN", True]:
            with self.subTest(amount=amount):
                book = Workbook()
                book.active.title = "使用登记汇总"
                book.active.append(SUMMARY_HEADERS)
                book.active.append([1, "张老师", 100, 0, 0, 100])
                detail = book.create_sheet("张老师")
                detail.append(DETAIL_HEADERS)
                detail.append([1, "支出", "2026-11-05", amount, ""])
                stream = io.BytesIO()
                book.save(stream)
                book.close()
                person = parse_funding_workbook(stream.getvalue())[0]
                self.assertTrue(person["needs_review"])
                self.assertIsNone(person["details"][0]["usage_amount"])

    def test_broken_detail_headers_stop_import_instead_of_discarding_records(self):
        book = Workbook()
        book.active.title = "使用登记汇总"
        book.active.append(SUMMARY_HEADERS)
        book.active.append([1, "张老师", 100, 0, 0, 100])
        book.create_sheet("张老师").append(["incorrect"])
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        with self.assertRaises(WorkbookImportError):
            parse_funding_workbook(stream.getvalue())

    def test_school_shared_ip_does_not_block_different_teacher_registrations(self):
        with main.connection() as db:
            db.executemany("INSERT INTO staff(employee_no,name,active) VALUES(?,?,1)",
                           [(str(index), f"教师{index}") for index in range(10)])
            db.commit()
        with TestClient(main.app) as client:
            for index in range(10):
                result = client.post("/api/register", json={"employee_no": str(index), "password": "test-password"})
                self.assertEqual(result.status_code, 201)

    def test_signed_malformed_claims_are_rejected_cleanly(self):
        import hmac
        import json
        from app.security import _b64
        for claims in [[1, 2], {"sub": "admin", "role": "admin", "exp": 9999999999, "ver": "invalid"}]:
            payload = _b64(json.dumps(claims).encode())
            signature = _b64(hmac.new(main.JWT_SECRET.encode(), payload.encode(), "sha256").digest())
            with TestClient(main.app) as client:
                result = client.get("/api/me", headers={"Authorization": "Bearer " + payload + "." + signature})
                self.assertEqual(result.status_code, 401)

    def test_polling_resumes_after_temporary_database_failure(self):
        import asyncio
        from dataclasses import replace
        async def scenario():
            stop = asyncio.Event()
            sync = Synchronizer(replace(self.sync.config, interval=0), main.connection, self.reader)
            calls = []
            def once():
                calls.append(1)
                if len(calls) == 1:
                    raise OSError("temporary DB failure")
                stop.set()
            with patch.object(sync, "once", side_effect=once):
                await asyncio.wait_for(sync.run(stop), timeout=3)
            self.assertEqual(len(calls), 2)
        asyncio.run(scenario())

    def test_duplicate_names_block_teacher_lookup(self):
        self.seed_staff()
        with main.connection() as db:
            db.execute("UPDATE staff SET name=? WHERE employee_no='002'", ("张老师",))
            db.commit()
        self.sync.once()
        with TestClient(main.app) as client:
            headers = self.teacher_token(client)
            self.assertEqual(client.get("/api/my/funding", headers=headers).status_code, 409)

    def test_page_assets_and_cache_headers(self):
        with TestClient(main.app) as client:
            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn('name="confirm_password"', page.text)
            self.assertIn('/static/portal.js', page.text)
            self.assertIn('/static/portal.css', page.text)
            directives = {part.strip().split()[0]: part.strip().split()[1:]
                          for part in page.headers['content-security-policy'].split(';') if part.strip()}
            self.assertIn("'self'", directives['style-src'], 'Browser policy must permit the stylesheet URL')
            self.assertIn('role="tablist"', page.text)
            self.assertNotIn('id="passwordForm"', page.text)
            self.assertNotIn('id="changePasswordButton"', page.text)
            styles = client.get('/static/portal.css')
            self.assertEqual(styles.status_code, 200)
            self.assertIn('text/css', styles.headers['content-type'])
            self.assertNotIn("'unsafe-inline'", page.headers["content-security-policy"].split("script-src")[1])
            script = client.get("/static/portal.js")
            self.assertEqual(script.status_code, 200)
            self.assertEqual(script.headers["cache-control"], "no-store")
            import re
            selectors = re.search(r"const REQUIRED_SELECTORS = \[(.*?)\];", script.text, re.S).group(1)
            required_ids = set(re.findall(r"'#([A-Za-z0-9_]+)'", selectors))
            html_ids = set(re.findall(r'id="([A-Za-z0-9_]+)"', page.text))
            self.assertTrue(required_ids)
            self.assertEqual(required_ids - html_ids, set(), 'HTML must contain every element required by the script')

    def test_staff_can_be_managed_without_excel(self):
        with TestClient(main.app) as client:
            login = client.post("/api/login", json={"employee_no": "admin", "password": "test-password-12345"})
            admin = {"Authorization": "Bearer " + login.json()["access_token"]}
            created = client.post("/api/admin/staff", headers=admin,
                                  json={"employee_no": "001", "name": "张老师"})
            self.assertEqual(created.status_code, 201)
            self.assertEqual(client.post("/api/admin/staff", headers=admin,
                             json={"employee_no": "001", "name": "other"}).status_code, 409)
            teacher = self.teacher_token(client)
            listing = client.get("/api/admin/staff?limit=1", headers=admin).json()
            self.assertEqual(listing["total"], 1)
            self.assertEqual(listing["staff"][0]["employee_no"], "001")
            self.assertTrue(listing["staff"][0]["registered"])
            changed = client.put("/api/admin/staff", headers=admin,
                                 json={"employee_no": "001", "name": "张新名", "active": True})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(client.get("/api/me", headers=teacher).status_code, 401)
            fresh = client.post("/api/login", json={"employee_no": "001", "password": "test-password"})
            self.assertEqual(fresh.json()["name"], "张新名")
            client.put("/api/admin/staff", headers=admin,
                       json={"employee_no": "001", "name": "张新名", "active": False})
            self.assertEqual(client.post("/api/login", json={"employee_no": "001", "password": "test-password"}).status_code, 403)

    def test_staff_management_requires_admin_and_valid_data(self):
        self.seed_staff()
        with TestClient(main.app) as client:
            teacher = self.teacher_token(client)
            self.assertEqual(client.get("/api/admin/staff").status_code, 401)
            self.assertEqual(client.get("/api/admin/staff", headers=teacher).status_code, 403)
            self.assertEqual(client.post("/api/admin/staff", headers=teacher,
                             json={"employee_no": "003", "name": "other"}).status_code, 403)
            login = client.post("/api/login", json={"employee_no": "admin", "password": "test-password-12345"})
            admin = {"Authorization": "Bearer " + login.json()["access_token"]}
            self.assertEqual(client.post("/api/admin/staff", headers=admin,
                             json={"employee_no": "   ", "name": "other"}).status_code, 422)
            self.assertEqual(client.post("/api/admin/staff", headers=admin,
                             json={"employee_no": "admin", "name": "other"}).status_code, 409)
            self.assertEqual(client.put("/api/admin/staff", headers=admin,
                             json={"employee_no": "missing", "name": "other"}).status_code, 404)
            self.assertEqual(client.get("/api/admin/staff?offset=-1", headers=admin).status_code, 422)

    def test_local_roster_import_preserves_accounts_and_unlisted_staff(self):
        from app.manage import import_roster_file
        self.seed_staff()
        book = Workbook()
        book.active.append(["姓名", "职工号", "备注"])
        book.active.append(["新教师", "003", ""])
        source = Path(self.temp.name) / "roster.xlsx"
        book.save(source)
        book.close()
        result = import_roster_file(source, main.DB_PATH)
        self.assertEqual(result["created"], 1)
        with main.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM staff WHERE active=1").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0], 0)
        self.assertEqual(import_roster_file(source, main.DB_PATH)["status"], "ALREADY_IMPORTED")

    def test_local_import_can_initialize_database_without_creating_admin(self):
        from app.manage import import_roster_file
        book = Workbook()
        book.active.append(["姓名", "职工号"])
        book.active.append(["张老师", "001"])
        source = Path(self.temp.name) / "initial.xlsx"
        book.save(source)
        book.close()
        database = Path(self.temp.name) / "fresh.sqlite3"
        self.assertEqual(import_roster_file(source, database)["count"], 1)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT employee_no FROM staff").fetchone()[0], "001")

    def test_registration_limit_handles_repeated_requests_and_window_expiry(self):
        from types import SimpleNamespace
        from fastapi import HTTPException
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        with main.connection() as db:
            with patch("time.time", return_value=10000):
                for _ in range(8):
                    main.rate_limit(db, request, "register:test", limit=8, window_seconds=3600)
                with self.assertRaises(HTTPException) as error:
                    main.rate_limit(db, request, "register:test", limit=8, window_seconds=3600)
                self.assertEqual(error.exception.status_code, 429)
                main.rate_limit(db, request, "register:other", limit=8, window_seconds=3600)
            with patch("time.time", return_value=13600):
                main.rate_limit(db, request, "register:test", limit=8, window_seconds=3600)


    def test_admin_initial_password_error_matches_existing_minimum(self):
        """密码提示与现有八位门槛一致，不通过更新意外阻止旧配置启动。"""
        with patch.dict(os.environ, {"PORTAL_ADMIN_PASSWORD": "short-pass1"}):
            main.init_db()
        with patch.dict(os.environ, {"PORTAL_ADMIN_PASSWORD": "short"}):
            with self.assertRaisesRegex(RuntimeError, "8 characters"):
                main.init_db()


if __name__ == "__main__":
    unittest.main()
