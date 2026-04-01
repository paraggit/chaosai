from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools.tool import Tool, ToolRunOptions
from beeai_framework.tools.types import StringToolOutput

logger = logging.getLogger(__name__)


class KrknctlInput(BaseModel):
    scenario_file: str = Field(
        "",
        description=(
            "Path to a scenario JSON file on disk. If empty, "
            "'scenario_config' will be written to a temp file automatically."
        ),
    )
    scenario_config: dict = Field(
        default_factory=dict,
        description=(
            "Scenario configuration dict. When 'scenario_file' is empty this "
            "is written to a temporary JSON file and passed to krknctl."
        ),
    )
    max_parallel: int = Field(
        default=2,
        description="Maximum number of parallel chaos workers (--max-parallel).",
    )
    extra_args: list[str] = Field(
        default_factory=list,
        description="Any additional CLI flags to append to the krknctl command.",
    )


def build_krknctl_graph(scenario_config: dict) -> dict:
    """Wrap a single scenario node into the graph structure
    that krknctl random run expects.

    krknctl needs: root node (dummy-scenario) + scenario node
    with depends_on pointing to the root key."""
    root_key = "root_chaos"
    name = scenario_config.get("name", "scenario")
    scenario_key = f"{name}_chaos"

    graph = {
        root_key: {
            "image": (
                "quay.io/krkn-chaos/krkn-hub:dummy-scenario"
            ),
            "name": "dummy-scenario",
            "env": {"END": "10", "EXIT_STATUS": "0"},
        },
        scenario_key: {
            **scenario_config,
            "depends_on": root_key,
        },
    }
    return graph


class KrknctlTool(Tool[KrknctlInput, ToolRunOptions, StringToolOutput]):
    """
    Wraps the real krknctl CLI:
        krknctl random run <scenario.json> --max-parallel=N --kubeconfig PATH
    """

    name = "krknctl_chaos_inject"
    description = (
        "Injects chaos scenarios into the OpenShift cluster "
        "using krknctl. "
        "Invocation: krknctl random run <scenario.json> "
        "--max-parallel=N --kubeconfig PATH. "
        "Supports pod-kill, node-drain, network-partition "
        "and other Kraken scenarios."
    )

    def __init__(
        self, binary_path: str = "krknctl",
        kubeconfig: str = "",
    ) -> None:
        super().__init__()
        self._binary_path = binary_path
        self._kubeconfig = kubeconfig

    @property
    def input_schema(self) -> type[KrknctlInput]:
        return KrknctlInput

    async def _run(
        self, input: KrknctlInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        tmp_file = None
        try:
            if input.scenario_file:
                scenario_path = input.scenario_file
            elif input.scenario_config:
                graph = build_krknctl_graph(
                    input.scenario_config,
                )
                tmp_file = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json",
                    delete=False,
                    prefix="krknctl_scenario_",
                )
                json.dump(graph, tmp_file, indent=2)
                tmp_file.close()
                scenario_path = tmp_file.name
            else:
                logger.warning(
                    "[krknctl] No scenario_file or "
                    "scenario_config provided",
                )
                return StringToolOutput(
                    "[error] No scenario_file or "
                    "scenario_config provided",
                )

            cmd = [
                self._binary_path, "random", "run",
                scenario_path,
                f"--max-parallel={input.max_parallel}",
            ]
            if self._kubeconfig:
                cmd.append(f"--kubeconfig={self._kubeconfig}")
            cmd.extend(input.extra_args)

            logger.info("[krknctl] command: %s", " ".join(cmd))
            if input.scenario_config:
                logger.info("[krknctl] scenario_config:\n%s", json.dumps(input.scenario_config, indent=2))

            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=900,
            )

            output_parts = []
            if result.stdout:
                output_parts.append(result.stdout)
            if result.stderr:
                output_parts.append(f"[stderr] {result.stderr}")
            if result.returncode != 0:
                output_parts.append(f"[exit_code={result.returncode}]")

            combined = "\n".join(output_parts) or "(no output)"

            logger.info("[krknctl] exit_code=%d", result.returncode)
            logger.info("[krknctl] stdout:\n%s", result.stdout[:3000] if result.stdout else "(empty)")
            if result.stderr:
                logger.info("[krknctl] stderr:\n%s", result.stderr[:2000])

            return StringToolOutput(combined)
        finally:
            if tmp_file:
                os.unlink(tmp_file.name)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "krknctl"], creator=self)

    async def clone(self):
        tool = self.__class__(binary_path=self._binary_path, kubeconfig=self._kubeconfig)
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool


class KrknctlListInput(BaseModel):
    subcommand: str = Field(
        default="running",
        description="Subcommand for 'krknctl list': 'running' or 'available'.",
    )
    extra_args: list[str] = Field(
        default_factory=list,
        description="Additional flags for 'krknctl list <subcommand>'.",
    )


class KrknctlListTool(Tool[KrknctlListInput, ToolRunOptions, StringToolOutput]):
    """Lists running / available krknctl scenario containers."""

    name = "krknctl_list"
    description = (
        "Lists krknctl chaos scenarios. "
        "Use subcommand='running' to check active/completed runs, "
        "or subcommand='available' to list installable scenarios."
    )

    def __init__(self, binary_path: str = "krknctl") -> None:
        super().__init__()
        self._binary_path = binary_path

    @property
    def input_schema(self) -> type[KrknctlListInput]:
        return KrknctlListInput

    async def _run(
        self, input: KrknctlListInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        sub = input.subcommand if input.subcommand in ("running", "available") else "running"
        cmd = [self._binary_path, "list", sub, *input.extra_args]

        logger.info("[krknctl-list] command: %s", " ".join(cmd))

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"[stderr] {result.stderr}")

        combined = "\n".join(output_parts) or "(no output)"

        logger.info("[krknctl-list] output:\n%s", combined[:2000])

        return StringToolOutput(combined)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "krknctl_list"], creator=self)

    async def clone(self):
        tool = self.__class__(binary_path=self._binary_path)
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool
