from __future__ import annotations

import logging
import os
import subprocess

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools.tool import Tool, ToolRunOptions
from beeai_framework.tools.types import StringToolOutput

logger = logging.getLogger(__name__)


class BobCliInput(BaseModel):
    command: str = Field(
        ...,
        description=(
            "The BOB CLI sub-command to execute, e.g. "
            "'run --test tests/test_pvc_snapshot.py'. "
            "Do NOT include the binary name itself."
        ),
    )
    extra_env: dict[str, str] = Field(
        default_factory=dict,
        description="Optional extra environment variables to pass to the process.",
    )


class BobCliTool(Tool[BobCliInput, ToolRunOptions, StringToolOutput]):
    name = "bob_cli"
    description = (
        "Executes ODF/OCS storage test commands through the IBM BOB CLI. "
        "Use this to run OCS-CI tests, create PVCs, snapshots, and other "
        "storage operations on the cluster."
    )

    def __init__(self, binary_path: str = "bob", kubeconfig: str = "") -> None:
        super().__init__()
        self._binary_path = binary_path
        self._kubeconfig = kubeconfig

    @property
    def input_schema(self) -> type[BobCliInput]:
        return BobCliInput

    async def _run(
        self, input: BobCliInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        cmd = [self._binary_path, *input.command.split()]
        env = {**os.environ, **input.extra_env}
        if self._kubeconfig:
            env["KUBECONFIG"] = self._kubeconfig

        logger.info("[bob] command: %s %s", self._binary_path, input.command)
        if input.extra_env:
            logger.info("[bob] extra_env: %s", input.extra_env)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)

        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"[stderr] {result.stderr}")
        if result.returncode != 0:
            output_parts.append(f"[exit_code={result.returncode}]")

        combined = "\n".join(output_parts) or "(no output)"

        logger.info("[bob] exit_code=%d", result.returncode)
        logger.info("[bob] stdout:\n%s", result.stdout[:2000] if result.stdout else "(empty)")
        if result.stderr:
            logger.info("[bob] stderr:\n%s", result.stderr[:2000])

        return StringToolOutput(combined)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "bob_cli"], creator=self)

    async def clone(self):
        tool = self.__class__(binary_path=self._binary_path, kubeconfig=self._kubeconfig)
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool
