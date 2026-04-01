from __future__ import annotations

import logging
import os
import subprocess

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools.tool import Tool, ToolRunOptions
from beeai_framework.tools.types import StringToolOutput

from chaosminds.cmd_split import UnsafeCommandError, split_command
from chaosminds.iteration_placeholders import expand_iteration_placeholders
from chaosminds.oc_cmd_guard import oc_get_missing_resource

logger = logging.getLogger(__name__)


class OcInput(BaseModel):
    command: str = Field(
        ...,
        description=(
            "oc sub-command and arguments to run, e.g. "
            "'get pods -n openshift-storage -o json' or 'apply -f -'. "
            "Never use bare 'get' without a resource type. "
            "Do NOT include the 'oc' binary name itself."
        ),
    )
    yaml: str = Field(
        default="",
        description=(
            "YAML manifest to pipe to stdin. Use together with 'apply -f -' "
            "to create Kubernetes resources. Leave empty for read commands."
        ),
    )


class OcTool(Tool[OcInput, ToolRunOptions, StringToolOutput]):
    name = "oc"
    description = (
        "Runs an oc command against the target cluster and returns "
        "the combined stdout/stderr output. Use for cluster inspection, "
        "applying YAML manifests, and ad-hoc queries. "
        "To apply a YAML manifest, set command='apply -f -' and put "
        "the manifest in the 'yaml' field."
    )

    def __init__(self, binary_path: str = "oc", kubeconfig: str = "") -> None:
        super().__init__()
        self._binary_path = binary_path
        self._kubeconfig = kubeconfig

    @property
    def input_schema(self) -> type[OcInput]:
        return OcInput

    async def _run(
        self, input: OcInput, options: ToolRunOptions | None, context: RunContext
    ) -> StringToolOutput:
        command = expand_iteration_placeholders(input.command, idx=1)
        yaml_in = expand_iteration_placeholders(input.yaml, idx=1) if input.yaml else ""
        try:
            parts = split_command(command)
        except UnsafeCommandError as exc:
            return StringToolOutput(
                f"[error] command rejected: {exc}",
            )
        if oc_get_missing_resource(parts):
            return StringToolOutput(
                "[error] `oc get` requires a resource type (e.g. pods, pvc, "
                "storagecluster) and optional name, or `-f <file>`. "
                "Example: get pods -n openshift-storage",
            )
        cmd = [self._binary_path, *parts]
        env = None
        if self._kubeconfig:
            env = {**os.environ, "KUBECONFIG": self._kubeconfig}

        stdin_data = yaml_in if yaml_in else None

        logger.info("[oc] command: %s %s", self._binary_path, command)
        if stdin_data:
            logger.info("[oc] stdin yaml:\n%s", stdin_data)

        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"[stderr] {result.stderr}")
        if result.returncode != 0:
            output_parts.append(f"[exit_code={result.returncode}]")

        combined = "\n".join(output_parts) or "(no output)"

        logger.info("[oc] exit_code=%d", result.returncode)
        logger.info("[oc] stdout:\n%s", result.stdout[:2000] if result.stdout else "(empty)")
        if result.stderr:
            logger.info("[oc] stderr:\n%s", result.stderr[:2000])

        return StringToolOutput(combined)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "oc"], creator=self)

    async def clone(self):
        tool = self.__class__(binary_path=self._binary_path, kubeconfig=self._kubeconfig)
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool
