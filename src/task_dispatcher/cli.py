#!/usr/bin/env python3
"""
task-dispatcher.py — Agent orchestration task queue dispatcher
Runs every 2 minutes via PM2 cron.

Logic:
  1. Process submitted tasks: route to agent, auto-approve or set pending-approval
     - Exponential backoff retry on routing failures (default 3 retries: 5m, 10m, 20m)
  2. Archive terminal tasks past ttl_days
  3. Log all transitions to dispatcher.log
"""

import contextlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import yaml

# agent-bus client — write path for code that cannot call MCP directly. Optional:
# installed via the `bus` extra (pip install task-dispatcher[bus]). When it is not
# installed, event logging degrades to a no-op rather than failing the tick.
try:
    from agent_bus_client import log_event as bus_log
except ImportError:

    def bus_log(*a, **kw):
        pass  # no-op if client missing (safe degradation)


# --- Config ---
TASK_QUEUE_DIR = Path.home() / ".claude" / "task-queue"
ARCHIVE_DIR = TASK_QUEUE_DIR / "archive"
DEAD_LETTER_DIR = TASK_QUEUE_DIR / "dead-letters"
MANIFEST_DIR = Path.home() / ".claude" / "manifests"
LOG_FILE = TASK_QUEUE_DIR / "dispatcher.log"
MATRIX_MCP_URL = "http://127.0.0.1:8487/mcp"

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
RETRY_BASE_SECONDS = 300  # 5 min base; backoff: 5m, 10m, 20m

