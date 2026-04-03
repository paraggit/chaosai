from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from beeai_framework.agents.tool_calling.agent import ToolCallingAgent
from beeai_framework.agents.types import AgentMeta
from beeai_framework.backend.chat import ChatModel

from chaosminds.agents._prompts import system_prompt_template
from chaosminds.state import Phase, WorkflowState
from chaosminds.tools.cluster_discovery import ClusterDiscoveryTool
from chaosminds.tools.krknctl_tool import KrknctlTool, run_krknctl_from_scenario_config

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_CHAOS_HEALTH_PROMPT_MAX = 12000


def _health_json_for_prompt(health: object) -> str:
    """Bound prompt size — huge cluster_health JSON can break the chat backend."""
    raw = json.dumps(health, indent=2) if health is not None else "{}"
    if len(raw) <= _CHAOS_HEALTH_PROMPT_MAX:
        return raw
    return raw[:_CHAOS_HEALTH_PROMPT_MAX] + "\n... [truncated for prompt size]"


class ChaosAgent:
    """Injects chaos scenarios using krknctl.

    Has access to ClusterDiscoveryTool so it can inspect the
    cluster topology (daemon replica counts, etc.) before
    deciding chaos parameters.
    """

    def __init__(
        self,
        llm: ChatModel,
        krknctl_tool: KrknctlTool,
        discovery_tool: ClusterDiscoveryTool | None = None,
        rag_tools: Sequence[Any] | None = None,
        chaos_max_parallel: int = 4,
    ) -> None:
        system_prompt = (PROMPTS_DIR / "chaos_system.txt").read_text()
        self._krknctl = krknctl_tool
        self._chaos_max_parallel = chaos_max_parallel
        tools: list[Any] = []
        tools.append(krknctl_tool)
        if discovery_tool:
            tools.append(discovery_tool)
        if rag_tools:
            tools.extend(rag_tools)

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
        """Run krknctl for ``scenario_config`` without an LLM round-trip.

        The ToolCallingAgent can invoke krknctl successfully, but the follow-up
        LLM turn (final assistant message) may **timeout** after long krkn runs
        (e.g. 600s Ollama limit) even though chaos already finished — so we
        inject directly whenever the plan includes ``scenario_config``.
        """
        params = step.get("params") or {}
        scenario_config = params.get("scenario_config")
        try:
            mp = int(params.get("max_parallel", self._chaos_max_parallel))
        except (TypeError, ValueError):
            mp = self._chaos_max_parallel

        logger.info("[ChaosAgent] Step %s: %s", step.get("id"), step.get("action"))

        if isinstance(scenario_config, dict) and scenario_config:
            logger.info(
                "[ChaosAgent] Direct krknctl (no LLM) max_parallel=%s — "
                "avoids post-chaos chat timeout",
                mp,
            )
            rc, out = run_krknctl_from_scenario_config(
                self._krknctl.binary_path,
                self._krknctl.kubeconfig,
                scenario_config,
                max_parallel=mp,
            )
            output = f"exit_code={rc}\n{out}"
            if rc == 0:
                state.log_step(
                    step_id=step["id"],
                    tool="krknctl",
                    action=step["action"],
                    status="success",
                    output=output,
                )
                state.phase = Phase.WAITING
            else:
                state.log_step(
                    step_id=step["id"],
                    tool="krknctl",
                    action=step["action"],
                    status="failed",
                    error=output,
                )
                state.phase = Phase.FAILED
            return state

        health_blob = _health_json_for_prompt(state.cluster_health)
        prompt = (
            f"Execute this chaos injection step:\n{json.dumps(step, indent=2)}\n\n"
            f"Current cluster health:\n{health_blob}\n\n"
            "Use the krknctl_chaos_inject tool with the parameters from the step. "
            "Report the run ID and injection status."
        )

        logger.info("[ChaosAgent] No scenario_config — LLM path (%d chars)", len(prompt))
        logger.debug("[ChaosAgent] Prompt:\n%s", prompt)

        try:
            output = await self.agent.run(prompt)
            result_text = output.last_message.text

            logger.info(
                "[ChaosAgent] LLM response (%d chars):\n%s",
                len(result_text),
                result_text[:2000],
            )
            logger.debug("[ChaosAgent] LLM response (full):\n%s", result_text)

            state.log_step(
                step_id=step["id"],
                tool="krknctl",
                action=step["action"],
                status="success",
                output=result_text,
            )
            state.phase = Phase.WAITING
        except Exception as e:
            logger.error("[ChaosAgent] Step %s LLM failed: %s", step["id"], e, exc_info=True)
            state.log_step(
                step_id=step["id"],
                tool="krknctl",
                action=step["action"],
                status="failed",
                error=str(e),
            )
            state.phase = Phase.FAILED
        return state
