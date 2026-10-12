"""GOAT Gauge launcher."""

from __future__ import annotations

import argparse
import json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Command Code GOAT usage dashboard")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--chrome", action="store_true", help="run the server and open Chrome")
    mode.add_argument("--serve", action="store_true", help="run only the local server")
    mode.add_argument("--detach", action="store_true", help="remove the injected readout from Command Code")
    mode.add_argument("--watch", action="store_true", help="follow the Command Code process (registered at login)")
    mode.add_argument("--status", action="store_true", help="print host / port / watchdog diagnostics")
    mode.add_argument("--launch", action="store_true", help="start Command Code with a loopback debugging port")
    mode.add_argument("--probe", action="store_true", help="report the shape of the host bridge, values excluded")
    mode.add_argument("--install-shortcut", action="store_true",
                      help="add the loopback debugging arguments to your own Command Code shortcut")
    mode.add_argument("--uninstall-shortcut", action="store_true",
                      help="put the shortcut arguments back exactly as they were before")
    parser.add_argument("--auto-launch", action="store_true",
                        help="with --watch: start the host with a port once, when it is not running at all")
    parser.add_argument("--host", default="127.0.0.1", help="server host")
    parser.add_argument("--port", type=int, default=0, help="server port; 0 chooses a free port")
    parser.add_argument("--demo", action="store_true", help="show generated demo data")
    parser.add_argument("--no-browser", action="store_true", help="do not open Chrome in --chrome mode")
    parser.add_argument(
        "--inject",
        action="store_true",
        help="also inject the readout into the running Command Code app",
    )
    return parser.parse_args()


def detach_widget() -> None:
    from app.inject import commandcode_profile_dir, detach

    result = detach(commandcode_profile_dir())
    if result.get("pages"):
        print(f"已从 {result['pages']} 个 Command Code 页面移除读数条。", flush=True)
    if not result.get("ok"):
        print(
            "未能完成移除："
            f"{result.get('reason') or result.get('errors')}；"
            "Command Code 需要正以环回调试端口运行（gauge-launch.cmd）。",
            flush=True,
        )


def main() -> None:
    args = parse_args()
    if args.install_shortcut or args.uninstall_shortcut:
        from app.launch_shortcut import apply as apply_shortcut
        from app.launch_shortcut import restore as restore_shortcut

        result = apply_shortcut() if args.install_shortcut else restore_shortcut()
        print(json.dumps(result, ensure_ascii=False), flush=True)
        raise SystemExit(0 if result.get("ok") else 1)

    if args.watch or args.status or args.launch or args.probe:
        from app.watchdog import main as watchdog_main

        # --watch is the public name (it is what the HKCU Run key stores); the
        # watchdog's own loop is called --follow. Forwarding the public name here
        # made argparse reject it, so the login autostart died before the first
        # poll while every other mode flag worked.
        if args.status:
            chosen = ["--status"]
        elif args.launch:
            chosen = ["--launch"]
        elif args.probe:
            chosen = ["--probe"]
        else:
            chosen = ["--follow"]
            if args.auto_launch:
                chosen.append("--auto-launch")
        raise SystemExit(watchdog_main(chosen))

    if args.detach:
        detach_widget()
        return

    if args.chrome:
        from app.chrome_mode import main as chrome_main

        chrome_main(
            host=args.host,
            port=args.port,
            demo=args.demo,
            open_browser=not args.no_browser,
            inject=args.inject,
        )
        return

    from app.server import run_foreground

    run_foreground(host=args.host, port=args.port, demo=args.demo, inject=args.inject)


if __name__ == "__main__":
    main()
