"""Tool for the ExecutorAgent to validate oc commands before running them.

Catches common mistakes like wrong wait conditions, mismatched
braces in jsonpath, and invalid resource types — allowing the
agent to self-correct instead of relying on regex hacks.
"""
from __future__ import annotations

import logging
import re
from typing import cast

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools.tool import Tool, ToolRunOptions
from beeai_framework.tools.types import StringToolOutput

from chaosminds.cmd_split import UnsafeCommandError, split_command
from chaosminds.iteration_placeholders import expand_iteration_placeholders
from chaosminds.oc_cmd_guard import oc_get_missing_resource

logger = logging.getLogger(__name__)

_KNOWN_WAIT_CONDITIONS: dict[str, list[str]] = {
    "pvc": [
        "--for=jsonpath={.status.phase}=Bound",
        "--for=delete",
    ],
    "persistentvolumeclaim": [
        "--for=jsonpath={.status.phase}=Bound",
        "--for=delete",
    ],
    "volumesnapshot": [
        "--for=jsonpath={.status.readyToUse}=true",
        "--for=delete",
    ],
    "pod": [
        "--for=condition=Ready",
        "--for=condition=Initialized",
        "--for=delete",
    ],
}

_BAD_PATTERNS = [
    (
        re.compile(r"--for=condition=bound", re.IGNORECASE),
        "Use --for=jsonpath={.status.phase}=Bound for PVCs",
    ),
    (
        re.compile(r"--for=jsonpath=\{\.status\.phase\}=Ready"),
        "PVC phase is 'Bound' not 'Ready'. Use --for=jsonpath={.status.phase}=Bound",
    ),
    (
        re.compile(r"--for=condition=deleted", re.IGNORECASE),
        "Use --for=delete (not condition=deleted)",
    ),
    (
        re.compile(r"\{(\.[^}]+)\)"),
        "Mismatched brace: found '{...)', should be '{...}'",
    ),
]


class OcValidationInput(BaseModel):
    command: str = Field(
        ...,
        description=(
            "The full oc command string to validate "
            "(e.g., 'wait pvc/my-pvc -n ns --for=jsonpath={.status.phase}=Bound --timeout=300s'). "
            "Do NOT include the 'oc' binary name."
        ),
    )


class OcValidationTool(Tool[OcValidationInput, ToolRunOptions, StringToolOutput]):
    name = "oc_validate"
    description = (
        "Validates an oc command for common mistakes BEFORE running it. "
        "Checks wait conditions, jsonpath brace matching, and resource "
        "type correctness. Returns 'VALID' or a list of issues with "
        "suggested fixes. Use this to self-check your oc commands."
    )

    @property
    def input_schema(self) -> type[OcValidationInput]:
        return OcValidationInput

    async def _run(
        self, input: OcValidationInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        cmd = cast(
            str,
            expand_iteration_placeholders(input.command, idx=1),
        )
        issues: list[str] = []

        try:
            split_parts = split_command(cmd)
        except UnsafeCommandError:
            split_parts = cmd.split()

        if oc_get_missing_resource(split_parts):
            issues.append(
                "`oc get` requires a resource type (e.g. pods, pvc) or "
                "`-f <manifest>`; bare `get` is invalid.",
            )

        for pattern, message in _BAD_PATTERNS:
            if pattern.search(cmd):
                issues.append(message)

        if "wait " in cmd:
            parts = cmd.split()
            resource_part = ""
            for p in parts:
                if "/" in p and not p.startswith("-"):
                    resource_part = p.split("/")[0].lower()
                    break

            if resource_part:
                valid = _KNOWN_WAIT_CONDITIONS.get(resource_part, [])
                if valid:
                    has_valid = any(v in cmd for v in valid)
                    if not has_valid:
                        issues.append(
                            f"Wait condition for '{resource_part}' should "
                            f"be one of: {valid}"
                        )

        open_braces = cmd.count("{")
        close_braces = cmd.count("}")
        if open_braces != close_braces:
            issues.append(
                f"Unbalanced braces: {open_braces} opening vs "
                f"{close_braces} closing"
            )

        if issues:
            result = "ISSUES FOUND:\n" + "\n".join(
                f"  - {i}" for i in issues
            )
            logger.info("[oc_validate] %s → %d issues", cmd[:80], len(issues))
        else:
            result = "VALID"
            logger.info("[oc_validate] %s → VALID", cmd[:80])

        return StringToolOutput(result)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "oc_validate"], creator=self,
        )

    async def clone(self):
        tool = self.__class__()
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool
