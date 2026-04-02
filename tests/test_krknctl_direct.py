from chaosminds.tools.krknctl_tool import run_krknctl_from_scenario_config


def test_run_krknctl_empty_scenario_config() -> None:
    rc, out = run_krknctl_from_scenario_config(
        "krknctl", "", {}, max_parallel=2,
    )
    assert rc == 1
    assert "empty" in out.lower()
