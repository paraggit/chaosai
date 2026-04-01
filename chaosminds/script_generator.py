"""Converts a ChaosMinds structured plan into a multi-phase bash script.

Script phases:
  1. Setup    — create resources, baseline health check
  2. Chaos    — inject chaos, confirm active
  3. Test Ops — loop operations N times under chaos
  4. Teardown — stop chaos
  5. Post     — final health check, analysis, cleanup
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from chaosminds.config import AppConfig

logger = logging.getLogger(__name__)

_POD_SCENARIO_VARIANTS = [
    {"POD_LABEL": "app=rook-ceph-osd", "KILL_TIMEOUT": "180", "EXPECTED_RECOVERY_TIME": "120"},
    {"POD_LABEL": "app=rook-ceph-mon", "KILL_TIMEOUT": "120", "EXPECTED_RECOVERY_TIME": "60"},
    {"POD_LABEL": "app=rook-ceph-mgr", "KILL_TIMEOUT": "120", "EXPECTED_RECOVERY_TIME": "60"},
    {"POD_LABEL": "app=rook-ceph-mds", "KILL_TIMEOUT": "120", "EXPECTED_RECOVERY_TIME": "60"},
]


def _build_chaos_graph(sc: dict) -> tuple[dict, str]:
    """Build a krknctl random-run graph from a scenario config.

    For pod-scenarios the planner's single scenario_config is fanned
    out into multiple variant nodes (one per ODF component) so that
    krknctl runs them concurrently.  Other scenario types produce a
    single chaos node.  Returns (graph_dict, chaos_prefix).
    """
    name = sc.get("name", "scenario")

    graph: dict = {
        "root_chaos": {
            "image": "quay.io/krkn-chaos/krkn-hub:dummy-scenario",
            "name": "dummy-scenario",
            "env": {"END": "10", "EXIT_STATUS": "0"},
        },
    }

    variants = (
        _POD_SCENARIO_VARIANTS if name == "pod-scenarios" else [{}]
    )

    for variant in variants:
        suffix = os.urandom(2).hex()
        node_key = f"{name}_chaos_{suffix}"
        node = {k: v for k, v in sc.items()}
        if variant:
            merged_env = dict(node.get("env", {}))
            merged_env.update(variant)
            node["env"] = merged_env
        node["depends_on"] = "root_chaos"
        graph[node_key] = node

    chaos_prefix = f"{name}_chaos_"
    return graph, chaos_prefix

_HEADER = """\
#!/usr/bin/env bash
# ════════════════════════════════════════════════════════
# ChaosMinds — Multi-phase chaos workflow script
# Generated: {timestamp}
# Instruction: {instruction}
# Loop count: {loop_count}
# ════════════════════════════════════════════════════════
set -uo pipefail

OC="{oc_path}"
KRKNCTL="{krknctl_path}"
BOB="{bob_cli_path}"
NS="openshift-storage"
export KUBECONFIG="{kubeconfig}"
POLL_INTERVAL={poll_interval}
CHAOS_TIMEOUT={chaos_timeout}
CHAOS_SETTLE_TIME={chaos_settle_time}
CHAOS_MAX_PARALLEL={chaos_max_parallel}
LOOP_COUNT={loop_count}
COLLECT_MUST_GATHER="{collect_must_gather}"

LOGFILE="logs/run_$(date -u +%Y%m%d_%H%M%S).log"
mkdir -p logs

PASS_COUNT=0
FAIL_COUNT=0
CHAOS_PID=""
CHAOS_TMP=""
CHAOS_STOP=""

# ── Cleanup trap — always stop chaos on exit ──────────

on_exit() {{
    local exit_code=$?
    log ""
    log "═══ EXIT TRAP (exit_code=$exit_code) ═══"
    if [ -n "$CHAOS_STOP" ]; then
        touch "$CHAOS_STOP"
    fi
    stop_chaos
    if [ -n "$CHAOS_PID" ]; then
        kill "$CHAOS_PID" 2>/dev/null || true
        wait "$CHAOS_PID" 2>/dev/null || true
    fi
    if [ -n "$CHAOS_TMP" ]; then
        rm -f "$CHAOS_TMP"
    fi
    if [ -n "$CHAOS_STOP" ]; then
        rm -f "$CHAOS_STOP"
    fi
    resource_cleanup
    log "═══ EXIT TRAP COMPLETE ═══"
}}
trap on_exit EXIT

