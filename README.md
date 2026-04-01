# ChaosMinds

Multi-agent chaos engineering system for OpenShift Data Foundation (ODF/OCS) clusters. ChaosMinds uses a **Planner → Executor → Observer** pipeline to plan, execute, and monitor chaos experiments against storage workloads, all driven by a local LLM.

## Architecture

```
User CLI Input
     │
     ▼
┌──────────────────┐
│  Planner Agent   │  LLM converts instruction → 5-phase structured plan
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Executor Agent  │  Generates modular bash script → executes it
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Observer Agent  │  Post-chaos analysis via bob → markdown report
└──────────────────┘
```

### Workflow Summary

```
Planner  →  validates & structures 5-phase plan
             ↓
Executor →  generates & runs multi-phase bash script
             ↓
Observer →  monitors & validates execution → report
```

## 5-Phase Script Structure

The generated script has five distinct phases:

| Phase | Name | Description |
|---|---|---|
| 1 | **Setup** | Create all required resources (PVCs, pods, configs), baseline health check |
| 2 | **Chaos Injection** | Start krknctl chaos, wait until confirmed active, settle period |
| 3 | **Test Execution** | Run target operations in a loop (default 10 iterations) under active chaos |
| 4 | **Chaos Termination** | Stop all chaos containers, clean up krknctl |
| 5 | **Post-Workflow** | Final health check, resource cleanup |

After script execution, the **Observer** (AnalysisAgent) scans the cluster for product bugs via bob.

## Prerequisites

| Dependency | Purpose |
|---|---|
| Python 3.12+ | Runtime |
| [uv](https://docs.astral.sh/uv/) | Package manager |
| [Ollama](https://ollama.com/) | Local LLM backend |
| `oc` (OpenShift CLI) | Cluster access |
| IBM BOB CLI | OCS-CI test execution & analysis |
| `krknctl` | Chaos injection (Kraken) |
| `podman` | Container runtime (used by krknctl) |
| Access to an OpenShift cluster with ODF installed | Target environment |

## Quick Start

### 1. Clone and install

```bash
cd chaosAI
uv sync
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your paths:
#   KUBECONFIG, LLM_ENDPOINT, LLM_MODEL,
#   BOB_CLI_PATH, KRKNCTL_PATH, LOOP_COUNT, etc.
```

### 3. Start Ollama with a model

```bash
ollama pull granite3.3:8b
ollama serve
```

### 4. Run

```bash
uv run chaosminds "test PVC creation under OSD pod chaos"
```

This will:
1. Ask the LLM to generate a 5-phase structured plan
2. Convert the plan into a bash script
3. Execute the script (setup → chaos → looped operations → teardown)
4. Run post-chaos analysis via bob
5. Clean up test resources
6. Print a final report

### Customize loop count

```bash
uv run chaosminds --loop-count 5 "test PVC creation under OSD pod chaos"
```

### Run modes

| Flag | Behavior |
|---|---|
| *(none)* | **Default** — Planner → Executor (script) → Observer (analysis) |
| `--script-only` | Generate a bash script for review (not executed) |
| `--agent-mode` | Full LLM-per-step agent loop (slower, adaptive) |

**Generate a script for review (no execution):**

```bash
uv run chaosminds --script-only "test PVC creation under OSD pod chaos"
# Review the generated script:
cat scripts/chaos_20260330_131500.sh
# Run it manually:
./scripts/chaos_20260330_131500.sh
```

**Agent mode** — full multi-agent LLM loop (slower, but adaptive):

```bash
uv run chaosminds --agent-mode "test PVC creation under chaos"
```

**Comparison:**

| | Default | --script-only | --agent-mode |
|---|---|---|---|
| LLM calls | **2** (plan + analysis) | **1** (plan) | ~16-20 |
| Execution | Bash script (5 phases) | Manual review | LLM per step |
| Loop support | Yes (N iterations) | Yes (in script) | No |
| Analysis | Prompt → bob | None | Prompt → bob |
| Runtime | **2-5 min** | manual | 8-15 min |

## Project Structure

```
chaosminds/
├── main.py                  CLI entry (Planner → Executor → Observer)
├── config.py                Loads .env + CLI overrides
├── state.py                 WorkflowState dataclass (shared context)
├── supervisor.py            Agent-mode orchestration loop
├── script_generator.py      Converts structured plan → multi-phase bash script
├── agents/
│   ├── planner.py           PlannerAgent — 5-phase structured plan from LLM
│   ├── analysis.py          AnalysisAgent (Observer) — post-chaos bug scan via bob
│   ├── executor.py          ExecutorAgent — runs BOB CLI / oc (agent-mode)
│   ├── chaos.py             ChaosAgent — injects faults via krknctl (agent-mode)
│   ├── waiter.py            WaitAgent — polls chaos completion (agent-mode)
│   └── monitor.py           ClusterMonitorAgent — health checks (agent-mode)
├── tools/
│   ├── kubectl_tool.py      oc wrapper (BeeAI Tool)
│   ├── bob_cli_tool.py      IBM BOB CLI wrapper (BeeAI Tool)
│   ├── krknctl_tool.py      krknctl inject + status wrappers
│   └── cluster_health.py    Pod/PVC/Ceph/Node health checker
└── prompts/
    ├── planner_system.txt         System prompt for PlannerAgent (5-phase output)
    ├── post_analysis_system.txt   LLM prompt for Observer analysis (→ bob)
    ├── health_rules.txt           Configurable cluster health rules
    ├── executor_system.txt        System prompt for ExecutorAgent
    ├── chaos_system.txt           System prompt for ChaosAgent
    ├── waiter_system.txt          System prompt for WaitAgent
    └── monitor_system.txt         System prompt for ClusterMonitorAgent
logs/                        Per-run log files (auto-created, git-ignored)
└── run_YYYYMMDD_HHMMSS.log
scripts/                     Generated bash scripts (auto-created, git-ignored)
└── chaos_YYYYMMDD_HHMMSS.sh
analysis/                    Post-chaos analysis reports (auto-created)
└── analysis_YYYYMMDD_HHMMSS.md
```

## Data Flow (Default Mode)

```
1. Planner Agent:
     CLI instruction → LLM → 5-phase plan (JSON)
       { setup: [...], chaos: {...}, test_ops: [...], post: [...] }

2. Executor Agent:
     Plan → multi-phase bash script → execute
       Phase 1: Setup — create resources, baseline health
       Phase 2: Chaos — krknctl inject, confirm active, settle
       Phase 3: Test Ops — loop N times under active chaos
       Phase 4: Teardown — stop chaos, clean krknctl
       Phase 5: Post — final health check

3. Observer Agent:
     LLM generates analysis prompt → AnalysisAgent
     Each check passed to bob (oc fallback)
     Findings classified as BUG or WARN → verdict
     Report saved to analysis/analysis_<run_id>.md

4. Resource cleanup (delete chaos-test-* snapshots, PVCs, pods)

5. Final report with script result, analysis verdict, findings
```

### Phase 3: Test Operations Loop

Operations run in a loop under active chaos. Each iteration creates uniquely named resources using the loop index:

```
for i in 1..LOOP_COUNT:
    oc apply PVC chaos-test-pvc-$i
    oc wait PVC bound
    oc verify PVC status
```

`LOOP_COUNT` defaults to 10. Override via `--loop-count` or `LOOP_COUNT` in `.env`.

## Configuration Reference

All settings can be provided via `.env` or CLI flags. CLI flags take precedence.

| Variable | CLI Flag | Default | Description |
|---|---|---|---|
| `KUBECONFIG` | `--kubeconfig` | — | Path to kubeconfig |
| `LLM_ENDPOINT` | `--llm-endpoint` | `http://localhost:11434` | Ollama / local LLM URL |
| `LLM_MODEL` | `--llm-model` | `granite3.1-dense:8b` | Model name |
| `BOB_CLI_PATH` | `--bob-cli` | `bob` | BOB CLI binary path |
| `KRKNCTL_PATH` | `--krknctl` | `krknctl` | krknctl binary path |
| `OC_PATH` | `--oc` | `oc` | OpenShift CLI (oc) binary path |
| `SCENARIO_PLAN_PATH` | `--scenario-plan` | `./scenario_plan.json` | Chaos scenario file |
| `LOOP_COUNT` | `--loop-count` | `10` | Number of test iterations |
| `CHAOS_TIMEOUT` | — | `600` | Max wait for chaos completion (seconds) |
| `CHAOS_POLL_INTERVAL` | — | `15` | Polling interval (seconds) |
| `CHAOS_SETTLE_TIME` | — | `30` | Wait after chaos active before operations (seconds) |
| `COLLECT_MUST_GATHER` | — | `false` | Collect ODF must-gather logs before cleanup |
| `LOG_LEVEL` | — | `INFO` | Logging verbosity |

## Logging

Every run creates a timestamped log file under `logs/`:

```
logs/run_20260330_111842.log
```

The log file captures **everything** at DEBUG level regardless of the console `LOG_LEVEL`:

| What gets logged | Example |
|---|---|
| All `oc` commands + full stdout/stderr | `[oc] command: oc apply -f -` |
| All `bob` CLI executions + output | `[bob] command: bob "check pods..." -y` |
| All `krknctl` inject/list commands | `[krknctl] command: krknctl random run ...` |
| LLM prompts sent by each agent | `[PlannerAgent] Sending prompt to LLM (1423 chars)` |
| LLM raw responses | `[PlannerAgent] Raw LLM response: ...` |
| Script generation details | `[ScriptGenerator] Generated 180 lines, 5230 bytes` |
| Script execution output | Streamed to console and log file |
| Analysis findings + classification | `[analysis] BUG: Ceph crashes — 3 unarchived` |

Console output uses `LOG_LEVEL` from `.env` (default `INFO`). Set `LOG_LEVEL=DEBUG` for full verbosity.

## Health Rules

Cluster health checks are **rule-driven** via `chaosminds/prompts/health_rules.txt`. Edit this file to customize what ChaosMinds considers HEALTHY, DEGRADED, or CRITICAL.

### Rule categories

| Section | Effect |
|---|---|
| `critical` | Any matching condition → **CRITICAL** (chaos steps are skipped) |
| `degraded` | Any matching condition → **DEGRADED** (proceed with warnings) |
| `healthy` | All conditions must be true for HEALTHY status |
| `expected_pods` | ODF pods that must exist and be Running |
| `critical_ceph_checks` | Ceph health checks that escalate to CRITICAL |

### Rule syntax

Rules use simple conditions: `variable operator value`

```
pods_failed >= 3          # numeric comparison
ceph_status == HEALTH_ERR # string equality
nodes_not_ready >= 2      # numeric threshold
```

## Post-Chaos Analysis (Observer)

After script execution, the **Observer** (AnalysisAgent) scans the cluster:

1. The LLM reads `chaosminds/prompts/post_analysis_system.txt` and generates an analysis plan
2. Each check is passed to **bob** as a natural language prompt (with kubeconfig)
3. Bob's verbose output is cleaned via noise stripping before classification
4. Findings are classified as **BUG** (product defect) or **WARN** (expected chaos effect)
5. A markdown report is saved to `analysis/analysis_<run_id>.md`

### What gets checked

| Check | bob prompt | BUG if |
|---|---|---|
| Pod health | "list pods in openshift-storage..." | CrashLoopBackOff, Error |
| Component restarts | "check restart counts..." | restarts >= 3 |
| Ceph health | "run ceph health detail..." | HEALTH_ERR, OSD_DOWN |
| Ceph crashes | "run ceph crash ls-new..." | Unarchived crash entries |
| StorageCluster | "check storagecluster status..." | Error / Failed phase |
| PVC binding | "list PVCs in openshift-storage..." | Lost PVCs |
| Node readiness | "list node status..." | NotReady |
| Warning events | "list warning events..." | FailedMount, OOMKilled |

## Resource Cleanup

After all workflow steps complete, ChaosMinds automatically deletes:
- All `chaos-test-*` VolumeSnapshots
- All `chaos-test-*` PVCs
- All `chaos-test-*` pods

Set `COLLECT_MUST_GATHER=true` in `.env` to collect ODF diagnostic logs before cleanup.
