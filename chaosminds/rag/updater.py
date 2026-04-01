"""Incremental updater — git diff → selective re-index."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from git import Repo

from chaosminds.config import RagConfig
from chaosminds.rag.ingestion import (
    _should_skip,
    clone_or_pull,
    load_and_chunk,
)
from chaosminds.rag.sync_state import SyncState
from chaosminds.rag.vectorstore import VectorStore

logger = logging.getLogger(__name__)


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def full_ingest(
    cfg: RagConfig,
    store: VectorStore,
    progress_cb: callable | None = None,
) -> SyncState:
    """Wipe and re-index the entire repo from scratch."""
    from chaosminds.rag.ingestion import collect_files

    repo = clone_or_pull(cfg)
    repo_root = Path(cfg.repo_local_path)

    try:
        store.reset()
    except Exception:
        pass

    files = collect_files(cfg)
    logger.info("[rag] Full ingest: %d files", len(files))

    state = SyncState()
    total_chunks = 0

    for i, fpath in enumerate(files):
        rel = str(fpath.relative_to(repo_root))
        chunks = load_and_chunk(fpath, repo_root, cfg)
        if chunks:
            store.add_documents(chunks)
            total_chunks += len(chunks)
            state.file_hashes[rel] = _hash_file(fpath)

        if progress_cb:
            progress_cb(i + 1, len(files), rel)

    state.last_synced_sha = repo.head.commit.hexsha
    state.last_synced_at = datetime.now(timezone.utc)
    state.files_indexed = len(state.file_hashes)
    state.total_chunks = total_chunks
    state.save(cfg.state_file)

    logger.info(
        "[rag] Full ingest complete: %d files, %d chunks",
        state.files_indexed, total_chunks,
    )
    return state


def incremental_update(
    cfg: RagConfig,
    store: VectorStore,
    progress_cb: callable | None = None,
) -> SyncState:
    """Pull latest and re-index only changed files."""
    state = SyncState.load(cfg.state_file)
    repo = clone_or_pull(cfg)
    repo_root = Path(cfg.repo_local_path)
    new_sha = repo.head.commit.hexsha

    if state.last_synced_sha is None:
        logger.info("[rag] No previous sync — running full ingest")
        return full_ingest(cfg, store, progress_cb)

    if state.last_synced_sha == new_sha:
        logger.info("[rag] Already up to date at %s", new_sha[:12])
        return state

    diff_output = repo.git.diff(
        "--name-status",
        state.last_synced_sha,
        new_sha,
    )
    if not diff_output.strip():
        logger.info("[rag] No file changes detected")
        state.last_synced_sha = new_sha
        state.last_synced_at = datetime.now(timezone.utc)
        state.save(cfg.state_file)
        return state

    ext_set = set(cfg.include_extensions)
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []

    for line in diff_output.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status, filepath = parts[0].strip(), parts[1].strip()
        if _should_skip(filepath, cfg):
            continue
        if Path(filepath).suffix not in ext_set:
            continue

        if status.startswith("A"):
            added.append(filepath)
        elif status.startswith("M") or status.startswith("R"):
            modified.append(filepath)
        elif status.startswith("D"):
            deleted.append(filepath)

    logger.info(
        "[rag] Changes: +%d ~%d -%d files",
        len(added), len(modified), len(deleted),
    )

    total_ops = len(deleted) + len(modified) + len(added)
    done = 0

    for rel in deleted:
        store.delete_by_source(rel)
        state.file_hashes.pop(rel, None)
        done += 1
        if progress_cb:
            progress_cb(done, total_ops, f"DEL {rel}")

    for rel in modified:
        store.delete_by_source(rel)
        fpath = repo_root / rel
        if fpath.exists():
            chunks = load_and_chunk(fpath, repo_root, cfg)
            if chunks:
                store.add_documents(chunks)
                state.total_chunks += len(chunks)
                state.file_hashes[rel] = _hash_file(fpath)
        done += 1
        if progress_cb:
            progress_cb(done, total_ops, f"MOD {rel}")

    for rel in added:
        fpath = repo_root / rel
        if fpath.exists():
            chunks = load_and_chunk(fpath, repo_root, cfg)
            if chunks:
                store.add_documents(chunks)
                state.total_chunks += len(chunks)
                state.file_hashes[rel] = _hash_file(fpath)
        done += 1
        if progress_cb:
            progress_cb(done, total_ops, f"ADD {rel}")

    state.last_synced_sha = new_sha
    state.last_synced_at = datetime.now(timezone.utc)
    state.files_indexed = len(state.file_hashes)
    state.save(cfg.state_file)

    logger.info(
        "[rag] Incremental update complete: %d files indexed",
        state.files_indexed,
    )
    return state