# --- Task queue vocabulary (vikunja#324) -----------------------------------
# These four sets MUST stay identical to task-queue-mcp's src/tools/queue.py.
#
# This dispatcher is a fourth writer to the same queue and shares no validation code
# with the MCP that owns it, so the two spellings can drift apart silently — and have.
# task-queue-headless-chain-2026-08 added two more values that both sides must agree on
# (`notify` being self-terminal, `manual-then-auto`), which is what finally justified
# pinning them down here instead of scattering the literals through the branches below.
#
# scripts/tests/test_task_queue_vocabulary.py asserts these are equal to the MCP's, read
# from the source of truth rather than from a second copy. It fails on a mismatch AND on
# being unable to read the upstream — a vocabulary check that quietly skips is
# indistinguishable from one that passes, which is the failure mode that produced #324.
VALID_STATUSES = {
    "submitted",
    "approved",
    "pending-approval",
    "in-progress",
    "parked",
    "routing-failed",
    "completed",
    "failed",
    "cancelled",
}
VALID_TASK_TYPES = {
    "build",
    "deploy",
    "fix",
    "research",
    "review",
    "audit",
    "notify",
    "docs",
    "ticket_audit",
    "ticket_audit_complete",
}
VALID_WORKFLOW_MODES = {"semi-auto", "auto", "manual-then-auto"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

# Types this dispatcher must never launch a session for. `notify` carries a result, not
# work: task-queue-mcp writes it straight to `completed`, so it should never reach here
# as `submitted` at all. The branch below is defence in depth for a queue file written by
# something other than the MCP — see #324 for why that is not hypothetical.
SELF_TERMINAL_TASK_TYPES = {"notify"}

# `workflow` is NOT in VALID_TASK_TYPES and never has been: Temporal workflow tasks are
# written to the queue directly by temporal-workflow-start.sh, bypassing submit_task. The
# branch that handles them predates the MCP's validation. Recorded here so the unknown-type
# guard below does not dead-letter every workflow task the first time it runs.
DISPATCHER_ONLY_TASK_TYPES = {"workflow"}

# Backwards-compatible alias. The dispatcher spelled this TERMINAL_STATES for its whole
# life; the MCP spells it TERMINAL_STATUSES. Same set, and now provably so.
TERMINAL_STATES = TERMINAL_STATUSES

TEMPORAL_START_SCRIPT = Path.home() / "scripts" / "temporal-workflow-start.sh"

# --- Agent launch policy (build task-queue-plugin-repair-2026-08, vikunja#523) ---
#
# AGENT_PROJECT_DIRS and AGENT_RUN_AS used to be two literal dicts here. The CloudCLI
# task-queue plugin carried a THIRD literal — its own `AGENT_PROJECTS`, which never
# gained a steward entry, so its Start button could not launch steward at all. Three
# rosters of one fact, and the drift between them is #523.
#
# They are now one file, scripts/agent-launch.yml, read by this dispatcher and by the
# plugin. Editing the roster means editing that file; do not reintroduce a literal here.
#
# WHAT THE OLD COMMENT GOT RIGHT AND THIS MUST KEEP. The literals existed so that no
# user-supplied value could reach subprocess.Popen. Loading from a file does not by
# itself preserve that, so validate_launch_policy() below re-establishes it: every field
# is checked against a closed set (name shape, project_dir under ~/.claude/projects,
# run_as_user shape, launcher under /usr/local/sbin/forge) and the WHOLE file is
# rejected on any violation. Nothing derived from task content ever enters this table.
#
# A missing or malformed file raises. It must NOT degrade to {} — an empty roster makes
# run_as_user absent for every agent, which is exactly how steward gets launched as ted:
# a session that looks like steward in every log and holds none of its credentials
# (vikunja#404). Failing the tick loudly is strictly better than that.
#
# THE INVARIANT. An agent with run_as_user set must be launched only via its launcher.
# load_agent_env() must never be called for such an agent: the launcher runs as that user
# and is the only thing that can read its environment file. Reading it from this process
# would raise PermissionError and take down the whole dispatcher tick, not merely return
# an empty dict. (vikunja#404.)
#
# Resolved from $HOME, NOT from this file's own directory. The roster is shared with the
# CloudCLI task-queue plugin, which hardcodes ~/scripts/agent-launch.yml (launch-policy.ts).
# This package deploys to a venv outside ~/scripts, so resolving from __file__ would look
# beside cli.py, find nothing, and hard-fail every tick — while the plugin kept reading the
# real roster. Two consumers, one path, agreeing by construction. Do not reintroduce a
# __file__-relative default. AGENT_LAUNCH_POLICY overrides it (tests set it, and the
# crontab sets it explicitly so the resolution is visible at the call site rather than
# implied). tests/fixtures/agent-launch.yml is a FIXTURE COPY, never the live roster.
LAUNCH_POLICY_PATH = Path(
    os.environ.get("AGENT_LAUNCH_POLICY") or (Path.home() / "scripts" / "agent-launch.yml")
)

# Closed sets. These are the constants that used to be the dict literals: a policy file
# cannot introduce a user, a launcher directory, or a project root outside them.
_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_RUN_AS_USER_RE = re.compile(r"^agent-[a-z0-9-]{1,30}$")
_LAUNCHER_DIR = "/usr/local/sbin/forge/"
_PROJECT_ROOT = Path.home() / ".claude" / "projects"


class LaunchPolicyError(Exception):
    """Raised when agent-launch.yml is missing, unparseable, or fails validation."""


def validate_launch_policy(raw: object, project_root: Path | None = None) -> dict:
    """Validate a parsed agent-launch.yml and return {agent: {...}}.

    Rejects the whole document on any violation. A partially-honoured roster would
    silently launch some agent the wrong way, which is the failure this guards.

    project_root is injectable so tests need not own ~/.claude/projects.
    """
    # NO .resolve() here, deliberately — see the matching comment in the plugin's
    # validateLaunchPolicy(). This used to be `.resolve()`, which follows symlinks, while
    # the TypeScript side computes its root with a plain path join. Neither side resolves
    # the CANDIDATE project_dir (it may legitimately not exist yet), so resolving only the
    # root makes the two halves of the comparison incommensurable the moment anything in
    # the path becomes a symlink — and `~/.claude/manifests` on this host already is one,
    # so that is not hypothetical.
    #
    # The effect was not a rejected entry. LAUNCH_POLICY = load_launch_policy() runs at
    # import, so a symlinked ~/.claude/projects made THIS MODULE fail to import — every
    # tick, for every agent. Fail-closed, but a total dispatcher outage.
    #
    # Found by the security audit of task-queue-plugin-repair-2026-08 (Low, dormant:
    # nothing on that path is a symlink today). Both sides now compute the root the same
    # way, and test_agent_launch_policy.py pins it with a symlinked-root fixture.
    root = Path(os.path.normpath(str(project_root or _PROJECT_ROOT)))
    if not isinstance(raw, dict) or not raw:
        raise LaunchPolicyError(f"{LAUNCH_POLICY_PATH}: expected a non-empty mapping of agents")

    policy: dict[str, dict] = {}
    for agent, entry in raw.items():
        if not isinstance(agent, str) or not _AGENT_NAME_RE.match(agent):
            raise LaunchPolicyError(f"{LAUNCH_POLICY_PATH}: invalid agent name {agent!r}")
        if not isinstance(entry, dict):
            raise LaunchPolicyError(f"{LAUNCH_POLICY_PATH}: {agent}: expected a mapping")

        unknown = set(entry) - {"project_dir", "run_as_user", "launcher"}
        if unknown:
            raise LaunchPolicyError(
                f"{LAUNCH_POLICY_PATH}: {agent}: unknown key(s) {sorted(unknown)}"
            )

        project_dir = entry.get("project_dir")
        if not isinstance(project_dir, str) or not project_dir:
            raise LaunchPolicyError(f"{LAUNCH_POLICY_PATH}: {agent}: project_dir is required")
        resolved = Path(project_dir).expanduser()
        if not resolved.is_absolute():
            raise LaunchPolicyError(
                f"{LAUNCH_POLICY_PATH}: {agent}: project_dir must be absolute: {project_dir}"
            )
        # Normalise `..` before the containment check; do not resolve symlinks, which
        # would depend on the dir existing (it may legitimately not, and the caller
        # reports that as a routing failure with a better message than this would).
        normalised = Path(os.path.normpath(str(resolved)))
        if root != normalised and root not in normalised.parents:
            raise LaunchPolicyError(
                f"{LAUNCH_POLICY_PATH}: {agent}: project_dir must be under {root}: {project_dir}"
            )

        run_as_user = entry.get("run_as_user")
        launcher = entry.get("launcher")
        if (run_as_user is None) != (launcher is None):
            raise LaunchPolicyError(
                f"{LAUNCH_POLICY_PATH}: {agent}: run_as_user and launcher must be given together"
            )
        if run_as_user is not None:
            if not isinstance(run_as_user, str) or not _RUN_AS_USER_RE.match(run_as_user):
                raise LaunchPolicyError(
                    f"{LAUNCH_POLICY_PATH}: {agent}: invalid run_as_user {run_as_user!r}"
                )
            if not isinstance(launcher, str) or not launcher.startswith(_LAUNCHER_DIR):
                raise LaunchPolicyError(
                    f"{LAUNCH_POLICY_PATH}: {agent}: launcher must be under "
                    f"{_LAUNCHER_DIR}: {launcher!r}"
                )
            # No `..` escape out of the launcher dir.
            if os.path.normpath(launcher) != launcher:
                raise LaunchPolicyError(
                    f"{LAUNCH_POLICY_PATH}: {agent}: launcher must be a normalised "
                    f"path: {launcher!r}"
                )

        policy[agent] = {
            "project_dir": normalised,
            "run_as_user": run_as_user,
            "launcher": launcher,
        }
    return policy


def load_launch_policy(path: Path | None = None) -> dict:
    """Read and validate agent-launch.yml. Raises LaunchPolicyError; never returns {}."""
    p = path or LAUNCH_POLICY_PATH
    try:
        text = p.read_text()
    except OSError as e:
        raise LaunchPolicyError(f"cannot read launch policy {p}: {e}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise LaunchPolicyError(f"cannot parse launch policy {p}: {e}") from e
    return validate_launch_policy(raw)


LAUNCH_POLICY = load_launch_policy()

# Kept as names so the rest of the file — and anything importing this module — reads the
# same as it did before the extraction. Both are now views of one source.
AGENT_PROJECT_DIRS = {a: e["project_dir"] for a, e in LAUNCH_POLICY.items()}
AGENT_RUN_AS = {
    a: (e["run_as_user"], e["launcher"])
    for a, e in LAUNCH_POLICY.items()
    if e["run_as_user"] is not None
}

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(
    logging.WARNING
)  # keep matrix_notify()'s own per-request lines out of dispatcher.log


# --- Atomic YAML write ---
def atomic_write(path: Path, data: dict) -> None:
    """Write YAML to a tmp file then mv into place to prevent race conditions."""
    tmp = path.with_suffix(".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        tmp.rename(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --- History append ---
def append_history(task: dict, status: str, actor: str, note: str = "") -> None:
    entry = {"timestamp": now_iso(), "status": status, "actor": actor}
    if note:
        entry["note"] = note
    task.setdefault("history", []).append(entry)


# --- Retry eligibility check ---
def is_eligible(task: dict) -> bool:
    """Return True if the task is eligible for routing (retry window has passed)."""
    next_retry = task.get("retry_policy", {}).get("next_retry_at")
    if next_retry is None:
        return True
    return datetime.now(UTC) >= datetime.fromisoformat(next_retry)


# --- Routing failure handler with exponential backoff ---
def handle_routing_failure(path: Path, task: dict, reason: str) -> None:
    """Handle a task routing failure with exponential backoff retry.

    Between retries the task sits in "routing-failed" status so it does NOT
    re-enter the submitted → approval pipeline on each attempt.  A separate
    process_routing_failed() pass picks up eligible tasks and retries routing
    directly, bypassing re-approval (the task was already approved).

    On exhaustion the task is dead-lettered and moved out of the active queue.
    Failure behavior is documented explicitly so operators understand the outcome:
      - retry 1-3: status=routing-failed, next_retry_at set (exponential backoff)
      - after 3 retries: status=failed → dead-lettered to dead-letters/
      - Matrix #alerts notification on dead-letter
    """
    policy = task.setdefault("retry_policy", {})
    retry_count = policy.get("retry_count", 0)
    max_retries = policy.get("max_retries", 3)

    policy["last_failure_reason"] = reason
    append_history(task, "routing-failed", "dispatcher", reason)

    if retry_count < max_retries:
        backoff_seconds = RETRY_BASE_SECONDS * (2**retry_count)
        policy["retry_count"] = retry_count + 1
        policy["next_retry_at"] = (
            datetime.now(UTC) + timedelta(seconds=backoff_seconds)
        ).isoformat()
        # FIX(TQMCP-1/MDISP-1): park in "routing-failed" not "submitted".
        # Previously reset to "submitted" caused the task to re-run the full
        # approval pipeline (submitted→approved→routing-failed) on every retry,
        # firing spurious NATS tasks.approved events.  process_routing_failed()
        # now handles retry routing directly without re-approval.
        task["status"] = "routing-failed"
        atomic_write(path, task)
        bus_log(
            "task.routing-failed",
            source="dispatcher",
            summary=f"Routing failed (retry {retry_count + 1}/{max_retries}): {reason}",
            target=task.get("target_agent"),
            artifact_path=str(path),
        )
        log.info(
            f"{path.name}: routing failed ({reason}); "
            f"retry {retry_count + 1}/{max_retries} in {backoff_seconds // 60}m"
        )
    else:
        task["status"] = "failed"
        publish_nats("tasks.failed", {"task_id": task.get("id"), "summary": task.get("summary")})
        bus_log(
            "task.failed",
            source="dispatcher",
            summary=f"Task failed (exhausted {max_retries} retries): {reason}",
            target=task.get("target_agent"),
            artifact_path=str(path),
        )
        log.warning(f"{path.name}: exhausted {max_retries} retries: {reason}")
        move_to_dead_letter(path, task, reason)


# --- Matrix notification (via matrix-mcp HTTP endpoint) ---
# MDISP-2: matrix-mcp's streamable-HTTP endpoint requires a completed MCP
# handshake (initialize -> capture the Mcp-Session-Id response header -> pass
# it on the follow-up tools/call) and rejects requests without it (406 with no
# Accept header, 400 "Missing session ID" without the session header). The
# dispatcher runs as a PM2 cron job, not a launched agent session, so it has no
# scoped-mcp bearer identity to route through scoped-mcp's gateway with — this
# talks to matrix-mcp's own unauthenticated localhost endpoint directly instead,
# same as the original code did (just completing the handshake this time).
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _parse_mcp_response(resp: httpx.Response, expected_id: int) -> dict:
    """Extract the JSON-RPC payload matching expected_id from an SSE- or JSON-formatted body.

    Scans every `data:` line rather than just the first (SECURITY[fixed]: F-2 from
    dispatcher-auth-and-notify-2026-07 audit) — if matrix-mcp's streamable-HTTP
    transport ever emits an intermediate event (e.g. notifications/progress) ahead
    of the terminal JSON-RPC response, taking the first line would treat that
    intermediate event as the answer instead of the actual result.
    """
    candidates = []
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            candidate = json.loads(line[len("data:") :].strip())
            if candidate.get("id") == expected_id:
                return candidate
            candidates.append(candidate)
    if candidates:
        return candidates[-1]
    return resp.json()


def matrix_notify(room: str, title: str, body: str) -> None:
    """Post a notification to a named Matrix room via matrix-mcp, verifying delivery."""
    try:
        with httpx.Client(timeout=10) as client:
            init_resp = client.post(
                MATRIX_MCP_URL,
                headers=_MCP_HEADERS,
                json={
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "task-dispatcher", "version": "1.0"},
                    },
                    "id": 1,
                },
            )
            session_id = init_resp.headers.get("mcp-session-id")
            init_body = _parse_mcp_response(init_resp, expected_id=1)
            if init_resp.status_code // 100 != 2 or init_body.get("error") or not session_id:
                log.warning(
                    f"matrix_notify: init handshake failed for #{room} "
                    f"(status={init_resp.status_code}, body={init_body})"
                )
                return

            call_headers = {**_MCP_HEADERS, "mcp-session-id": session_id}
            call_resp = client.post(
                MATRIX_MCP_URL,
                headers=call_headers,
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {
                        "name": "send_matrix_message",
                        "arguments": {
                            "room_name": room,
                            "message": f"**{title}**\n{body}",
                        },
                    },
                    "id": 2,
                },
            )
            call_body = _parse_mcp_response(call_resp, expected_id=2)
            tool_result = call_body.get("result", {})
            if (
                call_resp.status_code // 100 != 2
                or call_body.get("error")
                or tool_result.get("isError")
            ):
                log.warning(
                    f"matrix_notify failed for #{room}: {title!r} "
                    f"(status={call_resp.status_code}, body={call_body})"
                )
                return
        log.info(f"matrix_notify sent to #{room}: {title}")
    except Exception as e:
        log.warning(f"matrix_notify failed for #{room}: {title!r}: {e}")


