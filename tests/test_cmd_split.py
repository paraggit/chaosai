"""Tests for safe command splitting."""
from __future__ import annotations

import pytest

from chaosminds.cmd_split import (
    UnsafeCommandError,
    reject_shell_metacharacters,
    split_command,
)


def test_split_preserves_quoted_args() -> None:
    assert split_command(
        'get pods -n openshift-storage -l "app=rook-ceph-osd"',
    ) == [
        "get",
        "pods",
        "-n",
        "openshift-storage",
        "-l",
        "app=rook-ceph-osd",
    ]


def test_reject_injection() -> None:
    with pytest.raises(UnsafeCommandError):
        split_command("get pods; rm -rf /")
    with pytest.raises(UnsafeCommandError):
        reject_shell_metacharacters("foo $(whoami)")
    with pytest.raises(UnsafeCommandError):
        reject_shell_metacharacters("a && b")


def test_empty() -> None:
    assert split_command("") == []
    assert split_command("   ") == []
