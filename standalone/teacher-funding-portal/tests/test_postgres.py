import os
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from app.database import PostgresConnection, database_url, initialize_schema, postgres_sql


class PostgresAdapterTests(unittest.TestCase):
    def test_registration_limit_query_qualifies_all_conflict_source_columns(self):
        from app.main import rate_limit
        raw = MagicMock()
        raw.cursor.return_value.fetchone.return_value = {"attempts": 1}
        rate_limit(PostgresConnection(raw), SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
                   "register:001", limit=8, window_seconds=3600)
        sql, params = raw.cursor.return_value.execute.call_args_list[0].args
        self.assertIn("INSERT INTO teacher_funding_rate_limits", sql)
        self.assertIn("WHEN teacher_funding_rate_limits.window_start<=%s", sql)
        self.assertIn("ELSE teacher_funding_rate_limits.attempts+1", sql)
        self.assertIn("THEN excluded.window_start ELSE teacher_funding_rate_limits.window_start", sql)
        self.assertNotIn("WHEN window_start", sql)
        self.assertNotIn("MIN(", sql)
        self.assertEqual(sql.count("%s"), len(params))

    def test_table_names_are_isolated_but_values_and_literals_are_not_changed(self):
        query = postgres_sql("SELECT * FROM users JOIN staff ON staff.employee_no=users.employee_no "
                             "WHERE users.username=? AND 'users staff ?'='users staff ?'")
        self.assertIn("FROM teacher_funding_users JOIN teacher_funding_staff", query)
        self.assertIn("username=%s", query)
        self.assertIn("'users staff ?'", query)

    def test_generated_ids_use_returning_and_dont_affect_normal_selects(self):
        raw = MagicMock()
        raw.cursor.return_value.fetchone.return_value = [42]
        connection = PostgresConnection(raw)
        cursor = connection.execute("INSERT INTO roster_imports(filename) VALUES(?)", ("staff.xlsx",))
        self.assertEqual(cursor.lastrowid, 42)
        self.assertEqual(raw.cursor.return_value.execute.call_args.args,
                         ("INSERT INTO teacher_funding_roster_imports(filename) VALUES(%s) RETURNING id", ("staff.xlsx",)))
        connection.execute("SELECT * FROM staff WHERE employee_no=?", ("001",))
        self.assertEqual(raw.cursor.return_value.execute.call_args.args,
                         ("SELECT * FROM teacher_funding_staff WHERE employee_no=%s", ("001",)))

    def test_schema_has_serial_ids_timestamps_and_only_prefixed_tables(self):
        raw = MagicMock()
        initialize_schema(PostgresConnection(raw))
        statements = [call.args[0] for call in raw.cursor.return_value.execute.call_args_list]
        tables = [query for query in statements if "CREATE TABLE" in query]
        self.assertEqual(len(tables), 13)
        self.assertTrue(all("CREATE TABLE IF NOT EXISTS teacher_funding_" in query for query in tables))
        self.assertTrue(any("BIGSERIAL PRIMARY KEY" in query for query in tables))
        self.assertTrue(any("created_at TIMESTAMPTZ" in query for query in tables))
        self.assertTrue(all("AUTOINCREMENT" not in query for query in statements))
        raw.commit.assert_called_once()

    def test_postgres_transaction_lock_and_least_translation(self):
        raw = MagicMock()
        db = PostgresConnection(raw)
        db.execute("BEGIN IMMEDIATE")
        self.assertIn("pg_advisory_xact_lock", raw.cursor.return_value.execute.call_args.args[0])
        self.assertEqual(postgres_sql("MIN(attempts+1,?)"), "LEAST(attempts+1,%s)")

    def test_database_url_must_be_explicit_postgresql(self):
        with patch.dict(os.environ, {"PORTAL_DATABASE_URL": ""}):
            with self.assertRaises(RuntimeError):
                database_url()
        with patch.dict(os.environ, {"PORTAL_DATABASE_URL": "sqlite:///old.db"}):
            with self.assertRaises(RuntimeError):
                database_url()
        with patch.dict(os.environ, {"PORTAL_DATABASE_URL": "postgresql+psycopg2://user:pass@host/db"}):
            self.assertEqual(database_url(), "postgresql://user:pass@host/db")


if __name__ == "__main__":
    unittest.main()
