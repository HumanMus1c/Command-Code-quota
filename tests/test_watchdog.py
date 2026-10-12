"""Tests for the host-following watchdog (spec L-1..L-6, N-6, D-5/D-8)."""

from __future__ import annotations

import json
import io
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app import watchdog

NETSTAT_OURS = """
  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:53584        0.0.0.0:0              LISTENING       4242
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       900
  TCP    127.0.0.1:53584        127.0.0.1:40001        ESTABLISHED     4242
  UDP    0.0.0.0:5353           *:*                                    900
"""
NETSTAT_OTHER = NETSTAT_OURS.replace("127.0.0.1:53584", "127.0.0.1:53585")
NETSTAT_REMOTE = """
  TCP    10.0.0.5:53584         0.0.0.0:0              LISTENING       4242
"""
CSV_OURS = '"Command Code.exe","4242","Console","1","300,000 K"\n'
CSV_OTHER = '"code.exe","4242","Console","1","300,000 K"\n'
CSV_HEADER_ONLY = '"Image Name","PID","Session Name","Session#","Mem Usage"\n' + CSV_OURS


class ParseTests(unittest.TestCase):
    def test_loopback_listener_wins_over_established_row(self) -> None:
        self.assertEqual(watchdog.parse_listening_pid(NETSTAT_OURS, 53584), 4242)

    def test_wrong_port_and_non_tcp_rows_are_ignored(self) -> None:
        self.assertIsNone(watchdog.parse_listening_pid(NETSTAT_OTHER, 53584))
        self.assertIsNone(watchdog.parse_listening_pid(NETSTAT_REMOTE, 53584))

    def test_non_loopback_bind_is_refused(self) -> None:
        self.assertIsNone(watchdog.parse_listening_pid(NETSTAT_REMOTE, 53584))

    def test_process_name_reads_by_position_not_header(self) -> None:
        self.assertEqual(watchdog.parse_process_name(CSV_HEADER_ONLY, 4242), "Command Code.exe")
        self.assertEqual(watchdog.parse_process_name(CSV_OURS, 4242), "Command Code.exe")
        self.assertEqual(watchdog.parse_process_name(CSV_OURS, 77), "")

    def test_process_count(self) -> None:
        self.assertEqual(watchdog.parse_process_count(CSV_OURS * 3, "Command Code.exe"), 3)
        self.assertEqual(watchdog.parse_process_count(CSV_HEADER_ONLY, "Command Code.exe"), 1)


class OwnershipTests(unittest.TestCase):
    def test_pid_is_returned_only_when_the_owner_is_the_host(self) -> None:
        def capture(argv):
            if argv[0] == "netstat":
                return NETSTAT_OURS
            return CSV_OURS

        self.assertEqual(watchdog.host_listener(53584, capture=capture), 4242)

        def capture_other(argv):
            if argv[0] == "netstat":
                return NETSTAT_OURS
            return CSV_OTHER

        self.assertIsNone(watchdog.host_listener(53584, capture=capture_other))

    def test_no_port_means_no_probe_and_no_lookup(self) -> None:
        def capture(argv):  # pragma: no cover - must not be reached
            raise AssertionError("缺席的端口不该发起系统查询")

        self.assertIsNone(watchdog.host_listener(None, capture=capture))
        self.assertIsNone(watchdog.host_listener(0, capture=capture))

    def test_alive_uses_tasklist_and_never_signal_zero(self) -> None:
        seen = []

        def capture(argv):
            seen.append(argv)
            return CSV_OURS

        self.assertTrue(watchdog.pid_alive(4242, capture=capture))
        self.assertFalse(watchdog.pid_alive(-1, capture=capture))
        self.assertTrue(seen and all("kill" not in " ".join(row).lower() for row in seen))


class RuntimeDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="goat-watchdog-"))
        self.environment = mock.patch.dict(os.environ, {"GOATGAUGE_DATA": str(self.dir)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_atomic_write_and_read_round_trip(self) -> None:
        path = watchdog.runtime_dir() / "nested" / "state.json"
        watchdog.write_json_atomic(path, {"b": 2, "a": 1})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1, "b": 2})
        self.assertEqual([leftover.name for leftover in path.parent.iterdir()], ["state.json"])

    def test_writing_outside_the_runtime_dir_is_refused(self) -> None:
        outside = self.dir.parent / f"goat-escape-{self.dir.name}.json"
        with self.assertRaises(ValueError):
            watchdog.write_json_atomic(outside, {"x": 1})
        self.assertFalse(outside.exists())

    def test_corrupt_file_reads_as_absent(self) -> None:
        broken = watchdog.runtime_dir() / "broken.json"
        broken.write_text('{"pid": 1', encoding="utf-8")
        self.assertIsNone(watchdog.read_json(broken))

    def test_live_foreign_owner_blocks_us_but_a_dead_one_does_not(self) -> None:
        lock = watchdog.runtime_dir() / watchdog.LOCK_NAME
        watchdog.write_json_atomic(lock, {"pid": 9999, "token": "other"})
        dead = mock.Mock(return_value=False)
        alive = mock.Mock(return_value=True)
        self.assertTrue(watchdog.acquire_lock(lock, token="mine", pid=1, alive=dead))
        self.assertEqual(watchdog.read_json(lock)["token"], "mine")
        self.assertFalse(watchdog.acquire_lock(lock, token="third", pid=2, alive=alive))
        self.assertEqual(watchdog.read_json(lock)["token"], "mine")

    def test_release_only_removes_our_own_lock(self) -> None:
        lock = watchdog.runtime_dir() / watchdog.LOCK_NAME
        watchdog.write_json_atomic(lock, {"pid": 1, "token": "mine"})
        watchdog.release_lock(lock, token="someone-else")
        self.assertTrue(lock.exists())
        watchdog.release_lock(lock, token="mine")
        self.assertFalse(lock.exists())


class InjectLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="goat-injectlock-"))
        environment = mock.patch.dict(os.environ, {"GOATGAUGE_DATA": str(self.dir)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_a_second_injector_backs_off_instead_of_double_registering(self) -> None:
        mine = watchdog.claim_inject_lock(alive=lambda pid: pid == os.getpid())
        self.assertIsNotNone(mine)
        self.assertIsNone(watchdog.claim_inject_lock(alive=lambda pid: pid == os.getpid()))
        watchdog.release_inject_lock(mine)
        again = watchdog.claim_inject_lock(alive=lambda pid: pid == os.getpid())
        self.assertIsNotNone(again)
        watchdog.release_inject_lock(again)


class InjectModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="goat-injectmode-"))
        environment = mock.patch.dict(os.environ, {"GOATGAUGE_DATA": str(self.dir)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_demo_never_replaces_a_live_owner_even_after_it_dies(self) -> None:
        lock = watchdog.runtime_dir() / watchdog.INJECT_LOCK_NAME
        watchdog.write_json_atomic(lock, {"pid": 9999, "token": "dead", "mode": "live"})
        dead = mock.Mock(return_value=False)
        self.assertIsNone(watchdog.claim_inject_lock(mode="demo", alive=dead))
        self.assertEqual(watchdog.read_json(lock)["mode"], "live")
        claim = watchdog.claim_inject_lock(mode="live", alive=dead)
        self.assertIsNotNone(claim)
        self.assertEqual(watchdog.read_json(lock)["mode"], "live")
        watchdog.release_inject_lock(claim)


class FakeInjector:
    def __init__(self, **kwargs) -> None:
        self.count = 0
        self.detach_calls = 0

    def attached(self) -> int:
        return self.count

    def attach(self) -> None:
        self.count = 1

    def detach_all(self) -> None:
        if self.count:
            self.detach_calls += 1
        self.count = 0


class FollowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="goat-follow-"))
        self.environment = mock.patch.dict(os.environ, {"GOATGAUGE_DATA": str(self.dir)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.profile = self.dir / "profile"
        self.profile.mkdir()

    def _run(self, ports: list[int | None], capture, stops_after: int = 4, *,
             auto_launch: bool = False, launcher=None) -> tuple[list, FakeInjector]:
        remaining = list(ports)
        logs: list[str] = []
        injector = FakeInjector()
        clock_values = iter([float(step) * 40.0 for step in range(stops_after)])

        def sleeper(_seconds: float) -> None:
            sleeper.calls += 1
            if sleeper.calls >= 4:
                raise KeyboardInterrupt

        sleeper.calls = 0

        with (
            mock.patch.object(watchdog, "WidgetInjector", lambda **kwargs: injector),
            mock.patch.object(watchdog, "devtools_port", lambda _profile: remaining.pop(0) if remaining else None),
        ):
            watchdog.follow(
                profile=self.profile,
                log=logs.append,
                capture=capture,
                clock=lambda: next(clock_values),
                sleeper=sleeper,
                auto_launch=auto_launch,
                launcher=launcher,
            )
        return logs, injector

    def test_attaches_only_while_the_host_owns_the_port(self) -> None:
        def capture(argv):
            return NETSTAT_OURS if argv[0] == "netstat" else CSV_OURS

        logs, injector = self._run([None, 53584, 53584, None], capture)
        self.assertEqual(injector.count, 0)
        self.assertGreaterEqual(injector.detach_calls, 1)
        self.assertIn("宿主或调试端口已消失", " ".join(logs))

    def test_absent_host_opens_no_devtools_session(self) -> None:
        def capture(argv):  # pragma: no cover - a missing host must not be probed
            raise AssertionError("宿主缺席时不得发起任何系统查询")

        logs, injector = self._run([None, None, None, None], capture, stops_after=4)
        self.assertEqual(injector.count, 0)
        self.assertEqual(injector.detach_calls, 0)
        self.assertEqual(logs, [])

    def test_second_instance_backs_off(self) -> None:
        lock = watchdog.runtime_dir() / watchdog.LOCK_NAME
        watchdog.write_json_atomic(lock, {"pid": 4242, "token": "other"})
        logs, _injector = self._run(
            [None, None, None, None], lambda argv: CSV_OURS if argv[0] == "tasklist" else ""
        )
        self.assertIn("已有守护进程在跟随", " ".join(logs))
        self.assertEqual(watchdog.read_json(lock)["token"], "other")

    def test_auto_launch_starts_an_absent_host_exactly_once(self) -> None:
        calls: list[int] = []

        def launcher() -> dict:
            calls.append(1)
            return {"ok": True, "state": "launched"}

        logs, _injector = self._run(
            [None, None, None, None], lambda argv: "", auto_launch=True, launcher=launcher
        )
        self.assertEqual(len(calls), 1, "四个循环里只许拉起一次")
        self.assertIn("已带调试端口拉起", " ".join(logs))

    def test_auto_launch_never_reopens_a_host_the_user_closed(self) -> None:
        calls: list[int] = []
        seen: list[int] = []

        def launcher() -> dict:  # pragma: no cover - must not run
            calls.append(1)
            return {"ok": True}

        def capture(argv):
            # First look: the host is running without a port. Later it is gone - the
            # user quit it, and reopening it from the background is not allowed.
            if argv[0] != "tasklist":
                return ""
            seen.append(1)
            return CSV_OURS if len(seen) == 1 else ""

        _logs, _injector = self._run(
            [None, None, None, None], capture, auto_launch=True, launcher=launcher,
        )
        self.assertEqual(calls, [], "只在开机那一次判定里拉起；此后不再动宿主")
        self.assertEqual(len(seen), 1, "判定只许做一次")

    def test_auto_launch_is_off_by_default(self) -> None:
        def capture(argv):  # pragma: no cover - nothing may be queried
            raise AssertionError("未开启 auto_launch 时不得探测进程，也不得拉起宿主")

        _logs, _injector = self._run([None, None, None, None], capture)


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = Path(tempfile.mkdtemp(prefix="goat-profile-"))

    def test_running_without_a_port_refuses_to_restart_the_session(self) -> None:
        spawned = []

        def capture(argv):
            if argv[0] == "tasklist" and "IMAGENAME" in " ".join(argv):
                return CSV_OURS
            return CSV_OTHER  # no host-owned listener

        result = watchdog.launch_host(
            r"D:\Program Files\Command Code\Command Code.exe",
            profile=self.profile,
            capture=capture,
            spawn=spawned.append,
        )
        self.assertEqual(result["state"], "running-without-port")
        self.assertFalse(result["ok"])
        self.assertEqual(spawned, [])

    def test_launch_carries_loopback_and_random_port(self) -> None:
        spawned = []
        result = watchdog.launch_host(
            r"D:\Program Files\Command Code\Command Code.exe",
            profile=self.profile,
            capture=lambda argv: "",
            spawn=spawned.append,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(spawned, [[r"D:\Program Files\Command Code\Command Code.exe", *watchdog.LAUNCH_ARGS]])
        self.assertIn("--remote-debugging-port=0", watchdog.LAUNCH_ARGS)
        self.assertIn("127.0.0.1", " ".join(watchdog.LAUNCH_ARGS))


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="goat-probe-"))
        self.environment = mock.patch.dict(os.environ, {"GOATGAUGE_DATA": str(self.dir)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.profile = Path(tempfile.mkdtemp(prefix="goat-probe-profile-"))

    def test_only_shapes_and_a_hash_are_written(self) -> None:
        value = {
            "ok": True,
            "accountHash": "a" * 12,
            # Anything the page returns beyond the declared fields must not be
            # echoed, so a stray value is planted to prove the whitelist holds.
            "leak": "alice@corp.example",
            "identity": "Alice",
            "methods": {"accountOverview": "function"},
            "shapes": {
                "accountOverview": {
                    "user": {"id": "string", "email": "string"},
                    "windowLimits": {"fiveHour": {"used": "number", "cap": "number"}},
                }
            },
        }
        page = mock.Mock(ws_url="ws://127.0.0.1:1/t", page_id="t")
        with (
            mock.patch.object(watchdog, "CdpConnection", _connection_factory(value)),
            mock.patch.object(watchdog, "devtools_port", lambda _profile: 53584),
            mock.patch.object(watchdog, "discover_targets", lambda _port: [page]),
        ):
            result = watchdog.probe_host(profile=self.profile, now=lambda: 100.0)
        self.assertTrue(result["ok"])
        written = Path(result["path"])
        self.assertEqual(written.parent.name, "a" * 12)
        body = written.read_text(encoding="utf-8")
        self.assertIn("accountOverview", body)
        self.assertNotIn("alice@corp.example", body)
        self.assertNotIn("Alice", body)
        self.assertNotIn("a" * 12, body)

    def test_no_resolvable_account_still_returns_the_structure(self) -> None:
        value = {
            "ok": False,
            "reason": "no-account-identity",
            "methods": {"accountOverview": "function"},
            "envelopes": {"accountOverview": "overview,success"},
            "shapes": {"accountOverview": {"user": {"id": "string"}}},
        }
        page = mock.Mock(ws_url="ws://127.0.0.1:1/t", page_id="t")
        with (
            mock.patch.object(watchdog, "CdpConnection", _connection_factory(value)),
            mock.patch.object(watchdog, "devtools_port", lambda _profile: 53584),
            mock.patch.object(watchdog, "discover_targets", lambda _port: [page]),
        ):
            result = watchdog.probe_host(profile=self.profile)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no-account-identity")
        # The structure is the point of the probe, so it survives the failure.
        self.assertEqual(result["envelopes"], {"accountOverview": "overview,success"})
        self.assertEqual(result["shapes"]["accountOverview"]["user"], {"id": "string"})
        self.assertEqual(sorted(path.name for path in watchdog.runtime_dir().glob("*")), [])

    def test_oversized_self_report_is_rejected(self) -> None:
        value = {
            "ok": True,
            "accountHash": "b" * 12,
            "methods": {},
            "shapes": {"accountOverview": {"k": "v" * (watchdog.MAX_PROBE_BYTES + 1)}},
        }
        page = mock.Mock(ws_url="ws://127.0.0.1:1/t", page_id="t")
        with (
            mock.patch.object(watchdog, "CdpConnection", _connection_factory(value)),
            mock.patch.object(watchdog, "devtools_port", lambda _profile: 53584),
            mock.patch.object(watchdog, "discover_targets", lambda _port: [page]),
        ):
            result = watchdog.probe_host(profile=self.profile)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "probe-too-large")


def _connection_factory(value):
    class Connection:
        def __init__(self, _ws_url, *, timeout=0) -> None:
            pass

        def call(self, _method, _params=None):
            return {"result": {"value": value}}

        def close(self) -> None:
            pass

    return Connection


class EntryCLITests(unittest.TestCase):
    """entry.py and the watchdog are two argparse surfaces that have to agree.

    The HKCU Run key stores `entry.py --watch`, so a flag that one side forwards and
    the other does not recognise kills the login autostart before the first poll -
    while every other mode flag keeps working, which is why it survives a manual run.
    """

    def forwarded(self, argv: list[str]) -> list[str]:
        import entry

        calls: list[list[str]] = []

        def record(argv_inner: list[str]) -> int:
            calls.append(list(argv_inner))
            return 0

        with mock.patch.object(sys, "argv", ["entry.py", *argv]), mock.patch(
            "app.watchdog.main", record
        ):
            with self.assertRaises(SystemExit):
                entry.main()
        self.assertEqual(len(calls), 1, f"{argv} reached the watchdog exactly once")
        return calls[0]

    def test_watch_forwards_a_flag_the_watchdog_parses(self) -> None:
        forwarded = self.forwarded(["--watch"])
        with mock.patch.object(watchdog, "follow", lambda *a, **k: 0):
            self.assertEqual(watchdog.main(forwarded), 0)

    def test_auto_launch_forwards_a_pair_the_watchdog_parses(self) -> None:
        forwarded = self.forwarded(["--watch", "--auto-launch"])
        self.assertEqual(forwarded, ["--follow", "--auto-launch"])
        captured: dict = {}
        with mock.patch.object(watchdog, "follow", lambda **kwargs: captured.update(kwargs) or 0):
            self.assertEqual(watchdog.main(forwarded), 0)
        self.assertTrue(captured.get("auto_launch"))

    def test_shortcut_flags_stay_in_entry_and_report_the_result(self) -> None:
        import entry
        from app import launch_shortcut

        for argv, function in (
            (["--install-shortcut"], "apply"),
            (["--uninstall-shortcut"], "restore"),
        ):
            with (
                mock.patch.object(sys, "argv", ["entry.py", *argv]),
                mock.patch.object(watchdog, "main", lambda argv=None: self.fail(f"{argv} 不该走守护进程")),
                mock.patch.object(launch_shortcut, function, lambda **kwargs: {"ok": True, "state": "patched"}),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                with self.assertRaises(SystemExit) as raised:
                    entry.main()
            self.assertEqual(raised.exception.code, 0, function)

    def test_every_mode_flag_maps_to_a_parsed_flag(self) -> None:
        for argv, expected in (
            (["--status"], ["--status"]),
            (["--launch"], ["--launch"]),
            (["--probe"], ["--probe"]),
            (["--watch"], ["--follow"]),
        ):
            self.assertEqual(self.forwarded(argv), expected, argv[0])


if __name__ == "__main__":
    unittest.main()
