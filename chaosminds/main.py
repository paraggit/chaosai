from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from chaosminds.cleanup import cleanup_from_config
from chaosminds.cmd_split import (
    UnsafeCommandError,
    split_command,
)
from chaosminds.config import AppConfig
from chaosminds.supervisor import Supervisor

LOGS_DIR = Path("logs")
SCRIPTS_DIR = Path("scripts")


def setup_logging(level: str) -> Path:
    """Configure dual logging.

    - **Console** (stderr): level from ``level`` (typically INFO). Use
      ``--verbose`` or ``LOG_LEVEL=DEBUG`` for full console detail.
    - **Log file** (``logs/run_*.log``): always **DEBUG** so full plans,
      prompts, and tool I/O remain available without cluttering the console.
    """
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
    parser.add_argument(
        "--loop-count", type=int,
        help="Number of test iterations (default: 10)",
    )
    parser.add_argument(
        "--chaos-max-parallel", type=int,
        help=(
            "Krknctl --max-parallel in script mode "
            "(default: CHAOS_MAX_PARALLEL or 4)"
        ),
    )
    parser.add_argument("--env-file", default=".env", help="Path to .env file (default: .env)")
    parser.add_argument(
        "--script-mode", action="store_true",
        help=(
            "Plan once + generate + execute a bash script."
            " Default: full agent mode (LLM at every step)."
        ),
    )
    parser.add_argument(
        "--script-only", action="store_true",
        help=(
            "Generate a bash script but do NOT execute it"
            " (for review/editing)."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "Console: show DEBUG (full detail). "
            "Without this, console is INFO; log file always has DEBUG."
        ),
    )
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
        loop_count=args.loop_count,
        chaos_max_parallel=args.chaos_max_parallel,
    )

    log_level = "DEBUG" if args.verbose else config.log_level
    log_file = setup_logging(log_level)
    logger = logging.getLogger("chaosminds")

    logger.info(
        "Logging: stderr=%s  |  file=%s (file always DEBUG)",
        log_level.upper(),
        log_file.resolve(),
    )

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

    run_id = log_file.stem.replace("run_", "")
    logger.info("  Loop count : %d", config.loop_count)

    if args.script_only:
        _script_only_mode(config, args.instruction, logger)
    elif args.script_mode:
        _script_mode(
            config, args.instruction, logger, run_id,
        )
    else:
        _agent_mode(config, args.instruction, logger, log_file)


