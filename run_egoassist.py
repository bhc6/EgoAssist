#!/usr/bin/env python3
"""Command-line launch and backwards-compatible public application factory."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import threading
import webbrowser

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from egoassist.application.composition import create_app as create_app
from egoassist.application.ownership import RuntimeRootLease as RuntimeRootLease

# Existing embedding clients and verification scripts patch these model adapters.
from egoassist.realtime import (
    ContextHarness as ContextHarness,
    SemanticTurnRouter as SemanticTurnRouter,
)
from egoassist.memory.insights import InsightGenerator as InsightGenerator
from egoassist.product.answer_jobs import AnswerJobs as AnswerJobs

ROOT = Path(__file__).resolve().parent


def runtime_root() -> Path:
    """Resolve user-owned runtime state consistently for source and wheel launches."""
    working_root = Path.cwd()
    if (working_root / ".env").is_file():
        return working_root
    if (ROOT / ".env").is_file():
        return ROOT
    return working_root


def main():
    local_root = runtime_root()
    parser = argparse.ArgumentParser(
        description="EgoAssist full-duplex embodied assistance framework"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--skills", default=str(BUILTIN_SKILLS_ROOT))
    parser.add_argument(
        "--active-skill",
        default=None,
        help="Optional startup Skill; default is generic unloaded mode",
    )
    parser.add_argument(
        "--memory-path", default=str(local_root / ".egoassist" / "runtime.json")
    )
    parser.add_argument("--model", default="qwen3.5-omni-flash-realtime")
    parser.add_argument("--realtime-backend", choices=("omni", "modular"), default=None)
    parser.add_argument("--voice", default="Ethan")
    args = parser.parse_args()
    log_root = local_root / ".egoassist"
    log_root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_root / "server.log",
        level=logging.INFO,
        encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    env_path = local_root / ".env"
    load_env(str(env_path) if env_path.is_file() else None)
    if args.realtime_backend is not None:
        os.environ["EGOASSIST_REALTIME_BACKEND"] = args.realtime_backend
    from aiohttp import web

    def announce_ready(message):
        print(message)
        # aiohttp announces readiness after all listening sockets are bound.
        if args.open:
            threading.Thread(
                target=webbrowser.open,
                args=(f"http://{args.host}:{args.port}",),
                daemon=True,
            ).start()

    web.run_app(
        create_app(
            args.skills, args.active_skill, args.memory_path, args.model, args.voice
        ),
        host=args.host,
        port=args.port,
        print=announce_ready,
    )


if __name__ == "__main__":
    main()
