from chaosminds.agents.planner import PlannerAgent
from chaosminds.chaos_plan import normalize_chaos_scenarios
from chaosminds.script_generator import _build_chaos_graph_for_script


def test_normalize_single_scenario_config() -> None:
    chaos = {
        "scenario_config": {
            "name": "pod-scenarios",
            "image": "quay.io/krkn-chaos/krkn-hub:pod-scenarios",
            "env": {},
        },
    }
    out = normalize_chaos_scenarios(chaos)
    assert len(out) == 1
    assert out[0]["name"] == "pod-scenarios"


def test_normalize_scenario_configs_wins() -> None:
    chaos = {
        "scenario_config": {"name": "ignored", "image": "x", "env": {}},
        "scenario_configs": [
            {"name": "a", "image": "ia", "env": {}},
            {"name": "b", "image": "ib", "env": {}},
        ],
    }
    out = normalize_chaos_scenarios(chaos)
    assert [x["name"] for x in out] == ["a", "b"]


def test_normalize_empty_scenario_configs_falls_back_to_single() -> None:
    chaos = {
        "scenario_configs": [],
        "scenario_config": {"name": "pod-scenarios", "image": "x", "env": {}},
    }
    out = normalize_chaos_scenarios(chaos)
    assert len(out) == 1
    assert out[0]["name"] == "pod-scenarios"


def test_flatten_plan_multiple_chaos_steps() -> None:
    plan = {
        "setup": [],
        "chaos": {
            "scenario_configs": [
                {"name": "pod-scenarios", "image": "x", "env": {}},
                {"name": "network-chaos", "image": "y", "env": {}},
            ],
        },
        "test_ops": [],
        "post": [],
    }
    steps = PlannerAgent._flatten_plan(plan)
    assert len(steps) == 2
    assert steps[0]["tool"] == "krknctl"
    assert steps[0]["params"]["scenario_config"]["name"] == "pod-scenarios"
    assert steps[1]["params"]["scenario_config"]["name"] == "network-chaos"
    assert steps[1]["depends_on"] == [1]


def test_build_chaos_graph_multi_wait_pattern() -> None:
    scenarios = [
        {"name": "pod-scenarios", "image": "x", "env": {}},
        {"name": "network-chaos", "image": "y", "env": {}},
    ]
    graph, wait_pattern = _build_chaos_graph_for_script(scenarios)
    assert "root_chaos" in graph
    assert wait_pattern == "krknctl-"
    # pod-scenarios fans out to 4 variants + network = 5 scenario nodes
    assert len(graph) == 6


def test_build_chaos_graph_single_wait_pattern() -> None:
    scenarios = [
        {"name": "network-chaos", "image": "y", "env": {}},
    ]
    graph, wait_pattern = _build_chaos_graph_for_script(scenarios)
    assert wait_pattern == "krknctl-network-chaos_chaos_"
    assert len(graph) == 2