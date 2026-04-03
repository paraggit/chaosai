from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, PrivateAttr


class RagConfig(BaseModel):
    """RAG pipeline configuration for ocs-ci codebase."""

    repo_url: str = "https://github.com/red-hat-storage/ocs-ci.git"
    repo_local_path: str = "./data/ocs-ci"
    repo_branch: str = "master"
    persist_directory: str = "./data/chroma_db"
    collection_name: str = "ocs_ci_codebase"
    embedding_model: str = "nomic-embed-text"
    embedding_base_url: str = "http://localhost:11434"
    top_k: int = 8
    score_threshold: float = 0.3
    python_chunk_size: int = 1500
    python_chunk_overlap: int = 200
    default_chunk_size: int = 1200
    default_chunk_overlap: int = 150
    state_file: str = "./data/sync_state.json"
    include_extensions: list[str] = Field(default_factory=lambda: [
        ".py", ".yaml", ".yml", ".md", ".rst",
        ".j2", ".json", ".cfg", ".sh",
    ])
    exclude_patterns: list[str] = Field(default_factory=lambda: [
        "__pycache__", ".git", ".tox", "*.pyc",
        "*.egg-info", ".venv", ".eggs",
    ])


class AppConfig(BaseModel):
    """Application configuration loaded from .env and CLI args."""

    kubeconfig: str
    llm_endpoint: str = "http://localhost:11434"
    llm_model: str = "granite3.1-dense:8b"
    bob_cli_path: str = "bob"
    krknctl_path: str = "krknctl"
    oc_path: str = "oc"
    scenario_plan_path: str = "./scenario_plan.json"
    _scenario_plan_cache: dict | list | None = PrivateAttr(
        default=None,
    )
    chaos_timeout: int = 600
    chaos_poll_interval: int = 15
    chaos_settle_time: int = 30
    chaos_max_parallel: int = 4
    # ToolCallingAgent default is 10; PVC/YAML steps need RAG + validate + several oc calls + final_answer
    executor_max_iterations: int = 25
    loop_count: int = 10
    collect_must_gather: bool = False
    log_level: str = "INFO"
    rag: RagConfig = Field(default_factory=RagConfig)

    @property
    def scenario_plan(self) -> dict | list:
        """Load scenario catalog from disk on first access."""
        if self._scenario_plan_cache is not None:
            return self._scenario_plan_cache
        plan_path = Path(self.scenario_plan_path)
        if plan_path.exists():
            try:
                self._scenario_plan_cache = json.loads(
                    plan_path.read_text(encoding="utf-8"),
                )
            except (json.JSONDecodeError, OSError):
                self._scenario_plan_cache = []
        else:
            self._scenario_plan_cache = []
        return self._scenario_plan_cache

    @classmethod
    def load(cls, env_file: str = ".env", **cli_overrides) -> AppConfig:
        """Load config from .env, then overlay any CLI overrides."""
        load_dotenv(env_file)

        env_values = {
            "kubeconfig": os.getenv("KUBECONFIG", ""),
            "llm_endpoint": os.getenv("LLM_ENDPOINT", "http://localhost:11434"),
            "llm_model": os.getenv("LLM_MODEL", "granite3.1-dense:8b"),
            "bob_cli_path": os.getenv("BOB_CLI_PATH", "bob"),
            "krknctl_path": os.getenv("KRKNCTL_PATH", "krknctl"),
            "oc_path": os.getenv("OC_PATH", "oc"),
            "scenario_plan_path": os.getenv("SCENARIO_PLAN_PATH", "./scenario_plan.json"),
            "chaos_timeout": int(os.getenv("CHAOS_TIMEOUT", "600")),
            "chaos_poll_interval": int(os.getenv("CHAOS_POLL_INTERVAL", "15")),
            "chaos_settle_time": int(os.getenv("CHAOS_SETTLE_TIME", "30")),
            "chaos_max_parallel": int(os.getenv("CHAOS_MAX_PARALLEL", "4")),
            "executor_max_iterations": int(os.getenv("EXECUTOR_MAX_ITERATIONS", "25")),
            "loop_count": int(os.getenv("LOOP_COUNT", "10")),
            "collect_must_gather": os.getenv("COLLECT_MUST_GATHER", "false").lower() in ("true", "1", "yes"),
            "log_level": os.getenv("LOG_LEVEL", "INFO"),
        }

        rag_overrides: dict[str, str | int | float] = {}
        for key, env_key, cast in [
            ("repo_url", "RAG_REPO_URL", str),
            ("repo_local_path", "RAG_REPO_LOCAL_PATH", str),
            ("repo_branch", "RAG_REPO_BRANCH", str),
            ("persist_directory", "RAG_PERSIST_DIR", str),
            ("collection_name", "RAG_COLLECTION", str),
            ("embedding_model", "RAG_EMBED_MODEL", str),
            ("embedding_base_url", "RAG_EMBED_URL", str),
            ("top_k", "RAG_TOP_K", int),
            ("score_threshold", "RAG_SCORE_THRESHOLD", float),
        ]:
            val = os.getenv(env_key)
            if val:
                rag_overrides[key] = cast(val)

        merged = {k: v for k, v in env_values.items() if v}
        merged.update({k: v for k, v in cli_overrides.items() if v is not None})

        if rag_overrides:
            merged["rag"] = RagConfig(**rag_overrides)

        return cls(**merged)
