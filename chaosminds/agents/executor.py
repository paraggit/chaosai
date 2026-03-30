from __future__ import annotations

import json
import logging
from pathlib import Path

from beeai_framework.agents.tool_calling.agent import ToolCallingAgent
from beeai_framework.agents.types import AgentMeta
from beeai_framework.backend.chat import ChatModel

from chaosminds.agents._prompts import system_prompt_template
from chaosminds.state import WorkflowState
from chaosminds.tools.bob_cli_tool import BobCliTool
from chaosminds.tools.kubectl_tool import OcTool

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class ExecutorAgent:
    """Runs ODF storage operations via BOB CLI and oc."""

    def __init__(self, llm: ChatModel, bob_tool: BobCliTool, oc_tool: OcTool) -> None:
        system_prompt = (PROMPTS_DIR / "executor_system.txt").read_text()
        tools = [bob_tool, oc_tool]

        self.agent = ToolCallingAgent(
            llm=llm,
            tools=tools,
            meta=AgentMeta(
                name="ExecutorAgent",
                description="Executes storage operations on the cluster",
                tools=tools,
            ),
            templates={
                "system": system_prompt_template(system_prompt),
            },
        )

    async def execute(self, step: dict, state: WorkflowState) -> WorkflowState:
        prompt = (
            f"Execute this step:\n{json.dumps(step, indent=2)}\n\n"
            f"Current cluster health:\n{json.dumps(state.cluster_health, indent=2)}\n\n"
            "Use the appropriate tool (bob_cli or oc) based on the step's 'tool' field. "
            "Report the result."
        )

        logger.info("[ExecutorAgent] Step %s: %s", step.get("id"), step.get("action"))
        logger.info("[ExecutorAgent] Sending prompt to LLM (%d chars)", len(prompt))
        logger.debug("[ExecutorAgent] Prompt:\n%s", prompt)

        try:
            output = await self.agent.run(prompt)
            result_text = output.last_message.text

            logger.info("[ExecutorAgent] LLM response (%d chars):\n%s", len(result_text), result_text[:2000])

            state.log_step(
                step_id=step["id"],
                tool=step["tool"],
                action=step["action"],
                status="success",
                output=result_text,
            )
        except Exception as e:
            logger.error("[ExecutorAgent] Step %s failed: %s", step["id"], e, exc_info=True)
            state.log_step(
                step_id=step["id"],
                tool=step["tool"],
                action=step["action"],
                status="failed",
                error=str(e),
            )
        return state
