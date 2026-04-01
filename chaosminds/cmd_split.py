"""Safe splitting of LLM-supplied command strings for subprocess (no shell)."""
from __future__ import annotations

import re
import shlex

# Reject injection into un-shelled subprocess calls.
_FORBIDDEN = re.compile(
    r"[;&|`$]|\$\(|&&|\|\||\n|\r",
)


class UnsafeCommandError(ValueError):
    """Command contains shell metacharacters or newlines."""


def reject_shell_metacharacters(command: str) -> None:
    """Raise if the string is unsafe to pass through shlex.split + subprocess."""
    if _FORBIDDEN.search(command):
        raise UnsafeCommandError(
            "Command contains disallowed shell metacharacters or newlines",
        )


def split_command(command: str) -> list[str]:
    """Split a user/LLM command string like shell would (quoted args preserved)."""
    command = command.strip()
    if not command:
        return []
    reject_shell_metacharacters(command)
    return shlex.split(command, posix=True)
