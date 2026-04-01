from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from beeai_framework.agents.tool_calling.agent import ToolCallingAgent
from beeai_framework.agents.types import AgentMeta
from beeai_framework.backend.chat import ChatModel

from chaosminds.agents._prompts import system_prompt_template
from chaosminds.state import Phase, WorkflowState
from chaosminds.tools.krknctl_tool import KrknctlListTool

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class WaitAgent:
    """Polls 'krknctl list' until all chaos containers finish or timeout."""

    def __init__(
        self,
        llm: ChatModel,
        list_tool: KrknctlListTool,
        timeout: int = 600,
        poll_interval: int = 15,
    ) -> None:
        system_prompt = (PROMPTS_DIR / "waiter_system.txt").read_text()
        tools = [list_tool]

        self.agent = ToolCallingAgent(
            llm=llm,
            tools=tools,
            meta=AgentMeta(
                name="WaitAgent",
                description="Polls chaos scenario completion status",
                tools=tools,
            ),
            templates={
                "system": system_prompt_template(system_prompt),
            },
        )
        self.list_tool = list_tool
        self.timeout = timeout
        self.poll_interval = poll_interval

    async def wait_for_completion(
        self, step: dict, state: WorkflowState,
    ) -> WorkflowState:
        step_id = step.get("id", -1)
        elapsed = 0
        poll = self.poll_interval
        max_poll = 60
        prev_running: bool | None = None

        logger.info(
            "[WaitAgent] Waiting for chaos completion "
            "(timeout=%ds, poll=%ds→%ds cap)",
            self.timeout, self.poll_interval, max_poll,
        )

        while elapsed < self.timeout:
            try:
                result = await self.list_tool.run({})
                text = result.get_text_content()

                logger.info(
                    "[WaitAgent] Poll at %ds — krknctl list output:\n%s",
                    elapsed, text[:1000],
                )

                still_running = self._has_running_scenarios(text)
                if prev_running is not None and still_running != prev_running:
                    poll = self.poll_interval
                prev_running = still_running

                if not still_running:
                    logger.info(
                        "[WaitAgent] All chaos scenarios completed after %ds",
                        elapsed,
                    )
                    state.log_step(
                        step_id=step_id,
                        tool="wait",
                        action="wait for chaos completion",
                        status="success",
                        output=(
                            f"All chaos scenarios completed after {elapsed}s\n{text}"
                        ),
                    )
                    return state

                lower = text.lower()
                if "error" in lower and "running" not in lower:
                    logger.error(
                        "[WaitAgent] Chaos scenario error detected:\n%s",
                        text,
                    )
                    state.log_step(
                        step_id=step_id,
                        tool="wait",
                        action="wait for chaos completion",
                        status="failed",
                        error=f"Chaos scenario error detected:\n{text}",
                    )
                    state.phase = Phase.FAILED
                    return state

            except Exception as e:
                logger.warning("[WaitAgent] Error polling krknctl list: %s", e)

            await asyncio.sleep(poll)
            elapsed += poll
            poll = min(poll * 2, max_poll)

        logger.error("[WaitAgent] Timed out after %ds", self.timeout)
        state.log_step(
            step_id=step_id,
            tool="wait",
            action="wait for chaos completion",
            status="failed",
            error=f"Chaos scenarios timed out after {self.timeout}s",
        )
        state.phase = Phase.FAILED
        return state

    _KRKNCTL_CONTAINER_RE = re.compile(
        r"\bkrknctl-[a-zA-Z0-9_.-]+\b",
    )

    @classmethod
    def _has_running_scenarios(cls, list_output: str) -> bool:
        """True if krknctl list output names at least one krknctl-* container."""
        lower = list_output.lower().strip()
        if not lower:
            return False

        no_running_phrases = (
            "no scenarios are currently running",
            "no running scenarios",
            "no release found",
        )
        for phrase in no_running_phrases:
            if phrase in lower:
                return False

        return bool(cls._KRKNCTL_CONTAINER_RE.search(list_output))
