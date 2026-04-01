"""CLI for the ocs-ci RAG pipeline."""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from chaosminds.config import AppConfig

console = Console()
logger = logging.getLogger(__name__)

RAG_SYSTEM_PROMPT = """\
You are an expert assistant for the ocs-ci test automation framework \
(OpenShift Container Storage / OpenShift Data Foundation CI).

Your knowledge comes from the ocs-ci codebase retrieved via vector search.

Domain context:
- ocs-ci is a Python pytest-based framework for ODF testing
- It tests Ceph storage (OSDs, MONs, MDS, RGW), NooBaa/MCG, CSI drivers
- Key directories: ocs_ci/ocs/ (core libs), ocs_ci/helpers/ (utilities), \
tests/ (pytest cases), conf/ (YAML configs), ocs_ci/templates/ (Jinja2)
- Common patterns: pytest fixtures, markers, resource factories, OCP CLI wrappers

Rules:
1. ALWAYS ground answers in retrieved code — cite file paths
2. If retrieved context lacks the answer, say so explicitly
3. Show relevant code snippets from retrieved chunks
4. Explain code in context of ODF/OCS testing patterns
5. For CRDs, StorageClasses, YAML, and workloads (e.g. FIO Jobs), prefer \
patterns shown in ocs-ci templates and tests over generic Kubernetes examples
"""


def _setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(handler)


def _get_store(config: AppConfig):
    from chaosminds.rag.vectorstore import VectorStore
    return VectorStore(config.rag)


def cmd_ingest(config: AppConfig) -> None:
    """Full clone + index entire repo from scratch."""
    from chaosminds.rag.updater import full_ingest

    store = _get_store(config)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Ingesting ocs-ci...", total=None)

        def on_progress(done: int, total: int, name: str):
            progress.update(
                task,
                description=f"[{done}/{total}] {name}",
                total=total,
                completed=done,
            )

        state = full_ingest(config.rag, store, progress_cb=on_progress)

    console.print(
        f"\n[bold green]Ingest complete:[/] "
        f"{state.files_indexed} files, "
        f"{state.total_chunks} chunks, "
        f"SHA {state.last_synced_sha[:12] if state.last_synced_sha else '?'}",
    )


def cmd_update(config: AppConfig) -> None:
    """Pull latest + re-index only changed files."""
    from chaosminds.rag.updater import incremental_update

    store = _get_store(config)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Updating...", total=None)

        def on_progress(done: int, total: int, name: str):
            progress.update(
                task,
                description=f"[{done}/{total}] {name}",
                total=total,
                completed=done,
            )

        state = incremental_update(
            config.rag, store, progress_cb=on_progress,
        )

    console.print(
        f"\n[bold green]Update complete:[/] "
        f"{state.files_indexed} files tracked, "
        f"SHA {state.last_synced_sha[:12] if state.last_synced_sha else '?'}",
    )


def _build_agent(config: AppConfig):
    """Create a ToolCallingAgent with ThinkTool for RAG Q&A."""
    from beeai_framework.adapters.ollama.backend.chat import (
        OllamaChatModel,
    )
    from beeai_framework.agents.tool_calling.agent import (
        ToolCallingAgent,
    )
    from beeai_framework.tools.think import ThinkTool

    from chaosminds.agents._prompts import (
        system_prompt_template,
    )

    llm = OllamaChatModel(
        model_id=config.llm_model,
        settings={"base_url": config.llm_endpoint},
    )
    return ToolCallingAgent(
        llm=llm,
        tools=[ThinkTool()],
        templates={
            "system": system_prompt_template(RAG_SYSTEM_PROMPT),
        },
    )


def cmd_query(config: AppConfig, question: str) -> None:
    """One-shot RAG query."""
    store = _get_store(config)
    results = store.similarity_search(question)

    if not results:
        console.print("[yellow]No relevant results found.[/]")
        return

    context_parts: list[str] = []
    for doc in results:
        src = doc.metadata.get("source", "?")
        context_parts.append(
            f"### {src}\n```\n{doc.page_content[:600]}\n```"
        )
    context = "\n\n".join(context_parts)

    agent = _build_agent(config)
    prompt = (
        f"Context from ocs-ci codebase:\n\n{context}\n\n"
        f"Question: {question}"
    )

    async def _ask():
        return await agent.run(prompt)

    output = asyncio.run(_ask())
    console.print(f"\n{output.last_message.text}")


def cmd_chat(config: AppConfig) -> None:
    """Interactive REPL for Q&A."""
    store = _get_store(config)
    agent = _build_agent(config)

    console.print(
        "[bold]ocs-ci RAG Chat[/] — type 'exit' or 'quit' to leave\n",
    )

    while True:
        try:
            q = console.input("[bold blue]> [/]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in ("exit", "quit", "q"):
            break
        if not q:
            continue

        results = store.similarity_search(q)
        context_parts = []
        for doc in results:
            src = doc.metadata.get("source", "?")
            context_parts.append(
                f"### {src}\n```\n{doc.page_content[:600]}\n```"
            )
        context = (
            "\n\n".join(context_parts)
            if context_parts else "(no context found)"
        )

        prompt = (
            f"Context from ocs-ci codebase:\n\n{context}\n\n"
            f"Question: {q}"
        )
        async def _ask():
            return await agent.run(prompt)

        output = asyncio.run(_ask())
        console.print(f"\n{output.last_message.text}\n")


def cmd_status(config: AppConfig) -> None:
    """Print vector DB stats + last sync info."""
    from chaosminds.rag.sync_state import SyncState

    store = _get_store(config)

    try:
        stats = store.stats()
    except Exception:
        console.print("[yellow]No index found. Run 'ingest' first.[/]")
        return

    state = SyncState.load(config.rag.state_file)

    table = Table(title="ocs-ci RAG Index Status")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Total chunks", str(stats["total_chunks"]))
    table.add_row("Unique files", str(stats["unique_files"]))
    table.add_row(
        "Last sync SHA",
        state.last_synced_sha[:12] if state.last_synced_sha else "—",
    )
    table.add_row(
        "Last sync at",
        state.last_synced_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        if state.last_synced_at else "—",
    )
    table.add_row("Files tracked", str(state.files_indexed))

    file_types = stats.get("file_types", {})
    if file_types:
        ft_str = ", ".join(
            f"{ext}: {cnt}" for ext, cnt in file_types.items()
        )
        table.add_row("File types", ft_str)

    console.print(table)


def cmd_reset(config: AppConfig) -> None:
    """Wipe vector DB + sync state."""
    store = _get_store(config)
    try:
        store.reset()
    except Exception:
        pass

    state_path = Path(config.rag.state_file)
    if state_path.exists():
        state_path.unlink()

    db_path = Path(config.rag.persist_directory)
    if db_path.exists():
        shutil.rmtree(db_path)

    console.print("[bold red]Reset complete.[/] Index and state wiped.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="chaosminds-rag",
        description="ChaosMinds — ocs-ci RAG pipeline",
    )
    parser.add_argument(
        "--env-file", default=".env",
        help="Path to .env file (default: .env)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ingest", help="Full clone + index from scratch")
    sub.add_parser("update", help="Pull + incremental re-index")

    p_query = sub.add_parser("query", help="One-shot RAG query")
    p_query.add_argument("question", type=str)

    sub.add_parser("chat", help="Interactive Q&A REPL")
    sub.add_parser("status", help="Show index stats")
    sub.add_parser("reset", help="Wipe index + sync state")

    args = parser.parse_args(argv)

    config = AppConfig.load(env_file=args.env_file)
    _setup_logging(config.log_level)

    match args.command:
        case "ingest":
            cmd_ingest(config)
        case "update":
            cmd_update(config)
        case "query":
            cmd_query(config, args.question)
        case "chat":
            cmd_chat(config)
        case "status":
            cmd_status(config)
        case "reset":
            cmd_reset(config)


if __name__ == "__main__":
    main()
