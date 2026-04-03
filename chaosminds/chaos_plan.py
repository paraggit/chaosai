"""Helpers for chaos phase shape in structured plans."""

from __future__ import annotations

from typing import Any


def normalize_chaos_scenarios(
    chaos: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return scenario dicts from the ``chaos`` phase.

    Supports:

    - ``scenario_config``: single scenario (backward compatible).
    - ``scenario_configs``: non-empty list of scenarios (multiple krkn steps /
      merged script graph).

    If both are present, ``scenario_configs`` wins when it is a non-empty list.
    """
    if not isinstance(chaos, dict):
        return []
    multi = chaos.get("scenario_configs")
    if isinstance(multi, list):
        out = [x for x in multi if isinstance(x, dict) and x]
        if out:
            return out
    single = chaos.get("scenario_config")
    if isinstance(single, dict) and single:
        return [single]
    return []
