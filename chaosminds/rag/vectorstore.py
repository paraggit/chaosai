"""ChromaDB vector store wrapper with batched operations."""
from __future__ import annotations

import logging
from collections import Counter

import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_ollama import OllamaEmbeddings

from chaosminds.config import RagConfig

logger = logging.getLogger(__name__)

BATCH_SIZE = 150


class VectorStore:
    """Manages a persistent ChromaDB collection for ocs-ci."""

    def __init__(self, cfg: RagConfig) -> None:
        self._cfg = cfg
        self._embeddings = OllamaEmbeddings(
            model=cfg.embedding_model,
            base_url=cfg.embedding_base_url,
        )
        self._client = chromadb.PersistentClient(
            path=cfg.persist_directory,
        )
        self._init_chroma()

    def _init_chroma(self) -> None:
        """(Re-)create the LangChain Chroma wrapper."""
        self._chroma = Chroma(
            client=self._client,
            collection_name=self._cfg.collection_name,
            embedding_function=self._embeddings,
        )

    def add_documents(self, docs: list[Document]) -> int:
        """Add documents in batches. Returns count added."""
        if not docs:
            return 0
        total = 0
        for start in range(0, len(docs), BATCH_SIZE):
            batch = docs[start:start + BATCH_SIZE]
            ids = [
                f"{d.metadata['source']}::chunk_{d.metadata['chunk_index']}"
                for d in batch
            ]
            self._chroma.add_documents(batch, ids=ids)
            total += len(batch)
            logger.info(
                "[vectorstore] Added batch %d-%d (%d docs)",
                start, start + len(batch), len(batch),
            )
        return total

    def delete_by_source(self, source_path: str) -> int:
        """Delete all chunks for a given source file."""
        collection = self._client.get_collection(
            self._cfg.collection_name,
        )
        results = collection.get(
            where={"source": source_path},
        )
        ids = results.get("ids", [])
        if ids:
            collection.delete(ids=ids)
            logger.info(
                "[vectorstore] Deleted %d chunks for %s",
                len(ids), source_path,
            )
        return len(ids)

    def similarity_search(
        self,
        query: str,
        top_k: int | None = None,
        file_type: str | None = None,
        module: str | None = None,
    ) -> list[Document]:
        """Semantic search with optional metadata filters."""
        k = top_k or self._cfg.top_k
        where_filter: dict | None = None
        conditions: list[dict] = []
        if file_type:
            conditions.append({"file_type": file_type})
        if module:
            conditions.append({"module": module})

        if len(conditions) == 1:
            where_filter = conditions[0]
        elif len(conditions) > 1:
            where_filter = {"$and": conditions}

        kwargs: dict = {"k": k}
        if where_filter:
            kwargs["filter"] = where_filter

        return self._chroma.similarity_search(query, **kwargs)

    def search_by_name(
        self,
        term: str,
        top_k: int = 10,
    ) -> list[Document]:
        """Keyword search in page_content for exact names."""
        collection = self._client.get_collection(
            self._cfg.collection_name,
        )
        results = collection.get(
            where_document={"$contains": term},
            limit=top_k,
            include=["documents", "metadatas"],
        )
        docs: list[Document] = []
        for content, meta in zip(
            results.get("documents", []),
            results.get("metadatas", []),
        ):
            docs.append(Document(
                page_content=content,
                metadata=meta or {},
            ))
        return docs

    def stats(self) -> dict:
        """Return index statistics."""
        collection = self._client.get_collection(
            self._cfg.collection_name,
        )
        total_chunks = collection.count()

        all_meta = collection.get(include=["metadatas"])
        metadatas = all_meta.get("metadatas", [])

        sources: set[str] = set()
        ext_counter: Counter[str] = Counter()
        module_counter: Counter[str] = Counter()

        for m in metadatas:
            if not m:
                continue
            src = m.get("source", "")
            if src:
                sources.add(src)
            ext_counter[m.get("file_type", "")] += 1
            module_counter[m.get("module", "")] += 1

        return {
            "total_chunks": total_chunks,
            "unique_files": len(sources),
            "file_types": dict(ext_counter.most_common()),
            "modules": dict(module_counter.most_common(10)),
        }

    def reset(self) -> None:
        """Wipe the collection and re-initialize."""
        try:
            self._client.delete_collection(
                self._cfg.collection_name,
            )
            logger.info(
                "[vectorstore] Deleted collection %s",
                self._cfg.collection_name,
            )
        except ValueError:
            pass
        self._init_chroma()
