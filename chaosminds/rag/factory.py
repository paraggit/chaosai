"""Build BeeAI RAG tools (ocs-ci codebase) for multi-agent workflows."""
from __future__ import annotations

import logging
from typing import Any

from chaosminds.config import RagConfig

logger = logging.getLogger(__name__)


def build_rag_tools(cfg: RagConfig) -> list[Any]:
    """Return ``ocs_ci_search``, ``ocs_ci_lookup``, ``ocs_ci_stats`` tools.

    On failure (missing index, chromadb, embeddings), logs a warning and
    returns an empty list so agents still run without RAG.
    """
    try:
        from chaosminds.rag.tools import (
            CodeLookupTool,
            RepoStatsTool,
            VectorSearchTool,
        )
        from chaosminds.rag.vectorstore import VectorStore
    except ImportError as exc:
        logger.warning("[rag] RAG dependencies unavailable: %s", exc)
        return []

    try:
        store = VectorStore(cfg)
    except Exception as exc:
        logger.warning(
            "[rag] Could not open vector store (%s). "
            "Run: chaosminds-rag ingest",
            exc,
        )
        return []

    return [
        VectorSearchTool(store),
        CodeLookupTool(store),
        RepoStatsTool(store, cfg.state_file),
    ]