# --- NATS publish (fire-and-forget) ---
def publish_nats(subject: str, payload: dict) -> None:
    """Fire-and-forget NATS publish — never blocks the dispatcher."""
    with contextlib.suppress(Exception):
        subprocess.run(
            ["nats", "pub", "--server", "nats://localhost:4222", subject, json.dumps(payload)],
            timeout=5,
            capture_output=True,
        )


# --- Per-agent .env loader (SMCP-28) ---
def load_agent_env(agent_type: str) -> dict:
    """Read /opt/appdata/agents/<agent_type>/.env (KEY=VALUE lines).

    Mirrors the sourcing run-scoped-mcp-http.sh does server-side, so headlessly
    launched claude -p sessions get SCOPED_MCP_BEARER_TOKEN (and other agent
    secrets) in their environment instead of relying on the dispatcher's own env.
    """
    env_path = Path(f"/opt/appdata/agents/{agent_type}/.env")
    env = {}
    if not env_path.is_file():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


# --- Anthropic credential preflight (SMCP-29) ---
# A headless `claude -p` needs valid Anthropic credentials at startup or it
# short-circuits to "Not logged in - Please run /login" and never reads the
# task prompt.  It gets them from ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or
# CLAUDE_CODE_OAUTH_TOKEN in its env (SMCP-32 — any of the three is sufficient,
# same short-circuit pattern), or a valid OAuth token in
# ~/.claude/.credentials.json as a last resort.  Crucially, headless mode does
# NOT interactively refresh an expired OAuth token (it prints the login prompt
# instead), so an expired/zeroed expiry is genuinely unusable here — making
# `expiresAt > now` a correct usability test with no false positives on
# refreshable-but-valid tokens.
OAUTH_CRED_PATH = Path.home() / ".claude" / ".credentials.json"
AUTH_ALERT_STAMP = TASK_QUEUE_DIR / ".auth-alert-stamp"
AUTH_ALERT_DEBOUNCE_SEC = 900  # 15 min — a dead OAuth hits every queued task at once


def anthropic_creds_usable(child_env: dict) -> bool:
    """True if a headless claude -p launched with child_env can authenticate."""
    if (
        child_env.get("ANTHROPIC_API_KEY")
        or child_env.get("ANTHROPIC_AUTH_TOKEN")
        or child_env.get("CLAUDE_CODE_OAUTH_TOKEN")
    ):
        return True
    try:
        oauth = json.loads(OAUTH_CRED_PATH.read_text()).get("claudeAiOauth", {})
    except Exception:
        return False
    if not oauth.get("accessToken"):
        return False
    now_ms = datetime.now(UTC).timestamp() * 1000
    return oauth.get("expiresAt", 0) > now_ms


def alert_auth_blocked(agent: str, task_id: str) -> None:
    """Debounced Matrix alert to #sysadmin when the auth guard blocks a launch."""
    try:
        if AUTH_ALERT_STAMP.exists():
            last = datetime.fromisoformat(AUTH_ALERT_STAMP.read_text().strip())
            if (datetime.now(UTC) - last).total_seconds() < AUTH_ALERT_DEBOUNCE_SEC:
                return
    except Exception:
        pass
    matrix_notify(
        "sysadmin",
        "[auth] Headless launch blocked — no usable Anthropic credential",
        f"claude -p cannot start (OAuth expired / no ANTHROPIC_API_KEY). "
        f"Run `claude /login` to restore headless agents. "
        f"Latest blocked task: {task_id} (agent: {agent}).",
    )
    with contextlib.suppress(Exception):
        AUTH_ALERT_STAMP.write_text(now_iso())


