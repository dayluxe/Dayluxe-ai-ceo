"""Command line entry point.

    python -m dayluxe serve        # dashboard + API (+ scheduler if RUN_INTERVAL_MINUTES > 0)
    python -m dayluxe run          # one operating cycle, prints the report
    python -m dayluxe health       # integration health checks
    python -m dayluxe check-config # validate settings without calling any service
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from .ceo import CEO
from .config import load_settings
from .context import Context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dayluxe", description="DAYLUXE AI CEO")
    parser.add_argument("command", choices=["serve", "run", "health", "check-config"])
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.env_file)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    problems = settings.validate()
    if args.command == "check-config":
        print(json.dumps({"problems": problems, "settings": settings.redacted()}, indent=2))
        return 1 if any(not p.startswith("Warning") for p in problems) else 0
    for p in problems:
        logging.warning(p)

    ctx = Context.build(settings)
    ceo = CEO(ctx)

    if args.command == "health":
        result = ceo.health()
        print(json.dumps(result, indent=2))
        return 0 if all(v["status"] != "error" for v in result.values()) else 1

    if args.command == "run":
        report = ceo.run_cycle("cli")
        print(json.dumps(report, indent=2, default=str, ensure_ascii=False))
        return 0 if report.get("status") in ("ok", "partial") else 1

    # serve
    from .web import App, make_server, start_scheduler

    if not settings.admin_token or len(settings.admin_token) < 16:
        logging.error("ADMIN_TOKEN must be set (16+ chars) before serving the dashboard.")
        return 2
    app = App(ceo)
    server = make_server(app, settings.host, settings.port)
    stop = threading.Event()
    start_scheduler(app, settings.run_interval_minutes, stop)

    def shutdown(*_: object) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    logging.info("Dayluxe AI CEO listening on http://%s:%s", settings.host, settings.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        ctx.storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
