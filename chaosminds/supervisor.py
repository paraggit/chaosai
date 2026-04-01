from __future__ import annotations

import json
import logging
import os
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from beeai_framework.adapters.ollama.backend.chat import OllamaChatModel

from chaosminds.agents.analysis import AnalysisAgent
from chaosminds.agents.chaos import ChaosAgent
from chaosminds.agents.executor import ExecutorAgent
from chaosminds.agents.monitor import ClusterMonitorAgent
from chaosminds.agents.planner import PlannerAgent
from chaosminds.agents.waiter import WaitAgent
from chaosminds.config import AppConfig
from chaosminds.rag.factory import build_rag_tools
from chaosminds.state import Phase, WorkflowState
from chaosminds.tools.bob_cli_tool import BobCliTool
from chaosminds.tools.cluster_discovery import ClusterDiscoveryTool
from chaosminds.tools.cluster_health import ClusterHealthTool
from chaosminds.tools.krknctl_tool import KrknctlListTool, KrknctlTool
from chaosminds.tools.kubectl_tool import OcTool

logger = logging.getLogger(__name__)


class Supervisor:
    """
    Orchestrates the chaos engineering workflow:
    plan -> execute -> chaos -> wait -> monitor -> report

    Uses a topological sort over plan steps to respect dependencies,
    and routes each step to the correct agent.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.llm = OllamaChatModel(
            model_id=config.llm_model,
            settings={"base_url": config.llm_endpoint},
        )

        self.oc_tool = OcTool(
            binary_path=config.oc_path, kubeconfig=config.kubeconfig
        )
        self.bob_tool = BobCliTool(
            binary_path=config.bob_cli_path, kubeconfig=config.kubeconfig
        )
        self.krknctl_tool = KrknctlTool(
            binary_path=config.krknctl_path, kubeconfig=config.kubeconfig
        )
        self.krknctl_list_tool = KrknctlListTool(binary_path=config.krknctl_path)
        self.health_tool = ClusterHealthTool(
            oc_path=config.oc_path, kubeconfig=config.kubeconfig
        )
        self.discovery_tool = ClusterDiscoveryTool(
            oc_path=config.oc_path, kubeconfig=config.kubeconfig
        )

        rag_tools = build_rag_tools(config.rag)
        if rag_tools:
            logger.info(
                "[Supervisor] RAG tools enabled (%d): ocs-ci codebase search",
                len(rag_tools),
            )

        self.planner = PlannerAgent(
            self.llm, config.scenario_plan, rag_tools=rag_tools,
        )
        self.executor = ExecutorAgent(
            self.llm, self.bob_tool, self.oc_tool, rag_tools=rag_tools,
        )
        self.chaos_agent = ChaosAgent(
            self.llm, self.krknctl_tool, self.discovery_tool,
            rag_tools=rag_tools,
        )
        self.wait_agent = WaitAgent(
            self.llm,
            self.krknctl_list_tool,
            timeout=config.chaos_timeout,
            poll_interval=config.chaos_poll_interval,
        )
        self.monitor = ClusterMonitorAgent(self.llm, self.health_tool)
        self.analysis_agent = AnalysisAgent(self.llm, config)

    def _log_phase(self, old: Phase, new: Phase) -> None:
        if old != new:
            logger.info("[Supervisor] Phase transition: %s → %s", old.name, new.name)

    def _log_health_snapshot(self, state: WorkflowState, label: str) -> None:
        h = state.cluster_health
        if not h:
            return
        logger.info("[Supervisor] Health [%s]: status=%s  healthy=%s  ceph=%s  pods_pending=%s  nodes_not_ready=%s",
                     label,
                     h.get("overall_status", "?"),
                     h.get("overall_healthy", "?"),
                     h.get("ceph", {}).get("status", "?"),
                     h.get("pods", {}).get("pending", "?"),
                     len(h.get("nodes", {}).get("not_ready", [])))
        triggered = h.get("triggered_rules", [])
        if triggered:
            logger.info("[Supervisor] Health [%s] triggered rules: %s", label, triggered)

    async def run(self, instruction: str) -> WorkflowState:
        state = WorkflowState(instruction=instruction)
        start_time = datetime.now(timezone.utc)

        logger.info("=" * 60)
        logger.info("[Supervisor] ChaosMinds Workflow Starting")
        logger.info("[Supervisor] Instruction: %s", instruction)
        logger.info("[Supervisor] Start time: %s", start_time.isoformat())
        logger.info("=" * 60)

        # ── Phase 1: Plan ──
        old_phase = state.phase
        logger.info("[Supervisor] Phase 1: PLANNING")
        state = await self.planner.plan(state)
        self._log_phase(old_phase, state.phase)

        if not state.plan_steps and state.structured_plan:
            state.plan_steps = PlannerAgent._flatten_plan(
                state.structured_plan,
            )
            logger.info(
                "[Supervisor] Rebuilt %d plan_steps from "
                "structured_plan",
                len(state.plan_steps),
            )

        if not state.plan_steps:
            state.phase = Phase.FAILED
            state.errors.append("Planner produced an empty plan")
            logger.error("[Supervisor] Planner produced an empty plan — aborting")
            state.final_report = state.summary()
            return state

        logger.info("[Supervisor] Plan: %d steps", len(state.plan_steps))
        for step in state.plan_steps:
            logger.info("[Supervisor]   Step %s: [%s] %s", step.get("id"), step.get("tool"), step.get("action"))

        # ── Phase 2: Execute steps in dependency order ──
        sorted_steps = self._topological_sort(state.plan_steps)
        total = len(sorted_steps)

        for idx, step in enumerate(sorted_steps, 1):
            step_id = step.get("id", "?")
            tool = step.get("tool", "unknown")
            action = step.get("action", "")

            logger.info("-" * 60)
            logger.info("[Supervisor] Step %s/%s — id=%s tool=%s action=%s", idx, total, step_id, tool, action)
            logger.info("[Supervisor] Step params: %s", json.dumps(step.get("params", {}), indent=2))

            # Pre-step health check
            old_phase = state.phase
            state = await self.monitor.monitor(state)
            self._log_health_snapshot(state, f"pre-step-{step_id}")

            if self._is_cluster_critical(state) and tool == "krknctl":
                logger.warning("[Supervisor] Skipping chaos step %s — cluster is CRITICAL", step_id)
                state.log_step(step_id, tool, action, "skipped", error="Cluster in critical state")
                continue

            # Route to the correct agent
            logger.info("[Supervisor] Routing step %s to agent for tool=%s", step_id, tool)
            old_phase = state.phase

            match tool:
                case "bob_cli":
                    state = await self.executor.execute(step, state)
                case "oc":
                    state = await self.executor.execute(step, state)
                case "krknctl":
                    state.phase = Phase.CHAOS_INJECTING
                    self._log_phase(old_phase, state.phase)
                    state = await self.chaos_agent.inject(step, state)
                    last_log = state.execution_log[-1] if state.execution_log else None
                    chaos_succeeded = (
                        state.phase != Phase.FAILED
                        and last_log is not None
                        and last_log.status == "success"
                    )
                    if chaos_succeeded:
                        state.phase = Phase.WAITING
                        self._log_phase(Phase.CHAOS_INJECTING, state.phase)
                        state = await self.wait_agent.wait_for_completion(step, state)
                    elif state.phase != Phase.FAILED:
                        logger.warning("[Supervisor] Chaos injection did not succeed for step %s — skipping wait", step_id)
                        if last_log and last_log.status != "failed":
                            state.log_step(step_id, tool, action, "failed", error="Chaos injection did not produce a running scenario")
                case "wait":
                    state = await self.wait_agent.wait_for_completion(step, state)
                case "health_check":
                    state.phase = Phase.MONITORING
                    self._log_phase(old_phase, state.phase)
                    state = await self.monitor.monitor(state)
                    state.log_step(step_id, tool, action, "success", output="Health check completed")
                case _:
                    logger.warning("[Supervisor] Unknown tool '%s' in step %s — skipping", tool, step_id)
                    state.log_step(step_id, tool, action, "skipped", error=f"Unknown tool: {tool}")

            # Log step result
            if state.execution_log:
                last = state.execution_log[-1]
                logger.info("[Supervisor] Step %s result: status=%s", step_id, last.status)
                if last.error:
                    logger.error("[Supervisor] Step %s error: %s", step_id, last.error)

            # Post-step health check
            state = await self.monitor.monitor(state)
            self._log_health_snapshot(state, f"post-step-{step_id}")

            if state.phase == Phase.FAILED:
                logger.error("[Supervisor] Pipeline FAILED at step %s — aborting remaining steps", step_id)
                break

            # Monitor feedback: if cluster went CRITICAL during
            # test_ops, log a warning but allow the agent loop to
            # continue — the pre-step check will skip further chaos.
            if step.get("phase") == "test_ops" and self._is_cluster_critical(state):
                logger.warning(
                    "[Supervisor] Cluster CRITICAL during test_ops "
                    "after step %s — monitor flagged for review", step_id,
                )

        # ── Post-chaos analysis (prompt → bob) ──
        run_ts = datetime.now(
            timezone.utc,
        ).strftime("%Y%m%d_%H%M%S")
        analysis = await self.analysis_agent.analyze(
            instruction, run_id=run_ts,
        )
        state.analysis = analysis

        # ── Pre-cleanup checks + resource cleanup ──
        self._precheck_before_cleanup(state)
        self._resource_cleanup()

        # ── Final ──
        elapsed = (
            datetime.now(timezone.utc) - start_time
        ).total_seconds()
        if state.phase != Phase.FAILED:
            state.phase = Phase.COMPLETED

        logger.info("=" * 60)
        logger.info(
            "[Supervisor] Workflow finished: "
            "phase=%s  elapsed=%.1fs  errors=%d",
            state.phase.name, elapsed, len(state.errors),
        )

        state.final_report = self._generate_report(
            state, elapsed,
        )
        logger.info("\n%s", state.final_report)
        return state

    @staticmethod
    def _topological_sort(steps: list[dict]) -> list[dict]:
        """Sort steps respecting depends_on ordering. Falls back to id order."""
        id_to_step = {s["id"]: s for s in steps}
        in_degree: dict[int, int] = defaultdict(int)
        graph: dict[int, list[int]] = defaultdict(list)

        for s in steps:
            sid = s["id"]
            deps = s.get("depends_on", [])
            in_degree[sid] += 0
            for dep in deps:
                graph[dep].append(sid)
                in_degree[sid] += 1

        queue = sorted(sid for sid, deg in in_degree.items() if deg == 0)
        result = []
        while queue:
            current = queue.pop(0)
            if current in id_to_step:
                result.append(id_to_step[current])
            for neighbor in sorted(graph[current]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(result) != len(steps):
            logger.warning("Topological sort incomplete (cycle?), appending remaining steps by id")
            seen = {s["id"] for s in result}
            for s in sorted(steps, key=lambda x: x["id"]):
                if s["id"] not in seen:
                    result.append(s)

        return result

    def _run_oc_cmd(self, args: str) -> str:
        """Run an oc command and return combined output."""
        from chaosminds.cmd_split import (
            UnsafeCommandError,
            split_command,
        )

        try:
            parts = split_command(args)
        except UnsafeCommandError as exc:
            logger.error("[cleanup-oc] unsafe command: %s", exc)
            return ""
        cmd = [self.config.oc_path, *parts]
        env = None
        if self.config.kubeconfig:
            env = {**os.environ, "KUBECONFIG": self.config.kubeconfig}
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=120, env=env,
            )
            output = result.stdout.strip()
            if result.stderr:
                output += "\n" + result.stderr.strip()
            logger.info("[cleanup-oc] %s → exit=%d",
                        args, result.returncode)
            return output
        except subprocess.TimeoutExpired:
            logger.error("[cleanup-oc] timed out: %s", args)
            return ""

    def _precheck_before_cleanup(self, state: WorkflowState) -> None:
        """Run pre-cleanup validations. Extend this method for future checks."""
        logger.info("=" * 60)
        logger.info("[Supervisor] PRE-CLEANUP CHECKS")

        if self.config.collect_must_gather:
            self._collect_must_gather()
        else:
            logger.info("[precheck] COLLECT_MUST_GATHER=false — skipping")

        out = self._run_oc_cmd(
            "exec -n openshift-storage deploy/rook-ceph-tools -- ceph health",
        )
        logger.info("[precheck] Ceph health: %s", out.split("\n")[0] if out else "unknown")

        # Add future pre-cleanup checks below this line

        logger.info("[Supervisor] END PRE-CLEANUP CHECKS")

    def _collect_must_gather(self) -> None:
        """Collect ODF must-gather logs."""
        logger.info("[must-gather] Collecting must-gather logs...")
        mg_dir = Path("must-gather") / f"must-gather_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        mg_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            self.config.oc_path, "adm", "must-gather",
            "--image=quay.io/ocs-dev/ocs-must-gather",
            f"--dest-dir={mg_dir}",
        ]
        env = None
        if self.config.kubeconfig:
            env = {**os.environ, "KUBECONFIG": self.config.kubeconfig}

        logger.info("[must-gather] command: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=600, env=env,
            )
            logger.info("[must-gather] exit_code=%d", result.returncode)
            if result.stdout:
                logger.info("[must-gather] stdout:\n%s", result.stdout[:3000])
            if result.stderr:
                logger.info("[must-gather] stderr:\n%s", result.stderr[:2000])
        except subprocess.TimeoutExpired:
            logger.error("[must-gather] Timed out after 600s")

        file_count = sum(1 for _ in mg_dir.rglob("*") if _.is_file())
        logger.info("[must-gather] Collected %d files → %s", file_count, mg_dir)

    def _resource_cleanup(self) -> None:
        """Delete test resources (chaos-test-* snapshots, PVCs, pods)."""
        from chaosminds.cleanup import cleanup_from_config

        logger.info("[Supervisor] RESOURCE CLEANUP")
        cleanup_from_config(self.config, logger)
        logger.info("[Supervisor] END RESOURCE CLEANUP")

    @staticmethod
    def _is_cluster_critical(state: WorkflowState) -> bool:
        health = state.cluster_health
        if not health:
            return False
        if health.get("overall_status") == "CRITICAL":
            return True
        if health.get("overall_healthy") is False:
            ceph = health.get("ceph", {})
            if ceph.get("status") == "HEALTH_ERR":
                return True
            nodes = health.get("nodes", {})
            if len(nodes.get("not_ready", [])) > 1:
                return True
        return False

    @staticmethod
    def _generate_report(
        state: WorkflowState, elapsed: float,
    ) -> str:
        lines = [
            "=" * 60,
            "  ChaosMinds — Workflow Report",
            "=" * 60,
            f"  Instruction : {state.instruction}",
            f"  Final Phase : {state.phase.name}",
            f"  Elapsed     : {elapsed:.1f}s",
            f"  Total Steps : {len(state.plan_steps)}",
            f"  Executed    : {len(state.execution_log)}",
            f"  Errors      : {len(state.errors)}",
            "-" * 60,
            "  Step Results:",
        ]

        status_map = {
            "success": "PASS", "failed": "FAIL",
            "skipped": "SKIP",
        }
        for r in state.execution_log:
            marker = status_map.get(r.status, "????")
            lines.append(
                f"    [{marker}] Step {r.step_id}: {r.action}"
            )
            if r.error:
                lines.append(f"           Error: {r.error}")

        # Analysis findings
        analysis = getattr(state, "analysis", None)
        if analysis:
            lines.append("-" * 60)
            lines.append(
                f"  Analysis Verdict: {analysis['verdict']}"
            )
            lines.append(
                f"  Bugs: {analysis['bugs']}  "
                f"Warnings: {analysis['warnings']}"
            )
            if analysis.get("findings"):
                lines.append("  Findings:")
                for f in analysis["findings"]:
                    lines.append(f"    - {f}")

        if state.health_timeline:
            lines.append("-" * 60)
            lines.append("  Health Timeline:")
            for snap in state.health_timeline:
                h = snap.get("health", {})
                lines.append(
                    f"    {snap['timestamp']} "
                    f"[{snap['phase']}] "
                    f"status="
                    f"{h.get('overall_status', '?')}  "
                    f"healthy="
                    f"{h.get('overall_healthy', '?')}"
                )

        if state.errors:
            lines.append("-" * 60)
            lines.append("  Errors:")
            for e in state.errors:
                lines.append(f"    - {e}")

        lines.append("=" * 60)
        return "\n".join(lines)