# --- Workflow mode propagation ---
def child_workflow_mode(parent_mode: str) -> str:
    """The mode a task spawned by this task should inherit.

    `manual-then-auto` gates only its own leg. By the time anything downstream exists,
    the operator has already pressed Start — the gate has been satisfied, and repeating
    it on every handoff is the behaviour that left four security→steward return tasks
    unactioned from 2026-08-18 (vikunja#533). Everything else propagates verbatim.

    THIS MUST BE THE ONLY PLACE THAT DECIDES THIS. There are three propagation sites —
    the env var a launched agent reads, the --workflow-mode flag handed to a run-as
    launcher, and the originating_task_id inheritance for a task submitted by an agent
    that never read either. Implementing the downgrade at some of them and not others
    gives a mode that leaks correctly in one direction only, which is close to invisible
    in testing: the failure looks like an occasional handoff that did not auto-run.
    """
    return "auto" if parent_mode == "manual-then-auto" else parent_mode


# --- Headless agent launch ---
def launch_agent_headless(task: dict) -> None:
    """Launch the target agent headlessly via claude -p.

    Passes FORGE_WORKFLOW_MODE into the subprocess environment so that child
    tasks submitted by the launched agent can inherit the parent's workflow_mode
    (FIX: TQMCP-2 — workflow_mode not propagated to child tasks by dispatcher).
    Agents should read FORGE_WORKFLOW_MODE when calling submit_task and pass it
    as workflow_mode if they do not have an explicit override.
    """
    target = task.get("target_agent", "")
    project_dir = AGENT_PROJECT_DIRS.get(target)
    if project_dir is None:
        handle_routing_failure(
            Path(TASK_QUEUE_DIR / f"{task.get('id', 'unknown')}.yml"),
            task,
            f"Unknown agent for headless launch: {target!r}",
        )
        return
    if not project_dir.is_dir():
        handle_routing_failure(
            Path(TASK_QUEUE_DIR / f"{task.get('id', 'unknown')}.yml"),
            task,
            f"Project dir missing: {project_dir}",
        )
        return
    task_id = task.get("id", "unknown")
    if not re.fullmatch(r"[0-9a-f\-]{36}", task_id):
        task_id = "invalid-id"
    # SECURITY[resolved]: summary removed from prompt to prevent prompt injection.
    # Agent discovers task content via task-queue tools using task_id.
    # Audit: 2026-05-29/forge-build-workflow-infra-2026-05.
    workflow_mode = task.get("workflow_mode", "semi-auto")
    # PROPAGATION SITE 1 of 3 — the env var the launched agent reads and passes back to
    # submit_task. Downgraded, not copied: see child_workflow_mode(). The task's OWN mode
    # is what still gets logged below, because that is what the operator chose; this is
    # what its children will get.
    inherited_mode = child_workflow_mode(workflow_mode)
    # Build env: inherit the running process env, layer in the target agent's
    # own .env (SCOPED_MCP_BEARER_TOKEN etc. — SMCP-28), then inject FORGE_WORKFLOW_MODE.
    # SECURITY[control]: workflow_mode is validated against VALID_WORKFLOW_MODES in task-queue-mcp
    # before reaching the dispatcher; we accept only the stored value, never user-supplied input.
    # SECURITY[deferred]: dict(os.environ) is a broad passthrough, not an explicit allowlist —
    # any var in the dispatcher's own process env reaches every headlessly launched agent.
    # Target: refactor alongside the instant-clone agent pool work. Audit: 2026-07-12/
    # dispatcher-auth-and-notify-2026-07 F-1. Ticket: MDISP-4.
    child_env = dict(os.environ)
    run_as = AGENT_RUN_AS.get(target)
    if run_as is None:
        child_env.update(load_agent_env(target))
    child_env["FORGE_WORKFLOW_MODE"] = inherited_mode
    # SMCP-28 fail-loud guard: .mcp.json's ${VAR} header interpolation only
    # supports bare $VAR/${VAR} (no bash :?/:- operators), so an unresolved
    # bearer token fails silently as a 401 deep inside the launched session
    # instead of here. Catch it before spawning a session doomed to have no
    # scoped-mcp tools.
    prompt = f"You have a pending task (id={task_id}). Check your task queue and proceed."

    if run_as is None:
        if not child_env.get("SCOPED_MCP_BEARER_TOKEN"):
            handle_routing_failure(
                TASK_QUEUE_DIR / f"{task.get('id', 'unknown')}.yml",
                task,
                f"SCOPED_MCP_BEARER_TOKEN unresolved for agent '{target}' — "
                f"refusing headless launch",
            )
            return
        # SMCP-29 auth guard: don't launch a session that will just print
        # "Not logged in" — fail loud and alert instead.
        if not anthropic_creds_usable(child_env):
            alert_auth_blocked(target, task_id)
            handle_routing_failure(
                TASK_QUEUE_DIR / f"{task.get('id', 'unknown')}.yml",
                task,
                f"No usable Anthropic credential (OAuth expired / no "
                f"ANTHROPIC_API_KEY) — refusing headless launch for '{target}'",
            )
            return
        argv = ["claude", "-p", "--dangerously-skip-permissions", prompt]
    else:
        # Run-as agent. The two guards above are deliberately skipped rather
        # than adapted: both inspect child_env, and for a run-as agent the
        # credentials are not in child_env by design — the launcher sources
        # them as the target user from a file this process cannot read. Running
        # them here would fail every launch for the one agent whose isolation
        # is working correctly.
        #
        # The launcher performs the equivalent fail-loud checks itself
        # (SCOPED_MCP_BEARER_TOKEN, TASK_QUEUE_TOKEN, GITHOST_MCP_AUTH_TOKEN,
        # CLAUDE_CODE_OAUTH_TOKEN, all `: "${VAR:?}"`-guarded by name). What is
        # NOT covered there is the launcher itself going missing — an
        # undeployed script would surface as an uncaught FileNotFoundError from
        # Popen and kill the tick for every other agent too.
        run_user, launcher = run_as
        if not os.access(launcher, os.X_OK):
            handle_routing_failure(
                TASK_QUEUE_DIR / f"{task.get('id', 'unknown')}.yml",
                task,
                f"Launcher missing or not executable for run-as agent '{target}': {launcher} "
                f"— deploy it with forge-scripts-deploy.sh",
            )
            return
        # sudo resets the environment, so FORGE_WORKFLOW_MODE cannot be passed
        # through child_env here; the launcher takes it as a validated flag and
        # re-exports it. Setting it on the sudo command line instead would need
        # a SETENV tag in sudoers, which widens that rule for no gain.
        #
        # PROPAGATION SITE 2 of 3. Because the launcher re-exports this flag verbatim as
        # FORGE_WORKFLOW_MODE, it is the same channel as site 1 and takes the same
        # downgrade — passing the parent mode here would leave the one run-as agent
        # (steward, which is precisely the agent #533 is about) as the only one whose
        # children did not inherit correctly.
        argv = [
            "sudo",
            "-n",
            "-u",
            run_user,
            launcher,
            "--workflow-mode",
            inherited_mode,
            "--",
            prompt,
        ]

    # One launch-log destination, shared with the CloudCLI task-queue plugin
    # (task-queue-plugin-repair-2026-08 Phase 3). This used to be
    # ~/.pm2/logs/agent-launch-<agent>-<task8>.log while the plugin wrote
    # ~/.claude/comms/artifacts/task-launches/<taskId>.log — two destinations for one
    # concept, so no consumer could list "the launches" without knowing both.
    #
    # ~/.claude/comms is the side both can read: it is already in the plugin's
    # PREVIEW_ALLOWED_PREFIXES, whereas ~/.pm2/logs is not and must not be added — that
    # prefix covers every PM2 service log on the host, which would make the plugin's
    # file-preview endpoint a reader of all of them.
    log_dir = Path.home() / ".claude" / "comms" / "artifacts" / "task-launches"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{target}-{task_id[:8]}.log"
    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            argv,
            cwd=str(project_dir),
            stdout=lf,
            stderr=lf,
            env=child_env,
        )
    log.info(
        f"Headless launch: {target} (pid={proc.pid}) task={task_id[:8]} "
        f"workflow_mode={workflow_mode} child_workflow_mode={inherited_mode}"
    )