def _script_mode(
    config: AppConfig,
    instruction: str,
    logger: logging.Logger,
    run_id: str = "",
) -> None:
    """Script mode (--script-mode): Planner → Script → Execute → Analyze.

    1. Planner: LLM generates a structured 5-phase plan
    2. Executor: convert plan to bash script and run it
    3. Observer: post-chaos analysis via bob
    """
    import json
    import os
    import subprocess

    from beeai_framework.adapters.ollama.backend.chat import (
        OllamaChatModel,
    )

    from chaosminds.agents.analysis import AnalysisAgent
    from chaosminds.agents.planner import PlannerAgent
    from chaosminds.rag.factory import build_rag_tools
    from chaosminds.script_generator import generate_script
    from chaosminds.state import WorkflowState

    logger.info(
        "Mode: Planner → Executor → Observer "
        "(plan → script → execute → analyze)"
    )

    # ── PLANNER ──
    logger.info("=" * 60)
    logger.info("PLANNER: Generating structured plan...")
    logger.info("=" * 60)

    llm = OllamaChatModel(
        model_id=config.llm_model,
        settings={"base_url": config.llm_endpoint},
    )
    rag_tools = build_rag_tools(config.rag)
    planner = PlannerAgent(
        llm, config.scenario_plan, rag_tools=rag_tools,
    )
    state = WorkflowState(instruction=instruction)
    state = asyncio.run(planner.plan(state))

    plan = state.structured_plan
    if not plan or not any(
        plan.get(k) for k in ("setup", "chaos", "test_ops")
    ):
        logger.error("Planner produced an empty/invalid plan")
        sys.exit(1)

    # Plan summary + full JSON: see PlannerAgent (INFO summary, DEBUG detail)

    # ── EXECUTOR: generate script ──
    logger.info("=" * 60)
    logger.info("EXECUTOR: Generating multi-phase script...")
    logger.info("=" * 60)

    script = generate_script(plan, config, instruction)

    SCRIPTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    script_path = SCRIPTS_DIR / f"chaos_{ts}.sh"
    script_path.write_text(script)
    script_path.chmod(0o755)

    logger.info("Script saved: %s", script_path.resolve())
    logger.info(
        "Script (%d lines):\n%s",
        script.count("\n"), script[:5000],
    )

    # ── EXECUTOR: run the script ──
    logger.info("=" * 60)
    logger.info("EXECUTOR: Running script...")
    logger.info("=" * 60)

    env = {**os.environ}
    if config.kubeconfig:
        env["KUBECONFIG"] = config.kubeconfig

    try:
        result = subprocess.run(
            ["bash", str(script_path)],
            capture_output=False,
            text=True,
            env=env,
            timeout=config.chaos_timeout + 600,
        )
        script_rc = result.returncode
    except subprocess.TimeoutExpired:
        logger.error(
            "Script execution timed out after %ds",
            config.chaos_timeout + 600,
        )
        script_rc = 1

    logger.info(
        "Script exit code: %d", script_rc,
    )

    # ── OBSERVER: post-chaos analysis ──
    logger.info("=" * 60)
    logger.info(
        "OBSERVER: Running post-chaos analysis...",
    )
    logger.info("=" * 60)

    analysis: dict = {
        "verdict": "ANALYSIS FAILED",
        "bugs": 0,
        "warnings": 0,
        "findings": [],
        "report_path": "",
    }
    try:
        analysis_agent = AnalysisAgent(llm, config)
        analysis = asyncio.run(
            analysis_agent.analyze(instruction, run_id=run_id),
        )
    except Exception:
        logger.exception(
            "Post-chaos analysis failed — "
            "continuing to cleanup",
        )
    finally:
        cleanup_from_config(config, logger)

    # ── Final Report ──
    print("\n" + "=" * 60)
    print("  ChaosMinds — Execution Report")
    print("=" * 60)
    print(f"  Script: {script_path}")
    print(
        f"  Script result: "
        f"{'SUCCESS' if script_rc == 0 else 'FAILED'}"
    )
    print(f"  Loop count: {config.loop_count}")
    print("-" * 60)
    print(f"  Analysis: {analysis['verdict']}")
    print(
        f"  Bugs: {analysis['bugs']}  "
        f"Warnings: {analysis['warnings']}"
    )
    for f in analysis.get("findings", []):
        print(f"    - {f}")
    if analysis.get("report_path"):
        print("-" * 60)
        print(f"  Report: {analysis['report_path']}")
    print("=" * 60)

    sys.exit(0 if script_rc == 0 else 1)


def _exec_step(
    step: dict,
    config: AppConfig,
    env: dict,
    logger: logging.Logger,
) -> tuple[int, str]:
    """Execute a single plan step via the appropriate tool."""
    tool = step.get("tool", "")
    params = step.get("params", {})

    if tool == "oc":
        return _exec_oc(params, config, env, logger)

    elif tool == "bob_cli":
        test = params.get("test", "")
        extra = params.get("extra_args", "")
        cmd_parts = [config.bob_cli_path, "run"]
        if test:
            cmd_parts.extend(["--test", test])
        if extra:
            try:
                cmd_parts.extend(split_command(extra))
            except UnsafeCommandError as exc:
                return 1, f"unsafe bob extra_args: {exc}"
        return _run_cmd(cmd_parts, "bob", logger, env)

    elif tool == "chaos_during":
        return _exec_chaos_during(
            params, config, env, logger,
        )

    elif tool == "krknctl":
        return _exec_krknctl_blocking(
            params, config, env, logger,
        )

    elif tool == "wait":
        return _exec_wait(config, env, logger)

    elif tool == "health_check":
        ns = params.get("namespace", "openshift-storage")
        rc, out = _run_cmd(
            [config.oc_path, "get", "pods", "-n", ns,
             "--no-headers"],
            "health", logger, env,
        )
        _run_cmd(
            [config.oc_path, "exec", "-n", ns,
             "deploy/rook-ceph-tools", "--",
             "ceph", "status"],
            "health-ceph", logger, env,
        )
        return rc, out

    return 1, f"Unknown tool: {tool}"


