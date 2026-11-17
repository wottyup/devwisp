from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class IdleRestartTests(unittest.TestCase):
    def test_running_claude_turns_in_both_state_directories_block_restart(self):
        script = Path(__file__).resolve().parents[2] / "scripts/restart-devwisp-when-idle.py"
        spec = importlib.util.spec_from_file_location("idle_restart", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with sqlite3.connect(root / "state_5.sqlite") as database:
                database.execute("CREATE TABLE threads (id TEXT)")
            paths = []
            for name in ("devwisp", "codex-remote"):
                path = root / name / "claude-code.sqlite3"
                path.parent.mkdir()
                with sqlite3.connect(path) as database:
                    database.execute("CREATE TABLE claude_threads (thread_id TEXT, running_turn_id TEXT)")
                    database.execute("INSERT INTO claude_threads VALUES (?, 'active-turn')", (name,))
                    database.execute("INSERT INTO claude_threads VALUES ('idle', NULL)")
                paths.append(path)
            with patch.object(module, "CODEX_HOME", root), patch.object(module, "CLAUDE_DATABASES", paths):
                self.assertEqual(set(module.active_turns()), {"devwisp", "codex-remote"})
