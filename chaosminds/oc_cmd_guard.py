"""Guardrails for LLM-supplied oc/kubectl-style command argument lists."""
from __future__ import annotations

# Flags that consume the next token (kubectl/oc get).
_GET_TWO_TOKEN_FLAGS = frozenset({
    "-n",
    "--namespace",
    "-l",
    "--selector",
    "-o",
    "--output",
    "--field-selector",
    "--chunk-size",
    "-f",
    "--filename",
    "-k",
    "--kustomize",
    "--sort-by",
    "--template",
    "--request-timeout",
    "--label-columns",
    "-L",
})


def _get_uses_file_or_kustomize(parts: list[str]) -> bool:
    """``kubectl get -f <file>`` / ``-k <dir>`` does not need a resource type."""
    for j in range(1, len(parts) - 1):
        if parts[j] in ("-f", "--filename", "-k", "--kustomize") and parts[j + 1]:
            return True
    return False


def oc_get_missing_resource(parts: list[str]) -> bool:
    """True if ``parts`` is a ``get`` subcommand with no resource / -f / positionals.

    ``parts`` is the argv after ``oc`` (e.g. ``['get', 'pods', '-n', 'ns']``).
    """
    if len(parts) < 1 or parts[0] != "get":
        return False
    if _get_uses_file_or_kustomize(parts):
        return False
    i = 1
    while i < len(parts):
        p = parts[i]
        if p in _GET_TWO_TOKEN_FLAGS:
            if i + 1 >= len(parts):
                return True
            i += 2
            continue
        if p.startswith("-") and "=" in p:
            i += 1
            continue
        if p.startswith("-"):
            i += 1
            continue
        return False
    return True