# ── Helpers ──────────────────────────────────────────

log() {{
    echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOGFILE"
}}

die() {{
    log "FATAL: $*"
    exit 1
}}

run_oc() {{
    log "[oc] $OC $*"
    local output rc
    output=$("$OC" "$@" 2>&1) && rc=$? || rc=$?
    log "[oc] exit=$rc"
    if [ -n "$output" ]; then
        echo "$output" | tee -a "$LOGFILE"
    fi
    return $rc
}}

run_krknctl() {{
    log "[krknctl] $KRKNCTL $*"
    local output rc
    output=$("$KRKNCTL" "$@" 2>&1) && rc=$? || rc=$?
    log "[krknctl] exit=$rc"
    if [ -n "$output" ]; then
        echo "$output" | tee -a "$LOGFILE"
    fi
    return $rc
}}

health_check() {{
    local label="$1"
    log ""
    log "═══ HEALTH CHECK: $label ═══"
    run_oc get pods -n "$NS" --no-headers || true
    run_oc exec -n "$NS" deploy/rook-ceph-tools -- \\
        ceph status || true
    run_oc exec -n "$NS" deploy/rook-ceph-tools -- \\
        ceph crash ls-new || true
    run_oc get storagecluster -n "$NS" \\
        -o jsonpath='{{.items[0].status.phase}}' || true
    echo | tee -a "$LOGFILE"
    run_oc get nodes --no-headers || true
    log "═══ END HEALTH CHECK ═══"
    log ""
}}

wait_chaos_active() {{
    local pattern="${{1:-krknctl-}}"
    log "[chaos] Waiting for scenario matching '$pattern' to become active..."
    local elapsed=0
    local deadline=180
    while [ "$elapsed" -lt "$deadline" ]; do
        sleep 10
        elapsed=$((elapsed + 10))
        local status
        status=$("$KRKNCTL" list running 2>&1) || true
        if echo "$status" | grep -q "$pattern"; then
            log "[chaos] Scenario '$pattern' is ACTIVE after ${{elapsed}}s"
            echo "$status" | tee -a "$LOGFILE"
            return 0
        fi
        log "[chaos] Not active yet (${{elapsed}}s/${{deadline}}s)..."
    done
    log "[chaos] WARNING: chaos '$pattern' did not become active within ${{deadline}}s"
    return 1
}}

stop_chaos() {{
    log "[chaos] Stopping all running chaos scenarios..."
    local containers
    containers=$("$KRKNCTL" list running 2>&1) || true
    echo "$containers" | tee -a "$LOGFILE"

    local names
    names=$(echo "$containers" | awk '{{print $NF}}' \\
        | grep "^krknctl-" || true)

    if [ -n "$names" ]; then
        for name in $names; do
            log "[chaos] Stopping container: $name"
            podman stop "$name" 2>&1 | tee -a "$LOGFILE" || true
        done
    else
        log "[chaos] No running containers to stop"
    fi

    "$KRKNCTL" clean 2>&1 | tee -a "$LOGFILE" || true
    log "[chaos] Chaos terminated"
}}

run_chaos_loop() {{
    local graph_file="$1"
    while [ ! -f "$CHAOS_STOP" ]; do
        log "[chaos] (Re)starting chaos scenario..."
        "$KRKNCTL" random run "$graph_file" \\
            --max-parallel="$CHAOS_MAX_PARALLEL" \\
            --kubeconfig="$KUBECONFIG" >> "$LOGFILE" 2>&1 || true
        if [ -f "$CHAOS_STOP" ]; then break; fi
        log "[chaos] Scenario completed, restarting in 5s..."
        "$KRKNCTL" clean >> "$LOGFILE" 2>&1 || true
        sleep 5
    done
    log "[chaos] Chaos loop exited"
}}

