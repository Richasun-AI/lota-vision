"""CLI entry point for the independently run engine."""

import argparse
import json
import logging
import os
import time
import uuid
from pathlib import Path

from .engine import Engine
from .store import Scenario


def parser():
    root = argparse.ArgumentParser(prog="lota")
    root.add_argument("--home", type=Path, default=os.environ.get("LOTA_HOME", "runtime"))
    root.add_argument("--source-root", type=Path, action="append", default=[])
    root.add_argument("--weights-root", type=Path, action="append", default=[])
    commands = root.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run the HTTP API and workbench")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8795)

    worker = commands.add_parser("worker", help="Run the durable job worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll-seconds", type=float, default=2)

    seed = commands.add_parser("seed", help="Create a scenario from a JSON configuration")
    seed.add_argument("project_id")
    seed.add_argument("file", type=Path)
    seed.add_argument("--activate", action="store_true")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "serve":
        import uvicorn
        from .api import create_app

        token = os.environ.get("LOTA_API_TOKEN")
        if args.host not in ("127.0.0.1", "::1", "localhost") and not token:
            raise SystemExit("LOTA_API_TOKEN is required for a non-loopback listener")
        # 停止时最多等 5 秒：网页上开着的实时画面（MJPEG 长连接）不会自己结束，不设上限会一直卡在关闭中，重启起不来
        uvicorn.run(create_app(args.home, args.source_root, args.weights_root, token),
                    host=args.host, port=args.port, timeout_graceful_shutdown=5)
        return
    engine = Engine(args.home, args.source_root, args.weights_root)
    if args.command == "seed":
        config = Scenario.model_validate_json(args.file.read_text(encoding="utf-8-sig"))
        try:
            project = engine.store.project(args.project_id)
        except FileNotFoundError:
            project = engine.save(args.project_id, config)
        if args.activate and not project["active"]:
            project = engine.store.activate(args.project_id, True)
        print(json.dumps(project, ensure_ascii=False, indent=2))
        return
    identity = uuid.uuid4().hex
    try:
        while True:
            engine.store.schedule_scans()
            job = engine.run_once(identity)
            if job is not None:
                logging.info("%s %s %s", job["project_id"], job["id"], job["status"])
            if args.once:
                return
            if job is None:
                time.sleep(max(0.1, args.poll_seconds))
    except KeyboardInterrupt:
        return
