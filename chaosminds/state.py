from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto


class Phase(Enum):
    PLANNING = auto()
    EXECUTING = auto()
    CHAOS_INJECTING = auto()
    WAITING = auto()
    MONITORING = auto()
    COMPLETED = auto()
    FAILED = auto()


@dataclass
class StepResult:
    step_id: int
    tool: str
    action: str
    status: str  # "success" | "failed" | "skipped"
    output: str = ""
    error: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class WorkflowState:
    instruction: str = ""
    phase: Phase = Phase.PLANNING

    plan_steps: list[dict] = field(default_factory=list)
    chaos_scenarios: list[dict] = field(default_factory=list)

    execution_log: list[StepResult] = field(default_factory=list)
    cluster_health: dict = field(default_factory=dict)
    health_timeline: list[dict] = field(default_factory=list)

    errors: list[str] = field(default_factory=list)
    final_report: str = ""

    def log_step(self, step_id: int, tool: str, action: str, status: str, output: str = "", error: str = ""):
        result = StepResult(
            step_id=step_id, tool=tool, action=action,
            status=status, output=output, error=error,
        )
        self.execution_log.append(result)
        if error:
            self.errors.append(f"Step {step_id} ({tool}): {error}")

    def snapshot_health(self):
        if self.cluster_health:
            self.health_timeline.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "phase": self.phase.name,
                "health": dict(self.cluster_health),
            })

    def summary(self) -> str:
        lines = [
            "=" * 60,
            "ChaosMinds Workflow Report",
            "=" * 60,
            f"Instruction : {self.instruction}",
            f"Final Phase : {self.phase.name}",
            f"Total Steps : {len(self.plan_steps)}",
            f"Executed    : {len(self.execution_log)}",
            f"Errors      : {len(self.errors)}",
            "-" * 60,
        ]
        for r in self.execution_log:
            marker = "PASS" if r.status == "success" else "FAIL" if r.status == "failed" else "SKIP"
            lines.append(f"  [{marker}] Step {r.step_id}: {r.action}")
            if r.error:
                lines.append(f"         Error: {r.error}")

        if self.errors:
            lines.append("-" * 60)
            lines.append("Errors:")
            for e in self.errors:
                lines.append(f"  - {e}")

        lines.append("=" * 60)
        return "\n".join(lines)
