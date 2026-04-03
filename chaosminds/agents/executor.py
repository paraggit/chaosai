from __future__ import annotations

import copy
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from beeai_framework.agents.tool_calling.agent import ToolCallingAgent
from beeai_framework.agents.types import AgentMeta
from beeai_framework.backend.chat import ChatModel

from chaosminds.agents._prompts import system_prompt_template
from chaosminds.iteration_placeholders import expand_iteration_placeholders
from chaosminds.state import WorkflowState
from chaosminds.tools.bob_cli_tool import BobCliTool
from chaosminds.tools.kubectl_tool import OcTool
from chaosminds.tools.oc_validation import OcValidationTool

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class ExecutorAgent:
    """Runs ODF storage operations via BOB CLI and oc.

    Has access to OcValidationTool so it can self-check
    commands (especially wait conditions) before executing.
    """

    def __init__(
        self,
        llm: ChatModel,
        bob_tool: BobCliTool,
        oc_tool: OcTool,
        rag_tools: Sequence[Any] | None = None,
        *,
        max_iterations: int = 25,
    ) -> None:
        system_prompt = (PROMPTS_DIR / "executor_system.txt").read_text()
        tools: list[Any] = [bob_tool, oc_tool, OcValidationTool()]
        if rag_tools:
            tools.extend(rag_tools)

        self._max_iterations = max(1, max_iterations)
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
        step_run = expand_iteration_placeholders(
            copy.deepcopy(step),
            idx=1,
        )
        prompt = (
            f"Execute this step:\n{json.dumps(step_run, indent=2)}\n\n"
            f"Current cluster health:\n{json.dumps(state.cluster_health, indent=2)}\n\n"
            "Use the appropriate tool (bob_cli or oc) based on the step's 'tool' field. "
            "Report the result."
        )

        logger.info("[ExecutorAgent] Step %s: %s", step.get("id"), step.get("action"))
        logger.info("[ExecutorAgent] Sending prompt to LLM (%d chars)", len(prompt))
        logger.debug("[ExecutorAgent] Prompt:\n%s", prompt)

        try:
            output = await self.agent.run(
                prompt,
                max_iterations=self._max_iterations,
            )
            result_text = output.last_message.text

            logger.info(
                "[ExecutorAgent] LLM response (%d chars):\n%s",
                len(result_text),
                result_text[:2000],
            )
            logger.debug("[ExecutorAgent] LLM response (full):\n%s", result_text)

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
