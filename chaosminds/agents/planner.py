from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from beeai_framework.agents.tool_calling.agent import ToolCallingAgent
from beeai_framework.agents.types import AgentMeta
from beeai_framework.backend.chat import ChatModel
from beeai_framework.tools.think import ThinkTool

from chaosminds.agents._prompts import system_prompt_template
from chaosminds.chaos_plan import normalize_chaos_scenarios
from chaosminds.logging_utils import log_plan
from chaosminds.state import Phase, WorkflowState

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class PlannerAgent:
    """Interprets a user instruction + scenario plan
    and produces an ordered step plan."""

    def __init__(
        self,
        llm: ChatModel,
        scenario_plan: dict | list,
        rag_tools: Sequence[Any] | None = None,
    ) -> None:
        system_prompt = (PROMPTS_DIR / "planner_system.txt").read_text()

        tools: list[Any] = [ThinkTool()]
        if rag_tools:
            tools.extend(rag_tools)

        self.agent = ToolCallingAgent(
            llm=llm,
            tools=tools,
            meta=AgentMeta(
                name="PlannerAgent",
                description="Plans the chaos engineering workflow",
                tools=tools,
            ),
            templates={
                "system": system_prompt_template(system_prompt),
            },
        )
        self.scenario_plan = scenario_plan

    _MAX_RETRIES = 3
    _MIN_RESPONSE_LEN = 50

    async def plan(
        self, state: WorkflowState,
    ) -> WorkflowState:
        scenario_names = []
        if isinstance(self.scenario_plan, dict):
            for key, val in self.scenario_plan.items():
                if isinstance(val, dict) and "name" in val:
                    scenario_names.append(val["name"])

        prompt = (
            f"User instruction: {state.instruction}\n\n"
            f"Available krknctl chaos scenario names: "
            f"{scenario_names}\n\n"
            f"Full scenario catalog:\n"
            f"{json.dumps(self.scenario_plan, indent=2)}"
            f"\n\n"
            "Produce the JSON plan object. "
            "Output ONLY the JSON object, nothing else."
        )

        logger.info(
            "[PlannerAgent] Sending prompt to LLM "
            "(%d chars)", len(prompt),
        )
        logger.debug(
            "[PlannerAgent] Prompt:\n%s", prompt,
        )

        plan: dict = {}

        for attempt in range(1, self._MAX_RETRIES + 1):
            logger.info(
                "[PlannerAgent] Attempt %d/%d",
                attempt, self._MAX_RETRIES,
            )

            output = await self.agent.run(prompt)
            raw_text = output.last_message.text

            logger.info(
                "[PlannerAgent] Received LLM response "
                "(%d chars)", len(raw_text),
            )
            logger.info(
                "[PlannerAgent] Raw LLM response:\n%s",
                raw_text[:3000],
            )

            if len(raw_text) < self._MIN_RESPONSE_LEN:
                logger.warning(
                    "[PlannerAgent] Response too short "
                    "(%d chars), retrying...",
                    len(raw_text),
                )
                continue

            plan = self._parse_structured_plan(
                raw_text,
            )
            if plan:
                break

            logger.warning(
                "[PlannerAgent] Parse returned empty, "
                "retrying...",
            )

        state.structured_plan = plan
        state.plan_steps = self._flatten_plan(plan)
        state.phase = Phase.EXECUTING
        log_plan(logger, "[PlannerAgent] Plan", plan, state.plan_steps)
        return state

    @staticmethod
    def _flatten_plan(plan: dict) -> list[dict]:
        """Convert a 5-phase structured plan into a flat step list
        with sequential ids and depends_on for the Supervisor."""
        steps: list[dict] = []
        step_id = 1
        prev_id: int | None = None

        phase_order = ["setup", "chaos", "test_ops", "post"]

        for phase_name in phase_order:
            phase_data = plan.get(phase_name)
            if not phase_data:
                continue

            if phase_name == "chaos":
                if not isinstance(phase_data, dict):
                    continue
                for sc in normalize_chaos_scenarios(phase_data):
                    step = {
                        "id": step_id,
                        "phase": phase_name,
                        "tool": "krknctl",
                        "action": (
                            f"Inject chaos: {sc.get('name', 'scenario')}"
                        ),
                        "params": {"scenario_config": sc},
                        "depends_on": [prev_id] if prev_id else [],
                    }
                    steps.append(step)
                    prev_id = step_id
                    step_id += 1
                continue

            if not isinstance(phase_data, list):
                continue

            for raw_step in phase_data:
                tool = raw_step.get("tool", "oc")
                action = raw_step.get("action", "")
                params = raw_step.get("params", {})

                step = {
                    "id": step_id,
                    "phase": phase_name,
                    "tool": tool,
                    "action": action,
                    "params": params,
                    "depends_on": [prev_id] if prev_id else [],
                }
                steps.append(step)
                prev_id = step_id
                step_id += 1

        return steps

    @staticmethod
    def _parse_structured_plan(raw_text: str) -> dict:
        """Parse a 5-phase structured plan (JSON object)
        from LLM output."""
        text = raw_text.strip()

        if "```" in text:
            blocks = re.findall(
                r"```(?:json)?\s*([\s\S]*?)```", text,
            )
            for block in blocks:
                block = block.strip()
                if block.startswith("{"):
                    text = block
                    break

        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            logger.warning(
                "[PlannerAgent] No JSON object found",
            )
            return {}

        candidate = text[start:end + 1]

        phase_keys = ("setup", "chaos", "test_ops", "post")

        for attempt_text in (
            candidate,
            PlannerAgent._repair_json(candidate),
        ):
            try:
                parsed = json.loads(attempt_text)
                if not isinstance(parsed, dict):
                    continue
                if any(k in parsed for k in phase_keys):
                    return parsed
                # Unwrap common LLM wrappers like
                # {"PHASES": {...}} or {"plan": {...}}
                for wrapper in parsed.values():
                    if (
                        isinstance(wrapper, dict)
                        and any(
                            k in wrapper for k in phase_keys
                        )
                    ):
                        logger.info(
                            "[PlannerAgent] Unwrapped "
                            "nested plan object",
                        )
                        return wrapper
            except json.JSONDecodeError:
                continue

        logger.warning(
            "[PlannerAgent] Failed to parse "
            "structured plan:\n%s",
            raw_text[:2000],
        )
        return {}

    @staticmethod
    def _repair_json(text: str) -> str:
        """Best-effort fix for common LLM JSON mistakes."""
        text = re.sub(r",\s*([}\]])", r"\1", text)
        text = re.sub(
            r"([}\]])\s*\n(\s*\")", r"\1,\n\2", text,
        )
        text = re.sub(
            r'"\s*\n(\s*")', r'",\n\1', text,
        )
        return text
