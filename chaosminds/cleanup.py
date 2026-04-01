"""Shared cleanup of chaos-test-* resources on the cluster."""
from __future__ import annotations

import logging
import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chaosminds.config import AppConfig

logger = logging.getLogger(__name__)


def delete_chaos_test_resources(
    oc_path: str,
    kubeconfig: str | None,
    log: logging.Logger | None = None,
    namespace: str = "openshift-storage",
) -> None:
    """Delete volumesnapshot, pvc, pod resources matching chaos-test* in ns."""
    lg = log or logger
    env = {**os.environ}
    if kubeconfig:
        env["KUBECONFIG"] = kubeconfig

    lg.info("=" * 60)
    lg.info("RESOURCE CLEANUP")

    for rtype in ("volumesnapshot", "pvc", "pod"):
        cmd = [
            oc_path, "get", rtype, "-n", namespace,
            "--no-headers",
            "-o", "custom-columns=NAME:.metadata.name",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=120, env=env,
            )
            out = result.stdout.strip()
        except subprocess.TimeoutExpired:
            lg.error("[cleanup] get %s timed out", rtype)
            continue

        for name in out.splitlines():
            name = name.strip()
            if "chaos-test" in name:
                lg.info("[cleanup] Deleting %s/%s", rtype, name)
                subprocess.run(
                    [
                        oc_path, "delete", rtype, name,
                        "-n", namespace, "--timeout=120s",
                    ],
                    capture_output=True, text=True,
                    timeout=180, env=env,
                )

    verify_cmd = [
        oc_path, "get", "volumesnapshot,pvc,pods",
        "-n", namespace, "--no-headers",
    ]
    try:
        vr = subprocess.run(
            verify_cmd, capture_output=True, text=True,
            timeout=60, env=env,
        )
        remaining = [
            ln for ln in vr.stdout.splitlines()
            if "chaos-test" in ln
        ]
        if remaining:
            lg.warning(
                "[cleanup] Remaining test resources:\n%s",
                "\n".join(remaining),
            )
        else:
            lg.info("[cleanup] All chaos-test resources cleaned up")
    except subprocess.TimeoutExpired:
        lg.warning("[cleanup] verify get timed out")

    lg.info("END RESOURCE CLEANUP")


def cleanup_from_config(config: AppConfig, log: logging.Logger | None = None) -> None:
    """Convenience wrapper using AppConfig paths."""
    delete_chaos_test_resources(
        config.oc_path, config.kubeconfig or None, log,
    )
