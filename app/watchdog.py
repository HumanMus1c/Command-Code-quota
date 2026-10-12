"""Keep the readout alive for exactly as long as Command Code itself is alive.

The host ships no plugin container, so nothing inside it can own this lifecycle.
The loop in :func:`follow` is the whole substitute: it never opens a DevTools
session unless a ``Command Code`` process really owns the port named by the
host's own profile file, and it lets go the moment that stops being true.

Liveness is asked of ``netstat``/``tasklist`` rather than :func:`os.kill`, which
on Windows terminates the process instead of probing it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import traceback
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from .inject import (
    CdpConnection,
    InjectError,
    WidgetInjector,
    commandcode_profile_dir,
    detach,
    devtools_port,
    discover_targets,
)

HOST_PROCESS = "Command Code"
# Port 0 asks the host for an arbitrary free port, which it then writes to
# DevToolsActivePort; a fixed port would advertise this window to every local
# process under a guessable number.
LAUNCH_ARGS = ("--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1")
IDLE_INTERVAL = 5.0
ATTACH_INTERVAL = 2.5
LISTENER_RECHECK = 30.0
TICK = 0.5
LOCK_NAME = "watchdog.lock"
LOG_NAME = "watchdog.log"
PROBE_NAME = "probe.json"
MAX_PROBE_BYTES = 262_144
ACCOUNT_HASH = re.compile(r"^[0-9a-f]{12}$")


def runtime_dir() -> Path:
    """Where the lock and the probe self-report live (``GOATGAUGE_DATA`` overrides)."""
    configured = os.environ.get("GOATGAUGE_DATA", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(root) / "GOATGauge"


def _capture(argv: list[str]) -> str:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_listening_pid(text: str, port: int) -> int | None:
    """Loopback TCP LISTENING row for ``port``, or None."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        local = parts[1]
        if not local.endswith(f":{port}"):
            continue
        if "127.0.0.1" not in local and not local.startswith("[::1]"):
            continue
        if parts[-1].isdigit():
            return int(parts[-1])
    return None


def parse_process_name(text: str, pid: int) -> str:
    """Image name for ``pid`` in ``tasklist /FO CSV`` output.

    The header is localized, so rows are matched by column position only.
    """
    for line in text.splitlines():
        row = [cell.strip().strip('"') for cell in line.split('","')]
        if len(row) >= 2 and row[1] == str(pid):
            return row[0]
    return ""


def parse_process_count(text: str, image: str) -> int:
    wanted = image.lower()
    total = 0
    for line in text.splitlines():
        row = [cell.strip().strip('"') for cell in line.split('","')]
        if len(row) >= 2 and row[0].lower() == wanted and row[1].isdigit():
            total += 1
    return total