resource_cleanup() {{
    log ""
    log "═══ RESOURCE CLEANUP ═══"
    for rtype in volumesnapshot pvc pod; do
        local items
        items=$("$OC" get "$rtype" -n "$NS" --no-headers \\
            -o custom-columns=NAME:.metadata.name 2>/dev/null \\
            | grep "chaos-test" || true)
        if [ -n "$items" ]; then
            for item in $items; do
                log "[cleanup] Deleting $rtype/$item"
                run_oc delete "$rtype" "$item" -n "$NS" \\
                    --timeout=120s || true
            done
        fi
    done
    log "═══ END RESOURCE CLEANUP ═══"
}}
"""


def _sanitize_oc_cmd(cmd: str) -> str:
    """Quote jsonpath / custom-columns brace expressions for bash safety.

    Fixes two classes of problems:
    1. LLM typo: ``{.status.phase)`` — closing ``)`` instead of ``}``.
    2. Unquoted ``{...}`` that bash interprets as brace expansion.
    """
    # Fix LLM mistake: ) used as closing brace after {.
    cmd = re.sub(r"\{(\.[^})]+)\)", r"{\1}", cmd)
    # Single-quote any unquoted {…} (jsonpath / Go-template expressions).
    cmd = re.sub(r"(?<!')(\{[^}'\"]+\})(?!')", r"'\1'", cmd)
    return cmd


def _render_oc_step(
    step: dict, idx_var: str = "",
) -> str:
    """Render a single oc step (non-YAML only).

    For YAML steps use _render_oc_yaml_function instead.
    Replaces {i} with idx_var reference.
    """
    params = step.get("params", {})
    action = step.get("action", "oc command")
    cmd = params.get("command", "")

    if idx_var:
        action = action.replace("{i}", f"${idx_var}")
        cmd = cmd.replace("{i}", f"${idx_var}")

    cmd = _sanitize_oc_cmd(cmd)

    lines: list[str] = []
    lines.append(f'log "  {action}"')
    if cmd:
        lines.append(f'run_oc {cmd}')
    return "\n".join(lines)


def _render_oc_yaml_function(
    func_name: str,
    step: dict,
) -> str:
    """Render a bash function for an oc apply step.

    The function takes one argument ($1) used as the loop
    index. Heredocs are at column 0 inside the function
    body so bash parses them correctly.
    """
    params = step.get("params", {})
    action = step.get("action", "oc command")
    yaml_body = params.get("yaml", "").strip()

    action = action.replace("{i}", "$1")
    yaml_body = yaml_body.replace("{i}", "$1")

    lines: list[str] = [
        f'{func_name}() {{',
        '    local idx="$1"',
        f'    log "  {action}"',
        '    "$OC" apply -f - <<YAML_EOF 2>&1 '
        '| tee -a "$LOGFILE"',
        yaml_body,
        'YAML_EOF',
        '    log "[oc] exit=$?"',
        '}',
    ]
    return "\n".join(lines)


def _render_oc_inline_heredoc(step: dict) -> str:
    """Render an oc apply with inline heredoc (top-level, no indent)."""
    params = step.get("params", {})
    action = step.get("action", "oc apply")
    yaml_body = params.get("yaml", "").strip()

    lines: list[str] = [
        f'log "  {action}"',
        '"$OC" apply -f - <<YAML_EOF 2>&1 '
        '| tee -a "$LOGFILE"',
        yaml_body,
        'YAML_EOF',
        'log "[oc] exit=$?"',
    ]
    return "\n".join(lines)


def _render_health_step(step: dict) -> str:
    action = step.get("action", "Health check")
    label = action.replace('"', '\\"')
    return f'health_check "{label}"'


def generate_script(
    plan: dict,
    config: AppConfig,
    instruction: str = "",
) -> str:
    """Convert a structured plan dict into a bash script."""
    timestamp = datetime.now(
        timezone.utc,
    ).strftime("%Y-%m-%d %H:%M:%S UTC")

    header = _HEADER.format(
        timestamp=timestamp,
        instruction=instruction.replace('"', '\\"'),
        oc_path=config.oc_path,
        krknctl_path=config.krknctl_path,
        bob_cli_path=config.bob_cli_path,
        kubeconfig=config.kubeconfig,
        poll_interval=config.chaos_poll_interval,
        chaos_timeout=config.chaos_timeout,
        chaos_settle_time=config.chaos_settle_time,
        chaos_max_parallel=config.chaos_max_parallel,
        loop_count=config.loop_count,
        collect_must_gather=(
            "true" if config.collect_must_gather
            else "false"
        ),
    )

    body: list[str] = []
    body.append(
        'log "ChaosMinds — Starting multi-phase '
        'chaos workflow"',
    )
    body.append(f'log "Instruction: {instruction}"')
    body.append('log "Loop count: $LOOP_COUNT"')
    body.append("")

    # ── Phase 1: Setup ──
    body.append(
        "# ════════════════════════════════════════",
    )
    body.append(
        'log "╔══ PHASE 1: SETUP ══╗"',
    )
    for step in plan.get("setup", []):
        tool = step.get("tool", "")
        params = step.get("params", {})
        if tool == "health_check":
            body.append(_render_health_step(step))
        elif tool == "oc" and params.get("yaml"):
            body.append(
                _render_oc_inline_heredoc(step),
            )
        elif tool == "oc":
            body.append(_render_oc_step(step))
        body.append("")

    # ── Phase 2: Chaos Injection ──
    body.append(
        "# ════════════════════════════════════════",
    )
    body.append(
        'log "╔══ PHASE 2: CHAOS INJECTION ══╗"',
    )

    chaos = plan.get("chaos", {})
    sc = chaos.get("scenario_config", {})
    if sc:
        body.append(
            'log "[chaos] Cleaning stale chaos '
            'containers from previous runs..."',
        )
        body.append("stop_chaos")
        body.append("")

        graph, chaos_prefix = _build_chaos_graph(sc)
        graph_json = json.dumps(graph, indent=2)

        num_nodes = len(graph) - 1
        body.append(
            f'log "[chaos] Graph: {num_nodes} variant(s) '
            f'of {sc.get("name", "scenario")}"',
        )
        body.append(
            "CHAOS_TMP=$(mktemp /tmp/krknctl_XXXXXX)",
        )
        body.append(
            "cat > \"$CHAOS_TMP\" <<'JSON_EOF'",
        )
        body.append(graph_json)
        body.append("JSON_EOF")
        body.append(
            'log "[chaos] Scenario graph:"',
        )
        body.append(
            'cat "$CHAOS_TMP" | tee -a "$LOGFILE"',
        )
        body.append("")
        body.append(
            "CHAOS_STOP=$(mktemp /tmp/chaos_stop_XXXXXX)",
        )
        body.append(
            'rm -f "$CHAOS_STOP"',
        )
        body.append("")
        body.append(
            'log "[chaos] Starting chaos loop '
            'in background..."',
        )
        body.append(
            'run_chaos_loop "$CHAOS_TMP" &',
        )
        body.append("CHAOS_PID=$!")
        body.append(
            'log "[chaos] Chaos loop PID=$CHAOS_PID"',
        )
        body.append("")

        body.append(
            "# Wait for ANY chaos variant "
            "(not the dummy)",
        )
        body.append(
            f'wait_chaos_active "krknctl-{chaos_prefix}" '
            "|| "
            'log "[chaos] Proceeding despite '
            'no confirmation"',
        )
        body.append("")

        body.append(
            "# Settle period — let chaos disrupt",
        )
        body.append(
            'log "[chaos] Settle period: '
            '${CHAOS_SETTLE_TIME}s..."',
        )
        body.append("sleep $CHAOS_SETTLE_TIME")
        body.append("")

    # ── Phase 3: Test Operations Loop ──
    #
    # YAML-apply steps become top-level functions to
    # avoid heredoc-indentation issues inside for loops.
    test_ops = plan.get("test_ops", [])
    yaml_funcs: list[str] = []
    op_calls: list[str] = []

    for op_idx, step in enumerate(test_ops, 1):
        tool = step.get("tool", "")
        params = step.get("params", {})

        if tool == "oc" and params.get("yaml"):
            fname = f"test_op_{op_idx}"
            yaml_funcs.append(
                _render_oc_yaml_function(fname, step),
            )
            op_calls.append(
                f'    {fname} "$i"',
            )
        elif tool == "oc":
            rendered = _render_oc_step(step, "i")
            for line in rendered.splitlines():
                op_calls.append(f"    {line}")
        elif tool == "health_check":
            rendered = _render_health_step(step)
            op_calls.append(f"    {rendered}")

    if yaml_funcs:
        body.append(
            "# ── Test-op functions (YAML apply) ──",
        )
        for fn in yaml_funcs:
            body.append(fn)
            body.append("")

    body.append(
        "# ════════════════════════════════════════",
    )
    body.append(
        'log "╔══ PHASE 3: TEST EXECUTION '
        '(loop $LOOP_COUNT) ══╗"',
    )
    body.append("")

    if op_calls:
        body.append(
            'for i in $(seq 1 $LOOP_COUNT); do',
        )
        body.append(
            '    log ""',
        )
        body.append(
            '    log "── Iteration $i / '
            '$LOOP_COUNT ──"',
        )
        body.append('    iter_ok=true')
        for call in op_calls:
            stripped = call.lstrip()
            is_cmd = (
                stripped.startswith("run_oc ")
                or stripped.startswith("test_op_")
                or stripped.startswith("health_check ")
                or stripped.startswith('"$OC"')
            )
            if is_cmd:
                body.append(
                    f'{call} || {{ '
                    'log "  [FAIL] step failed"; '
                    'iter_ok=false; }',
                )
            else:
                body.append(call)
        body.append("")
        body.append(
            '    if $iter_ok; then',
        )
        body.append(
            '        PASS_COUNT=$((PASS_COUNT + 1))',
        )
        body.append(
            '        log "  [PASS] Iteration $i passed"',
        )
        body.append("    else")
        body.append(
            '        FAIL_COUNT=$((FAIL_COUNT + 1))',
        )
        body.append(
            '        log "  [FAIL] Iteration $i failed"',
        )
        body.append("    fi")
        body.append("done")
        body.append("")
        body.append(
            'log "Loop complete: '
            'PASS=$PASS_COUNT FAIL=$FAIL_COUNT"',
        )

    body.append("")

    # ── Phase 4: Chaos Termination ──
    body.append(
        "# ════════════════════════════════════════",
    )
    body.append(
        'log "╔══ PHASE 4: CHAOS TERMINATION ══╗"',
    )
    if sc:
        body.append(
            'log "[chaos] Signalling chaos loop to stop"',
        )
        body.append('touch "$CHAOS_STOP"')
    body.append("stop_chaos")
    if sc:
        body.append(
            "kill $CHAOS_PID 2>/dev/null || true",
        )
        body.append(
            "wait $CHAOS_PID 2>/dev/null || true",
        )
        body.append('rm -f "$CHAOS_TMP"')
        body.append('rm -f "$CHAOS_STOP"')
        body.append(
            '# Clear so EXIT trap does not re-run',
        )
        body.append('CHAOS_PID=""')
        body.append('CHAOS_TMP=""')
        body.append('CHAOS_STOP=""')
    body.append("")

    # ── Phase 5: Post-workflow ──
    body.append(
        "# ════════════════════════════════════════",
    )
    body.append(
        'log "╔══ PHASE 5: POST-WORKFLOW ══╗"',
    )
    for step in plan.get("post", []):
        tool = step.get("tool", "")
        params = step.get("params", {})
        if tool == "health_check":
            body.append(_render_health_step(step))
        elif tool == "oc" and params.get("yaml"):
            body.append(
                _render_oc_inline_heredoc(step),
            )
        elif tool == "oc":
            body.append(_render_oc_step(step))
        body.append("")

    body.append("resource_cleanup")
    body.append("")
    body.append("# Disable EXIT trap — cleanup already done")
    body.append("trap - EXIT")
    body.append("")

    # ── Final Report ──
    body.append(
        "# ════════════════════════════════════════",
    )
    body.append('log ""')
    body.append(
        'log "════════════════════════════════'
        '════════════════════"',
    )
    body.append(
        'log "  ChaosMinds — Execution Report"',
    )
    body.append(
        'log "════════════════════════════════'
        '════════════════════"',
    )
    body.append(
        'log "  Iterations: $LOOP_COUNT"',
    )
    body.append(
        'log "  Passed:     $PASS_COUNT"',
    )
    body.append(
        'log "  Failed:     $FAIL_COUNT"',
    )
    body.append(
        'log "  Log:        $LOGFILE"',
    )
    body.append(
        'log "════════════════════════════════'
        '════════════════════"',
    )
    body.append("")
    body.append(
        'if [ "$FAIL_COUNT" -gt 0 ]; then exit 1; fi',
    )

    script = header + "\n" + "\n".join(body) + "\n"

    logger.info(
        "[ScriptGenerator] Generated %d lines, "
        "%d bytes",
        script.count("\n"), len(script),
    )
    return script
