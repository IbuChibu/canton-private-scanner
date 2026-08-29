import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database
import demo


class DemoLauncherTests(unittest.TestCase):
    def test_env_loader_is_secret_safe_and_does_not_override_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "# ignored\n"
                "export C8_CLIENT_ID=from-file\n"
                "C8_CLIENT_SECRET='quoted-secret'\n"
            )
            environ = {"C8_CLIENT_ID": "from-shell"}
            self.assertTrue(demo.load_env_file(env_path, environ))
            self.assertEqual(environ["C8_CLIENT_ID"], "from-shell")
            self.assertEqual(environ["C8_CLIENT_SECRET"], "quoted-secret")

    def test_environment_validation_requires_devnet_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            configuration = {
                "C8_BASE": "https://ledger.example/api/ledger",
                "C8_WS_URL": "wss://ledger.example/api/ledger/v2/updates",
                "C8_IDP": "https://identity.example",
                "SCANNER_DB": str(Path(directory) / "scanner.db"),
            }
            with self.assertRaises(demo.DemoError) as raised:
                demo.validate_environment(configuration)
            self.assertIn("C8_CLIENT_SECRET", str(raised.exception))

            configuration["C8_CLIENT_SECRET"] = "configured-in-ignored-env"
            self.assertEqual(
                demo.validate_environment(configuration),
                Path(configuration["SCANNER_DB"]),
            )

    def test_database_preflight_preserves_existing_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scanner.db"
            original = database.DB_NAME
            database.DB_NAME = str(path)
            try:
                database.create_tables()
                database.save_offset(12345)
                demo.check_database(path)
                self.assertEqual(database.get_saved_offset(), 12345)
            finally:
                database.DB_NAME = original

    @unittest.skipIf(demo.fcntl is None, "file locking requires fcntl")
    def test_demo_lock_refuses_a_second_managed_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scanner.db"
            with demo.DemoLock(path):
                with self.assertRaises(demo.DemoError):
                    with demo.DemoLock(path):
                        pass

    def test_check_only_runs_all_preflight_checks_without_starting_server(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            database_path = Path(directory) / "scanner.db"
            env_path.write_text(
                "C8_BASE=http://localhost:2975\n"
                "C8_WS_URL=ws://localhost:2975/v2/updates\n"
                f"SCANNER_DB={database_path}\n"
                "SCANNER_ADMIN_TOKEN=test-admin-token\n"
            )
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                demo,
                "dependency_errors",
                return_value=[],
            ), mock.patch.object(
                demo,
                "check_participant",
                return_value=678,
            ):
                result = demo.main(
                    ["--env-file", str(env_path), "--check-only"]
                )
            self.assertEqual(result, 0)
            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                connection.close()

    def test_check_only_reports_unavailable_participant(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "C8_BASE=http://localhost:2975\n"
                "C8_WS_URL=ws://localhost:2975/v2/updates\n"
                f"SCANNER_DB={Path(directory) / 'scanner.db'}\n"
            )
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                demo,
                "dependency_errors",
                return_value=[],
            ), mock.patch.object(
                demo,
                "check_participant",
                side_effect=demo.DemoError("participant unavailable"),
            ):
                self.assertEqual(
                    demo.main(["--env-file", str(env_path), "--check-only"]),
                    2,
                )


if __name__ == "__main__":
    unittest.main()
