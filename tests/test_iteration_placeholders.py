import pytest

from chaosminds.iteration_placeholders import expand_iteration_placeholders


@pytest.mark.parametrize(
    ("s", "expected"),
    [
        ("cephfs-pvc-{i}", "cephfs-pvc-1"),
        ("cephfs-pvc-{{i}}", "cephfs-pvc-1"),
        (
            "wait pvc/x-{i} -n ns --for=jsonpath={.status.phase}=Bound",
            "wait pvc/x-1 -n ns --for=jsonpath={.status.phase}=Bound",
        ),
    ],
)
def test_expand_str(s: str, expected: str) -> None:
    assert expand_iteration_placeholders(s, idx=1) == expected


def test_expand_nested() -> None:
    step = {
        "params": {
            "command": "wait pvc/p-{i}",
            "yaml": "name: pvc-{i}\n",
        }
    }
    out = expand_iteration_placeholders(step, idx=2)
    assert out["params"]["command"] == "wait pvc/p-2"
    assert out["params"]["yaml"] == "name: pvc-2\n"
