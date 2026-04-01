from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools.tool import Tool, ToolRunOptions
from beeai_framework.tools.types import StringToolOutput

logger = logging.getLogger(__name__)

RULES_PATH = Path(__file__).resolve().parent.parent / "prompts" / "health_rules.txt"


def _parse_rules(path: Path) -> dict[str, list[str]]:
    """Parse the health_rules.txt into sections keyed by header name."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    if not path.exists():
        logger.warning("[health-rules] Rules file not found: %s — using defaults", path)
        return sections
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(":"):
            current = line[:-1].strip()
            sections[current] = []
        elif current and line.startswith("- "):
            sections[current].append(line[2:].strip())
    return sections


class HealthCheckInput(BaseModel):
    namespace: str = Field(
        default="openshift-storage",
        description="Kubernetes namespace to check health for.",
    )
    include_ceph: bool = Field(
        default=True,
        description="Whether to include Ceph cluster health status.",
    )
    include_nodes: bool = Field(
        default=True,
        description="Whether to include node readiness status.",
    )


class ClusterHealthTool(Tool[HealthCheckInput, ToolRunOptions, StringToolOutput]):
    name = "cluster_health_check"
    description = (
        "Comprehensive cluster health checker. Inspects pod status, PVC binding "
        "state, Ceph cluster health, and node readiness in the target namespace. "
        "Evaluates results against configurable rules in health_rules.txt and "
        "returns a structured health summary with HEALTHY/DEGRADED/CRITICAL status."
    )

    def __init__(self, oc_path: str = "oc", kubeconfig: str = "", rules_path: Path | None = None) -> None:
        super().__init__()
        self._oc = oc_path
        self._kubeconfig = kubeconfig
        self._rules = _parse_rules(rules_path or RULES_PATH)
        if self._rules:
            logger.info("[health-rules] Loaded rules: %s", list(self._rules.keys()))

    @property
    def input_schema(self) -> type[HealthCheckInput]:
        return HealthCheckInput

    # ── oc command runner ──

    def _run_oc(self, args: str) -> dict | str:
        cmd = [self._oc, *args.split()]
        env = None
        if self._kubeconfig:
            env = {**os.environ, "KUBECONFIG": self._kubeconfig}

        logger.info("[health-oc] command: %s %s", self._oc, args)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
            logger.info("[health-oc] exit_code=%d  stdout_len=%d  stderr_len=%d",
                        result.returncode, len(result.stdout), len(result.stderr))
            if result.returncode != 0:
                logger.warning("[health-oc] stderr: %s", result.stderr.strip()[:500])
                return f"ERROR: {result.stderr.strip()}"
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error("[health-oc] command timed out: %s", args)
            return "ERROR: command timed out"

    # ── Data summarizers ──

    def _summarize_pods(self, pods_json: dict | str) -> dict:
        if isinstance(pods_json, str):
            return {"error": pods_json}
        items = pods_json.get("items", [])
        summary = {
            "total": len(items), "running": 0, "pending": 0, "failed": 0,
            "crashloop": 0, "other": 0, "not_ready": [], "crashloop_pods": [],
        }
        for pod in items:
            phase = pod.get("status", {}).get("phase", "Unknown")
            name = pod.get("metadata", {}).get("name", "unknown")

            is_crashloop = self._has_crashloop(pod)
            if is_crashloop:
                summary["crashloop"] += 1
                summary["crashloop_pods"].append(name)
                summary["not_ready"].append(name)

            if phase == "Running" and not is_crashloop:
                summary["running"] += 1
            elif phase == "Pending":
                summary["pending"] += 1
                if name not in summary["not_ready"]:
                    summary["not_ready"].append(name)
            elif phase == "Failed":
                summary["failed"] += 1
                if name not in summary["not_ready"]:
                    summary["not_ready"].append(name)
            elif not is_crashloop:
                summary["other"] += 1
        return summary

    @staticmethod
    def _has_crashloop(pod: dict) -> bool:
        """Check if any container in the pod is in CrashLoopBackOff."""
        for container_list_key in ("containerStatuses", "initContainerStatuses"):
            for cs in pod.get("status", {}).get(container_list_key, []):
                waiting = cs.get("state", {}).get("waiting", {})
                if waiting.get("reason") == "CrashLoopBackOff":
                    return True
        return False

    def _summarize_pvcs(self, pvcs_json: dict | str) -> dict:
        if isinstance(pvcs_json, str):
            return {"error": pvcs_json}
        items = pvcs_json.get("items", [])
        summary = {"total": len(items), "bound": 0, "pending": 0, "lost": 0, "unbound": []}
        for pvc in items:
            phase = pvc.get("status", {}).get("phase", "Unknown")
            name = pvc.get("metadata", {}).get("name", "unknown")
            if phase == "Bound":
                summary["bound"] += 1
            elif phase == "Pending":
                summary["pending"] += 1
                summary["unbound"].append(name)
            elif phase == "Lost":
                summary["lost"] += 1
                summary["unbound"].append(name)
        return summary

    def _summarize_nodes(self, nodes_json: dict | str) -> dict:
        if isinstance(nodes_json, str):
            return {"error": nodes_json}
        items = nodes_json.get("items", [])
        summary = {"total": len(items), "ready": 0, "not_ready": []}
        for node in items:
            name = node.get("metadata", {}).get("name", "unknown")
            conditions = node.get("status", {}).get("conditions", [])
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            if ready:
                summary["ready"] += 1
            else:
                summary["not_ready"].append(name)
        return summary

    def _check_storagecluster(self, ns: str) -> dict:
        """Query the ODF StorageCluster CR and return its phase."""
        raw = self._run_oc(f"get storagecluster -n {ns} -o json")
        if isinstance(raw, str):
            return {"error": raw}
        items = raw.get("items", [raw]) if "items" in raw else [raw]
        clusters = []
        for sc in items:
            name = sc.get("metadata", {}).get("name", "unknown")
            phase = sc.get("status", {}).get("phase", "Unknown")
            conditions = sc.get("status", {}).get("conditions", [])
            error_conditions = [
                c for c in conditions
                if c.get("type") in ("Degraded", "Progressing")
                and c.get("status") == "True"
            ]
            clusters.append({
                "name": name,
                "phase": phase,
                "error_conditions": [
                    {"type": c["type"], "reason": c.get("reason", ""), "message": c.get("message", "")}
                    for c in error_conditions
                ],
            })
        has_error = any(c["phase"] in ("Error", "ERROR") for c in clusters)
        return {
            "clusters": clusters,
            "has_error": has_error,
        }

    def _check_ceph_crashes(self, ns: str) -> dict:
        """Query unarchived Ceph crashes via 'ceph crash ls-new'."""
        raw = self._run_oc(
            f"exec -n {ns} deploy/rook-ceph-tools -- ceph crash ls-new"
        )
        if isinstance(raw, str) and raw.startswith("ERROR"):
            return {"error": raw, "count": 0, "crashes": []}
        if isinstance(raw, dict):
            entries = raw if isinstance(raw, list) else []
            return {"count": len(entries), "crashes": entries}
        lines = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
        crash_ids = [
            ln for ln in lines
            if not ln.startswith("ID") and not ln.startswith("--")
        ]
        return {"count": len(crash_ids), "crashes": crash_ids[:10]}

    # ── Rules evaluation ──

    def _evaluate_rules(self, report: dict) -> tuple[str, list[str]]:
        """Evaluate health_rules.txt against the collected report.
        Returns (status, list_of_triggered_rules).
        """
        ceph_status = report.get("ceph", {}).get("status", "UNKNOWN")
        ceph_checks = report.get("ceph", {}).get("checks", [])
        nodes_not_ready = len(report.get("nodes", {}).get("not_ready", []))
        pods_failed = report.get("pods", {}).get("failed", 0)
        pods_pending = report.get("pods", {}).get("pending", 0)
        pods_crashloop = report.get("pods", {}).get("crashloop", 0)
        pvcs_pending = report.get("pvcs", {}).get("pending", 0)
        pvcs_lost = report.get("pvcs", {}).get("lost", 0)
        storagecluster_has_error = report.get("storagecluster", {}).get("has_error", False)
        ceph_crash_count = report.get("ceph_crashes", {}).get("count", 0)

        values = {
            "ceph_status": ceph_status,
            "nodes_not_ready": nodes_not_ready,
            "pods_failed": pods_failed,
            "pods_pending": pods_pending,
            "pods_crashloop": pods_crashloop,
            "pvcs_pending": pvcs_pending,
            "pvcs_lost": pvcs_lost,
            "storagecluster_error": int(storagecluster_has_error),
            "ceph_crash_count": ceph_crash_count,
        }

        triggered: list[str] = []

        critical_ceph = set(self._rules.get("critical_ceph_checks", []))
        if critical_ceph & set(ceph_checks):
            overlap = critical_ceph & set(ceph_checks)
            triggered.append(f"[critical] critical_ceph_checks matched: {overlap}")

        for level in ("critical", "degraded"):
            for rule_str in self._rules.get(level, []):
                if self._eval_condition(rule_str, values):
                    triggered.append(f"[{level}] {rule_str}")

        has_critical = any("[critical]" in t for t in triggered)
        has_degraded = any("[degraded]" in t for t in triggered)

        if has_critical:
            return "CRITICAL", triggered
        if has_degraded:
            return "DEGRADED", triggered
        return "HEALTHY", triggered

    @staticmethod
    def _eval_condition(condition: str, values: dict) -> bool:
        """Evaluate a simple condition like 'pods_failed >= 3' or 'ceph_status == HEALTH_ERR'."""
        match = re.match(r"(\w+)\s*(==|!=|>=|<=|>|<)\s*(.+)", condition)
        if not match:
            return False
        var, op, expected = match.group(1), match.group(2), match.group(3).strip()
        actual = values.get(var)
        if actual is None:
            return False

        if isinstance(actual, int) or expected.isdigit():
            try:
                actual_num = int(actual) if not isinstance(actual, int) else actual
                expected_num = int(expected)
                return {
                    "==": actual_num == expected_num,
                    "!=": actual_num != expected_num,
                    ">=": actual_num >= expected_num,
                    "<=": actual_num <= expected_num,
                    ">": actual_num > expected_num,
                    "<": actual_num < expected_num,
                }.get(op, False)
            except ValueError:
                pass

        actual_str = str(actual)
        if op == "==":
            return actual_str == expected
        if op == "!=":
            return actual_str != expected
        return False

    # ── Main run ──

    async def _run(
        self, input: HealthCheckInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        ns = input.namespace
        report: dict = {}

        logger.info("[health] Starting cluster health check for namespace=%s", ns)

        report["pods"] = self._summarize_pods(
            self._run_oc(f"get pods -n {ns} -o json")
        )
        logger.info("[health] Pods: %s", {k: v for k, v in report["pods"].items() if k not in ("not_ready", "crashloop_pods")})

        report["pvcs"] = self._summarize_pvcs(
            self._run_oc(f"get pvc -n {ns} -o json")
        )
        logger.info("[health] PVCs: %s", {k: v for k, v in report["pvcs"].items() if k != "unbound"})

        if input.include_ceph:
            ceph_raw = self._run_oc(
                f"exec -n {ns} deploy/rook-ceph-tools -- ceph status -f json"
            )
            if isinstance(ceph_raw, dict):
                health = ceph_raw.get("health", {})
                report["ceph"] = {
                    "status": health.get("status", "UNKNOWN"),
                    "checks": list(health.get("checks", {}).keys()),
                }
            else:
                report["ceph"] = {"raw": str(ceph_raw)[:500]}
            logger.info("[health] Ceph: %s", report["ceph"])

        if input.include_nodes:
            report["nodes"] = self._summarize_nodes(
                self._run_oc("get nodes -o json")
            )
            logger.info("[health] Nodes: ready=%d  not_ready=%s",
                        report["nodes"].get("ready", 0),
                        report["nodes"].get("not_ready", []))

        report["storagecluster"] = self._check_storagecluster(ns)
        logger.info("[health] StorageCluster: %s", report["storagecluster"])

        if input.include_ceph:
            report["ceph_crashes"] = self._check_ceph_crashes(ns)
            logger.info("[health] Ceph crashes (unarchived): count=%d  %s",
                        report["ceph_crashes"].get("count", 0),
                        report["ceph_crashes"].get("crashes", [])[:5])

        # Evaluate rules
        if self._rules:
            status, triggered_rules = self._evaluate_rules(report)
            report["overall_status"] = status
            report["overall_healthy"] = status == "HEALTHY"
            report["triggered_rules"] = triggered_rules
            if triggered_rules:
                logger.info("[health] Triggered rules:")
                for r in triggered_rules:
                    logger.info("[health]   %s", r)
        else:
            healthy = (
                report["pods"].get("failed", 0) == 0
                and report["pods"].get("pending", 0) == 0
                and report["pvcs"].get("pending", 0) == 0
                and report["pvcs"].get("lost", 0) == 0
                and report.get("ceph", {}).get("status") == "HEALTH_OK"
                and len(report.get("nodes", {}).get("not_ready", [])) == 0
            )
            report["overall_status"] = "HEALTHY" if healthy else "DEGRADED"
            report["overall_healthy"] = healthy

        logger.info("[health] Result: %s (healthy=%s)", report["overall_status"], report["overall_healthy"])

        return StringToolOutput(json.dumps(report, indent=2))

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "cluster_health"], creator=self)

    async def clone(self):
        tool = self.__class__(oc_path=self._oc, kubeconfig=self._kubeconfig)
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool
