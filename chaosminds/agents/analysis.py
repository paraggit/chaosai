"""Post-chaos analysis agent.

Reads the analysis prompt, asks the LLM for an analysis plan,
then passes each check to bob (with oc fallback).  Returns a
structured findings dict and writes a Markdown report.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from beeai_framework.agents.tool_calling.agent import ToolCallingAgent
from beeai_framework.agents.types import AgentMeta
from beeai_framework.backend.chat import ChatModel
from beeai_framework.tools.think import ThinkTool

from chaosminds.agents._prompts import system_prompt_template
from chaosminds.config import AppConfig

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class AnalysisAgent:
    """Generates an analysis plan via LLM, then runs it
    through bob (oc fallback) and classifies findings."""

    def __init__(
        self,
        llm: ChatModel,
        config: AppConfig,
    ) -> None:
        prompt_text = (
            PROMPTS_DIR / "post_analysis_system.txt"
        ).read_text()

        self.agent = ToolCallingAgent(
            llm=llm,
            tools=[ThinkTool()],
            meta=AgentMeta(
                name="AnalysisAgent",
                description="Post-chaos cluster analysis",
                tools=[ThinkTool()],
            ),
            templates={
                "system": system_prompt_template(prompt_text),
            },
        )
        self.config = config
        self._env = {**os.environ}
        if config.kubeconfig:
            self._env["KUBECONFIG"] = config.kubeconfig

    # ── Subprocess helpers ──

    def _run_bob(self, prompt: str) -> tuple[int, str]:
        """Run bob with a natural-language prompt.

        bob "<prompt with kubeconfig>" -y
        """
        if self.config.kubeconfig:
            full_prompt = (
                f"Using kubeconfig at "
                f"{self.config.kubeconfig}, "
                f"{prompt}"
            )
        else:
            full_prompt = prompt

        cmd = [
            self.config.bob_cli_path,
            full_prompt,
            "-y",
        ]
        logger.info(
            "[analysis-bob] %s %r -y",
            self.config.bob_cli_path, full_prompt,
        )
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=600, env=self._env,
            )
            out = result.stdout.strip()
            if result.stderr:
                out += (
                    "\n[stderr] "
                    + result.stderr.strip()
                )
            logger.info(
                "[analysis-bob] exit=%d",
                result.returncode,
            )
            logger.info(
                "[analysis-bob] output:\n%s",
                out[:3000],
            )
            return result.returncode, out
        except subprocess.TimeoutExpired:
            logger.error("[analysis-bob] timed out")
            return 1, "ERROR: bob timed out"
        except FileNotFoundError:
            logger.warning(
                "[analysis-bob] bob binary not found, "
                "using oc fallback",
            )
            return -1, ""

    def _run_oc(self, command: str) -> tuple[int, str]:
        cmd = [self.config.oc_path, *command.split()]
        logger.info("[analysis-oc] %s %s",
                    self.config.oc_path, command)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=120, env=self._env,
            )
            out = result.stdout.strip()
            if result.stderr:
                out += "\n[stderr] " + result.stderr.strip()
            logger.info(
                "[analysis-oc] exit=%d", result.returncode,
            )
            logger.info(
                "[analysis-oc] output:\n%s", out[:3000],
            )
            return result.returncode, out
        except subprocess.TimeoutExpired:
            logger.error("[analysis-oc] timed out")
            return 1, "ERROR: oc timed out"

    # ── Plan generation ──

    async def _get_analysis_plan(
        self, instruction: str,
    ) -> dict:
        prompt = (
            f"Chaos that was run: {instruction}\n\n"
            "Produce the JSON analysis plan. "
            "Output ONLY the JSON object."
        )

        logger.info(
            "[AnalysisAgent] Asking LLM for analysis plan "
            "(%d chars)", len(prompt),
        )
        output = await self.agent.run(prompt)
        raw = output.last_message.text

        logger.info(
            "[AnalysisAgent] LLM response (%d chars):\n%s",
            len(raw), raw[:3000],
        )
        return self._parse_plan(raw)

    @staticmethod
    def _repair_json(text: str) -> str:
        text = re.sub(r",\s*([}\]])", r"\1", text)
        text = re.sub(
            r"([}\]])\s*\n(\s*\")", r"\1,\n\2", text,
        )
        text = re.sub(r'"\s*\n(\s*")', r'",\n\1', text)
        return text

    @staticmethod
    def _parse_plan(raw: str) -> dict:
        text = raw.strip()
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
        if start != -1 and end != -1 and end > start:
            candidate = text[start:end + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

            repaired = AnalysisAgent._repair_json(candidate)
            try:
                parsed = json.loads(repaired)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        logger.warning(
            "[AnalysisAgent] Could not parse plan, "
            "using hardcoded defaults",
        )
        return {}

    # ── Execute analysis ──

    async def analyze(
        self,
        instruction: str,
        run_id: str = "",
    ) -> dict:
        """Run the full post-chaos analysis.

        Args:
            instruction: what chaos was run.
            run_id: unique run identifier (used for
                the markdown filename).

        Returns dict with bugs, warnings, verdict,
        findings, and report_path.
        """
        logger.info("=" * 60)
        logger.info(
            "POST-CHAOS ANALYSIS — via bob + LLM prompt",
        )

        plan = await self._get_analysis_plan(instruction)
        steps = plan.get("analysis_steps", [])
        verdict_rules = plan.get("verdict_rules", {})

        if not steps:
            steps = self._default_steps()
            verdict_rules = self._default_verdict_rules()

        bugs = 0
        warnings = 0
        findings: list[str] = []
        step_details: list[dict] = []

        for step in steps:
            check_name = step.get("check", "unknown")
            bob_prompt = (
                step.get("bob_prompt", "")
                or step.get("bob_command", "")
            )
            oc_fallback = step.get("oc_fallback", "")
            classify = step.get("classify", {})

            logger.info(
                "-" * 40 + " %s " + "-" * 10,
                check_name,
            )

            rc, raw_output = -1, ""
            source = "none"
            if bob_prompt:
                rc, raw_output = self._run_bob(
                    bob_prompt,
                )
                source = "bob"

            if rc != 0 and oc_fallback:
                logger.info(
                    "[analysis] bob failed/unavailable, "
                    "falling back to oc",
                )
                rc, raw_output = self._run_oc(
                    oc_fallback,
                )
                source = "oc"

            if rc != 0:
                warnings += 1
                findings.append(
                    f"WARN: Could not run check "
                    f"'{check_name}'",
                )
                step_details.append({
                    "check": check_name,
                    "source": source,
                    "status": "FAIL",
                    "output": raw_output[:2000],
                    "findings": [
                        "Could not run check",
                    ],
                })
                continue

            output = self._normalize_for_classification(
                raw_output,
            )
            logger.info(
                "[analysis] Normalized output for "
                "classification (%d→%d chars):\n%s",
                len(raw_output),
                len(output),
                output[:1000],
            )

            b, w, f = self._classify_output(
                output, classify, check_name,
            )
            bugs += b
            warnings += w
            findings.extend(f)

            step_details.append({
                "check": check_name,
                "source": source,
                "status": "OK",
                "output": raw_output[:2000],
                "findings": f if f else ["No issues"],
            })

        verdict = self._compute_verdict(
            bugs, warnings, verdict_rules,
        )

        logger.info("-" * 60)
        for finding in findings:
            logger.info("[analysis] %s", finding)
        logger.info("-" * 60)
        logger.info(
            "[analysis] bugs=%d  warnings=%d  verdict=%s",
            bugs, warnings, verdict,
        )
        logger.info("=" * 60)

        report_path = self._write_markdown(
            instruction=instruction,
            run_id=run_id,
            bugs=bugs,
            warnings=warnings,
            verdict=verdict,
            findings=findings,
            step_details=step_details,
        )

        return {
            "bugs": bugs,
            "warnings": warnings,
            "verdict": verdict,
            "findings": findings,
            "report_path": str(report_path),
        }

    # ── Markdown report ──

    @staticmethod
    def _write_markdown(
        *,
        instruction: str,
        run_id: str,
        bugs: int,
        warnings: int,
        verdict: str,
        findings: list[str],
        step_details: list[dict],
    ) -> Path:
        """Write the analysis report as Markdown."""
        analysis_dir = Path("analysis")
        analysis_dir.mkdir(exist_ok=True)

        if not run_id:
            run_id = datetime.now(
                timezone.utc,
            ).strftime("%Y%m%d_%H%M%S")

        report_file = (
            analysis_dir / f"analysis_{run_id}.md"
        )

        lines: list[str] = []
        lines.append(
            "# ChaosMinds — Post-Chaos Analysis Report",
        )
        lines.append("")
        lines.append(f"**Run ID:** `{run_id}`  ")
        lines.append(
            f"**Date:** "
            f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}"
            f" UTC  ",
        )
        lines.append(
            f"**Instruction:** {instruction}",
        )
        lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("## Verdict")
        lines.append("")

        if bugs > 0:
            icon = "CRITICAL"
        elif warnings > 0:
            icon = "WARNING"
        else:
            icon = "PASS"

        lines.append(f"**{icon}** — {verdict}")
        lines.append("")
        lines.append(
            "| Bugs | Warnings |",
        )
        lines.append("|------|----------|")
        lines.append(f"| {bugs} | {warnings} |")
        lines.append("")

        if findings:
            lines.append("---")
            lines.append("")
            lines.append("## Findings")
            lines.append("")
            for f in findings:
                if f.startswith("BUG:"):
                    lines.append(f"- **{f}**")
                else:
                    lines.append(f"- {f}")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("## Check Details")
        lines.append("")

        for detail in step_details:
            check = detail["check"]
            source = detail["source"]
            status = detail["status"]
            output = detail.get("output", "")
            step_findings = detail.get("findings", [])

            badge = "PASS" if status == "OK" else "FAIL"
            lines.append(f"### {check}")
            lines.append("")
            lines.append(
                f"**Status:** {badge} "
                f"| **Source:** {source}",
            )
            lines.append("")

            if step_findings:
                for sf in step_findings:
                    lines.append(f"- {sf}")
                lines.append("")

            if output:
                lines.append(
                    "<details><summary>"
                    "Raw output</summary>",
                )
                lines.append("")
                lines.append("```")
                lines.append(output[:2000])
                lines.append("```")
                lines.append("")
                lines.append("</details>")
                lines.append("")

        lines.append("---")
        lines.append(
            "*Generated by ChaosMinds Analysis Agent*",
        )
        lines.append("")

        report_file.write_text("\n".join(lines))

        logger.info(
            "[analysis] Report saved to %s",
            report_file.resolve(),
        )
        return report_file

    _BOB_NOISE_PATTERNS = (
        "[stderr]",
        "[ERROR]",
        "[using tool ",
        "Cost:",
        "YOLO mode",
        "Error during discovery",
        "spawn ",
        "Connection failed for",
        "attempt_completion",
        "Successfully completed",
        "kubectl_tool",
        "The kubectl command executed",
    )

    _THINKING_BLOCK_RE = re.compile(
        r"<thinking>[\s\S]*?</thinking>",
        re.IGNORECASE,
    )

    @classmethod
    def _strip_thinking_blocks(cls, text: str) -> str:
        return cls._THINKING_BLOCK_RE.sub("", text).strip()

    @classmethod
    def _strip_stderr_lines(cls, text: str) -> str:
        """Drop lines that are subprocess stderr appendages."""
        out: list[str] = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("[stderr]"):
                continue
            out.append(line)
        return "\n".join(out).strip()

    @classmethod
    def _normalize_for_classification(cls, raw: str) -> str:
        """Strip thinking, stderr noise, and bob wrappers before patterns run."""
        text = cls._strip_thinking_blocks(raw)
        text = cls._strip_stderr_lines(text)
        return cls._strip_bob_noise(text)

    @classmethod
    def _is_bob_noise(cls, line: str) -> bool:
        stripped = line.strip()
        for pat in cls._BOB_NOISE_PATTERNS:
            if pat in stripped:
                return True
        return False

    @classmethod
    def _strip_bob_noise(cls, output: str) -> str:
        """Extract actual command output from bob's
        verbose response, removing thinking blocks,
        tool-use markers, stderr, MCP errors, and
        summaries."""
        lines = output.splitlines()
        cleaned: list[str] = []
        in_thinking = False
        in_output_block = False

        for line in lines:
            stripped = line.strip()

            if stripped.startswith("<thinking>"):
                in_thinking = True
                continue
            if "</thinking>" in stripped:
                in_thinking = False
                continue
            if in_thinking:
                continue

            if cls._is_bob_noise(line):
                continue

            if stripped == "---output---":
                in_output_block = not in_output_block
                continue

            if in_output_block:
                cleaned.append(line)

        if cleaned:
            return "\n".join(cleaned).strip()

        # Fallback: no ---output--- markers found
        fallback: list[str] = []
        in_thinking = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("<thinking>"):
                in_thinking = True
                continue
            if "</thinking>" in stripped:
                in_thinking = False
                continue
            if in_thinking:
                continue
            if cls._is_bob_noise(line):
                continue
            fallback.append(line)

        return "\n".join(fallback).strip()

    _CRASH_ID_RE = re.compile(
        r"\d{4}-\d{2}-\d{2}[_T]\d{2}[.:]\d{2}",
    )

    @classmethod
    def _count_ceph_crashes(
        cls, output: str,
    ) -> int:
        """Count actual ceph crash entries.

        Real ceph crash ls output lines look like:
          2024-01-15_12:00:00.123_abc-def-123
        We only count lines matching that timestamp
        pattern, ignoring bob summaries and headers.
        """
        count = 0
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("ID"):
                continue
            if stripped.startswith("--"):
                continue
            if cls._CRASH_ID_RE.match(stripped):
                count += 1
        return count

    _NEGATION_RE = re.compile(
        r"\b(no|not|none|without|zero|0)\b",
        re.IGNORECASE,
    )

    @staticmethod
    def _line_is_negated(
        line: str, pattern: str,
    ) -> bool:
        """True if pattern appears in a negation context."""
        idx = line.lower().find(pattern.lower())
        if idx == -1:
            return False
        prefix = line[:idx].lower()
        negations = (
            "no ", "not ", "none", "without ",
            "zero ", "0 ", "no_", "non-",
            "never ", "isn't ", "aren't ",
            "doesn't ", "don't ", "did not ",
            "does not ", "found no ",
        )
        for neg in negations:
            if neg in prefix[-30:]:
                return True
        return False

    # Never emit WARN for these — they indicate healthy / expected state.
    # (LLM-generated analysis plans sometimes mis-label them as WARN.)
    _SKIP_WARN_FOR_POSITIVE_STATES = frozenset({
        "Ready",
        "Bound",
        "HEALTH_OK",
        "HEALTHY",
    })

    @classmethod
    def _pattern_matches_line(cls, line: str, pattern: str) -> bool:
        """Match pattern against line.

        Uses word boundaries for identifier-like patterns so ``Ready``
        does not match inside ``NotReady`` or ``KubeletReady``.
        """
        if pattern not in line:
            return False
        if re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", pattern):
            return bool(
                re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(pattern)}"
                    rf"(?![A-Za-z0-9])",
                    line,
                ),
            )
        return True

    @classmethod
    def _classify_output(
        cls,
        output: str,
        classify: dict,
        check_name: str,
    ) -> tuple[int, int, list[str]]:
        bugs = 0
        warns = 0
        findings: list[str] = []

        for pattern, level in classify.items():
            if (
                level == "WARN"
                and pattern in cls._SKIP_WARN_FOR_POSITIVE_STATES
            ):
                continue
            if pattern.startswith("restarts>="):
                threshold = int(
                    pattern.split(">=")[1],
                )
                for line in output.splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            count = int(parts[-1])
                            if count >= threshold:
                                bugs += 1
                                findings.append(
                                    f"BUG: {check_name}"
                                    f" — {parts[0]} "
                                    f"restarts={count}",
                                )
                        except ValueError:
                            pass
            elif pattern == "any_crash":
                crash_count = cls._count_ceph_crashes(
                    output,
                )
                if crash_count > 0:
                    bugs += 1
                    findings.append(
                        f"BUG: {check_name} — "
                        f"{crash_count} "
                        f"unarchived crashes",
                    )
            else:
                matched_lines = []
                for line in output.splitlines():
                    if not cls._pattern_matches_line(
                        line, pattern,
                    ):
                        continue
                    if not cls._line_is_negated(
                        line, pattern,
                    ):
                        matched_lines.append(
                            line.strip(),
                        )

                if matched_lines:
                    if level == "BUG":
                        bugs += 1
                        sample = matched_lines[0][:120]
                        findings.append(
                            f"BUG: {check_name} — "
                            f"'{pattern}' detected: "
                            f"{sample}",
                        )
                    else:
                        warns += 1
                        sample = matched_lines[0][:120]
                        findings.append(
                            f"WARN: {check_name} — "
                            f"'{pattern}' detected: "
                            f"{sample}",
                        )

        return bugs, warns, findings

    @staticmethod
    def _compute_verdict(
        bugs: int,
        warnings: int,
        rules: dict,
    ) -> str:
        if bugs > 0:
            return rules.get(
                "any_bug",
                "POTENTIAL PRODUCT BUGS DETECTED",
            )
        if warnings > 0:
            return rules.get(
                "only_warns",
                "CHAOS IMPACT (no bugs, may recover)",
            )
        return rules.get(
            "clean",
            "SYSTEM HEALTHY — no issues",
        )

    @staticmethod
    def _default_steps() -> list[dict]:
        ns = "openshift-storage"
        return [
            {
                "id": 1, "check": "Pod health",
                "bob_prompt": (
                    "List all pods in "
                    f"{ns} namespace and show their status"
                ),
                "oc_fallback": (
                    f"get pods -n {ns} --no-headers"
                ),
                "classify": {
                    "CrashLoopBackOff": "BUG",
                    "Error": "BUG",
                    "ImagePullBackOff": "BUG",
                    "Pending": "WARN",
                },
            },
            {
                "id": 2,
                "check": "ODF component restarts",
                "bob_prompt": (
                    "Show restart counts for all "
                    f"pods in {ns} namespace"
                ),
                "oc_fallback": (
                    f"get pods -n {ns} "
                    f"-o custom-columns="
                    f"NAME:.metadata.name,"
                    f"RESTARTS:.status."
                    f"containerStatuses"
                    f"[0].restartCount --no-headers"
                ),
                "classify": {"restarts>=3": "BUG"},
            },
            {
                "id": 3, "check": "Ceph health",
                "bob_prompt": (
                    "Run ceph health detail using "
                    "the rook-ceph-tools deployment "
                    f"in {ns} namespace"
                ),
                "oc_fallback": (
                    f"exec -n {ns} "
                    f"deploy/rook-ceph-tools "
                    f"-- ceph health detail"
                ),
                "classify": {
                    "HEALTH_ERR": "BUG",
                    "HEALTH_WARN": "WARN",
                    "OSD_DOWN": "BUG",
                    "MON_DOWN": "BUG",
                    "PG_DAMAGED": "BUG",
                },
            },
            {
                "id": 4, "check": "Ceph crashes",
                "bob_prompt": (
                    "Check for new ceph crashes "
                    "using rook-ceph-tools "
                    f"deployment in {ns} namespace"
                ),
                "oc_fallback": (
                    f"exec -n {ns} "
                    f"deploy/rook-ceph-tools "
                    f"-- ceph crash ls-new"
                ),
                "classify": {"any_crash": "BUG"},
            },
            {
                "id": 5,
                "check": "StorageCluster state",
                "bob_prompt": (
                    "Get the StorageCluster "
                    f"status phase in {ns} namespace"
                ),
                "oc_fallback": (
                    f"get storagecluster -n {ns} "
                    f"-o jsonpath="
                    f"{{.items[0].status.phase}}"
                ),
                "classify": {
                    "Error": "BUG",
                    "Failed": "BUG",
                    "Progressing": "WARN",
                },
            },
            {
                "id": 6, "check": "PVC binding",
                "bob_prompt": (
                    "List all PVCs in "
                    f"{ns} namespace "
                    "and show their status"
                ),
                "oc_fallback": (
                    f"get pvc -n {ns} --no-headers"
                ),
                "classify": {
                    "Lost": "BUG",
                    "Pending": "WARN",
                },
            },
            {
                "id": 7, "check": "Node readiness",
                "bob_prompt": (
                    "List all cluster nodes "
                    "and their Ready status"
                ),
                "oc_fallback": (
                    "get nodes --no-headers"
                ),
                "classify": {"NotReady": "BUG"},
            },
            {
                "id": 8, "check": "Warning events",
                "bob_prompt": (
                    "List recent warning events "
                    f"in {ns} namespace "
                    "sorted by time"
                ),
                "oc_fallback": (
                    f"get events -n {ns} "
                    f"--sort-by=.lastTimestamp "
                    f"--field-selector type=Warning"
                ),
                "classify": {
                    "FailedMount": "WARN",
                    "Evicted": "WARN",
                    "OOMKilled": "WARN",
                    "BackOff": "WARN",
                },
            },
        ]

    @staticmethod
    def _default_verdict_rules() -> dict:
        return {
            "any_bug": "POTENTIAL PRODUCT BUGS DETECTED",
            "only_warns": "CHAOS IMPACT (no bugs, may recover)",
            "clean": "SYSTEM HEALTHY — no issues",
        }
