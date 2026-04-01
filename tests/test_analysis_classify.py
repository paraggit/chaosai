"""Tests for post-chaos analysis classification."""
from __future__ import annotations

from chaosminds.agents.analysis import AnalysisAgent


def test_classify_ready_not_inside_notready() -> None:
    bugs, warns, _ = AnalysisAgent._classify_output(
        "node-1   NotReady   False\nnode-2   Ready   True",
        {"Ready": "WARN"},
        "nodes",
    )
    assert warns == 0


def test_skip_warn_positive_states() -> None:
    bugs, warns, _ = AnalysisAgent._classify_output(
        "Ready",
        {"Ready": "WARN"},
        "StorageCluster state",
    )
    assert warns == 0


def test_normalize_strips_stderr_lines() -> None:
    raw = "HEALTH_OK\n[stderr] something scary with Error in text"
    out = AnalysisAgent._normalize_for_classification(raw)
    assert "[stderr]" not in out
    assert "HEALTH_OK" in out
