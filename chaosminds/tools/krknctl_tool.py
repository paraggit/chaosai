from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools.tool import Tool, ToolRunOptions
from beeai_framework.tools.types import StringToolOutput

from chaosminds.logging_utils import short_json

logger = logging.getLogger(__name__)

_DEFAULT_KRKN_RETRIES = 3
_DEFAULT_KRKN_BACKOFF_S = 5.0


def _krkn_combined_output(result: subprocess.CompletedProcess[str]) -> str:
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr] {result.stderr}")
    return "\n".join(parts) or "(no output)"


def _krkn_failure_hint(combined: str) -> str:
    """Extra context when krknctl fails on registry/version (known flaky upstream)."""
    low = combined.lower()
    if not any(
        x in low
        for x in (
            "no release found",
            "failed to fetch krknctl version",
            "panic:",
            "nil pointer",
            "sigsegv",
            "failed to retrieve tags",
            "failed to retrieve scenario",
        )
    ):
        return ""
    return (
        "\n\n[chaosminds] krknctl talks to Quay (scenario metadata) and GitHub "
        "(version). Failures are retried automatically. If this persists: "
        "allow outbound HTTPS to quay.io and api.github.com, check VPN/firewall, "
        "and upgrade krknctl (older builds can panic when HTTP errors are ignored)."
    )


def run_krknctl_random_run(
    cmd: list[str],
    *,
    retries: int | None = None,
    backoff_seconds: float = _DEFAULT_KRKN_BACKOFF_S,
    timeout: int = 900,
) -> tuple[int, str]:
    """Run ``krknctl random run ...`` with retries for transient network/API errors."""
    n = retries if retries is not None else int(
        os.getenv("KRKNCTL_RETRIES", str(_DEFAULT_KRKN_RETRIES)),
    )
    n = max(1, n)
    env = os.environ.copy()
    last_rc = 1
    last_out = ""
    for attempt in range(1, n + 1):
        logger.info("[krknctl] run attempt %d/%d", attempt, n)
        logger.debug("[krknctl] command: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        last_rc = result.returncode
        last_out = _krkn_combined_output(result)
        if last_rc == 0:
            return 0, last_out
        if attempt < n:
            logger.warning(
                "[krknctl] attempt %d failed (exit=%d), retry in %ss",
                attempt,
                last_rc,
                backoff_seconds,
            )
            time.sleep(backoff_seconds)
    return last_rc, last_out + _krkn_failure_hint(last_out)


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


def run_krknctl_from_scenario_config(
    binary_path: str,
    kubeconfig: str,
    scenario_config: dict,
    max_parallel: int = 2,
) -> tuple[int, str]:
    """Run ``krknctl random run`` with a graph from ``scenario_config``.

    Used when the Chaos LLM path fails (e.g. chat model error). Returns
    ``(exit_code, combined stdout/stderr)``.
    """
    if not scenario_config:
        return 1, "[error] empty scenario_config"

    tmp_path: str | None = None
    try:
        graph = build_krknctl_graph(scenario_config)
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            prefix="krknctl_direct_",
        )
        json.dump(graph, tmp, indent=2)
        tmp.close()
        tmp_path = tmp.name

        cmd = [
            binary_path,
            "random",
            "run",
            tmp_path,
            f"--max-parallel={max_parallel}",
        ]
        if kubeconfig:
            cmd.append(f"--kubeconfig={kubeconfig}")

        rc, combined = run_krknctl_random_run(cmd, timeout=900)
        return rc, combined
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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
    def binary_path(self) -> str:
        return self._binary_path

    @property
    def kubeconfig(self) -> str:
        return self._kubeconfig

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

            sc_label = (
                input.scenario_config.get("name", "?")
                if input.scenario_config
                else scenario_path
            )
            logger.info(
                "[krknctl] scenario=%s max_parallel=%s",
                sc_label,
                input.max_parallel,
            )
            logger.debug("[krknctl] command: %s", " ".join(cmd))
            if input.scenario_config:
                logger.debug(
                    "[krknctl] scenario_config:\n%s",
                    json.dumps(input.scenario_config, indent=2),
                )

            rc, combined = run_krknctl_random_run(cmd, timeout=900)
            if rc != 0:
                combined = f"{combined}\n[exit_code={rc}]"

            logger.info("[krknctl] exit=%d %s", rc, short_json(combined, 400))
            logger.debug("[krknctl] output full:\n%s", combined)

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

        logger.debug("[krknctl-list] command: %s", " ".join(cmd))

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"[stderr] {result.stderr}")

        combined = "\n".join(output_parts) or "(no output)"

        logger.info(
            "[krknctl-list] sub=%s exit=%d %s",
            sub,
            result.returncode,
            short_json(combined, 400),
        )
        logger.debug("[krknctl-list] output full:\n%s", combined)

        return StringToolOutput(combined)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "krknctl_list"], creator=self)

    async def clone(self):
        tool = self.__class__(binary_path=self._binary_path)
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool
