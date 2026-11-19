import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import main
from app.research import HEADERS, parse_research_workbook, read_research, save_research
from app.snapshots import state
from app.sync import FileAgentReader, SyncConfig, Synchronizer, SyncError


def source_book(amount=100, item_amount=None):
    book = Workbook()
    summary = book.active
    summary.title = '汇总'
    summary.append(['序号', '姓名', '资助总额（元）'])
    for index, name in enumerate(['张老师', '李老师'], 1):
        summary.append([index, name, amount])
        sheet = book.create_sheet(name)
        sheet.append([name + '成果明细'])
        sheet.append(HEADERS)
        sheet.append([2026, 1, '论文', '期刊', name + '论文', 'SCI', '论文资助', '2026-01-01', 'A类',
                      item_amount if item_amount is not None else amount, None, '已确认', None])
        sheet.append(['合计', None, None, None, None, None, None, None, None, amount])
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    return buffer.getvalue()


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'PORTAL_TEST_SQLITE': 'true', 'PORTAL_SYNC_ENABLED': 'false',
                                          'PORTAL_JWT_SECRET': 'a' * 40, 'PORTAL_ADMIN_USERNAME': 'admin',
                                          'PORTAL_ADMIN_PASSWORD': 'admin-password-test'})
        self.env.start()
        self.paths = patch.multiple(main, DATA_DIR=Path(self.temp.name), DB_PATH=Path(self.temp.name) / 'portal.sqlite3')
        self.paths.start()
        main.init_db()
        self.config = SyncConfig('http://fileagent', 'secret', '余额.xlsx', research_filename='成果.xlsx')
        self.reader = Mock()
        self.reader.resolve_research.return_value = ('working', 'version-1')
        self.reader.download.return_value = (source_book(), 'version-1')
        self.sync = Synchronizer(self.config, main.connection, self.reader)

    def tearDown(self):
        self.paths.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_source_fields_and_aggregate_are_read(self):
        people = parse_research_workbook(source_book())
        self.assertEqual(len(people), 2)
        self.assertEqual(people[0]['items'][0]['title'], '张老师论文')
        self.assertEqual(people[0]['items'][0]['amount'], '100.00')
        self.assertFalse(people[0]['needs_review'])
        self.assertTrue(parse_research_workbook(source_book(item_amount=80))[0]['needs_review'])

    def test_only_logged_in_teacher_income_is_returned_even_without_balance_data(self):
        self.sync.research_once()
        with main.connection() as db:
            db.execute("INSERT INTO staff(employee_no,name,active) VALUES('001',?,1)", ('张老师',))
            db.commit()
        with TestClient(main.app) as client:
            self.assertEqual(client.post('/api/register', json={'employee_no': '001', 'password': 'test-password'}).status_code, 201)
            login = client.post('/api/login', json={'employee_no': '001', 'password': 'test-password'})
            headers = {'Authorization': 'Bearer ' + login.json()['access_token']}
            response = client.get('/api/my/funding?name=李老师', headers=headers)
            self.assertEqual(response.status_code, 200)
            data = response.json()['research']
            self.assertEqual(data['status'], 'READY')
            self.assertEqual(data['items'][0]['title'], '张老师论文')
            self.assertNotIn('李老师', response.text)
            self.assertEqual(client.get('/api/my/funding').status_code, 401)

    def test_unchanged_source_does_not_download_again(self):
        self.sync.research_once()
        self.sync.research_once()
        self.assertEqual(self.reader.download.call_count, 1)

    def test_new_source_replaces_items_and_reports_balance_total_mismatch(self):
        self.sync.research_once()
        self.reader.resolve_research.return_value = ('working', 'version-2')
        self.reader.download.return_value = (source_book(95), 'version-2')
        self.sync.research_once()
        with main.connection() as db:
            result = read_research(db, '张老师', '100.00')
            self.assertEqual(result['status'], 'NEEDS_REVIEW')
            self.assertEqual(len(result['items']), 1)
            self.assertEqual(result['items'][0]['amount'], '95.00')
            self.assertEqual(result['version_id'], 'version-2')

    def test_failure_preserves_previous_income_and_does_not_change_balance(self):
        self.sync.research_once()
        with main.connection() as db:
            pointer = state(db, 'research_snapshot_id')
        self.reader.resolve_research.side_effect = SyncError('source unavailable')
        self.sync.research_once()
        with main.connection() as db:
            self.assertEqual(state(db, 'research_snapshot_id'), pointer)
            self.assertEqual(state(db, 'research_sync_status'), 'FAILED')
            self.assertIsNone(state(db, 'funding_snapshot_id'))
            self.assertEqual(read_research(db, '张老师', '100.00')['items'][0]['amount'], '100.00')

    def test_write_failure_rolls_back_income_pointer_and_rows(self):
        self.sync.research_once()
        self.reader.resolve_research.return_value = ('working', 'version-2')
        self.reader.download.return_value = (source_book(90), 'version-2')
        def fail(*args, **kwargs):
            save_research(*args, **kwargs)
            raise RuntimeError('injected')
        with patch('app.sync.save_research', side_effect=fail):
            self.sync.research_once()
        with main.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM research_snapshots').fetchone()[0], 1)
            self.assertEqual(read_research(db, '张老师', '100.00')['research_total'], '100.00')

    def test_source_version_race_does_not_publish_snapshot(self):
        self.reader.resolve_research.side_effect = [('working', 'version-1'), ('working', 'version-2')]
        self.sync.research_once()
        with main.connection() as db:
            self.assertIsNone(state(db, 'research_snapshot_id'))

    def research_file(self, **changes):
        return {'filename': '成果.xlsx', 'status': 'ACTIVE', 'publication_status': None,
                'id': '11111111-1111-4111-8111-111111111111',
                'current_version_id': '22222222-2222-4222-8222-222222222222',
                'created_at': '2026-10-20T21:51:19.747884+08:00', **changes}

    def test_source_resolver_accepts_single_published_file(self):
        reader = FileAgentReader(self.config)
        item = self.research_file()
        with patch.object(reader, 'get', return_value=[item]):
            self.assertEqual(reader.resolve_research(), (item['id'], item['current_version_id']))
            self.assertEqual(reader.research_resolved_filename, '成果.xlsx')

    def test_newest_creation_wins_regardless_of_list_order_update_time_or_timezone(self):
        reader = FileAgentReader(self.config)
        oldest = self.research_file(updated_at='2026-11-06T23:59:59+08:00')
        newest = self.research_file(id='ced287a8-6874-49fc-bd4c-d0b156bed326',
                                    created_at='2026-10-21T02:06:45.567845+00:00')
        # Lexically later than newest, but earlier once converted to UTC.
        middle = self.research_file(created_at='2026-10-21T09:00:00+08:00')
        for files in [[newest, oldest, middle], [middle, oldest, newest]]:
            with self.subTest(files=files), patch.object(reader, 'get', return_value=files):
                self.assertEqual(reader.resolve_research()[0], newest['id'])

    def test_resolver_excludes_archived_and_inexact_filenames(self):
        reader = FileAgentReader(self.config)
        item = self.research_file()
        others = [self.research_file(status='ARCHIVED', created_at='2026-11-06T00:00:00+08:00'),
                  self.research_file(filename='其他成果.xlsx', created_at='2026-11-06T00:00:00+08:00')]
        with patch.object(reader, 'get', return_value=others + [item]):
            self.assertEqual(reader.resolve_research()[0], item['id'])
        with patch.object(reader, 'get', return_value=others):
            with self.assertRaisesRegex(SyncError, '未找到活动成果总表'):
                reader.resolve_research()

    def test_latest_unpublished_does_not_fall_back_and_syncs_after_publication(self):
        reader = FileAgentReader(self.config)
        old = self.research_file()
        new = self.research_file(id='ced287a8-6874-49fc-bd4c-d0b156bed326',
                                created_at='2026-10-21T10:06:45.567845+08:00')
        with patch.object(reader, 'get', return_value=[old, new]):
            reader.get.return_value[1]['publication_status'] = 'DB_COMMITTED'
            with self.assertRaisesRegex(SyncError, '尚未发布完成'):
                reader.resolve_research()
            reader.get.return_value[1]['publication_status'] = None
            self.assertEqual(reader.resolve_research()[0], new['id'])
        for change in [{'publication_status': 'NEEDS_REPAIR'}, {'current_version_id': None}]:
            with self.subTest(change=change), patch.object(reader, 'get', return_value=[old, {**new, **change}]):
                with self.assertRaises(SyncError):
                    reader.resolve_research()

    def test_uncertain_creation_or_publication_metadata_blocks_source_selection(self):
        reader = FileAgentReader(self.config)
        item = self.research_file()
        malformed = [{**item, 'created_at': value} for value in [None, '', 'not-a-date', '2026-10-21T10:06:45']]
        malformed.extend([{key: value for key, value in item.items() if key != missing}
                          for missing in ['created_at', 'publication_status']])
        for invalid in malformed:
            with self.subTest(invalid=invalid), patch.object(reader, 'get', return_value=[invalid]):
                with self.assertRaises(SyncError):
                    reader.resolve_research()
        same_instant = {**item, 'id': 'ced287a8-6874-49fc-bd4c-d0b156bed326',
                        'created_at': '2026-10-20T13:51:19.747884Z'}
        with patch.object(reader, 'get', return_value=[item, same_instant]):
            with self.assertRaisesRegex(SyncError, '创建时间相同'):
                reader.resolve_research()

    def test_same_name_replacement_automatically_follows_new_file_id(self):
        reader = FileAgentReader(self.config)
        sync = Synchronizer(self.config, main.connection, reader)
        old = self.research_file()
        new = self.research_file(id='ced287a8-6874-49fc-bd4c-d0b156bed326',
                                current_version_id='33333333-3333-4333-8333-333333333333',
                                created_at='2026-10-21T10:06:45.567845+08:00')
        with patch.object(reader, 'get', return_value=[old]), patch.object(
                reader, 'download', return_value=(source_book(), old['current_version_id'])):
            sync.research_once()
        for status in ['DB_COMMITTED', 'NEEDS_REPAIR', None]:
            with self.subTest(status=status), patch.object(reader, 'get', return_value=[old, {**new, 'publication_status': status}]), patch.object(
                    reader, 'download', return_value=(source_book(90), new['current_version_id'])) as download:
                sync.research_once()
                with main.connection() as db:
                    result = read_research(db, '张老师', None)
                    self.assertEqual(result['version_id'], new['current_version_id'] if status is None else old['current_version_id'])
                    self.assertIsNone(state(db, 'funding_snapshot_id'))
                self.assertEqual(download.call_count, 1 if status is None else 0)
        with main.connection() as db:
            self.assertIn(new['id'], state(db, 'research_source_key'))

    def test_new_file_uploaded_during_download_preserves_previous_snapshot(self):
        self.sync.research_once()
        with main.connection() as db:
            previous = state(db, 'research_snapshot_id')
        reader = FileAgentReader(self.config)
        old = self.research_file()
        new = self.research_file(id='ced287a8-6874-49fc-bd4c-d0b156bed326',
                                created_at='2026-10-21T10:06:45.567845+08:00')
        with patch.object(reader, 'get', side_effect=[[old], [old, new]]), patch.object(
                reader, 'download', return_value=(source_book(), old['current_version_id'])):
            Synchronizer(self.config, main.connection, reader).research_once()
        with main.connection() as db:
            self.assertEqual(state(db, 'research_snapshot_id'), previous)
            self.assertEqual(state(db, 'research_sync_status'), 'FAILED')

    def test_obsolete_pinned_id_does_not_override_filename_configuration(self):
        with patch.dict(os.environ, {'PORTAL_SYNC_ENABLED': 'true', 'PORTAL_FILEAGENT_BASE_URL': 'http://fileagent',
                                    'PORTAL_FILEAGENT_ACCESS_TOKEN': 'secret', 'PORTAL_SYNC_FILENAME': '余额.xlsx',
                                    'PORTAL_RESEARCH_FILENAME': '成果.xlsx',
                                    'PORTAL_RESEARCH_WORKING_COPY_ID': 'obsolete-even-if-invalid'}):
            config = SyncConfig.from_env()
        self.assertEqual(config.research_filename, '成果.xlsx')
        self.assertFalse(hasattr(config, 'research_working_copy_id'))