def _exec_oc(
    params: dict,
    config: AppConfig,
    env: dict,
    logger: logging.Logger,
) -> tuple[int, str]:
    cmd_str = params.get("command", "")
    yaml_body = params.get("yaml", "")
    if yaml_body and "apply" in cmd_str:
        return _run_cmd(
            [config.oc_path, "apply", "-f", "-"],
            "oc", logger, env,
            stdin_data=yaml_body,
        )
    elif cmd_str:
        try:
            parts = split_command(cmd_str)
        except UnsafeCommandError as exc:
            return 1, f"unsafe oc command: {exc}"
        return _run_cmd(
            [config.oc_path, *parts],
            "oc", logger, env,
        )
    return 1, "No command in oc step"


def _exec_chaos_during(
    params: dict,
    config: AppConfig,
    env: dict,
    logger: logging.Logger,
) -> tuple[int, str]:
    """Start chaos in background, run operations, stop chaos.

    Flow:
      1. Start krknctl random run as a background process
      2. Wait a few seconds for chaos container to start
      3. Run each inner operation step sequentially
      4. Stop chaos containers (podman stop)
      5. Terminate the krknctl process
    """
    import json
    import subprocess
    import tempfile
    import time

    from chaosminds.tools.krknctl_tool import (
        build_krknctl_graph,
    )

    scenario_config = params.get("scenario_config", {})
    operations = params.get("operations", [])
    max_parallel = params.get("max_parallel", 1)

    if not scenario_config:
        return 1, "No scenario_config in chaos_during"
    if not operations:
        return 1, "No operations in chaos_during"

    import os

    graph = build_krknctl_graph(scenario_config)
    tmp_path: str | None = None
    chaos_proc: subprocess.Popen | None = None

    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json",
            delete=False, prefix="krknctl_",
        )
        json.dump(graph, tmp, indent=2)
        tmp.close()
        tmp_path = tmp.name

        logger.info(
            "[chaos_during] Scenario: %s",
            scenario_config.get("name", "?"),
        )
        logger.info(
            "[chaos_during] Graph:\n%s",
            json.dumps(graph, indent=2),
        )
        logger.info(
            "[chaos_during] Operations: %d inner steps",
            len(operations),
        )

        krknctl_args = [
            config.krknctl_path, "random", "run",
            tmp_path, f"--max-parallel={max_parallel}",
        ]
        if config.kubeconfig:
            krknctl_args.append(
                f"--kubeconfig={config.kubeconfig}",
            )

        logger.info(
            "[chaos_during] Starting chaos: %s",
            " ".join(krknctl_args),
        )
        chaos_proc = subprocess.Popen(
            krknctl_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        logger.info(
            "[chaos_during] Waiting for chaos to become "
            "active on the cluster...",
        )
        chaos_confirmed = False
        wait_deadline = 120
        waited = 0
        poll = 10

        while waited < wait_deadline:
            time.sleep(poll)
            waited += poll
            rc, list_out = _run_cmd(
                [config.krknctl_path, "list", "running"],
                "chaos_during-poll", logger, env,
            )
            lower = list_out.lower()
            has_running = (
                "krknctl-" in list_out
                and "no scenarios" not in lower
                and "no release" not in lower
            )
            if has_running:
                logger.info(
                    "[chaos_during] Chaos is active "
                    "after %ds: %s",
                    waited, list_out[:300],
                )
                chaos_confirmed = True
                break
            logger.info(
                "[chaos_during] Not active yet "
                "(%ds/%ds)...", waited, wait_deadline,
            )

        if not chaos_confirmed:
            logger.warning(
                "[chaos_during] Chaos did not start "
                "within %ds, proceeding anyway",
                wait_deadline,
            )

        settle = config.chaos_settle_time
        logger.info(
            "[chaos_during] Chaos settle period: "
            "waiting %ds for disruption to take effect...",
            settle,
        )
        time.sleep(settle)

        op_results: list[str] = []
        all_ops_ok = True

        for i, op in enumerate(operations, 1):
            op_tool = op.get("tool", "")
            op_action = op.get("action", "")
            op_params = op.get("params", {})

            logger.info(
                "[chaos_during]   op %d/%d — [%s] %s",
                i, len(operations), op_tool, op_action,
            )

            if op_tool == "oc":
                op_rc, _ = _exec_oc(
                    op_params, config, env, logger,
                )
            elif op_tool == "bob_cli":
                test = op_params.get("test", "")
                extra = op_params.get("extra_args", "")
                cmd_parts = [config.bob_cli_path, "run"]
                if test:
                    cmd_parts.extend(["--test", test])
                if extra:
                    try:
                        cmd_parts.extend(split_command(extra))
                    except UnsafeCommandError:
                        op_rc = 1
                        op_results.append(
                            "FAIL: unsafe bob extra_args",
                        )
                        all_ops_ok = False
                        continue
                op_rc, _ = _run_cmd(
                    cmd_parts, "bob", logger, env,
                )
            else:
                op_rc = 1

            status = "OK" if op_rc == 0 else "FAIL"
            op_results.append(f"{status}: {op_action}")
            logger.info(
                "[chaos_during]   op %d result: %s", i, status,
            )
            if op_rc != 0:
                all_ops_ok = False

        logger.info(
            "[chaos_during] Operations done — stopping chaos",
        )
        _stop_krknctl_scenarios(config, env, logger)

        if chaos_proc is not None:
            if chaos_proc.poll() is None:
                logger.info(
                    "[chaos_during] Terminating krknctl process "
                    "(pid=%d)", chaos_proc.pid,
                )
                chaos_proc.terminate()
            try:
                stdout, stderr = chaos_proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                chaos_proc.kill()
                stdout, stderr = chaos_proc.communicate(timeout=15)
            if stdout:
                logger.info(
                    "[chaos_during] krknctl stdout:\n%s",
                    stdout[:2000],
                )
            if stderr:
                logger.info(
                    "[chaos_during] krknctl stderr:\n%s",
                    stderr[:2000],
                )

        _run_cmd(
            [config.krknctl_path, "clean"],
            "chaos_during-clean", logger, env,
        )

        summary = "\n".join(op_results)
        if all_ops_ok:
            return 0, f"All operations succeeded under chaos\n{summary}"
        return 1, f"Some operations failed under chaos\n{summary}"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _stop_krknctl_scenarios(
    config: AppConfig,
    env: dict,
    logger: logging.Logger,
) -> None:
    """Stop all running krknctl scenario containers."""
    import subprocess

    rc, out = _run_cmd(
        [config.krknctl_path, "list", "running"],
        "stop-chaos", logger, env,
    )

    container_names = []
    for line in out.splitlines():
        parts = line.split()
        if (len(parts) >= 4
                and parts[-1].startswith("krknctl-")):
            container_names.append(parts[-1])

    if not container_names:
        logger.info("[stop-chaos] No running containers found")
        return

    for name in container_names:
        logger.info("[stop-chaos] Stopping container: %s", name)
        try:
            subprocess.run(
                ["podman", "stop", name],
                capture_output=True, text=True,
                timeout=60, env=env,
            )
        except Exception as exc:
            logger.warning(
                "[stop-chaos] Failed to stop %s: %s",
                name, exc,
            )


def _exec_krknctl_blocking(
    params: dict,
    config: AppConfig,
    env: dict,
    logger: logging.Logger,
) -> tuple[int, str]:
    """Legacy blocking krknctl execution (for backward compat)."""
    import json
    import os
    import tempfile

    from chaosminds.tools.krknctl_tool import (
        build_krknctl_graph,
    )

    scenario_file = params.get("scenario_file", "")
    scenario_config = params.get("scenario_config", {})
    max_parallel = params.get("max_parallel", 2)
    tmp_path: str | None = None

    try:
        if scenario_config:
            graph = build_krknctl_graph(scenario_config)
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json",
                delete=False, prefix="krknctl_",
            )
            json.dump(graph, tmp, indent=2)
            tmp.close()
            tmp_path = tmp.name
            scenario_file = tmp_path
            logger.info(
                "[krknctl] Graph:\n%s",
                json.dumps(graph, indent=2),
            )
        elif not scenario_file:
            scenario_file = config.scenario_plan_path

        cmd = [
            config.krknctl_path, "random", "run",
            scenario_file,
            f"--max-parallel={max_parallel}",
        ]
        if config.kubeconfig:
            cmd.append(f"--kubeconfig={config.kubeconfig}")
        return _run_cmd(
            cmd,
            "krknctl", logger, env,
            timeout=config.chaos_timeout,
        )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _exec_wait(
    config: AppConfig,
    env: dict,
    logger: logging.Logger,
) -> tuple[int, str]:
    import time

    rc, out = _run_cmd(
        [config.krknctl_path, "list", "running"],
        "krknctl-wait", logger, env,
    )
    lower = out.lower()
    if ("no scenarios are currently running" in lower
            or "no release found" in lower):
        return 0, "No scenarios running"

    timeout = config.chaos_timeout
    interval = config.chaos_poll_interval
    elapsed = 0
    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval
        rc, out = _run_cmd(
            [config.krknctl_path, "list", "running"],
            "krknctl-wait", logger, env,
        )
        lower = out.lower()
        if ("no scenarios are currently running" in lower
                or "no release found" in lower):
            return 0, f"Completed after {elapsed}s"
    return 1, f"Timed out after {timeout}s"


