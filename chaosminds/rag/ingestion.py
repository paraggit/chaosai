"""Repository cloning, file loading, and document chunking."""
from __future__ import annotations

import fnmatch
import hashlib
import logging
from pathlib import Path

from git import Repo
from langchain_community.document_loaders import TextLoader
from langchain_core.documents import Document
from langchain_text_splitters import (
    Language,
    RecursiveCharacterTextSplitter,
)

from chaosminds.config import RagConfig

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 1_000_000  # 1 MB


def clone_or_pull(cfg: RagConfig) -> Repo:
    """Clone the repo if missing, otherwise pull latest."""
    repo_path = Path(cfg.repo_local_path)
    if (repo_path / ".git").exists():
        logger.info("[rag] Pulling %s (branch=%s)", repo_path, cfg.repo_branch)
        repo = Repo(repo_path)
        origin = repo.remotes.origin
        origin.pull(cfg.repo_branch)
        return repo

    logger.info("[rag] Cloning %s → %s", cfg.repo_url, repo_path)
    repo_path.mkdir(parents=True, exist_ok=True)
    return Repo.clone_from(
        cfg.repo_url,
        str(repo_path),
        branch=cfg.repo_branch,
        depth=1,
    )


def _should_skip(rel_path: str, cfg: RagConfig) -> bool:
    for pattern in cfg.exclude_patterns:
        for part in Path(rel_path).parts:
            if fnmatch.fnmatch(part, pattern):
                return True
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_files(cfg: RagConfig) -> list[Path]:
    """Return all indexable files in the repo."""
    repo_root = Path(cfg.repo_local_path)
    ext_set = set(cfg.include_extensions)
    results: list[Path] = []
    for fpath in repo_root.rglob("*"):
        if not fpath.is_file():
            continue
        rel = str(fpath.relative_to(repo_root))
        if _should_skip(rel, cfg):
            continue
        if fpath.suffix not in ext_set:
            continue
        if fpath.stat().st_size > MAX_FILE_SIZE:
            continue
        results.append(fpath)
    return results


def load_and_chunk(
    file_path: Path,
    repo_root: Path,
    cfg: RagConfig,
) -> list[Document]:
    """Load a single file and split into chunks with metadata."""
    rel_path = str(file_path.relative_to(repo_root))
    parts = Path(rel_path).parts
    module = parts[0] if parts else ""
    ext = file_path.suffix
    fhash = _file_hash(file_path)

    try:
        loader = TextLoader(str(file_path), autodetect_encoding=True)
        docs = loader.load()
    except Exception:
        logger.warning("[rag] Failed to load %s, skipping", rel_path)
        return []

    if ext == ".py":
        splitter = RecursiveCharacterTextSplitter.from_language(
            Language.PYTHON,
            chunk_size=cfg.python_chunk_size,
            chunk_overlap=cfg.python_chunk_overlap,
        )
    else:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=cfg.default_chunk_size,
            chunk_overlap=cfg.default_chunk_overlap,
        )

    chunks = splitter.split_documents(docs)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()

    enriched: list[Document] = []
    for i, chunk in enumerate(chunks):
        chunk.metadata = {
            "source": rel_path,
            "file_type": ext,
            "module": module,
            "file_hash": fhash,
            "chunk_index": i,
            "indexed_at": now,
        }
        enriched.append(chunk)

    return enriched
