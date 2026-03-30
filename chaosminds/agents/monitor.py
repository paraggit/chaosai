from __future__ import annotations

import json
import logging
from pathlib import Path

from beeai_framework.agents.tool_calling.agent import ToolCallingAgent
from beeai_framework.agents.types import AgentMeta
from beeai_framework.backend.chat import ChatModel

from chaosminds.agents._prompts import system_prompt_template
from chaosminds.state import WorkflowState
from chaosminds.tools.cluster_health import ClusterHealthTool

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class ClusterMonitorAgent:
    """Monitors cluster health at key workflow checkpoints."""

    def __init__(self, llm: ChatModel, health_tool: ClusterHealthTool) -> None:
        system_prompt = (PROMPTS_DIR / "monitor_system.txt").read_text()
        tools = [health_tool]

        self.agent = ToolCallingAgent(
            llm=llm,
            tools=tools,
            meta=AgentMeta(
                name="ClusterMonitorAgent",
                description="Monitors ODF cluster health during chaos experiments",
                tools=tools,
            ),
            templates={
                "system": system_prompt_template(system_prompt),
            },
        )
        self.health_tool = health_tool

    async def monitor(self, state: WorkflowState) -> WorkflowState:
        recent_logs = [
            {"step_id": r.step_id, "tool": r.tool, "status": r.status}
            for r in state.execution_log[-5:]
        ]

        prompt = (
            f"Current workflow phase: {state.phase.name}\n\n"
            f"Recent execution log:\n{json.dumps(recent_logs, indent=2)}\n\n"
            "Run a cluster health check on the openshift-storage namespace. "
            "Report the overall status, pod health, PVC status, Ceph status, and node readiness."
        )

        logger.info("[MonitorAgent] Phase=%s, sending health check prompt", state.phase.name)
        logger.debug("[MonitorAgent] Prompt:\n%s", prompt)

        try:
            output = await self.agent.run(prompt)
            raw = output.last_message.text

            logger.info("[MonitorAgent] LLM response (%d chars):\n%s", len(raw), raw[:2000])

            try:
                health_data = json.loads(raw)
            except json.JSONDecodeError:
                health_data = {"raw_report": raw, "overall_healthy": None}

            state.cluster_health = health_data
            state.snapshot_health()
            logger.info("[MonitorAgent] Health: overall_healthy=%s", health_data.get("overall_healthy", "unknown"))
        except Exception as e:
            logger.error("[MonitorAgent] Failed: %s", e, exc_info=True)
            state.cluster_health = {"error": str(e), "overall_healthy": False}
            state.snapshot_health()

        return state