# --- Temporal workflow launch ---
# SECURITY[control]: workflow_type allowlisted before subprocess invocation.
# Audit: 2026-06-02/temporal-workflow-trigger-2026-06.
ALLOWED_WORKFLOW_TYPES = {"BuildPipelineWorkflow", "BuildPlanWorkflow"}


def launch_temporal_workflow(path: Path, task: dict) -> bool:
    """Submit a Temporal workflow for a task_type=workflow task.

    Returns True on success, False on failure (failure already handled via
    handle_routing_failure before returning).
    """
    payload = task.get("payload", {})
    workflow_type = payload.get("workflow_type", "BuildPipelineWorkflow")
    if workflow_type not in ALLOWED_WORKFLOW_TYPES:
        handle_routing_failure(
            path,
            task,
            f"Unknown workflow_type: {workflow_type!r} (allowed: {ALLOWED_WORKFLOW_TYPES})",
        )
        return False
    task_id = task.get("id", "unknown")
    if not re.fullmatch(r"[0-9a-f\-]{36}", task_id):
        task_id = "invalid-id"
    plan_name = payload.get("plan_name", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", plan_name):
        handle_routing_failure(
            path, task, f"Invalid plan_name for workflow submission: {plan_name!r}"
        )
        return False
    workflow_id = f"{plan_name}-{task_id[:8]}"
    input_json = json.dumps(
        {
            "plan_name": plan_name,
            **{
                k: v
                for k, v in payload.items()
                if k not in ("workflow_type", "plan_name", "task_token")
            },
        }
    )
    log_file = Path.home() / ".pm2" / "logs" / f"temporal-start-{task_id[:8]}.log"
    try:
        with open(log_file, "a") as lf:
            result = subprocess.run(
                [str(TEMPORAL_START_SCRIPT), workflow_type, workflow_id, input_json],
                stdout=lf,
                stderr=lf,
                timeout=30,
            )
        if result.returncode != 0:
            handle_routing_failure(
                path, task, f"temporal-workflow-start.sh exited {result.returncode}"
            )
            return False
        log.info(f"Temporal workflow started: {workflow_type} id={workflow_id} task={task_id[:8]}")
        return True
    except subprocess.TimeoutExpired:
        handle_routing_failure(path, task, "temporal-workflow-start.sh timed out (30s)")
        return False


# --- Dead-letter queue ---
def move_to_dead_letter(path: Path, task: dict, reason: str) -> None:
    """Move a permanently failed task to the dead-letters directory and alert."""
    DEAD_LETTER_DIR.mkdir(exist_ok=True)
    task["failed_reason"] = {
        "timestamp": now_iso(),
        "reason": reason,
        "retry_count": task.get("retry_policy", {}).get("retry_count", 0),
    }
    dest = DEAD_LETTER_DIR / path.name
    atomic_write(dest, task)
    path.unlink(missing_ok=True)
    matrix_notify(
        "alerts",
        f"[dead-letter] {task.get('summary', path.stem)}",
        f"Task {task.get('id', path.stem)} failed after max retries.\nReason: {reason}",
    )
    log.warning(f"{path.name}: moved to dead-letters (reason={reason})")


# --- Load manifests ---
def load_manifests() -> dict:
    """Returns dict keyed by agent name."""
    manifests = {}
    for path in MANIFEST_DIR.glob("*.yml"):
        if path.name.startswith(".") or path.stem == "example-manifest":
            continue
        try:
            data = load_yaml(path)
            name = data.get("agent_type") or data.get("name")
            if name:
                manifests[name] = data
        except Exception as e:
            log.warning(f"Failed to load manifest {path}: {e}")
    return manifests


# --- Parent task lookup ---
def find_task_by_id(task_id: str) -> dict | None:
    """Search main queue and archive for a task by UUID. Returns the task dict or None."""
    for search_path in [TASK_QUEUE_DIR, ARCHIVE_DIR]:
        for yml in search_path.glob("*.yml"):
            if yml.name.startswith(".") or yml.suffix == ".tmp":
                continue
            try:
                t = load_yaml(yml)
                if t.get("id") == task_id:
                    return t
            except Exception:
                continue
    return None


# --- Auto-routing for target_agent: auto ---
def find_agent(task: dict, manifests: dict) -> str | None:
    """Match task_type + scope to an agent. Returns agent name or None."""
    task_type = task.get("task_type")
    for name, m in manifests.items():
        caps = m.get("capabilities", [])
        hosts = m.get("scope", {}).get("hosts", [])
        if task_type in caps and ("all" in hosts or "forge" in hosts):
            return name
    for name, m in manifests.items():
        if task_type in m.get("capabilities", []):
            return name
    return None


# --- Phase 1: Process submitted tasks ---
def process_submitted(manifests: dict) -> None:
    for path in sorted(TASK_QUEUE_DIR.glob("*.yml")):
        if path.name.startswith("."):
            continue
        try:
            task = load_yaml(path)
        except Exception as e:
            log.warning(f"Failed to parse {path.name}: {e}")
            continue

        if task.get("status") != "submitted":
            continue

        if not is_eligible(task):
            log.debug(f"{path.name}: retry not yet eligible, skipping")
            continue

        # Fail loudly on a value neither side of the vocabulary recognises, rather than
        # falling through to a default branch (vikunja#324). A task_type nothing routes
        # used to reach find_agent() and dead-letter with "No agent found", which reads
        # as a missing manifest capability; an unknown workflow_mode was worse, because
        # every branch below tests equality against "auto" and anything else silently
        # became operator-pickup. Both now say what actually happened.
        task_type = task.get("task_type")
        if task_type not in VALID_TASK_TYPES | DISPATCHER_ONLY_TASK_TYPES:
            handle_routing_failure(
                path,
                task,
                f"Unknown task_type {task_type!r} — not in task-queue-mcp's vocabulary "
                f"({sorted(VALID_TASK_TYPES)}). Queue file written outside submit_task?",
            )
            continue
        submitted_mode = task.get("workflow_mode", "semi-auto")
        if submitted_mode not in VALID_WORKFLOW_MODES:
            handle_routing_failure(
                path,
                task,
                f"Unknown workflow_mode {submitted_mode!r} — not in task-queue-mcp's "
                f"vocabulary ({sorted(VALID_WORKFLOW_MODES)}). Refusing to guess: "
                f"treating it as semi-auto would silently gate an auto chain, and as auto "
                f"would silently launch an ungated session.",
            )
            continue

        # A self-terminal type has no session and no target work list. submit_task writes
        # it straight to `completed`, so reaching here in `submitted` means the queue file
        # came from somewhere else. Record and close it rather than launching an agent to
        # read a notification — which is the entire point of vikunja#507.
        if task_type in SELF_TERMINAL_TASK_TYPES:
            task["status"] = "completed"
            task.setdefault("result", {})
            task["result"]["output"] = task.get("payload", {}).get("description")
            task["result"]["completed_by"] = f"dispatcher ({task_type})"
            task["result"]["completed_at"] = now_iso()
            append_history(
                task, "completed", "dispatcher", "Notification delivered — no session required"
            )
            atomic_write(path, task)
            log.info(f"{path.name}: → completed ({task_type}, no launch)")
            bus_log(
                "task.completed",
                source="dispatcher",
                summary=f"Notification delivered: {task.get('summary', path.stem)}",
                target=task.get("target_agent", ""),
                artifact_path=str(path),
            )
            # The room name is the ONE value here that is not just rendered — it selects
            # a destination. This branch returns before the routing block, so unlike the
            # semi-auto notify below, `target_agent` has NOT been through find_agent() or
            # a manifest lookup. Constrain it to a room we know, rather than passing a
            # queue file's string straight into a send call.
            notify_room = task.get("target_agent", "")
            if notify_room not in AGENT_PROJECT_DIRS:
                notify_room = "alerts"
            # SECURITY[accepted]: summary interpolated into a Matrix notification title —
            # Markdown injection possible but not HTML/JSON injection. Same disposition as
            # the semi-auto branch below; trust model is internal agents, not adversarial.
            # Audit: 2026-06-08/workflow-qol-2026-06 INFO-2. Recorded here rather than
            # inherited silently, so this instance is visible to the next audit.
            matrix_notify(
                notify_room,
                f"[notify] {task.get('summary', path.stem)}",
                f"From: {task.get('source_agent', 'unknown')}\n"
                f"No action required — this notification is already closed.",
            )
            continue

        log.info(f"Processing submitted task: {path.name}")
        publish_nats(
            "tasks.submitted",
            {
                "task_id": task.get("id"),
                "summary": task.get("summary"),
                "target_agent": task.get("target_agent"),
                "risk_level": task.get("risk_level", "low"),
            },
        )
        target = task.get("target_agent", "auto")

        # Resolve auto-routing
        if target == "auto":
            resolved = find_agent(task, manifests)
            if resolved is None:
                msg = f"No agent found for task_type={task.get('task_type')}"
                log.warning(f"{path.name}: {msg}")
                handle_routing_failure(path, task, msg)
                continue
            task["target_agent"] = resolved
            log.info(f"{path.name}: auto-routed to {resolved}")

        target_agent = task["target_agent"]

        # Inherit workflow_mode from parent task if originating_task_id is set.
        # This ensures auto-mode pipelines stay in auto across agent handoff boundaries.
        #
        # PROPAGATION SITE 3 of 3, and the one that is easy to forget: it covers a task
        # submitted by an agent that never read FORGE_WORKFLOW_MODE at all — an agent
        # started by hand, or one whose skill omits the inheritance line. Same helper as
        # sites 1 and 2 by construction, so the two directions cannot disagree.
        originating_task_id = task.get("payload", {}).get("originating_task_id")
        if originating_task_id:
            parent = find_task_by_id(originating_task_id)
            if parent:
                parent_mode = parent.get("workflow_mode")
                if parent_mode:
                    task["workflow_mode"] = child_workflow_mode(parent_mode)
                    log.info(
                        f"{path.name}: inherited workflow_mode={task['workflow_mode']} "
                        f"from parent {originating_task_id[:8]} (parent mode={parent_mode})"
                    )
            else:
                log.warning(
                    f"{path.name}: originating_task_id={originating_task_id[:8]} not found; "
                    f"keeping workflow_mode={task.get('workflow_mode', 'semi-auto')}"
                )

        manifest = manifests.get(target_agent)
        risk = task.get("risk_level", "low")

        # Determine max auto risk from manifest
        if manifest:
            max_auto = manifest.get("max_auto_risk", "low")
        else:
            max_auto = "low"
            log.warning(f"{path.name}: no manifest for agent '{target_agent}', defaulting to low")

        source_agent = task.get("submitted_by") or task.get("source_agent", "")
        interaction_perms = (manifest or {}).get("interaction_permissions", {})
        auto_approved_agents = interaction_perms.get("auto_approved", [])
        needs_approval_agents = interaction_perms.get("needs_approval", [])

        bypass = task.get("bypass_approval", False)
        explicit_approval = task.get("requires_approval")

        if bypass:
            needs_approval = False
            approval_reason = "bypass_approval=true (system override)"
        elif explicit_approval is True:
            needs_approval = True
            approval_reason = "requires_approval=true (explicit)"
        elif explicit_approval is False:
            needs_approval = False
            approval_reason = "requires_approval=false (explicit)"
        elif source_agent and source_agent in auto_approved_agents:
            needs_approval = False
            approval_reason = f"source '{source_agent}' in target manifest auto_approved list"
        elif source_agent and source_agent in needs_approval_agents:
            needs_approval = True
            approval_reason = f"source '{source_agent}' in target manifest needs_approval list"
        else:
            needs_approval = RISK_ORDER.get(risk, 0) > RISK_ORDER.get(max_auto, 0)
            approval_reason = f"risk={risk} vs max_auto_risk={max_auto} (fallback)"

        log.info(f"{path.name}: approval={needs_approval} — {approval_reason}")

        if needs_approval:
            task["status"] = "pending-approval"
            append_history(
                task, "pending-approval", "dispatcher", f"Needs approval: {approval_reason}"
            )
            atomic_write(path, task)
            publish_nats(
                "tasks.approval-requested",
                {
                    "task_id": task.get("id"),
                    "target_agent": target_agent,
                    "risk_level": risk,
                },
            )
            bus_log(
                "task.dispatched",
                source="dispatcher",
                summary=f"Dispatched for approval: {task.get('summary', path.stem)}",
                target=target_agent,
                artifact_path=str(path),
            )
            log.info(f"{path.name}: → pending-approval (risk={risk}, max_auto={max_auto})")
            matrix_notify(
                "approvals",
                f"[APPROVAL NEEDED] {task.get('summary', path.stem)}",
                f"Source: {task.get('source_agent')} | Type: {task.get('task_type')} "
                f"| Risk: {risk} | Agent: {target_agent}\n"
                f"`task-approve {task.get('id', path.stem)}`",
            )
        else:
            task["status"] = "approved"
            append_history(task, "approved", "dispatcher", f"Auto-approved: {approval_reason}")
            atomic_write(path, task)
            publish_nats(
                "tasks.approved",
                {
                    "task_id": task.get("id"),
                    "target_agent": target_agent,
                    "summary": task.get("summary"),
                },
            )
            if task.get("task_type") == "workflow":
                if launch_temporal_workflow(path, task):
                    task["status"] = "in-progress"
                    append_history(task, "in-progress", "dispatcher", "Temporal workflow submitted")
                    atomic_write(path, task)
                    bus_log(
                        "task.workflow_started",
                        source="dispatcher",
                        summary=f"Temporal workflow started: {task.get('summary', path.stem)}",
                        target=target_agent,
                        artifact_path=str(path),
                    )
                    log.info(f"{path.name}: → in-progress (temporal workflow)")
                    matrix_notify(
                        "announcements",
                        f"[temporal] {task.get('summary', path.stem)}",
                        f"Workflow: "
                        f"{task.get('payload', {}).get('workflow_type', 'BuildPipelineWorkflow')}"
                        f" | plan: {task.get('payload', {}).get('plan_name', '?')}",
                    )
                continue
            # NOTE: this branch is evaluated BEFORE the workflow_mode check below, so an
            # audit task launches headlessly even in semi-auto. That is deliberate — an
            # audit is a fixed, bounded, read-only procedure the operator has already
            # implicitly approved by requesting it, and gating it behind a manual resume
            # would stall every build at the same point. Recorded here because it reads
            # like an oversight and has been questioned more than once.
            elif task.get("task_type") == "audit" and target_agent == "security":
                payload = task.get("payload", {})
                request_path_str = payload.get("request", "") or next(
                    (r for r in (payload.get("context_refs") or []) if "audit-requests" in r), ""
                )
                build_name = (
                    Path(request_path_str).parent.name
                    if request_path_str
                    else next(
                        iter(
                            re.findall(
                                r"audit-requests/([a-zA-Z0-9_\-]+)", payload.get("description", "")
                            )
                        ),
                        "unknown",
                    )
                )
                # SECURITY[resolved]: reject "unknown" build_name to prevent silent
                # non-functional audit launches.
                # Audit: 2026-05-29/forge-build-workflow-infra-2026-05.
                if build_name == "unknown" or not re.fullmatch(r"[a-zA-Z0-9_\-]+", build_name):
                    handle_routing_failure(
                        path, task, f"Invalid or missing build_name in payload: {build_name!r}"
                    )
                    continue
                audit_root = (Path.home() / ".claude/comms/artifacts/audit-requests").resolve()
                request_path = (
                    Path(request_path_str).expanduser().resolve()
                    if request_path_str
                    else (audit_root / build_name / "request.md")
                )
                try:
                    request_path.relative_to(audit_root)
                except ValueError:
                    handle_routing_failure(
                        path, task, f"request_path outside audit-requests: {request_path}"
                    )
                    continue
                # SECURITY[resolved]: verify request_path exists on disk before launching.
                # Audit: 2026-05-29/forge-build-workflow-infra-2026-05.
                if not request_path.exists():
                    handle_routing_failure(
                        path, task, f"request_path does not exist: {request_path}"
                    )
                    continue
                security_project_dir = Path.home() / ".claude" / "projects" / "security"
                audit_log = Path.home() / ".pm2" / "logs" / f"security-audit-{build_name}.log"
                # SMCP-28: layer in security agent's .env (SCOPED_MCP_BEARER_TOKEN etc.)
                audit_env = dict(os.environ)
                audit_env.update(load_agent_env("security"))
                if not audit_env.get("SCOPED_MCP_BEARER_TOKEN"):
                    handle_routing_failure(
                        path,
                        task,
                        "SCOPED_MCP_BEARER_TOKEN unresolved for agent 'security' — "
                        "refusing headless audit launch",
                    )
                    continue
                if not anthropic_creds_usable(audit_env):  # SMCP-29 auth guard
                    alert_auth_blocked("security", task.get("id", "unknown"))
                    handle_routing_failure(
                        path,
                        task,
                        "No usable Anthropic credential (OAuth expired / no "
                        "ANTHROPIC_API_KEY) — refusing headless audit launch",
                    )
                    continue
                # Name the task in the prompt. Headlessly this is the only way the security
                # agent learns which queue entry to claim and close — it has no session-start
                # sweep to fall back on, and build_name alone does not identify a task. Same
                # guard as launch_agent_headless: a malformed id degrades to a literal rather
                # than reaching the prompt. Still no `summary` — that was deliberately
                # removed as a prompt-injection vector.
                audit_task_id = task.get("id", "unknown")
                if not re.fullmatch(r"[0-9a-f\-]{36}", audit_task_id):
                    audit_task_id = "invalid-id"
                with open(audit_log, "a") as audit_log_fh:
                    proc = subprocess.Popen(
                        [
                            "claude",
                            "-p",
                            "--dangerously-skip-permissions",
                            f"Run security audit for build: {build_name}. "
                            f"Task ID: {audit_task_id}. "
                            f"Request at: {request_path}",
                        ],
                        cwd=str(security_project_dir),
                        stdout=audit_log_fh,
                        stderr=audit_log_fh,
                        env=audit_env,
                    )
                log.info(
                    f"{path.name}: headless audit launched for {build_name} "
                    f"(pid={proc.pid}) log={audit_log}"
                )
            else:
                workflow_mode = task.get("workflow_mode", "semi-auto")
                # DO NOT rewrite this as `!= "semi-auto"`. It reads like a tidy-up and it
                # is the one edit that would silently auto-launch `manual-then-auto`,
                # destroying the only property that mode has: that its own leg waits for
                # an operator. An equality test against "auto" is load-bearing — every
                # mode that is not literally "auto" must fall to operator pickup.
                if workflow_mode == "auto":
                    # workflow_mode is the only switch here. An older comment mentioned an
                    # `auto_start` flag as an alternative trigger; no such field is a
                    # submit_task parameter, none is written to any task, and nothing reads
                    # one — it is gone from the agent docs too as of 2026-08-16.
                    log.info(f"{path.name}: workflow_mode=auto — launching headless")
                    launch_agent_headless(task)
                else:
                    # semi-auto mode (default): queue for operator pickup, notify agent room
                    log.info(
                        f"{path.name}: workflow_mode={workflow_mode} — queued for operator pickup"
                    )
                    task_id = task.get("id", path.stem)
                    # SECURITY[accepted]: summary interpolated into Matrix notification
                    # title — Markdown injection possible but not HTML/JSON injection.
                    # Trust model: internal agents not adversarial. Pre-existing pattern.
                    # Audit: 2026-06-08/workflow-qol-2026-06 INFO-1.
                    summary = task.get("summary", path.stem)
                    source = task.get("source_agent", "unknown")
                    matrix_notify(
                        target_agent,
                        f"[task ready] {summary}",
                        f"Task ID: {task_id}\nFrom: {source} | Risk: {risk}\n"
                        f"Resume: Check task queue (id={task_id}) and run shared-build-review.",
                    )
            bus_log(
                "task.approved",
                source="dispatcher",
                summary=task.get("summary", path.stem),
                target=target_agent,
                artifact_path=str(path),
            )
            log.info(f"{path.name}: → approved (auto)")
            matrix_notify(
                "announcements",
                f"[auto-approved] {task.get('summary', path.stem)}",
                f"Agent: {target_agent} | Risk: {risk} | "
                f"Mode: {task.get('workflow_mode', 'semi-auto')}",
            )


# --- Phase 3: Archive terminal tasks past TTL ---
def archive_expired() -> None:
    ARCHIVE_DIR.mkdir(exist_ok=True)
    now = datetime.now(UTC)
    for path in sorted(TASK_QUEUE_DIR.glob("*.yml")):
        if path.name.startswith("."):
            continue
        try:
            task = load_yaml(path)
        except Exception:
            continue

        if task.get("status") not in TERMINAL_STATES:
            continue

        ttl_days = task.get("ttl_days", 30)
        created_str = task.get("created", "")
        try:
            created = datetime.fromisoformat(str(created_str))
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue

        age_days = (now - created).days
        if age_days >= ttl_days:
            dest = ARCHIVE_DIR / path.name
            path.rename(dest)
            log.info(
                f"Archived {path.name} (age={age_days}d, ttl={ttl_days}d, status={task['status']})"
            )


# --- Phase 1b: Retry routing-failed tasks (FIX: TQMCP-1/MDISP-1) ---
def process_routing_failed(manifests: dict) -> None:
    """Retry tasks that failed routing but haven't exhausted their retry budget.

    These tasks sit in "routing-failed" status between retries (not "submitted"),
    so they skip the approval pipeline — they were already approved.  When the
    retry window passes, this function picks them up and attempts routing directly,
    jumping to the post-approval dispatch logic.

    If routing succeeds the task transitions normally (in-progress, approved, etc.).
    If it fails again, handle_routing_failure() increments the counter and parks
    the task in "routing-failed" again until the next window or dead-letter.
    """
    for path in sorted(TASK_QUEUE_DIR.glob("*.yml")):
        if path.name.startswith("."):
            continue
        try:
            task = load_yaml(path)
        except Exception as e:
            log.warning(f"Failed to parse {path.name}: {e}")
            continue

        if task.get("status") != "routing-failed":
            continue

        if not is_eligible(task):
            log.debug(f"{path.name}: routing-failed retry not yet eligible, skipping")
            continue

        policy = task.get("retry_policy", {})
        retry_count = policy.get("retry_count", 0)
        max_retries = policy.get("max_retries", 3)
        log.info(
            f"{path.name}: retrying routing-failed task "
            f"(attempt {retry_count}/{max_retries}): "
            f"{policy.get('last_failure_reason', '?')}"
        )

        # The same vocabulary guards process_submitted applies. Without them the retry
        # pass is a way around them: an unknown workflow_mode dead-lettered on the first
        # tick would come back here five minutes later and fall through the `== "auto"`
        # test into operator-pickup, quietly reaching the state the guard exists to
        # refuse. A guard only on the first pass is not a guard.
        retry_type = task.get("task_type")
        if retry_type not in VALID_TASK_TYPES | DISPATCHER_ONLY_TASK_TYPES:
            handle_routing_failure(
                path,
                task,
                f"Unknown task_type {retry_type!r} (routing-failed retry) — not in "
                f"task-queue-mcp's vocabulary ({sorted(VALID_TASK_TYPES)})",
            )
            continue
        retry_mode = task.get("workflow_mode", "semi-auto")
        if retry_mode not in VALID_WORKFLOW_MODES:
            handle_routing_failure(
                path,
                task,
                f"Unknown workflow_mode {retry_mode!r} (routing-failed retry) — not in "
                f"task-queue-mcp's vocabulary ({sorted(VALID_WORKFLOW_MODES)})",
            )
            continue
        if retry_type in SELF_TERMINAL_TASK_TYPES:
            task["status"] = "completed"
            task.setdefault("result", {})
            task["result"]["output"] = task.get("payload", {}).get("description")
            task["result"]["completed_by"] = f"dispatcher ({retry_type})"
            task["result"]["completed_at"] = now_iso()
            append_history(
                task,
                "completed",
                "dispatcher",
                "Notification delivered — no session required (routing-failed retry)",
            )
            atomic_write(path, task)
            log.info(f"{path.name}: → completed ({retry_type}, no launch, routing-failed retry)")
            continue

        # Re-attempt routing: resolve target and dispatch exactly as process_submitted
        # would after approval — but skip the approval check since it already passed.
        target_agent = task.get("target_agent", "")
        if not target_agent or target_agent == "auto":
            resolved = find_agent(task, manifests)
            if resolved is None:
                handle_routing_failure(
                    path,
                    task,
                    f"No agent found for task_type={task.get('task_type')} (routing-failed retry)",
                )
                continue
            task["target_agent"] = resolved
            target_agent = resolved

        if task.get("task_type") == "workflow":
            if launch_temporal_workflow(path, task):
                task["status"] = "in-progress"
                append_history(
                    task,
                    "in-progress",
                    "dispatcher",
                    "Temporal workflow submitted (routing-failed retry)",
                )
                atomic_write(path, task)
                log.info(f"{path.name}: → in-progress (temporal, routing-failed retry)")
            continue

        if task.get("task_type") == "audit" and target_agent == "security":
            payload = task.get("payload", {})
            request_path_str = payload.get("request", "") or next(
                (r for r in (payload.get("context_refs") or []) if "audit-requests" in r), ""
            )
            build_name = (
                Path(request_path_str).parent.name
                if request_path_str
                else next(
                    iter(
                        re.findall(
                            r"audit-requests/([a-zA-Z0-9_\-]+)", payload.get("description", "")
                        )
                    ),
                    "unknown",
                )
            )
            if build_name == "unknown" or not re.fullmatch(r"[a-zA-Z0-9_\-]+", build_name):
                handle_routing_failure(
                    path, task, f"Invalid or missing build_name in payload: {build_name!r}"
                )
                continue
            audit_root = (Path.home() / ".claude/comms/artifacts/audit-requests").resolve()
            request_path = (
                Path(request_path_str).expanduser().resolve()
                if request_path_str
                else (audit_root / build_name / "request.md")
            )
            try:
                request_path.relative_to(audit_root)
            except ValueError:
                handle_routing_failure(
                    path, task, f"request_path outside audit-requests: {request_path}"
                )
                continue
            if not request_path.exists():
                handle_routing_failure(path, task, f"request_path does not exist: {request_path}")
                continue
            security_project_dir = Path.home() / ".claude" / "projects" / "security"
            audit_log = Path.home() / ".pm2" / "logs" / f"security-audit-{build_name}.log"
            # SMCP-28: layer in security agent's .env (SCOPED_MCP_BEARER_TOKEN etc.)
            audit_env = dict(os.environ)
            audit_env.update(load_agent_env("security"))
            if not audit_env.get("SCOPED_MCP_BEARER_TOKEN"):
                handle_routing_failure(
                    path,
                    task,
                    "SCOPED_MCP_BEARER_TOKEN unresolved for agent 'security' — "
                    "refusing headless audit launch",
                )
                continue
            if not anthropic_creds_usable(audit_env):  # SMCP-29 auth guard
                alert_auth_blocked("security", task.get("id", "unknown"))
                handle_routing_failure(
                    path,
                    task,
                    "No usable Anthropic credential (OAuth expired / no "
                    "ANTHROPIC_API_KEY) — refusing headless audit launch",
                )
                continue
            with open(audit_log, "a") as audit_log_fh:
                proc = subprocess.Popen(
                    [
                        "claude",
                        "-p",
                        "--dangerously-skip-permissions",
                        f"Run security audit for build: {build_name}. Request at: {request_path}",
                    ],
                    cwd=str(security_project_dir),
                    stdout=audit_log_fh,
                    stderr=audit_log_fh,
                    env=audit_env,
                )
            task["status"] = "approved"
            append_history(task, "approved", "dispatcher", "Routing retry succeeded")
            atomic_write(path, task)
            log.info(
                f"{path.name}: headless audit launched for {build_name} "
                f"(pid={proc.pid}) — routing-failed retry"
            )
            continue

        # Generic task: launch headless or queue for operator pickup.
        # Equality against "auto" is load-bearing here too — see the note on the matching
        # branch in process_submitted.
        workflow_mode = task.get("workflow_mode", "semi-auto")
        if workflow_mode == "auto":
            launch_agent_headless(task)
        else:
            task_id = task.get("id", path.stem)
            summary = task.get("summary", path.stem)
            source = task.get("source_agent", "unknown")
            risk = task.get("risk_level", "low")
            matrix_notify(
                target_agent,
                f"[task ready] {summary}",
                f"Task ID: {task_id}\nFrom: {source} | Risk: {risk}\n"
                f"Resume: Check task queue (id={task_id}) and run shared-build-review.",
            )
        task["status"] = "approved"
        append_history(task, "approved", "dispatcher", "Routing retry succeeded")
        atomic_write(path, task)
        log.info(f"{path.name}: → approved (routing-failed retry, workflow_mode={workflow_mode})")


# --- Main ---
def main():
    # NOTE: --version is NOT handled here. It is answered in task_dispatcher._console
    # before this module is imported, because importing this module loads the roster and
    # raises when it cannot. See the docstring on _console() for why that ordering
    # matters. By the time main() runs, the roster has already loaded successfully.
    log.info("=== task-dispatcher run start ===")
    manifests = load_manifests()
    log.info(f"Loaded {len(manifests)} agent manifests: {list(manifests.keys())}")

    process_submitted(manifests)
    process_routing_failed(manifests)
    archive_expired()

    log.info("=== task-dispatcher run complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
