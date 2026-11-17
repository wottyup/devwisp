from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

from devwisp.state_migration import migrate_legacy_state_directory
from devwisp.claude_code import ClaudeCodeServer


@unittest.skipUnless(os.name == "posix", "Linux state migration")
class StateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "codex-remote"
        self.target = self.root / "devwisp"
        self.env = patch.dict(os.environ, {key: "" for key in (
            "DEVWISP_AUTH_FILE", "DEVWISP_HOME", "DEVWISP_STATE_FILE",
        )})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_full_state_and_absolute_attachment_paths_survive(self):
        self.source.mkdir(mode=0o700)
        with sqlite3.connect(self.source / "claude-code.sqlite3") as database:
            database.execute("CREATE TABLE history (text TEXT)")
            database.execute("INSERT INTO history VALUES ('retained conversation')")
        for name in ("message-images/image.png", "native-engine-gateway/snapshot.json", "ssh-mcp/config.toml"):
            file = self.source / name
            file.parent.mkdir(exist_ok=True)
            file.write_bytes(b"retained fixture")
        self.assertTrue(migrate_legacy_state_directory(self.target))
        self.assertTrue(self.source.is_symlink())
        self.assertEqual(self.source.resolve(), self.target)
        self.assertEqual((self.source / "message-images/image.png").read_bytes(), b"retained fixture")
        with sqlite3.connect(self.target / "claude-code.sqlite3") as database:
            self.assertEqual(database.execute("SELECT text FROM history").fetchone()[0], "retained conversation")
        self.assertEqual((self.target / "native-engine-gateway/snapshot.json").read_bytes(), b"retained fixture")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o700)
        self.assertFalse(migrate_legacy_state_directory(self.target))

    def test_existing_destination_is_not_overwritten_or_merged(self):
        self.source.mkdir()
        self.target.mkdir()
        (self.source / "old").touch()
        (self.target / "current").touch()
        self.assertFalse(migrate_legacy_state_directory(self.target))
        self.assertEqual([p.name for p in self.target.iterdir()], ["current"])
        self.assertTrue((self.source / "old").exists())

    def test_explicit_deployment_paths_are_not_migrated(self):
        self.source.mkdir()
        for key in ("DEVWISP_AUTH_FILE", "DEVWISP_HOME", "DEVWISP_STATE_FILE"):
            with self.subTest(key=key), patch.dict(os.environ, {key: "/custom/path"}):
                self.assertFalse(migrate_legacy_state_directory(self.target))
                self.assertTrue(self.source.is_dir())
                self.assertFalse(self.target.exists())

    def test_failed_compatibility_link_restores_original_directory(self):
        self.source.mkdir()
        (self.source / "history").touch()
        with patch.object(Path, "symlink_to", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                migrate_legacy_state_directory(self.target)
        self.assertTrue((self.source / "history").is_file())
        self.assertFalse(self.target.exists())

    def test_unrelated_directory_or_legacy_symlink_is_not_moved(self):
        self.source.symlink_to(self.root, target_is_directory=True)
        self.assertFalse(migrate_legacy_state_directory(self.target))
        self.assertFalse(migrate_legacy_state_directory(self.root / "custom"))


class ClaudeWorkspaceMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_missing_old_project_directory_is_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "devwisp"
            project.mkdir()
            old = root / "code-remote"
            database = root / "claude.sqlite3"
            fake_module = project / "server/devwisp/claude_code.py"
            with patch("devwisp.claude_code.__file__", str(fake_module)):
                server = ClaudeCodeServer(sys.executable, root, database, AsyncMock())
                thread = await server.create_thread()
                other = await server.create_thread()
                server._connection.execute("UPDATE claude_threads SET cwd = ? WHERE thread_id = ?", (str(old), thread["id"]))
                server._connection.commit()
                await server.close()
                server = ClaudeCodeServer(sys.executable, root, database, AsyncMock())
                try:
                    self.assertEqual(server._row(thread["id"])["cwd"], str(project))
                    self.assertEqual(server._row(other["id"])["cwd"], str(root))
                finally:
                    await server.close()

    async def test_existing_old_workspace_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, old = root / "devwisp", root / "code-remote"
            project.mkdir()
            old.mkdir()
            database = root / "claude.sqlite3"
            with patch("devwisp.claude_code.__file__", str(project / "server/devwisp/claude_code.py")):
                server = ClaudeCodeServer(sys.executable, root, database, AsyncMock())
                thread = await server.create_thread(old)
                await server.close()
                server = ClaudeCodeServer(sys.executable, root, database, AsyncMock())
                try:
                    self.assertEqual(server._row(thread["id"])["cwd"], str(old))
                finally:
                    await server.close()
