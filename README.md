# ChaosMinds

Multi-agent chaos engineering system for OpenShift Data Foundation (ODF/OCS) clusters. ChaosMinds uses a supervisor-orchestrated pipeline of AI agents to plan, execute, and monitor chaos experiments against storage workloads, all driven by a local LLM.

## Architecture

```
User CLI Input
     │
     ▼
┌─────────────┐
│  Supervisor  │  orchestrates flow, holds state
└─────┬───────┘
      │
      ├──▶ PlannerAgent         interprets instruction → step plan
      ├──▶ ExecutorAgent         runs BOB CLI / oc commands
      ├──▶ ChaosAgent           injects faults via krknctl
      ├──▶ WaitAgent            polls chaos run completion
      └──▶ ClusterMonitorAgent  watches pod/PVC/node/Ceph health
```

Each agent wraps a BeeAI `ToolCallingAgent` backed by a local LLM (Ollama), with domain-specific tools and system prompts.

## Prerequisites

| Dependency | Purpose |
|---|---|
| Python 3.12+ | Runtime |
| [uv](https://docs.astral.sh/uv/) | Package manager |
| [Ollama](https://ollama.com/) | Local LLM backend |
| `oc` (OpenShift CLI) | Cluster access |
| IBM BOB CLI | OCS-CI test execution |
| `krknctl` | Chaos injection (Kraken) |
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
#   BOB_CLI_PATH, KRKNCTL_PATH, etc.
```

### 3. Start Ollama with a model

```bash
ollama pull granite3.1-dense:8b
ollama serve
```

### 4. Run

```bash
uv run chaosminds "test PVC snapshot creation under various chaos scenarios"
```

Or with explicit flags:

```bash
uv run chaosminds \
  "test PVC snapshot creation under chaos" \
  --kubeconfig ~/.kube/config \
  --llm-endpoint http://localhost:11434 \
  --llm-model granite3.1-dense:8b \
  --scenario-plan ./scenario_plan.json
```

## Project Structure

```
chaosminds/
├── main.py                  CLI entry point
├── config.py                Loads .env + CLI overrides
├── state.py                 WorkflowState dataclass (shared context)
├── supervisor.py            Orchestration loop (plan → execute → chaos → wait → monitor)
├── agents/
│   ├── planner.py           PlannerAgent — produces step plan from instruction
│   ├── executor.py          ExecutorAgent — runs BOB CLI / oc
│   ├── chaos.py             ChaosAgent — injects faults via krknctl
│   ├── waiter.py            WaitAgent — polls chaos completion
│   └── monitor.py           ClusterMonitorAgent — checks cluster health
├── tools/
│   ├── kubectl_tool.py      oc wrapper (BeeAI Tool)
│   ├── bob_cli_tool.py      IBM BOB CLI wrapper (BeeAI Tool)
│   ├── krknctl_tool.py      krknctl inject + status wrappers
│   └── cluster_health.py    Pod/PVC/Ceph/Node health checker
└── prompts/
    ├── planner_system.txt   System prompt for PlannerAgent
    ├── executor_system.txt  System prompt for ExecutorAgent
    ├── chaos_system.txt     System prompt for ChaosAgent
    ├── waiter_system.txt    System prompt for WaitAgent
    ├── monitor_system.txt   System prompt for ClusterMonitorAgent
    └── health_rules.txt     Configurable cluster health rules (thresholds & ODF checks)
logs/                        Per-run log files (auto-created, git-ignored)
└── run_YYYYMMDD_HHMMSS.log
```

## Scenario Plan

The `scenario_plan.json` defines chaos stages the PlannerAgent can draw from:

```json
{
  "scenario": "pvc-snapshot-chaos",
  "stages": [
    {
      "id": 1,
      "chaos_type": "pod_kill",
      "target": "ceph-osd",
      "namespace": "openshift-storage",
      "duration": "60s",
      "config": { "label_selector": "app=rook-ceph-osd", "kill_count": 1 }
    }
  ]
}
```

Supported chaos types: `pod_kill`, `node_drain`, `network_partition`, `container_kill`, `disk_fill`.

## Data Flow

```
1. CLI instruction → PlannerAgent produces ordered step plan
2. Supervisor iterates steps in dependency order:
     bob_cli / oc  →  ExecutorAgent
     krknctl             →  ChaosAgent → WaitAgent
     health_check        →  ClusterMonitorAgent
3. Health checks run before and after every step
4. Final report with pass/fail per step, health timeline, errors
```

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
| `CHAOS_TIMEOUT` | — | `600` | Max wait for chaos completion (seconds) |
| `CHAOS_POLL_INTERVAL` | — | `15` | Polling interval (seconds) |
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
| All `bob` CLI executions + output | `[bob] command: bob run --test ...` |
| All `krknctl` inject/list commands | `[krknctl] command: krknctl random run ...` |
| Cluster health check oc commands | `[health-oc] command: oc get pods ...` |
| LLM prompts sent by each agent | `[PlannerAgent] Sending prompt to LLM (1423 chars)` |
| LLM raw responses | `[PlannerAgent] Raw LLM response: ...` |
| Supervisor phase transitions | `[Supervisor] Phase transition: PLANNING → EXECUTING` |
| Step routing + params | `[Supervisor] Step 2/8 — tool=oc action=Create test PVC` |
| Health snapshots before/after steps | `[Supervisor] Health [pre-step-4]: ceph=HEALTH_OK ...` |
| Step results | `[Supervisor] Step 2 result: status=success` |
| Final report | Full workflow report with pass/fail per step |

Console output uses `LOG_LEVEL` from `.env` (default `INFO`). Set `LOG_LEVEL=DEBUG` in `.env` to see everything on the console too.

## Health Rules

The cluster health check is now **rule-driven** via `chaosminds/prompts/health_rules.txt`. Edit this file to customize what ChaosMinds considers HEALTHY, DEGRADED, or CRITICAL.

### Rule categories

| Section | Effect |
|---|---|
| `critical` | Any matching condition → **CRITICAL** (chaos steps are skipped) |
| `degraded` | Any matching condition → **DEGRADED** (proceed with warnings) |
| `healthy` | All conditions must be true for HEALTHY status |
| `expected_pods` | ODF pods that must exist and be Running |
| `critical_ceph_checks` | Ceph health checks that escalate to CRITICAL |
| `warn_ceph_checks` | Ceph health checks treated as warnings |

### Rule syntax

Rules use simple conditions: `variable operator value`

```
pods_failed >= 3          # numeric comparison
ceph_status == HEALTH_ERR # string equality
nodes_not_ready >= 2      # numeric threshold
```

Available variables: `ceph_status`, `nodes_not_ready`, `pods_failed`, `pods_pending`, `pvcs_pending`, `pvcs_lost`.

### Example: making the system more tolerant

```
# Allow 1 pending pod before flagging DEGRADED
degraded:
  - pods_pending >= 5     # was 3
  - pods_failed >= 2      # was 1
```

If the rules file is missing or empty, the tool falls back to hardcoded defaults.

## How It Works

1. The **PlannerAgent** uses the local LLM to interpret your instruction and the scenario plan, then emits a JSON array of steps with dependency ordering. It generates YAML CRs for any Kubernetes resources (PVCs, VolumeSnapshots, etc.) needed.

2. The **Supervisor** topologically sorts the steps and routes each one to the appropriate agent.

3. The **ExecutorAgent** applies YAML manifests via `oc` or runs OCS-CI tests via `bob`.

4. The **ChaosAgent** injects faults using `krknctl`, and the **WaitAgent** polls until completion.

5. The **ClusterMonitorAgent** checks pod/PVC/Ceph/node health before and after every step, building a health timeline.

6. A final report summarises pass/fail per step, the health timeline, and any errors.
