from __future__ import annotations

import json
import logging
from pathlib import Path

from beeai_framework.agents.tool_calling.agent import ToolCallingAgent
from beeai_framework.agents.types import AgentMeta
from beeai_framework.backend.chat import ChatModel

from chaosminds.agents._prompts import system_prompt_template
from chaosminds.state import Phase, WorkflowState
from chaosminds.tools.krknctl_tool import KrknctlTool

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class ChaosAgent:
    """Injects chaos scenarios using krknctl."""

    def __init__(self, llm: ChatModel, krknctl_tool: KrknctlTool) -> None:
        system_prompt = (PROMPTS_DIR / "chaos_system.txt").read_text()
        tools = [krknctl_tool]

        self.agent = ToolCallingAgent(
            llm=llm,
            tools=tools,
            meta=AgentMeta(
                name="ChaosAgent",
                description="Injects chaos scenarios into the cluster",
                tools=tools,
            ),
            templates={
                "system": system_prompt_template(system_prompt),
            },
        )

    async def inject(self, step: dict, state: WorkflowState) -> WorkflowState:
        prompt = (
            f"Execute this chaos injection step:\n{json.dumps(step, indent=2)}\n\n"
            f"Current cluster health:\n{json.dumps(state.cluster_health, indent=2)}\n\n"
            "Use the krknctl_chaos_inject tool with the parameters from the step. "
            "Report the run ID and injection status."
        )

        logger.info("[ChaosAgent] Step %s: %s", step.get("id"), step.get("action"))
        logger.info("[ChaosAgent] Sending prompt to LLM (%d chars)", len(prompt))
        logger.debug("[ChaosAgent] Prompt:\n%s", prompt)

        try:
            output = await self.agent.run(prompt)
            result_text = output.last_message.text

            logger.info("[ChaosAgent] LLM response (%d chars):\n%s", len(result_text), result_text[:2000])

            state.log_step(
                step_id=step["id"],
                tool="krknctl",
                action=step["action"],
                status="success",
                output=result_text,
            )
            state.phase = Phase.WAITING
        except Exception as e:
            logger.error("[ChaosAgent] Step %s failed: %s", step["id"], e, exc_info=True)
            state.log_step(
                step_id=step["id"],
                tool="krknctl",
                action=step["action"],
                status="failed",
                error=str(e),
            )
            state.phase = Phase.FAILED
        return state
