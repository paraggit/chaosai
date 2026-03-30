from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class AppConfig(BaseModel):
    """Application configuration loaded from .env and CLI args."""

    kubeconfig: str
    llm_endpoint: str = "http://localhost:11434"
    llm_model: str = "granite3.1-dense:8b"
    bob_cli_path: str = "bob"
    krknctl_path: str = "krknctl"
    oc_path: str = "oc"
    scenario_plan_path: str = "./scenario_plan.json"
    scenario_plan: dict | list = Field(default_factory=dict)
    chaos_timeout: int = 600
    chaos_poll_interval: int = 15
    log_level: str = "INFO"

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
            "log_level": os.getenv("LOG_LEVEL", "INFO"),
        }

        merged = {k: v for k, v in env_values.items() if v}
        merged.update({k: v for k, v in cli_overrides.items() if v is not None})

        plan_path = Path(merged.get("scenario_plan_path", "./scenario_plan.json"))
        if plan_path.exists():
            merged["scenario_plan"] = json.loads(plan_path.read_text())
        else:
            merged["scenario_plan"] = []

        return cls(**merged)
