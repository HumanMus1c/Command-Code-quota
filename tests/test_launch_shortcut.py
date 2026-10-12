"""Tests for the launch-shortcut helper (spec S1).

The argument merging is pure and always runs; the read/write round trip needs
PowerShell and a real .lnk, because a hand-written fake would not catch the one thing
that matters - that Save() puts back exactly what we took off.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import launch_shortcut


def powershell(script: str) -> str:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, errors="replace", timeout=60,
    ).stdout


def make_shortcut(path: Path, target: str, arguments: str) -> None:
    powershell(
        "$shell = New-Object -ComObject WScript.Shell;"
        f" $link = $shell.CreateShortcut('{launch_shortcut.quote(str(path))}');"
        f" $link.TargetPath = '{launch_shortcut.quote(target)}';"
        f" $link.Arguments = '{launch_shortcut.quote(arguments)}';"
        " $link.Save()"
    )


class MergeTests(unittest.TestCase):
    def test_appends_both_flags_once(self) -> None:
        merged = launch_shortcut.merge_arguments("")
        self.assertEqual(merged, " ".join(launch_shortcut.DEBUG_ARGS))

    def test_keeps_what_the_user_had(self) -> None:
        merged = launch_shortcut.merge_arguments("--enable-logging")
        self.assertTrue(merged.startswith("--enable-logging"))
        self.assertTrue(launch_shortcut.has_all(merged))

    def test_is_idempotent(self) -> None:
        once = launch_shortcut.merge_arguments("--x=1")
        twice = launch_shortcut.merge_arguments(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("--remote-debugging-port=0"), 1)

    def test_partial_flags_are_not_reported_ready(self) -> None:
        self.assertFalse(launch_shortcut.has_all("--remote-debugging-port=0"))
        self.assertTrue(launch_shortcut.has_all(" ".join(launch_shortcut.DEBUG_ARGS)))

    def test_single_quotes_are_doubled_not_backslash_escaped(self) -> None:
        self.assertEqual(launch_shortcut.quote("it's here"), "it''s here")


class InspectTests(unittest.TestCase):
    def test_absent_shortcut_is_reported_not_guessed(self) -> None:
        report = launch_shortcut.inspect(Path(tempfile.mkdtemp(prefix="goat-none-")) / "Command Code.lnk",
                                         capture=lambda argv: "")
        self.assertEqual(report["state"], "absent")

    def test_missing_arguments_are_named(self) -> None:
        with mock.patch.object(launch_shortcut, "read", return_value={"state": "ok", "target": "x", "arguments": ""}):
            self.assertEqual(launch_shortcut.inspect(Path("whatever.lnk"))["state"], "missing-args")
        with mock.patch.object(launch_shortcut, "read",
                               return_value={"state": "ok", "target": "x",
                                             "arguments": " ".join(launch_shortcut.DEBUG_ARGS)}):
            self.assertEqual(launch_shortcut.inspect(Path("whatever.lnk"))["state"], "ready")


@unittest.skipUnless(shutil.which("powershell"), "powershell is not installed")
class ShortcutRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="goat-shortcut-"))
        self.environment = mock.patch.dict(os.environ, {"GOATGAUGE_DATA": str(self.dir)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.notepad = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "notepad.exe"
        if not self.notepad.exists():  # a target Windows Script Host will accept
            self.notepad = Path(os.environ.get("ComSpec", "cmd.exe"))
        self.link = self.dir / "Command Code.lnk"
        make_shortcut(self.link, str(self.notepad), "--keep-me")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_apply_then_restore_returns_the_original_arguments_exactly(self) -> None:
        result = launch_shortcut.apply(self.link)
        self.assertTrue(result["ok"], json.dumps(result))
        self.assertEqual(result["state"], "patched")
        self.assertIn("--keep-me", result["after"])
        self.assertTrue(launch_shortcut.has_all(result["after"]))
        self.assertTrue((self.dir / launch_shortcut.BACKUP_NAME).exists())
        self.assertTrue((self.dir / launch_shortcut.LNK_BACKUP_NAME).exists())

        restored = launch_shortcut.restore()
        self.assertTrue(restored["ok"], json.dumps(restored))
        self.assertEqual(restored["actual"], "--keep-me")
        self.assertFalse((self.dir / launch_shortcut.BACKUP_NAME).exists())

    def test_second_apply_changes_nothing_and_reports_already(self) -> None:
        first = launch_shortcut.apply(self.link)
        before = launch_shortcut.read(self.link)["arguments"]
        second = launch_shortcut.apply(self.link)
        self.assertEqual(second["state"], "already")
        self.assertEqual(launch_shortcut.read(self.link)["arguments"], before)
        self.assertIn("--keep-me", first["after"])

    def test_restore_without_backup_is_not_an_error(self) -> None:
        self.assertEqual(launch_shortcut.restore()["state"], "nothing-to-restore")


if __name__ == "__main__":
    unittest.main()
