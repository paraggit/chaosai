from chaosminds.logging_utils import format_plan_summary, short_json


def test_short_json_truncates() -> None:
    long = "x" * 300
    s = short_json(long, max_len=50)
    assert len(s) == 50
    assert s.endswith("...")


def test_format_plan_summary() -> None:
    plan = {"setup": [], "chaos": {}, "test_ops": [], "post": []}
    steps = [
        {"id": 1, "tool": "oc", "action": "Create PVC"},
        {"id": 2, "tool": "krknctl", "action": "Inject chaos: pod-scenarios"},
    ]
    text = format_plan_summary(plan, steps)
    assert "phases=" in text
    assert "steps=2" in text
    assert "[oc]" in text
    assert "[krknctl]" in text