def _run_cmd(
    cmd: list[str],
    label: str,
    logger: logging.Logger,
    env: dict,
    *,
    stdin_data: str | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    import subprocess

    logger.info("[%s] %s", label, " ".join(cmd))
    if stdin_data:
        logger.info("[%s] stdin:\n%s", label, stdin_data[:2000])
    try:
        result = subprocess.run(
            cmd, input=stdin_data,
            capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        out = result.stdout.strip()
        if result.stderr:
            out += "\n[stderr] " + result.stderr.strip()
        logger.info("[%s] exit=%d", label, result.returncode)
        if out:
            logger.info("[%s] output:\n%s", label, out[:3000])
        return result.returncode, out
    except subprocess.TimeoutExpired:
        logger.error("[%s] timed out", label)
        return 1, "timed out"


def _agent_mode(
    config: AppConfig,
    instruction: str,
    logger: logging.Logger,
    log_file: Path,
) -> None:
    """Default mode: full agentic loop (LLM plans, agents execute each step)."""
    logger.info("Mode: agent (full LLM loop per step)")
    supervisor = Supervisor(config)
    state = asyncio.run(supervisor.run(instruction))

    print("\n" + state.final_report)
    logger.info(
        "Run complete. Log saved to %s", log_file.resolve(),
    )
    sys.exit(0 if state.phase.name == "COMPLETED" else 1)


def _script_only_mode(
    config: AppConfig,
    instruction: str,
    logger: logging.Logger,
) -> None:
    """Generate a bash script for review (opt-in, no execution)."""
    from beeai_framework.adapters.ollama.backend.chat import (
        OllamaChatModel,
    )

    from chaosminds.agents.planner import PlannerAgent
    from chaosminds.rag.factory import build_rag_tools
    from chaosminds.script_generator import generate_script
    from chaosminds.state import WorkflowState

    logger.info("Mode: script-only (generate bash script)")

    llm = OllamaChatModel(
        model_id=config.llm_model,
        settings={"base_url": config.llm_endpoint},
    )
    rag_tools = build_rag_tools(config.rag)
    planner = PlannerAgent(
        llm, config.scenario_plan, rag_tools=rag_tools,
    )

    state = WorkflowState(instruction=instruction)
    state = asyncio.run(planner.plan(state))

    plan = state.structured_plan
    if not plan:
        logger.error("Planner produced an empty plan")
        sys.exit(1)

    # Plan summary + full JSON: see PlannerAgent (INFO summary, DEBUG detail)

    script = generate_script(plan, config, instruction)

    SCRIPTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    script_path = SCRIPTS_DIR / f"chaos_{ts}.sh"
    script_path.write_text(script)
    script_path.chmod(0o755)

    logger.info("Script saved to %s", script_path.resolve())
    print(f"\nGenerated script: {script_path}")
    print(f"Run it with:  ./{script_path}")


if __name__ == "__main__":
    main()
