"""BeeAI tools for RAG queries over the ocs-ci codebase."""
from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools.tool import Tool, ToolRunOptions
from beeai_framework.tools.types import StringToolOutput

from chaosminds.config import RagConfig
from chaosminds.rag.vectorstore import VectorStore

logger = logging.getLogger(__name__)


# ── Tool 1: VectorSearchTool ──


class VectorSearchInput(BaseModel):
    query: str = Field(
        ...,
        description=(
            "Natural language question about the ocs-ci codebase: CRDs, "
            "StorageClasses, YAML templates, PVC/snapshot workflows, "
            "FIO or benchmark Job/Pod patterns, helpers. "
            "Example: 'How does ocs-ci create FIO workload on PVC?'"
        ),
    )
    file_type: Optional[str] = Field(
        default=None,
        description="Filter by extension, e.g. '.py', '.yaml'.",
    )
    module: Optional[str] = Field(
        default=None,
        description="Filter by top-level directory, e.g. 'ocs_ci', 'tests'.",
    )
    top_k: int = Field(
        default=8,
        description="Number of results to return.",
    )


class VectorSearchTool(
    Tool[VectorSearchInput, ToolRunOptions, StringToolOutput],
):
    name = "ocs_ci_search"
    description = (
        "Search the indexed ocs-ci repository: CRDs, StorageClasses, "
        "YAML manifests and templates, PVC/VolumeSnapshot/StorageCluster "
        "patterns, FIO and benchmark workloads (Job/Pod), helpers, and "
        "Ceph/storage test code. Use FIRST whenever the task needs "
        "resource definitions or ocs-ci-accurate manifests — "
        "then mirror retrieved patterns in oc commands."
    )

    def __init__(self, store: VectorStore) -> None:
        super().__init__()
        self._store = store

    @property
    def input_schema(self) -> type[VectorSearchInput]:
        return VectorSearchInput

    async def _run(
        self,
        input: VectorSearchInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        logger.info("[rag-search] query=%r top_k=%d", input.query, input.top_k)
        results = self._store.similarity_search(
            query=input.query,
            top_k=input.top_k,
            file_type=input.file_type,
            module=input.module,
        )
        if not results:
            return StringToolOutput("No results found for this query.")

        parts: list[str] = []
        for i, doc in enumerate(results, 1):
            src = doc.metadata.get("source", "?")
            ext = doc.metadata.get("file_type", "")
            snippet = doc.page_content[:800]
            parts.append(
                f"--- Result {i} ---\n"
                f"File: {src} ({ext})\n"
                f"```\n{snippet}\n```\n"
            )
        return StringToolOutput("\n".join(parts))

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "ocs_ci_search"], creator=self,
        )

    async def clone(self):
        tool = self.__class__(store=self._store)
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool


# ── Tool 2: CodeLookupTool ──


class CodeLookupInput(BaseModel):
    search_term: str = Field(
        ...,
        description=(
            "Exact name to look up: class name, function name, or "
            "file path. Example: 'class PVCInterface', "
            "'def create_ceph_block_pool'."
        ),
    )


class CodeLookupTool(
    Tool[CodeLookupInput, ToolRunOptions, StringToolOutput],
):
    name = "ocs_ci_lookup"
    description = (
        "Look up a specific file, class, function, or template path in "
        "ocs-ci by name. Use when you know the symbol (e.g. "
        "'class PVCInterface', FIO fixture, Jinja template for workload)."
    )

    def __init__(self, store: VectorStore) -> None:
        super().__init__()
        self._store = store

    @property
    def input_schema(self) -> type[CodeLookupInput]:
        return CodeLookupInput

    async def _run(
        self,
        input: CodeLookupInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        logger.info("[rag-lookup] term=%r", input.search_term)
        results = self._store.search_by_name(input.search_term, top_k=10)
        if not results:
            return StringToolOutput(
                f"No results found for '{input.search_term}'.",
            )

        parts: list[str] = []
        for i, doc in enumerate(results, 1):
            src = doc.metadata.get("source", "?")
            snippet = doc.page_content[:1000]
            parts.append(
                f"--- Match {i} ---\n"
                f"File: {src}\n"
                f"```\n{snippet}\n```\n"
            )
        return StringToolOutput("\n".join(parts))

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "ocs_ci_lookup"], creator=self,
        )

    async def clone(self):
        tool = self.__class__(store=self._store)
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool


# ── Tool 3: RepoStatsTool ──


class RepoStatsInput(BaseModel):
    pass


class RepoStatsTool(
    Tool[RepoStatsInput, ToolRunOptions, StringToolOutput],
):
    name = "ocs_ci_stats"
    description = (
        "Get statistics about the indexed ocs-ci codebase — "
        "total files, chunks, last sync time, and file type breakdown."
    )

    def __init__(
        self, store: VectorStore, state_file: str,
    ) -> None:
        super().__init__()
        self._store = store
        self._state_file = state_file

    @property
    def input_schema(self) -> type[RepoStatsInput]:
        return RepoStatsInput

    async def _run(
        self,
        input: RepoStatsInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        from chaosminds.rag.sync_state import SyncState

        stats = self._store.stats()
        state = SyncState.load(self._state_file)

        info = {
            **stats,
            "last_synced_sha": state.last_synced_sha,
            "last_synced_at": (
                state.last_synced_at.isoformat()
                if state.last_synced_at else None
            ),
            "files_tracked": state.files_indexed,
        }
        return StringToolOutput(json.dumps(info, indent=2))

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "ocs_ci_stats"], creator=self,
        )

    async def clone(self):
        tool = self.__class__(
            store=self._store, state_file=self._state_file,
        )
        tool._cache = await self.cache.clone()
        tool.middlewares.extend(self.middlewares)
        return tool
