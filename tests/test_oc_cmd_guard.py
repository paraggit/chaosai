import pytest

from chaosminds.oc_cmd_guard import oc_get_missing_resource


@pytest.mark.parametrize(
    ("parts", "expect_missing"),
    [
        (["get"], True),
        (["get", "-n", "openshift-storage"], True),
        (["get", "pods"], False),
        (["get", "pods", "-n", "openshift-storage"], False),
        (["get", "-n", "ns", "pods"], False),
        (["get", "-f", "x.yaml"], False),
        (["get", "-k", "dir/"], False),
        (["apply", "-f", "-"], False),
        (["wait", "pvc/x"], False),
    ],
)
def test_oc_get_missing_resource(
    parts: list[str],
    expect_missing: bool,
) -> None:
    assert oc_get_missing_resource(parts) is expect_missing
