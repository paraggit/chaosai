"""Expand loop index placeholders in plan strings.

Agent mode uses a fixed index. The executor prompt expands these before the LLM
sees the step, but the model may still emit ``{i}`` or ``{{i}}`` when
constructing tool arguments — tools apply this again as defense in depth.
"""


def expand_iteration_placeholders(obj: object, idx: int = 1) -> object:
    """Replace ``{{i}}`` then ``{i}`` in all strings (recursive).

    ``{{i}}`` must be handled before ``{i}``.
    """
    sub = str(idx)
    if isinstance(obj, str):
        return obj.replace("{{i}}", sub).replace("{i}", sub)
    if isinstance(obj, dict):
        return {k: expand_iteration_placeholders(v, idx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_iteration_placeholders(x, idx) for x in obj]
    return obj
