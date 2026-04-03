"""Helpers for readable INFO logs and detailed DEBUG logs."""

from __future__ import annotations

import json
import logging
from typing import Any


def short_json(obj: Any, max_len: int = 200) -> str:
    """Compact JSON for one-line INFO; truncate with ellipsis if long."""
    try:
        s = json.dumps(obj, default=str, ensure_ascii=False)
    except TypeError:
        s = repr(obj)
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def format_plan_summary(plan: dict, plan_steps: list[dict]) -> str:
    """Multi-line human-readable plan summary (for INFO)."""
    lines = [
        f"phases={list(plan.keys())}  steps={len(plan_steps)}",
    ]
    for s in plan_steps:
        sid = s.get("id", "?")
        tool = s.get("tool", "?")
        action = (s.get("action") or "").strip()
        if len(action) > 90:
            action = action[:87] + "..."
        lines.append(f"  {sid}. [{tool}] {action}")
    return "\n".join(lines)


def log_plan(
    logger: logging.Logger,
    tag: str,
    plan: dict,
    plan_steps: list[dict],
) -> None:
    """INFO: short summary. DEBUG: full structured plan + flat steps JSON."""
    logger.info("%s\n%s", tag, format_plan_summary(plan, plan_steps))
    logger.debug(
        "%s structured_plan JSON:\n%s",
        tag,
        json.dumps(plan, indent=2),
    )
    logger.debug(
        "%s plan_steps JSON:\n%s",
        tag,
        json.dumps(plan_steps, indent=2),
    )


def log_step_params(
    logger: logging.Logger,
    tag: str,
    params: dict,
) -> None:
    """INFO: compact params. DEBUG: pretty-printed JSON."""
    logger.info("%s %s", tag, short_json(params, max_len=240))
    logger.debug("%s (full)\n%s", tag, json.dumps(params, indent=2))
