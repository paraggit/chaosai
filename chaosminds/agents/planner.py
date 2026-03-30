from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from beeai_framework.agents.tool_calling.agent import ToolCallingAgent
from beeai_framework.agents.types import AgentMeta
from beeai_framework.backend.chat import ChatModel
from beeai_framework.tools.think import ThinkTool

from chaosminds.agents._prompts import system_prompt_template
from chaosminds.state import Phase, WorkflowState

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class PlannerAgent:
    """Interprets a user instruction + scenario plan and produces an ordered step plan."""

    def __init__(self, llm: ChatModel, scenario_plan: dict | list) -> None:
        system_prompt = (PROMPTS_DIR / "planner_system.txt").read_text()

        self.agent = ToolCallingAgent(
            llm=llm,
            tools=[ThinkTool()],
            meta=AgentMeta(
                name="PlannerAgent",
                description="Plans the chaos engineering workflow",
                tools=[ThinkTool()],
            ),
            templates={
                "system": system_prompt_template(system_prompt),
            },
        )
        self.scenario_plan = scenario_plan

    async def plan(self, state: WorkflowState) -> WorkflowState:
        scenario_names = []
        if isinstance(self.scenario_plan, dict):
            for key, val in self.scenario_plan.items():
                if isinstance(val, dict) and "name" in val:
                    scenario_names.append(val["name"])

        prompt = (
            f"User instruction: {state.instruction}\n\n"
            f"Available krknctl chaos scenario names: {scenario_names}\n\n"
            f"Full scenario catalog:\n{json.dumps(self.scenario_plan, indent=2)}\n\n"
            "Produce the JSON step plan. Output ONLY the JSON array, nothing else."
        )

        logger.info("[PlannerAgent] Sending prompt to LLM (%d chars)", len(prompt))
        logger.debug("[PlannerAgent] Prompt:\n%s", prompt)

        output = await self.agent.run(prompt)
        raw_text = output.last_message.text

        logger.info("[PlannerAgent] Received LLM response (%d chars)", len(raw_text))
        logger.info("[PlannerAgent] Raw LLM response:\n%s", raw_text[:3000])

        state.plan_steps = self._parse_plan(raw_text)
        state.phase = Phase.EXECUTING
        logger.info("[PlannerAgent] Parsed %d steps from plan", len(state.plan_steps))
        if state.plan_steps:
            logger.info("[PlannerAgent] Plan:\n%s", json.dumps(state.plan_steps, indent=2))
        return state

    @staticmethod
    def _parse_plan(raw_text: str) -> list[dict]:
        """Extract a JSON array from the LLM response, tolerating various wrappers."""
        text = raw_text.strip()

        if "```" in text:
            fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text)
            for block in fenced:
                block = block.strip()
                if block.startswith("["):
                    text = block
                    break

        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError as e:
                logger.warning("[PlannerAgent] JSON parse failed: %s", e)
                cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
                try:
                    parsed = json.loads(cleaned)
                    if isinstance(parsed, list):
                        return parsed
                except json.JSONDecodeError:
                    pass

        logger.warning("[PlannerAgent] Failed to parse plan. Raw response:\n%s", raw_text[:2000])
        return []
