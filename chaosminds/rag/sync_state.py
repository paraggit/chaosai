"""Sync state persistence for incremental updates."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SyncState(BaseModel):
    last_synced_sha: str | None = None
    last_synced_at: datetime | None = None
    files_indexed: int = 0
    total_chunks: int = 0
    file_hashes: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> SyncState:
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text())
            return cls(**data)
        return cls()

    def save(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.model_dump_json(indent=2))
        logger.info("[sync] State saved to %s", path)
