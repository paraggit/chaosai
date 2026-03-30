from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from chaosminds.config import AppConfig
from chaosminds.supervisor import Supervisor

LOGS_DIR = Path("logs")


def setup_logging(level: str) -> Path:
    """Configure dual logging: console (compact) + per-run log file (verbose)."""
    LOGS_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_file = LOGS_DIR / f"run_{timestamp}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    ))

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root.addHandler(console)
    root.addHandler(file_handler)

    return log_file


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="chaosminds",
        description="ChaosMinds — Multi-agent chaos engineering for ODF/OCS clusters",
    )
    parser.add_argument(
        "instruction",
        type=str,
        help='High-level task, e.g. "test PVC snapshot creation under chaos"',
    )
    parser.add_argument("--kubeconfig", help="Path to KUBECONFIG (overrides .env)")
    parser.add_argument("--llm-endpoint", help="Local LLM endpoint URL (overrides .env)")
    parser.add_argument("--llm-model", help="LLM model name (overrides .env)")
    parser.add_argument("--bob-cli", help="Path to BOB CLI binary (overrides .env)")
    parser.add_argument("--krknctl", help="Path to krknctl binary (overrides .env)")
    parser.add_argument("--oc", help="Path to oc binary (overrides .env)")
    parser.add_argument("--scenario-plan", help="Path to scenario plan JSON (overrides .env)")
    parser.add_argument("--env-file", default=".env", help="Path to .env file (default: .env)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    config = AppConfig.load(
        env_file=args.env_file,
        kubeconfig=args.kubeconfig,
        llm_endpoint=args.llm_endpoint,
        llm_model=args.llm_model,
        bob_cli_path=getattr(args, "bob_cli", None),
        krknctl_path=args.krknctl,
        oc_path=args.oc,
        scenario_plan_path=args.scenario_plan,
    )

    log_file = setup_logging(config.log_level)
    logger = logging.getLogger("chaosminds")

    logger.info("Log file: %s", log_file.resolve())

    if not config.kubeconfig:
        logger.error("KUBECONFIG not set. Provide via --kubeconfig or .env file.")
        sys.exit(1)

    logger.info("Configuration loaded:")
    logger.info("  LLM endpoint : %s", config.llm_endpoint)
    logger.info("  LLM model    : %s", config.llm_model)
    logger.info("  KUBECONFIG   : %s", config.kubeconfig)
    logger.info("  BOB CLI      : %s", config.bob_cli_path)
    logger.info("  krknctl      : %s", config.krknctl_path)
    logger.info("  oc           : %s", config.oc_path)
    logger.info("  Scenarios    : %d loaded", len(config.scenario_plan))

    supervisor = Supervisor(config)
    state = asyncio.run(supervisor.run(args.instruction))

    print("\n" + state.final_report)
    logger.info("Run complete. Log saved to %s", log_file.resolve())
    sys.exit(0 if state.phase.name == "COMPLETED" else 1)


if __name__ == "__main__":
    main()
