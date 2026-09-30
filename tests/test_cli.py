"""Process-level checks for CLI error handling (no network or Nix build needed)."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_obtain as support


class InspectionBoundaryTests(unittest.TestCase):
    setUp = support.StateTests.setUp
    tearDown = support.StateTests.tearDown

    def inspect(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(
                support.o.GitHub,
                "release_assets",
                return_value=({"tag_name": "v1"}, []),
            ) as metadata,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = support.o.main(["inspect", "https://github.com/owner/app"])
        self.assertEqual(status, 0, stderr.getvalue())
        metadata.assert_called_once()
        self.assertIn("Release: v1", stdout.getvalue())

    def test_inspection_ignores_corrupt_state_and_pending_recovery(self):
        paths = [
            self.store.config / "sources.json",
            self.store.config / "lock.json",
            self.store.data / "pending.json",
            self.store.data / "state-events.jsonl",
        ]
        for path in paths:
            path.write_text("invalid JSON")
        self.inspect()
        for path in paths:
            self.assertEqual(path.read_text(), "invalid JSON")
        self.assertFalse((self.store.data / ".lock").exists())

    def test_inspection_can_run_while_installation_session_is_locked(self):
        with self.store.session():
            self.inspect()

    def test_inspection_does_not_initialize_managed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    f"XDG_{part}_HOME": str(root / part.lower())
                    for part in ("CONFIG", "DATA", "CACHE")
                },
            ):
                self.inspect()
            self.assertFalse((root / "config").exists())
            self.assertFalse((root / "data").exists())


class CliStateTests(unittest.TestCase):
    def test_removed_flake_export_and_notification_cli_are_rejected(self):
        cli = Path(__file__).resolve().parents[1] / "obtain.py"
        for args in (
            ["add", "https://github.com/owner/app", "--type", "flake"],
            ["add", "https://github.com/owner/app", "--package", "default"],
            ["add", "https://github.com/owner/app", "--ref", "main"],
            ["inspect", "https://github.com/owner/app", "--ref", "main"],
            ["check", "--notify"],
            ["export", "/tmp/obtain-export"],
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, str(cli), *args],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)

    def test_non_object_state_entries_fail_cleanly_without_modifying_files(self):
        cli = Path(__file__).resolve().parents[1] / "obtain.py"
        for filename in ("sources.json", "lock.json"):
            for value in (None, [], "invalid", 42):
                with (
                    self.subTest(filename=filename, value=value),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    root = Path(tmp)
                    config = root / "config/obtain"
                    config.mkdir(parents=True)
                    path = config / filename
                    path.write_text(json.dumps({"schema": 1, "apps": {"app": value}}))
                    before = path.read_bytes()
                    env = dict(
                        os.environ,
                        XDG_CONFIG_HOME=str(root / "config"),
                        XDG_DATA_HOME=str(root / "data"),
                        XDG_CACHE_HOME=str(root / "cache"),
                    )
                    result = subprocess.run(
                        [sys.executable, str(cli), "list"],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertIn("obtain:", result.stderr)
                    self.assertEqual(path.read_bytes(), before)