def pid_alive(pid: int, *, capture: Callable[[list[str]], str] = _capture) -> bool:
    if pid <= 0:
        return False
    return bool(parse_process_name(capture(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"]), pid))


def host_listener(port: int | None, *, capture: Callable[[list[str]], str] = _capture) -> int | None:
    """The PID holding ``port``, but only when that process really is the host.

    A stale port file left behind by another Electron app must never earn us a
    DevTools session, so ownership is checked instead of trusted.
    """
    if not port:
        return None
    pid = parse_listening_pid(capture(["netstat", "-ano"]), port)
    if pid is None:
        return None
    name = parse_process_name(capture(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]), pid)
    return pid if name.lower() == f"{HOST_PROCESS.lower()}.exe" else None


def host_processes(*, capture: Callable[[list[str]], str] = _capture) -> int:
    return parse_process_count(
        capture(["tasklist", "/FI", f"IMAGENAME eq {HOST_PROCESS}.exe", "/FO", "CSV", "/NH"]),
        f"{HOST_PROCESS}.exe",
    )


def read_json(path: Path) -> dict[str, Any] | None:
    """A corrupt or half-written file reads as absent rather than raising (L-6)."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def inside_runtime(path: Path) -> bool:
    try:
        path.resolve().relative_to(runtime_dir().resolve())
    except ValueError:
        return False
    return True


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """temp + ``os.replace`` in the same directory, and never outside the runtime dir."""
    if not inside_runtime(path):
        raise ValueError(f"拒绝写出运行目录之外: {path}")
    if path.is_symlink():
        raise ValueError(f"拒绝写入符号链接: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        os.replace(temp_name, path)
    except OSError:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def acquire_lock(path: Path, *, token: str, pid: int, alive: Callable[[int], bool] = pid_alive) -> bool:
    """A live foreign owner wins; a stale lock is ours to overwrite."""
    current = read_json(path)
    if current and current.get("token") != token:
        try:
            owner = int(current.get("pid") or -1)
        except (TypeError, ValueError):
            owner = -1
        if alive(owner):
            return False
    write_json_atomic(path, {"pid": pid, "token": token, "started_at": round(time.time(), 3)})
    return True


def release_lock(path: Path, *, token: str) -> None:
    current = read_json(path)
    if current and current.get("token") == token:
        try:
            path.unlink()
        except OSError:
            pass


PROBE_EXPRESSION = """
(async () => {
  const PLAIN_KEY = /^[a-zA-Z][a-zA-Z0-9]{0,24}$/;
  const shape = (value, depth) => {
    if (value === null) return 'null';
    if (value === undefined) return 'undefined';
    if (Array.isArray(value)) {
      return {__array__: value.length, __item: value.length ? shape(value[0], depth + 1) : 'empty'};
    }
    const type = typeof value;
    if (type !== 'object') return type;
    if (depth >= 3) return 'object';
    const names = Object.keys(value);
    // The host keys some maps by thread ID, so a key name can itself be an
    // identifier. Anything that is not a plain field name is collapsed to a
    // count and a value type rather than written out.
    if (names.length > 20 || names.some((k) => !PLAIN_KEY.test(k))) {
      return {__map__: names.length, __value__: names.length ? shape(value[names[0]], depth + 1) : 'empty'};
    }
    const out = {};
    for (const key of names) out[key] = shape(value[key], depth + 1);
    return out;
  };
  const api = window.electronAPI;
  if (!api) return {ok: false, reason: 'no-electronAPI'};
  // Every bridge call answers an envelope, not the payload: accountOverview
  // resolves {success, overview} and getAppState resolves {success, state}.
  // Reading the envelope as if it were the payload yields undefined fields and
  // a readout that silently never appears, so unwrap before anything else.
  const NAMED = {accountOverview: 'overview', getAppState: 'state', sessionStats: 'stats',
    sessionContextBreakdown: 'breakdown'};
  const unwrap = (name, raw) => {
    if (!raw || typeof raw !== 'object') return raw;
    if (!Object.prototype.hasOwnProperty.call(raw, 'success')) return raw;
    const named = NAMED[name];
    if (named && raw[named] !== undefined) return raw[named];
    const rest = Object.keys(raw).filter((k) => k !== 'success' && !/error/i.test(k));
    return rest.length === 1 ? raw[rest[0]] : raw;
  };
  const out = {ok: true, methods: {}, envelopes: {}, shapes: {}};
  for (const name of ['accountOverview', 'getAppState', 'sessionStats', 'sessionContextBreakdown']) {
    out.methods[name] = typeof api[name];
  }
  let overview = null;
  let state = null;
  let threadId = null;
  try {
    const raw = await api.accountOverview();
    out.envelopes.accountOverview = Object.keys(Object(raw)).sort().join(',');
    overview = unwrap('accountOverview', raw);
    out.shapes.accountOverview = shape(overview, 0);
  } catch (error) { out.shapes.accountOverview = 'error'; }
  try {
    const raw = await api.getAppState();
    out.envelopes.getAppState = Object.keys(Object(raw)).sort().join(',');
    state = unwrap('getAppState', raw);
    out.shapes.getAppState = shape(state, 0);
    threadId = state && state.activeThreadId;
  } catch (error) { out.shapes.getAppState = 'error'; }
  if (typeof threadId === 'string' && threadId) {
    for (const name of ['sessionStats', 'sessionContextBreakdown']) {
      try {
        const raw = await api[name](threadId);
        out.envelopes[name] = Object.keys(Object(raw)).sort().join(',');
        out.shapes[name] = shape(unwrap(name, raw), 0);
      } catch (error) { out.shapes[name] = 'error'; }
    }
  } else {
    out.shapes.sessionStats = 'no-active-thread';
  }
  const user = overview && overview.user;
  const raw = user && (user.id || user.userId);
  const identity = (typeof raw === 'string' || typeof raw === 'number')
    ? String(raw)
    : (typeof (overview && overview.identity) === 'string' ? overview.identity : '');
  if (!identity || !globalThis.crypto || !crypto.subtle) {
    return Object.assign(out, {ok: false, reason: 'no-account-identity'});
  }
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(identity));
  out.accountHash = Array.from(new Uint8Array(bytes)).map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 12);
  return out;
})()
"""


def probe_host(
    *,
    profile: Path | None = None,
    port: int | None = None,
    timeout: float = 20.0,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Ask one live page for the *shape* of the host bridge, then store only that.

    Key names and types travel; no value does, because the same objects carry the
    account name, e-mail and an avatar as base64. Without a resolvable account the
    probe is dropped rather than filed under a guessed directory.
    """
    profile = profile or commandcode_profile_dir()
    found = port if port else devtools_port(profile)
    if not found:
        return {"ok": False, "reason": "no-debug-port"}
    try:
        targets = discover_targets(found)
    except InjectError as exc:
        return {"ok": False, "reason": str(exc)}
    last = ""
    for target in targets:
        try:
            connection = CdpConnection(target.ws_url, timeout=timeout)
            try:
                reply = connection.call(
                    "Runtime.evaluate",
                    {
                        "expression": PROBE_EXPRESSION,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                )
            finally:
                connection.close()
        except InjectError as exc:
            last = str(exc)
            continue
        value = (reply or {}).get("result", {}).get("value")
        if not isinstance(value, dict):
            last = "探针没有返回对象"
            continue
        digest = str(value.get("accountHash") or "")
        diagnostics = {
            "methods": value.get("methods"),
            "envelopes": value.get("envelopes"),
            "shapes": value.get("shapes"),
        }
        if not ACCOUNT_HASH.match(digest):
            # Key names and types are the point of the probe, so a failed identity
            # check must not throw away the very structure being diagnosed.
            return {"ok": False, "reason": str(value.get("reason") or "no-account-identity"), **diagnostics}
        payload = {"captured_at": round(now(), 3), **diagnostics}
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(body.encode("utf-8")) > MAX_PROBE_BYTES:
            return {"ok": False, "reason": "probe-too-large"}
        path = runtime_dir() / digest / PROBE_NAME
        write_json_atomic(path, payload)
        return {"ok": True, "path": str(path), **diagnostics}
    return {"ok": False, "reason": last or "no-page"}


def find_host_exe() -> str | None:
    """Prefer an override, then the running process, then the uninstall registry."""
    override = os.environ.get("GOATGAUGE_HOST_EXE", "").strip()
    if override and os.path.isfile(override):
        return override
    try:
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall") as root:
                for index in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        with winreg.OpenKey(root, winreg.EnumKey(root, index)) as item:
                            values = winreg.QueryInfoKey(item)[1]
                            found = {
                                winreg.EnumValue(item, slot)[0]: winreg.EnumValue(item, slot)[1]
                                for slot in range(values)
                            }
                    except OSError:
                        continue
                    if str(found.get("DisplayName", "")).strip() != HOST_PROCESS:
                        continue
                    location = str(found.get("InstallLocation", "")).strip('"').strip()
                    if location:
                        candidate = os.path.join(location, f"{HOST_PROCESS}.exe")
                        if os.path.isfile(candidate):
                            return candidate
                    uninstall = str(found.get("UninstallString", ""))
                    marker = uninstall.lower().rfind("uninstall")
                    if marker > 0:
                        candidate = os.path.join(uninstall[:marker].strip('"'), f"{HOST_PROCESS}.exe")
                        if os.path.isfile(candidate):
                            return candidate
    except OSError:
        pass
    for candidate in (
        rf"D:\Program Files\{HOST_PROCESS}\{HOST_PROCESS}.exe",
        rf"C:\Program Files\{HOST_PROCESS}\{HOST_PROCESS}.exe",
        os.path.expandvars(rf"%LOCALAPPDATA%\Programs\{HOST_PROCESS}\{HOST_PROCESS}.exe"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def launch_host(
    exe: str | None = None,
    *,
    profile: Path | None = None,
    capture: Callable[[list[str]], str] = _capture,
    spawn: Callable[[list[str]], Any] = None,
) -> dict[str, Any]:
    """Start the host with a loopback debugging port, but never restart it for them.

    A host already running without a port cannot gain one in place (single
    instance lock), and closing it would take the user's session with it, so that
    case returns an instruction instead of an action.
    """
    profile = profile or commandcode_profile_dir()
    port = devtools_port(profile)
    if host_listener(port, capture=capture):
        return {"ok": True, "state": "ready", "port": port}
    if host_processes(capture=capture):
        return {
            "ok": False,
            "state": "running-without-port",
            "hint": f"请完全退出 {HOST_PROCESS} 后再运行本命令；守护进程不会替你关闭正在使用的会话。",
        }
    target = exe or find_host_exe()
    if not target:
        return {"ok": False, "state": "no-host", "hint": f"找不到 {HOST_PROCESS}，可用 --exe 指定。"}
    popener = spawn or _spawn_detached
    popener([target, *LAUNCH_ARGS])
    return {"ok": True, "state": "launched", "exe": target}


def _spawn_detached(argv: list[str]) -> Any:
    detached = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(  # noqa: S603 - the executable path comes from the registry/override
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=detached,
    )


def follow(
    *,
    profile: Path | None = None,
    log: Callable[[str], None] = lambda message: None,
    capture: Callable[[list[str]], str] = _capture,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
    auto_launch: bool = False,
    launcher: Callable[[], dict[str, Any]] | None = None,
) -> int:
    """Attach while the host owns its port, detach the moment it does not.

    ``auto_launch`` is opt-in and gets exactly one evaluation, the first time the
    watchdog looks at a host that owns no port. If the host is already running it
    simply waits - otherwise quitting Command Code would make us reopen it behind the
    user's back, which is the opposite of following the host.
    """
    profile = profile or commandcode_profile_dir()
    lock = runtime_dir() / LOCK_NAME
    token = uuid.uuid4().hex
    if not acquire_lock(
        lock, token=token, pid=os.getpid(), alive=lambda pid: pid_alive(pid, capture=capture)
    ):
        log("已有守护进程在跟随 Command Code，本实例退出。")
        return 0
    log(f"守护启动：pid={os.getpid()} auto_launch={'on' if auto_launch else 'off'}")
    injector = WidgetInjector(profile=profile, payload_builder=dict, log=log)
    port: int | None = None
    next_check = 0.0
    next_attach = 0.0
    decide_launch = auto_launch
    try:
        while True:
            now = clock()
            if now >= next_check:
                found = devtools_port(profile)
                owner = host_listener(found, capture=capture)
                if not owner:
                    if injector.attached():
                        injector.detach_all()
                        port = None
                        log("宿主或调试端口已消失，读数条已卸载。")
                    elif decide_launch:
                        decide_launch = False
                        if host_processes(capture=capture) == 0:
                            result = (launcher or launch_host)()
                            log(
                                "宿主未运行，已带调试端口拉起。"
                                if result.get("ok")
                                else f"未能拉起宿主：{result.get('state') or result.get('reason')}"
                            )
                        else:
                            # Saying nothing here is how "why is there no board" turns
                            # into an hour of guessing: the chance is spent, on purpose.
                            log("宿主在跑但没有调试端口；不会重启它，也不再自动拉起。"
                                "带端口启动请用 gauge-launch.cmd 或改过参数的快捷方式。")
                    next_check = now + IDLE_INTERVAL
                else:
                    if port not in (None, found):
                        injector.detach_all()
                    port = found
                    next_check = now + LISTENER_RECHECK
            if port and now >= next_attach:
                injector.attach()
                next_attach = now + ATTACH_INTERVAL
            sleeper(TICK)
    except KeyboardInterrupt:
        pass
    except BaseException:  # noqa: BLE001 - a daemon with no console must leave a trace
        log("守护进程异常退出：\n" + traceback.format_exc())
        raise
    finally:
        log("守护进程退出。")
        injector.detach_all()
        release_lock(lock, token=token)
    return 0


def file_logger(path: Path, clock: Callable[[], float] = time.time) -> Callable[[str], None]:
    """Append with timestamps, and never let logging break the loop it is logging.

    ``pythonw`` has no console, so without this the watchdog can die mid-session and
    leave nothing behind - which is exactly how the ``--watch`` argparse mismatch
    stayed invisible for a whole day.
    """

    def log(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(clock()))} {message}{os.linesep}"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        except OSError:
            pass

    return log


INJECT_LOCK_NAME = "inject.lock"


def claim_inject_lock(
    *, mode: str = "live", alive: Callable[[int], bool] = pid_alive
) -> tuple[Path, str] | None:
    """Gate the DevTools registration to a single process, and record what it is.

    The HTTP port cannot serve as the mutex: an instance that fails to bind it
    still runs its injector thread, so N instances end up each registering the
    readout and pushing their own numbers over one another. The mode travels with
    the claim so synthetic data can never quietly replace a real reading.
    """
    path = runtime_dir() / INJECT_LOCK_NAME
    current = read_json(path)
    if current:
        try:
            owner = int(current.get("pid") or -1)
        except (TypeError, ValueError):
            owner = -1
        if alive(owner):
            return None
        if str(current.get("mode") or "") == "live" and mode == "demo":
            return None
    token = uuid.uuid4().hex
    write_json_atomic(
        path,
        {"pid": os.getpid(), "token": token, "mode": mode, "started_at": round(time.time(), 3)},
    )
    return path, token


def release_inject_lock(claim: tuple[Path, str] | None) -> None:
    if claim:
        release_lock(claim[0], token=claim[1])


def gauge_report(port: int = 18927, *, timeout: float = 2.0) -> dict[str, Any]:
    """Ask the port owner which mode it is serving, instead of inferring it.

    Guessing from a PID list is what let a synthetic 70-credit pool be read as
    the account's real ceiling; the endpoint states it outright.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(
            urllib.request.Request(f"http://127.0.0.1:{port}/api/state"), timeout=timeout
        ) as response:
            payload = json.loads(response.read(4_194_304).decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - a diagnostic never raises
        return {"port": port, "reachable": False, "error": type(exc).__name__}
    meta = payload.get("meta") or {}
    return {
        "port": port,
        "reachable": True,
        "mode": "demo" if meta.get("demo") else "live",
        "configured": bool(payload.get("configured")),
        "key_source": str(meta.get("key_source") or ""),
    }


def status(*, profile: Path | None = None, capture: Callable[[list[str]], str] = _capture,
           with_shortcut: bool = False) -> dict[str, Any]:
    profile = profile or commandcode_profile_dir()
    port = devtools_port(profile)
    lock = read_json(runtime_dir() / LOCK_NAME) or {}
    inject_lock = read_json(runtime_dir() / INJECT_LOCK_NAME) or {}
    owner = host_listener(port, capture=capture)
    report: dict[str, Any] = {
        "host_processes": host_processes(capture=capture),
        "debug_port": port or 0,
        "port_owner_is_host": bool(owner),
        "watchdog_pid": int(lock.get("pid") or 0) if pid_alive(int(lock.get("pid") or -1), capture=capture) else 0,
        "inject_mode": str(inject_lock.get("mode") or ""),
        "inject_pid": int(inject_lock.get("pid") or 0) if pid_alive(int(inject_lock.get("pid") or -1), capture=capture) else 0,
        "gauge": gauge_report(),
        "widget_source": str(Path(__file__).resolve().parent / "web" / "widget.js"),
    }
    if with_shortcut:
        # Costs a PowerShell round trip, so it is opt-in: only the CLI asks for it.
        from .launch_shortcut import inspect as shortcut_inspect

        try:
            report["launch_shortcut"] = shortcut_inspect()
        except Exception as exc:  # noqa: BLE001 - a diagnostic never raises
            report["launch_shortcut"] = {"state": f"error:{type(exc).__name__}"}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跟随 Command Code 的读数条守护进程")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--follow", action="store_true", help="常驻跟随宿主进程（默认）")
    mode.add_argument("--launch", action="store_true", help="带环回调试端口启动宿主")
    mode.add_argument("--detach", action="store_true", help="从当前所有宿主页面移除读数条")
    mode.add_argument("--probe", action="store_true", help="只回读宿主接口的键名与类型")
    mode.add_argument("--status", action="store_true", help="打印诊断信息")
    parser.add_argument("--exe", default=None, help="Command Code 可执行文件路径")
    parser.add_argument("--auto-launch", action="store_true",
                        help="宿主完全没运行时，随守护启动带端口拉起一次（不会在你手动退出后重开）")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(status(with_shortcut=True), ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.detach:
        result = detach()
        print(f"已从 {result.get('pages', 0)} 个页面移除读数条。" if result.get("ok") or result.get("pages")
              else f"未能移除：{result.get('reason')}", flush=True)
        return 0 if result.get("ok") or result.get("pages") else 1
    if args.probe:
        result = probe_host()
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result.get("ok") else 1
    if args.launch:
        result = launch_host(args.exe)
        message = {"ready": "宿主已带调试端口在跑。", "launched": "已带调试端口启动宿主。",
                   "running-without-port": result.get("hint", ""), "no-host": result.get("hint", "")}
        print(message.get(str(result.get("state")), ""), flush=True)
        return 0 if result.get("ok") else 2
    return follow(auto_launch=args.auto_launch, log=file_logger(runtime_dir() / LOG_NAME))


if __name__ == "__main__":
    raise SystemExit(main())
