"""GOAT Gauge launcher."""

from __future__ import annotations

import argparse


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
    if args.watch or args.status or args.launch or args.probe:
        from app.watchdog import main as watchdog_main

        chosen = "--watch"
        if args.status:
            chosen = "--status"
        elif args.launch:
            chosen = "--launch"
        elif args.probe:
            chosen = "--probe"
        raise SystemExit(watchdog_main([chosen]))

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
