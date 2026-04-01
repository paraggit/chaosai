"""Tool for agents to discover cluster topology dynamically.

Enables agents to query what ODF daemons are running, how many
replicas each has, which storage classes exist, etc. — so they
can make informed decisions about chaos targets instead of
relying on hardcoded values.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools.tool import Tool, ToolRunOptions
from beeai_framework.tools.types import StringToolOutput

logger = logging.getLogger(__name__)


class DiscoveryInput(BaseModel):
    query: str = Field(
        default="all",
        description=(
            "What to discover. Options: "
            "'all' — full cluster topology, "
            "'odf_daemons' — ODF pod labels + replica counts, "
            "'storage_classes' — available StorageClasses, "
            "'snapshot_classes' — VolumeSnapshotClasses, "
            "'nodes' — node roles + counts."
        ),
    )
    namespace: str = Field(
        default="openshift-storage",
        description="Namespace to inspect.",
    )


class ClusterDiscoveryTool(Tool[DiscoveryInput, ToolRunOptions, StringToolOutput]):
    name = "cluster_discovery"
    description = (
        "Discovers the live cluster topology: ODF daemon types and replica "
        "counts, storage classes, snapshot classes, and node layout. Use this "
        "BEFORE deciding chaos targets or storage parameters so you pick "
        "values that match the real cluster instead of guessing."
    )

    def __init__(self, oc_path: str = "oc", kubeconfig: str = "") -> None:
        super().__init__()
        self._oc = oc_path
        self._kubeconfig = kubeconfig

    @property
    def input_schema(self) -> type[DiscoveryInput]:
        return DiscoveryInput

    def _run_oc(self, args: str) -> str:
        cmd = [self._oc, *args.split()]
        env = None
        if self._kubeconfig:
            env = {**os.environ, "KUBECONFIG": self._kubeconfig}
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, env=env,
            )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            return ""

    def _discover_odf_daemons(self, ns: str) -> list[dict]:
        raw = self._run_oc(f"get pods -n {ns} -o json")
        try:
            pods = json.loads(raw)
        except json.JSONDecodeError:
            return []

        label_counts: dict[str, int] = {}
        for pod in pods.get("items", []):
            labels = pod.get("metadata", {}).get("labels", {})
            app = labels.get("app", "")
            if app.startswith("rook-ceph-") or app.startswith("noobaa"):
                phase = pod.get("status", {}).get("phase", "Unknown")
                if phase == "Running":
                    label_counts[app] = label_counts.get(app, 0) + 1

        return [
            {"app_label": app, "running_replicas": count}
            for app, count in sorted(label_counts.items())
        ]

    def _discover_storage_classes(self) -> list[dict]:
        raw = self._run_oc("get sc -o json")
        try:
            scs = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [
            {
                "name": sc.get("metadata", {}).get("name", ""),
                "provisioner": sc.get("provisioner", ""),
                "reclaim_policy": sc.get("reclaimPolicy", ""),
            }
            for sc in scs.get("items", [])
            if "openshift-storage" in sc.get("provisioner", "")
               or "ceph" in sc.get("provisioner", "")
        ]

    def _discover_snapshot_classes(self) -> list[dict]:
        raw = self._run_oc("get volumesnapshotclass -o json")
        try:
            vscs = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [
            {
                "name": vsc.get("metadata", {}).get("name", ""),
                "driver": vsc.get("driver", ""),
            }
            for vsc in vscs.get("items", [])
        ]

    def _discover_nodes(self) -> dict:
        raw = self._run_oc("get nodes -o json")
        try:
            nodes = json.loads(raw)
        except json.JSONDecodeError:
            return {"total": 0, "workers": 0, "masters": 0}
        workers = 0
        masters = 0
        for node in nodes.get("items", []):
            labels = node.get("metadata", {}).get("labels", {})
            if "node-role.kubernetes.io/master" in labels:
                masters += 1
            if "node-role.kubernetes.io/worker" in labels:
                workers += 1
        return {
            "total": len(nodes.get("items", [])),
            "workers": workers,
            "masters": masters,
        }

    async def _run(
        self, input: DiscoveryInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        ns = input.namespace
        result: dict = {}

        queries = (
            ["odf_daemons", "storage_classes", "snapshot_classes", "nodes"]
            if input.query == "all"
            else [input.query]
        )

        for q in queries:
            if q == "odf_daemons":
                result["odf_daemons"] = self._discover_odf_daemons(ns)
            elif q == "storage_classes":
                result["storage_classes"] = self._discover_storage_classes()
            elif q == "snapshot_classes":
                result["snapshot_classes"] = self._discover_snapshot_classes()
            elif q == "nodes":
                result["nodes"] = self._discover_nodes()

        logger.info(
            "[discovery] Query=%s ns=%s result_keys=%s",
            input.query, ns, list(result.keys()),
        )
        return StringToolOutput(json.dumps(result, indent=2))

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "cluster_discovery"], creator=self,
        )

    async def clone(self):
        tool = self.__class__(
            oc_path=self._oc, kubeconfig=self._kubeconfig,
        )
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool
