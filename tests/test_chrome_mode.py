"""Tests for the dedicated Chrome profile helpers."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import chrome_mode
from app.chrome_mode import _preserve_session_cookies


class PreserveSessionCookiesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self._tmp.name)
        (self.profile / "Default").mkdir(parents=True, exist_ok=True)
        self.preferences = self.profile / "Default" / "Preferences"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _read(self) -> dict:
        return json.loads(self.preferences.read_text(encoding="utf-8"))

    def test_enables_restore_on_startup(self) -> None:
        self.preferences.write_text(json.dumps({"session": {"startup_urls": []}}))
        _preserve_session_cookies(self.profile)
        self.assertEqual(self._read()["session"]["restore_on_startup"], 1)
        self.assertEqual(self._read()["session"]["startup_urls"], [])

    def test_creates_preferences_when_missing(self) -> None:
        _preserve_session_cookies(self.profile)
        self.assertEqual(self._read()["session"]["restore_on_startup"], 1)

    def test_ignores_corrupt_preferences(self) -> None:
        self.preferences.write_text("{not json")
        _preserve_session_cookies(self.profile)
        self.assertEqual(self._read()["session"]["restore_on_startup"], 1)

    def test_is_idempotent(self) -> None:
        _preserve_session_cookies(self.profile)
        first = self.preferences.read_text(encoding="utf-8")
        _preserve_session_cookies(self.profile)
        self.assertEqual(self.preferences.read_text(encoding="utf-8"), first)


class OpenChromeLifecycleTestCase(unittest.TestCase):
    """Every browser we start must have an owner or be reaped (2026-10-13 lesson).

    A dev tool discarded its Popen and left 61 browsers / 477 processes on the
    user's machine; `open_chrome` had the same shape - the handle was returned and
    dropped by every caller. These cases pin who reaps the transient handoff
    launcher, and that a cold start still hands the live instance up to the caller.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"GOATGAUGE_DATA": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.process = mock.Mock()
        self.process.wait.return_value = 0
        self.popener = mock.patch.object(
            chrome_mode.subprocess, "Popen", return_value=self.process
        ).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(chrome_mode, "chrome_path", return_value="chrome").start()
        mock.patch.object(chrome_mode, "log", lambda *_a: None).start()
        mock.patch.object(chrome_mode, "_preserve_session_cookies", lambda *_a: None).start()

    def _launch(self, alive: bool, wait_error: Exception | None = None):
        mock.patch.object(chrome_mode, "_cdp_alive", return_value=alive).start()
        if wait_error is not None:
            self.process.wait.side_effect = wait_error
        return chrome_mode.open_chrome("http://127.0.0.1:18927/")

    def test_cold_start_hands_the_instance_handle_to_the_caller(self) -> None:
        self.assertIs(self._launch(alive=False), self.process)
        self.process.wait.assert_not_called()

    def test_handoff_launcher_is_reaped_before_returning(self) -> None:
        # A warm profile means Chromium delivers the URL to the running instance and
        # this process exits; nobody else holds it, so open_chrome must wait for it.
        self.assertIsNone(self._launch(alive=True))
        self.process.wait.assert_called_once_with(timeout=10)

    def test_a_handoff_that_never_completes_still_leaves_an_owner(self) -> None:
        stalled = subprocess.TimeoutExpired(cmd="chrome", timeout=10)
        self.assertIs(self._launch(alive=True, wait_error=stalled), self.process)

    def test_both_branches_launch_on_the_same_profile(self) -> None:
        # The fix must not change what Chromium is asked to do, only who reaps it -
        # a different --user-data-dir would split the session in two.
        # Ask the production helper, not the env var: it resolves 8.3 short paths.
        profile = str(chrome_mode._profile_dir())
        self._launch(alive=False)
        argv = self.popener.call_args[0][0]
        self.assertIn(f"--user-data-dir={profile}", argv)
        self.assertIn("--app=http://127.0.0.1:18927/", argv)


if __name__ == "__main__":
    unittest.main()
