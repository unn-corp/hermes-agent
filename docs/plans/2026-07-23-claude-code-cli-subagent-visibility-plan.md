# Claude Code CLI Sub-Agent Visibility (Phase 3 of 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface Claude Code's own Task-tool sub-agent invocations as inline collapsed blocks in Hermes Desktop's conversation view — a dedicated `claude_subagent_task` tool_call, durably persisted per `(session_id, task_id)`, fetchable in full via a gateway RPC on expand.

**Architecture:** `agent/claude_code_runtime.py`'s `make_claude_code_sdk_event_bridge` (Phase 1) gains a second message-type branch: alongside its existing per-block `AssistantMessage.content` dispatch (`ToolUseBlock`/`ToolResultBlock`/`TextBlock`), it now also recognizes four top-level SDK message types — `TaskStartedMessage`, `TaskProgressMessage`, `TaskUpdatedMessage`, `TaskNotificationMessage` — and the sub-agent's own `AssistantMessage`s (identified by a non-null `parent_tool_use_id` matching a tracked Task invocation). The bridge fires the existing `agent.tool_start_callback`/`tool_complete_callback` pair (the same ones ordinary tools already use to create persisted, hydrated tool_call rows) with `name="claude_subagent_task"`, and mirrors the full ordered event list into a new `subagent_transcripts` sqlite table in Hermes' existing `hermes_state.SessionDB`. A new `subagent_transcript.get` gateway RPC (`tui_gateway/server.py`, the existing `@method(...)` / `_methods` dispatch convention) reads that table. Desktop gets one new component, `apps/desktop/src/components/assistant-ui/tool/subagent-task.tsx`, wired into `message-parts.tsx`'s existing `ChainToolFallback` string-equality dispatch, which fetches the full transcript via that RPC on expand.

**Tech Stack:** Python 3 (`hermes-agent/`), `pytest` (`./scripts/run_tests.sh`, no new dependency), TypeScript/React (`apps/desktop/`), Vitest (`npx vitest run --project ui`).

---

## Investigation: transcript persistence approach

The design doc (`docs/design/claude-code-integration.md`, component 4) assumed a hand-rolled `subagent_transcripts` sqlite table without checking whether the installed SDK already solves this. It also assumed generic `task_id`/`subagent_type` fields on ordinary messages, which is wrong (see Global Constraints for the real field names). This section documents what was actually found in the installed `claude-agent-sdk==0.2.126` package and states a recommendation.

**Where verified:** `hermes-agent/.worktrees/claude-code-sdk-transport-impl/.venv/lib/python3.12/site-packages/claude_agent_sdk/` (the venv for Phase 1's in-progress implementation worktree; confirmed present and installed at plan-writing time via direct source read of `types.py`, `_internal/sessions.py`, `_internal/session_store.py`).

**Option A — `SessionStore` Protocol (`ClaudeAgentOptions.session_store=`).** The SDK exposes `SessionStore` as a `typing.Protocol` (`types.py:1460`) with two required async methods (`append(key, entries)`, `load(key)`) plus optional ones (`list_sessions`, `list_session_summaries`, `delete`, `list_subkeys`). Only one concrete implementation ships: `InMemorySessionStore` (`_internal/session_store.py`), whose own docstring says plainly: *"Not suitable for production — data is lost when the process exits."* There is no built-in persistent adapter. Using this path would require Hermes to:
1. Implement the full async Protocol itself (still real, non-trivial work — nothing is "free").
2. Do it *inside* `ClaudeCodeSdkClient`'s dedicated background-thread event loop (Phase 1's `_run_loop`/`_async_main`), since that's the only asyncio context available and Hermes' hard rule is that "no asyncio may leak past `agent/transports/claude_code_sdk.py`'s internal boundary" — meaning any blocking sqlite call inside `append()`/`load()` would need `loop.run_in_executor(...)` to avoid stalling that client's own message pump.
3. Parse `SessionStoreEntry` — documented as *"the CLI's on-disk transcript format (a large discriminated union). That union is internal... adapters should treat entries as pass-through blobs"* — i.e. raw, undocumented JSONL blobs, not the already-typed dataclasses (`TaskStartedMessage` etc.) the event bridge already receives directly from `client.receive_messages()`.
4. Mirror **the entire conversation** (main transcript + all subagent subpaths), since `session_store` is a global per-client option, not scoped to just subagent data — much more surface than Hermes actually needs for this feature.

**Option B — CLI's own on-disk subagent transcripts (`list_subagents()` / `get_subagent_messages()`).** The SDK also ships synchronous, filesystem-based helpers (`_internal/sessions.py:1281,1323`) that read `~/.claude/projects/<project>/<sessionId>/subagents/agent-<agentId>.jsonl` directly off disk (or the store-backed async siblings `list_subagents_from_store`/`get_subagent_messages_from_store`, which have the same Option-A caveats). Rejected for three concrete reasons found in the source:
- The docstring for `SessionStore` itself notes local-disk transcripts *"are swept by the existing `cleanupPeriodDays` setting"* — i.e. the CLI can and does delete these files on its own retention schedule, so they are **not guaranteed durable** the way Hermes' own sqlite db is.
- `get_subagent_messages(session_id, agent_id, directory)` requires an `agent_id` — the on-disk filename slug — and nothing in the SDK source confirms `agent_id == task_id` (the ID `TaskStartedMessage` actually gives Hermes). That filename is written by the `claude` binary itself (not this Python package), so the mapping is unverifiable from the Python SDK alone. Building the RPC around an unverified ID mapping is fragile.
- It also needs the CLI-internal `session_id` (a *different* session id than Hermes' own — `TaskStartedMessage.session_id` is Claude's own conversation id, not Hermes') plus the exact working directory used at spawn time, both of which Hermes would have to track separately just to re-derive a filesystem path — more bookkeeping than a sqlite row.

**Option C (recommended) — hand-rolled `subagent_transcripts` table, populated from data the bridge already has.** The key discovery that makes this clean: **`AssistantMessage` and `UserMessage` both carry a `parent_tool_use_id: str | None` field** (`types.py`, `AssistantMessage` ~line 1030, `UserMessage` ~line 1018). When the Task tool spawns a sub-agent, that sub-agent's *own* `AssistantMessage`s stream through the exact same `client.receive_messages()` iterator the main turn already uses, tagged with `parent_tool_use_id` equal to the `tool_use_id` of the Task invocation's `ToolUseBlock` (which `TaskStartedMessage.tool_use_id` also references). This means the event bridge — which already consumes every message synchronously, on the same thread that already writes to Hermes' existing sqlite db for usage recording (`_record_claude_code_sdk_usage`, Phase 1 Task 7) — can assemble the **entire** ordered sub-agent transcript (its own text + tool calls/results) purely by watching `parent_tool_use_id`, with zero new async boundaries, zero raw-JSONL parsing, and zero dependency on an unverified on-disk ID mapping.

**Recommendation: Option C.** Add a `subagent_transcripts` table to Hermes' existing `hermes_state.SessionDB` (the same db `update_token_counts` already writes into), keyed by `(session_id, task_id)` using Hermes' *own* `session_id` (not Claude's internal one, which is a different value the RPC caller — Desktop — never has). Write to it synchronously from the event bridge, using the exact same call path and error-handling shape `_record_claude_code_sdk_usage` already established. This is strictly less code than implementing `SessionStore`, avoids the async-boundary and retention-sweep risks of Options A/B, and gives Hermes full schema control matching exactly what `subagent_transcript.get` and the Desktop component need. Task 1 below builds the table; Task 2 builds the bridge logic that populates it via `parent_tool_use_id` correlation.

---

## Global Constraints

- **Naming (hard rule):** the new tool_call's `name` is exactly `claude_subagent_task` — never the bare string `"claude_code"` (`agent/credential_sources.py:399-403` already uses that literal for an unrelated OAuth-credential-source id) and never `"subagent_task"` (too close to the *existing, separate* kanban/delegate_task subagent system's own event-type namespace — see the note on `event_type.startswith("subagent.")` below).
- **Real Task-message field names** (verified directly against installed `claude-agent-sdk==0.2.126`'s `types.py`; the design doc's generic `task_id`/`subagent_type` assumption was imprecise):
  - `TaskStartedMessage(subtype, data, task_id: str, description: str, uuid: str, session_id: str, tool_use_id: str | None = None, task_type: str | None = None)`
  - `TaskProgressMessage(subtype, data, task_id: str, description: str, usage: TaskUsage, uuid: str, session_id: str, tool_use_id: str | None = None, last_tool_name: str | None = None)`
  - `TaskNotificationMessage(subtype, data, task_id: str, status: Literal['completed','failed','stopped'], output_file: str, summary: str, uuid: str, session_id: str, tool_use_id: str | None = None, usage: TaskUsage | None = None)`
  - `TaskUpdatedMessage(subtype, data, task_id: str, patch: dict, status: Literal['pending','running','paused','completed','failed','killed'] | None = None, session_id: str | None = None, uuid: str | None = None)`
  - All four are dataclass subclasses of `SystemMessage` — safe to duck-type via `type(message).__name__` string equality, matching the bridge's existing dispatch convention (see next bullet).
  - `TaskStartedMessage.session_id` / the top-level `ResultMessage.session_id` etc. are Claude's **own** internal conversation id — a different value than Hermes' `agent.session_id`. This plan's `subagent_transcripts` table is keyed by Hermes' `session_id` throughout, never Claude's.
  - The SDK's own `TERMINAL_TASK_STATUSES` constant is `frozenset({"completed", "failed", "stopped", "killed"})` — this plan defines a local mirror rather than importing it, consistent with the "import the SDK lazily, never at module top level" rule Phase 1 established (`agent/claude_code_runtime.py` must stay importable with `claude-agent-sdk` uninstalled).
- **Dispatch convention is exact string-name matching, not `isinstance`.** Phase 1's event bridge (once landed) dispatches on `type(block).__name__ == "ToolUseBlock"` etc. — a plain string comparison, not an import of the real SDK classes. **Note for whoever implements this plan:** Phase 1's own plan document (`docs/plans/2026-07-23-claude-code-sdk-transport-plan.md`, Task 6, Step 1) writes its test fixtures as `class _FakeToolUseBlock: ...` / `class _FakeToolResultBlock: ...` / `class _FakeTextBlock: ...` (with a `_Fake` prefix), while the implementation it tests checks `type(block).__name__ == "ToolUseBlock"` (no prefix) — those will never match, since `type(x).__name__` reflects the actual defined class name, not any semantic role. If Phase 1's own tests are failing for this reason when you start this plan, that's a pre-existing Phase 1 issue to resolve there (out of scope here — Phase 1 is a separate, in-progress workstream this plan must not touch). **This plan's own new test fixtures avoid that mistake**: every fake class this plan adds that participates in `type(x).__name__` dispatch (`TaskStartedMessage`, `TaskProgressMessage`, `TaskUpdatedMessage`, `TaskNotificationMessage`, and the `TextBlock`/`ToolUseBlock`/`ToolResultBlock` fakes used for sub-agent content projection) is named **exactly** the string the dispatch code checks for — no `_Fake` prefix — because the class name IS the dispatch key.
- **Authoritative tool-call callbacks are `tool_start_callback`/`tool_complete_callback`, not `tool_progress_callback`.** Verified against `tui_gateway/server.py`: `_on_tool_progress` (the `tool_progress_callback` consumer) explicitly **ignores** `event_type == "tool.started"` (`tui_gateway/server.py:4385-4388`, comment: *"`_on_tool_start` already emits the authoritative `tool.start`... Emitting another id-less progress row here makes the desktop live view diverge from hydrated history"*). The row Desktop's `message-parts.tsx` keys off by `toolName` is created by `agent.tool_start_callback(tool_call_id: str, name: str, args: dict) -> None` and closed by `agent.tool_complete_callback(tool_call_id: str, name: str, args: dict, result: str) -> None` (confirmed exact signatures: `tui_gateway/server.py:4582-4585`, `_on_tool_start`/`_on_tool_complete` at `tui_gateway/server.py:4301,4328`). This plan's event-bridge task fires **both** of those (for the authoritative row) — mirroring `agent/codex_runtime.py`'s real, already-shipped pattern of calling `tool_start_callback`/`tool_complete_callback` *in addition to* `tool_progress_callback` (`agent/codex_runtime.py:499,533`) — not just `tool_progress_callback` alone the way Phase 1's own Task 6 sketch does for ordinary tool calls.
- **Never reuse the existing `subagent.*` event-type namespace.** `tui_gateway/server.py:4427` already branches on `event_type.startswith("subagent.")` for the *separate*, existing kanban/delegate_task subagent system (`apps/desktop/src/store/subagents.ts`, its own panel at `apps/desktop/src/app/agents/index.tsx`). This plan's new tool_call is deliberately **not** routed through that namespace or that store — see "Deliberate deviations" below.
- **Result truncation convention:** the main conversation's persisted `claude_subagent_task` tool_call's `result` string is a compact JSON summary truncated the same way Codex's `_codex_item_completion_payload` already truncates (`agent/codex_runtime.py`, `json.dumps(...)[:4000]`) — never the full nested transcript inline. Full detail is fetched separately via `subagent_transcript.get`.
- **RPC registration convention (verified real code, not invented syntax):** `tui_gateway/server.py` registers JSON-RPC methods via a `@method("name.verb")` decorator (`tui_gateway/server.py:1520`) that populates a module-level `_methods: dict[str, Callable]`; handlers have the signature `fn(rid, params: dict) -> dict`, returning `_ok(rid, result_dict)` or `_err(rid, code, message)` (both defined at `tui_gateway/server.py:1512-1516`). This plan's RPC follows that exact convention — not the design doc's placeholder syntax.
- **Desktop RPC call convention (verified real code):** components call `gateway.request<T>(method, params)` off the `$gateway` nanostore (`apps/desktop/src/store/gateway.ts`), read via `useStore($gateway)` — confirmed in `apps/desktop/src/components/assistant-ui/tool/approval.tsx:108,145` and `apps/desktop/src/components/assistant-ui/clarify-tool.tsx:286,343`.
- **Desktop dispatch convention (verified real code):** `apps/desktop/src/components/assistant-ui/thread/message-parts.tsx`'s `ChainToolFallback` (lines 39-51) does a plain `if (props.toolName === '<name>') return <Component {...props} />` chain, passed to assistant-ui as `tools: { Fallback: ChainToolFallback }` (line 221). One new branch is added for `'claude_subagent_task'`.
- **File placement (verified real code):** new component at `apps/desktop/src/components/assistant-ui/tool/subagent-task.tsx` with its test co-located as `subagent-task.test.tsx` — matching where `approval.tsx`/`approval.test.tsx` actually live (`apps/desktop/src/components/assistant-ui/tool/`), **not** next to `clarify-tool.tsx`, which lives one directory up at `apps/desktop/src/components/assistant-ui/clarify-tool.tsx` directly.
- **Collapsed-block disclosure state** uses the existing shared mechanism other tool rows already use — `toolPartDisclosureId(part)` / `$toolDisclosureOpen(id)` / `setToolDisclosureOpen(id, open)` (`apps/desktop/src/components/assistant-ui/tool/fallback-model/targets.ts:30`, `apps/desktop/src/store/tool-view.ts:79`) — not fresh local-only `useState`, so collapse state persists like every other tool row.
- **i18n is deliberately out of scope for this plan's new copy strings.** `apps/desktop/src/i18n/en.ts`/`zh.ts`/`zh-hant.ts`/`ja.ts` are all typed as the exact `Translations` shape (`export const en: Translations = {...}`, `export const zh: Translations = {...}`) — adding a new key group would require editing all four locale files plus `types.ts` with real translations this plan cannot authentically produce, and the task's own "what Phase 3 must cover" list does not mention localization. `subagent-task.tsx` uses plain literal English strings with a code comment flagging this as a deliberate, explicit scope decision (not an oversight) — a follow-up plan can localize it.
- **Schema migrations in `hermes_state.py` are additive-only here.** `SCHEMA_SQL`'s `CREATE TABLE IF NOT EXISTS` statements run unconditionally via `cursor.executescript(SCHEMA_SQL)` on every DB open (`hermes_state.py:2871`) — a brand-new table needs no version-gated migration block (those are only needed for destructive changes like column/PK rebuilds, e.g. the real `current_version < 22` example at `hermes_state.py:3061-3121`). `SCHEMA_VERSION` is still bumped from 23 to 24 as bookkeeping, per existing convention.
- **Depends on Phase 1 having landed** (`agent/claude_code_runtime.py`'s `make_claude_code_sdk_event_bridge`, `agent/transports/claude_code_sdk_session.py`'s `ClaudeCodeSdkTurnSession`) and is independent of Phase 2 (multi-account) — this plan does not touch `agent/cli_accounts.py` or account switching. If Phase 1 has not landed yet on this branch, land it first, then resume here.
- No AI attribution in any commit message (hard rule — do not add `Co-Authored-By` or similar trailers).
- Full spec: `hermes-agent/docs/design/claude-code-integration.md` (components 2, 4, 6). Phase 1's confirmed real signatures: `hermes-agent/docs/plans/2026-07-23-claude-code-sdk-transport-plan.md`.

---

### Task 1: `subagent_transcripts` table + `SessionDB` persistence methods

**Files:**
- Modify: `hermes-agent/hermes_state.py` (`SCHEMA_VERSION` at line 156, `SCHEMA_SQL` at line 995-1147, new methods added near `update_token_counts`/`get_session`)
- Test: `hermes-agent/tests/test_hermes_state_subagent_transcripts.py`

**Interfaces:**
- Consumes: nothing new — `SessionDB._execute_write(fn)`, `SessionDB._conn`, `SessionDB._lock` (all existing, real).
- Produces: `SessionDB.upsert_subagent_transcript(session_id: str, task_id: str, *, tool_use_id: Optional[str] = None, description: Optional[str] = None, status: str = "running", events: Optional[List[Dict[str, Any]]] = None, summary: Optional[str] = None) -> None`, `SessionDB.get_subagent_transcript(session_id: str, task_id: str) -> Optional[Dict[str, Any]]`.

- [ ] **Step 1: Write the failing tests**

```python
# hermes-agent/tests/test_hermes_state_subagent_transcripts.py
"""Tests for the subagent_transcripts table (Phase 3, sub-agent visibility).

Mirrors tests/test_hermes_state.py's SessionDB(db_path=tmp_path / ...) setup.
"""
from __future__ import annotations

from hermes_state import SessionDB


def _make_db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="cli")
    return db


def test_get_subagent_transcript_returns_none_when_never_recorded(tmp_path):
    db = _make_db(tmp_path)
    assert db.get_subagent_transcript("s1", "task-1") is None


def test_upsert_then_get_round_trips_all_fields(tmp_path):
    db = _make_db(tmp_path)
    db.upsert_subagent_transcript(
        "s1",
        "task-1",
        tool_use_id="tu_1",
        description="Investigate failing test",
        status="running",
        events=[{"type": "text", "text": "looking into it"}],
        summary=None,
    )

    row = db.get_subagent_transcript("s1", "task-1")

    assert row is not None
    assert row["session_id"] == "s1"
    assert row["task_id"] == "task-1"
    assert row["tool_use_id"] == "tu_1"
    assert row["description"] == "Investigate failing test"
    assert row["status"] == "running"
    assert row["events"] == [{"type": "text", "text": "looking into it"}]
    assert row["summary"] is None


def test_upsert_replaces_events_and_status_on_repeated_calls(tmp_path):
    db = _make_db(tmp_path)
    db.upsert_subagent_transcript(
        "s1", "task-1", tool_use_id="tu_1", description="first",
        status="running", events=[{"type": "text", "text": "step 1"}],
    )
    db.upsert_subagent_transcript(
        "s1", "task-1", status="completed",
        events=[
            {"type": "text", "text": "step 1"},
            {"type": "text", "text": "step 2"},
        ],
        summary="Done investigating",
    )

    row = db.get_subagent_transcript("s1", "task-1")

    assert row["status"] == "completed"
    assert row["summary"] == "Done investigating"
    assert len(row["events"]) == 2
    # tool_use_id/description are not re-sent on the second call — the
    # COALESCE-on-conflict keeps the values from the first call rather than
    # clobbering them with NULL.
    assert row["tool_use_id"] == "tu_1"
    assert row["description"] == "first"


def test_two_tasks_in_the_same_session_do_not_collide(tmp_path):
    db = _make_db(tmp_path)
    db.upsert_subagent_transcript("s1", "task-1", description="first task")
    db.upsert_subagent_transcript("s1", "task-2", description="second task")

    assert db.get_subagent_transcript("s1", "task-1")["description"] == "first task"
    assert db.get_subagent_transcript("s1", "task-2")["description"] == "second task"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/test_hermes_state_subagent_transcripts.py -v`
Expected: FAIL with `AttributeError: 'SessionDB' object has no attribute 'upsert_subagent_transcript'`

- [ ] **Step 3: Add the table to `SCHEMA_SQL` and bump `SCHEMA_VERSION`**

Edit `hermes-agent/hermes_state.py` line 156:

```python
SCHEMA_VERSION = 23
```

to:

```python
SCHEMA_VERSION = 24
```

Edit `hermes-agent/hermes_state.py`'s `SCHEMA_SQL` string — insert immediately after the `async_delegations` table definition (currently ends at line 1135 with `);`, right before the `CREATE INDEX` block that starts at line 1137):

```sql

CREATE TABLE IF NOT EXISTS subagent_transcripts (
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    tool_use_id TEXT,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    events_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, task_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
```

And add an index alongside the other index lines at the end of `SCHEMA_SQL` (after `CREATE INDEX IF NOT EXISTS idx_async_delegations_delivery ... ;`):

```sql
CREATE INDEX IF NOT EXISTS idx_subagent_transcripts_session
    ON subagent_transcripts(session_id);
```

- [ ] **Step 4: Add the two `SessionDB` methods**

Add to `hermes-agent/hermes_state.py`, immediately after `get_session` (which ends at line 4733):

```python
    def upsert_subagent_transcript(
        self,
        session_id: str,
        task_id: str,
        *,
        tool_use_id: Optional[str] = None,
        description: Optional[str] = None,
        status: str = "running",
        events: Optional[List[Dict[str, Any]]] = None,
        summary: Optional[str] = None,
    ) -> None:
        """Persist the full ordered sub-agent event history for one
        Task-tool invocation, keyed by (session_id, task_id) — Hermes' OWN
        session_id, never Claude's internal one (see the "transcript
        persistence approach" investigation in
        docs/plans/2026-07-23-claude-code-cli-subagent-visibility-plan.md).

        Called repeatedly as the sub-agent's lifecycle progresses (start /
        progress / terminal) by agent/claude_code_runtime.py's event
        bridge, which already keeps the authoritative ordered event list in
        memory — each call here replaces events_json/status wholesale with
        the caller's current full list rather than appending, since the
        bridge is the single source of truth for ordering.

        tool_use_id/description are only ever sent on the first (start)
        call; later calls omit them (None) and COALESCE preserves the
        original value instead of clobbering it with NULL.
        """
        now = time.time()
        events_json = json.dumps(events if events is not None else [], ensure_ascii=False)

        def _do(conn):
            conn.execute(
                """INSERT INTO subagent_transcripts (
                       session_id, task_id, tool_use_id, description, status,
                       events_json, summary, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (session_id, task_id) DO UPDATE SET
                       tool_use_id = COALESCE(excluded.tool_use_id, subagent_transcripts.tool_use_id),
                       description = COALESCE(excluded.description, subagent_transcripts.description),
                       status = excluded.status,
                       events_json = excluded.events_json,
                       summary = COALESCE(excluded.summary, subagent_transcripts.summary),
                       updated_at = excluded.updated_at""",
                (
                    session_id, task_id, tool_use_id, description, status,
                    events_json, summary, now, now,
                ),
            )

        self._execute_write(_do)

    def get_subagent_transcript(self, session_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one persisted sub-agent transcript. Returns None if never
        recorded. Works identically whether the session is still live or
        was reloaded from history, since it's always sourced from this
        table — used by the subagent_transcript.get RPC in
        tui_gateway/server.py."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM subagent_transcripts WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["events"] = json.loads(result.pop("events_json") or "[]")
        except (TypeError, ValueError):
            result["events"] = []
        return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/test_hermes_state_subagent_transcripts.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
cd hermes-agent
git add hermes_state.py tests/test_hermes_state_subagent_transcripts.py
git commit -m "feat: add subagent_transcripts table and SessionDB persistence methods"
```

---

### Task 2: Event bridge — Task sub-agent lifecycle, suppression, and transcript persistence

**Files:**
- Modify: `hermes-agent/agent/claude_code_runtime.py` (`make_claude_code_sdk_event_bridge`, added by Phase 1)
- Test: `hermes-agent/tests/agent/test_claude_code_sdk_subagent_bridge.py`

**Interfaces:**
- Consumes: `SessionDB.upsert_subagent_transcript` (Task 1); `agent.tool_start_callback(tool_call_id, name, args)` / `agent.tool_complete_callback(tool_call_id, name, args, result)` (existing, real — confirmed signatures in Global Constraints); `agent.session_id`, `agent._session_db`, `agent._session_db_created`, `agent._ensure_db_session()` (existing, real — same fields `_record_claude_code_sdk_usage` already uses per Phase 1 Task 7).
- Produces: `make_claude_code_sdk_event_bridge(agent)`'s returned `on_event` callback now also handles `TaskStartedMessage`/`TaskProgressMessage`/`TaskUpdatedMessage`/`TaskNotificationMessage` and `parent_tool_use_id`-tagged `AssistantMessage`s, firing `claude_subagent_task` tool_start_callback/tool_complete_callback and persisting via Task 1's methods. This task assumes Phase 1's version of `make_claude_code_sdk_event_bridge` already exists with its `started: dict[str, tuple[str, dict]]` state and `_fire_tool_started`/`_fire_tool_completed`/`_fire_text`/`on_event` closures (Phase 1 Task 6) — every step below is an incremental modification to that same function.

- [ ] **Step 1: Write the failing test for `TaskStartedMessage` → `tool_start_callback` + initial persistence**

```python
# hermes-agent/tests/agent/test_claude_code_sdk_subagent_bridge.py
"""Tests for Phase 3 sub-agent visibility in
agent.claude_code_runtime.make_claude_code_sdk_event_bridge.

Fake message/block classes are named EXACTLY the string the bridge's
type(x).__name__ dispatch checks for (TaskStartedMessage, AssistantMessage
is duck-typed via getattr so its fake name doesn't matter, but TextBlock/
ToolUseBlock/ToolResultBlock used inside a subagent's own content DO need
exact names — see this plan's Global Constraints "Dispatch convention"
note for why).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from agent.claude_code_runtime import make_claude_code_sdk_event_bridge


class TaskStartedMessage:
    def __init__(self, task_id, description, tool_use_id=None, task_type=None):
        self.task_id = task_id
        self.description = description
        self.tool_use_id = tool_use_id
        self.task_type = task_type


def _make_agent():
    agent = MagicMock()
    agent.session_id = "sess-1"
    agent._session_db_created = True
    return agent


def test_task_started_fires_tool_start_callback():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    message = TaskStartedMessage(
        task_id="task-1", description="Investigate flaky test", tool_use_id="tu_1"
    )
    bridge({"type": "raw_message", "message": message})

    agent.tool_start_callback.assert_called_once_with(
        "task-1",
        "claude_subagent_task",
        {"task_id": "task-1", "description": "Investigate flaky test"},
    )


def test_task_started_persists_initial_transcript_row():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    message = TaskStartedMessage(
        task_id="task-1", description="Investigate flaky test", tool_use_id="tu_1"
    )
    bridge({"type": "raw_message", "message": message})

    agent._session_db.upsert_subagent_transcript.assert_called_once_with(
        "sess-1",
        "task-1",
        tool_use_id="tu_1",
        description="Investigate flaky test",
        status="running",
        events=[],
        summary=None,
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_claude_code_sdk_subagent_bridge.py -v`
Expected: FAIL — `agent.tool_start_callback.assert_called_once_with(...)` raises `AssertionError: Expected 'tool_start_callback' to be called once. Called 0 times.` (the bridge doesn't recognize `TaskStartedMessage` yet).

- [ ] **Step 3: Add sub-agent state tracking + `TaskStartedMessage` handling**

Edit `hermes-agent/agent/claude_code_runtime.py`'s `make_claude_code_sdk_event_bridge`. Immediately after the existing `started: dict[str, tuple[str, dict]] = {}` line (Phase 1 Task 6), add:

```python
    # --- Phase 3: sub-agent (Task tool) visibility state -----------------
    # tool_use_id of the parent turn's "Task" ToolUseBlock -> task_id, once
    # TaskStartedMessage correlates the two (both reference the same
    # tool_use_id — see the Task-message field names in this plan's
    # Global Constraints).
    tool_use_id_to_task_id: dict[str, str] = {}
    # task_id -> mutable transcript state, accumulated as the sub-agent's
    # own AssistantMessage content streams in and mirrored to
    # subagent_transcripts on every change.
    subagent_state: dict[str, dict[str, Any]] = {}
    # tool_use_id of every ToolUseBlock in the PARENT turn whose name is
    # "Task" — used to suppress the generic tool.started/tool.completed
    # bubble for the Task invocation itself, since the dedicated
    # claude_subagent_task bubble (fired below) replaces it rather than
    # duplicating it.
    task_tool_use_ids: set[str] = set()

    _TERMINAL_TASK_STATUSES = {"completed", "failed", "stopped", "killed"}

    def _persist_subagent_transcript(task_id: str) -> None:
        state = subagent_state.get(task_id)
        if state is None:
            return
        session_id = getattr(agent, "session_id", None)
        db = getattr(agent, "_session_db", None)
        if not db or not session_id:
            return
        try:
            if not agent._session_db_created:
                agent._ensure_db_session()
            db.upsert_subagent_transcript(
                session_id,
                task_id,
                tool_use_id=state.get("tool_use_id"),
                description=state.get("description"),
                status=state.get("status", "running"),
                events=state.get("events", []),
                summary=state.get("summary"),
            )
        except Exception:
            logger.debug(
                "claude code sdk: subagent transcript persistence failed "
                "(task_id=%s)", task_id, exc_info=True,
            )

    def _handle_task_message(message_type: str, message) -> None:
        task_id = getattr(message, "task_id", None)
        if not task_id:
            return

        if message_type == "TaskStartedMessage":
            tool_use_id = getattr(message, "tool_use_id", None)
            description = getattr(message, "description", "") or ""
            if tool_use_id:
                tool_use_id_to_task_id[tool_use_id] = task_id
            subagent_state[task_id] = {
                "tool_use_id": tool_use_id,
                "description": description,
                "status": "running",
                "events": [],
                "summary": None,
            }
            cb = getattr(agent, "tool_start_callback", None)
            if cb is not None:
                try:
                    cb(
                        task_id,
                        "claude_subagent_task",
                        {"task_id": task_id, "description": description},
                    )
                except Exception:
                    logger.debug(
                        "tool_start_callback raised on claude_subagent_task "
                        "start for %s", task_id, exc_info=True,
                    )
            _persist_subagent_transcript(task_id)
            return
```

Then, in `on_event`, insert a new branch **before** the existing `content = getattr(message, "content", None)` line (Phase 1 Task 6):

```python
    def on_event(event: dict) -> None:
        if not isinstance(event, dict) or event.get("type") != "raw_message":
            return
        message = event.get("message")
        message_type = type(message).__name__

        if message_type in {
            "TaskStartedMessage", "TaskProgressMessage",
            "TaskUpdatedMessage", "TaskNotificationMessage",
        }:
            _handle_task_message(message_type, message)
            return

        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return
        for block in content:
            block_type = type(block).__name__
            if block_type == "ToolUseBlock":
                _fire_tool_started(block)
            elif block_type == "ToolResultBlock":
                _fire_tool_completed(block)
            elif block_type == "TextBlock":
                text = getattr(block, "text", "")
                if isinstance(text, str) and text:
                    _fire_text(text)

    return on_event
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_claude_code_sdk_subagent_bridge.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Write the failing tests for Task-tool bubble suppression**

Append to `hermes-agent/tests/agent/test_claude_code_sdk_subagent_bridge.py`:

```python
class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class ToolResultBlock:
    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class AssistantMessage:
    def __init__(self, content, parent_tool_use_id=None):
        self.content = content
        self.parent_tool_use_id = parent_tool_use_id


def test_task_tool_use_block_does_not_fire_generic_tool_started():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    message = AssistantMessage(
        content=[ToolUseBlock(id="tu_1", name="Task", input={"description": "go"})]
    )
    bridge({"type": "raw_message", "message": message})

    agent.tool_progress_callback.assert_not_called()


def test_task_tool_result_block_does_not_fire_generic_tool_completed():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    bridge({
        "type": "raw_message",
        "message": AssistantMessage(
            content=[ToolUseBlock(id="tu_1", name="Task", input={})]
        ),
    })
    bridge({
        "type": "raw_message",
        "message": AssistantMessage(
            content=[ToolResultBlock(tool_use_id="tu_1", content="sub-agent summary")]
        ),
    })

    agent.tool_progress_callback.assert_not_called()


def test_subagent_own_assistant_text_is_accumulated_not_fired_as_top_level():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    bridge({
        "type": "raw_message",
        "message": TaskStartedMessage(
            task_id="task-1", description="Investigate", tool_use_id="tu_1"
        ),
    })
    agent._session_db.reset_mock()

    bridge({
        "type": "raw_message",
        "message": AssistantMessage(
            content=[
                object.__new__(type("TextBlock", (), {})),
            ],
            parent_tool_use_id="tu_1",
        ),
    })

    # The sub-agent's own text must never hit the top-level stream delta —
    # it belongs in the persisted transcript only.
    agent._fire_stream_delta.assert_not_called()
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_claude_code_sdk_subagent_bridge.py -v`
Expected: FAIL — the first two new tests fail because `_fire_tool_started`/`_fire_tool_completed` still unconditionally call `agent.tool_progress_callback` for a `name == "Task"` block; the third fails a different way (the malformed `TextBlock` instance in the test is a placeholder for Step 7's replacement — see next step).

- [ ] **Step 7: Fix the malformed test fixture and add real `TextBlock` fakes**

Replace the third test in Step 5 with a correctly-constructed fixture (the `object.__new__` trick above was a placeholder to prove Step 6's failure mode — replace it now):

```python
class TextBlock:
    def __init__(self, text):
        self.text = text


def test_subagent_own_assistant_text_is_accumulated_not_fired_as_top_level():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    bridge({
        "type": "raw_message",
        "message": TaskStartedMessage(
            task_id="task-1", description="Investigate", tool_use_id="tu_1"
        ),
    })
    agent._session_db.reset_mock()

    bridge({
        "type": "raw_message",
        "message": AssistantMessage(
            content=[TextBlock("looking at the logs")],
            parent_tool_use_id="tu_1",
        ),
    })

    agent._fire_stream_delta.assert_not_called()
    agent._session_db.upsert_subagent_transcript.assert_called_once_with(
        "sess-1",
        "task-1",
        tool_use_id="tu_1",
        description="Investigate",
        status="running",
        events=[{"type": "text", "text": "looking at the logs"}],
        summary=None,
    )
```

- [ ] **Step 8: Implement suppression and sub-agent content accumulation**

Edit `hermes-agent/agent/claude_code_runtime.py`'s `_fire_tool_started` (Phase 1 Task 6) — add the suppression check as its first lines:

```python
    def _fire_tool_started(block) -> None:
        name = block.name
        if name == "Task":
            # Suppressed — see task_tool_use_ids docstring above. The
            # dedicated claude_subagent_task bubble (fired from
            # TaskStartedMessage) replaces this entirely.
            task_tool_use_ids.add(block.id)
            return
        args = block.input if isinstance(block.input, dict) else {}
        started[block.id] = (name, args)
        preview = None
        if isinstance(args, dict):
            command = args.get("command")
            file_path = args.get("file_path")
            preview = command or file_path
            if isinstance(preview, str):
                preview = preview[:120]
        cb = getattr(agent, "tool_progress_callback", None)
        if cb is not None:
            try:
                cb("tool.started", name, preview, args)
            except Exception:
                logger.debug(
                    "tool_progress_callback raised on tool.started for %s",
                    name, exc_info=True,
                )
```

Edit `_fire_tool_completed` — add the suppression check as its first lines:

```python
    def _fire_tool_completed(block) -> None:
        if block.tool_use_id in task_tool_use_ids:
            # Suppressed — the dedicated claude_subagent_task bubble's
            # terminal state comes from TaskNotificationMessage /
            # TaskUpdatedMessage, not from this ToolResultBlock.
            task_tool_use_ids.discard(block.tool_use_id)
            return
        prior = started.pop(block.tool_use_id, None)
        name = prior[0] if prior is not None else "unknown"
        content = block.content
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", part)) if isinstance(part, dict) else str(part)
                for part in content
            )
        is_error = bool(getattr(block, "is_error", False))
        cb = getattr(agent, "tool_progress_callback", None)
        if cb is not None:
            try:
                cb("tool.completed", name, None, None,
                   duration=None, is_error=is_error, result=content)
            except Exception:
                logger.debug(
                    "tool_progress_callback raised on tool.completed for %s",
                    name, exc_info=True,
                )
```

Add a new `_project_subagent_block` helper (next to `_fire_tool_started`/`_fire_tool_completed`):

```python
    def _project_subagent_block(block) -> Optional[dict]:
        """Project one content block from a sub-agent's own AssistantMessage
        into the lightweight dict shape persisted in subagent_transcripts —
        mirrors the top-level callback payload shapes (name/input for
        tool_use, content/is_error for tool_result, text for plain text)
        without touching any top-level UI callback."""
        block_type = type(block).__name__
        if block_type == "TextBlock":
            text = getattr(block, "text", "")
            if isinstance(text, str) and text:
                return {"type": "text", "text": text}
            return None
        if block_type == "ToolUseBlock":
            return {
                "type": "tool_use",
                "name": getattr(block, "name", ""),
                "input": block.input if isinstance(block.input, dict) else {},
            }
        if block_type == "ToolResultBlock":
            content = block.content
            if isinstance(content, list):
                content = "\n".join(
                    str(part.get("text", part)) if isinstance(part, dict) else str(part)
                    for part in content
                )
            return {
                "type": "tool_result",
                "is_error": bool(getattr(block, "is_error", False)),
                "content": content,
            }
        return None
```

Finally, edit `on_event` to route `parent_tool_use_id`-tagged messages into the accumulator instead of the normal top-level dispatch:

```python
    def on_event(event: dict) -> None:
        if not isinstance(event, dict) or event.get("type") != "raw_message":
            return
        message = event.get("message")
        message_type = type(message).__name__

        if message_type in {
            "TaskStartedMessage", "TaskProgressMessage",
            "TaskUpdatedMessage", "TaskNotificationMessage",
        }:
            _handle_task_message(message_type, message)
            return

        parent_tool_use_id = getattr(message, "parent_tool_use_id", None)
        if parent_tool_use_id and parent_tool_use_id in tool_use_id_to_task_id:
            task_id = tool_use_id_to_task_id[parent_tool_use_id]
            content = getattr(message, "content", None)
            if isinstance(content, list):
                state = subagent_state.setdefault(task_id, {
                    "tool_use_id": parent_tool_use_id, "description": "",
                    "status": "running", "events": [], "summary": None,
                })
                for block in content:
                    projected = _project_subagent_block(block)
                    if projected is not None:
                        state["events"].append(projected)
                _persist_subagent_transcript(task_id)
            return

        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return
        for block in content:
            block_type = type(block).__name__
            if block_type == "ToolUseBlock":
                _fire_tool_started(block)
            elif block_type == "ToolResultBlock":
                _fire_tool_completed(block)
            elif block_type == "TextBlock":
                text = getattr(block, "text", "")
                if isinstance(text, str) and text:
                    _fire_text(text)

    return on_event
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_claude_code_sdk_subagent_bridge.py -v`
Expected: PASS (5 tests)

- [ ] **Step 10: Write the failing test for `TaskProgressMessage` / non-terminal `TaskUpdatedMessage`**

Append to the test file:

```python
class TaskProgressMessage:
    def __init__(self, task_id, description, usage=None, last_tool_name=None):
        self.task_id = task_id
        self.description = description
        self.usage = usage or {"total_tokens": 0, "tool_uses": 0, "duration_ms": 0}
        self.last_tool_name = last_tool_name


class TaskUpdatedMessage:
    def __init__(self, task_id, patch=None, status=None):
        self.task_id = task_id
        self.patch = patch or {}
        self.status = status


def test_task_progress_updates_persisted_transcript_without_completing():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    bridge({"type": "raw_message", "message": TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    )})
    bridge({"type": "raw_message", "message": TaskProgressMessage(
        task_id="task-1", description="Investigate", last_tool_name="Bash",
    )})

    agent.tool_complete_callback.assert_not_called()
    agent._session_db.upsert_subagent_transcript.assert_called_with(
        "sess-1", "task-1", tool_use_id="tu_1", description="Investigate",
        status="running", events=[], summary=None,
    )


def test_non_terminal_task_updated_does_not_complete():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    bridge({"type": "raw_message", "message": TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    )})
    bridge({"type": "raw_message", "message": TaskUpdatedMessage(
        task_id="task-1", status="paused",
    )})

    agent.tool_complete_callback.assert_not_called()
```

- [ ] **Step 11: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_claude_code_sdk_subagent_bridge.py -v`
Expected: FAIL — `_handle_task_message` doesn't have `elif` branches for `"TaskProgressMessage"`/`"TaskUpdatedMessage"` yet, so `task_id = getattr(message, "task_id", None)` runs but nothing happens beyond the (currently absent) fallthrough — `AttributeError`/`AssertionError` depending on which assertion runs first; either way the tests do not pass yet.

- [ ] **Step 12: Add `TaskProgressMessage` / non-terminal `TaskUpdatedMessage` handling**

Edit `_handle_task_message` in `hermes-agent/agent/claude_code_runtime.py` — add after the `if message_type == "TaskStartedMessage": ... return` block:

```python
        state = subagent_state.setdefault(task_id, {
            "tool_use_id": None, "description": "", "status": "running",
            "events": [], "summary": None,
        })

        if message_type == "TaskProgressMessage":
            state["status"] = "running"
            _persist_subagent_transcript(task_id)
            return

        if message_type == "TaskUpdatedMessage":
            status = getattr(message, "status", None)
            if status:
                state["status"] = status
            if status not in _TERMINAL_TASK_STATUSES:
                _persist_subagent_transcript(task_id)
                return
            # Terminal via TaskUpdatedMessage (e.g. a killed background
            # task with no accompanying TaskNotificationMessage — the SDK's
            # own docs note this can happen).
            state["summary"] = state.get("summary") or f"task {status}"
            _finish_task(task_id, state)
            return
```

- [ ] **Step 13: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_claude_code_sdk_subagent_bridge.py -v`
Expected: FAIL with `NameError: name '_finish_task' is not defined` (Step 14 adds it) — for the non-terminal test only; the progress test should already pass. This confirms the terminal path is wired but its helper doesn't exist yet.

- [ ] **Step 14: Write the failing tests for terminal completion**

Append to the test file:

```python
class TaskNotificationMessage:
    def __init__(self, task_id, status, summary, output_file=""):
        self.task_id = task_id
        self.status = status
        self.summary = summary
        self.output_file = output_file


def test_task_notification_completed_fires_tool_complete_callback():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    bridge({"type": "raw_message", "message": TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    )})
    bridge({"type": "raw_message", "message": TaskNotificationMessage(
        task_id="task-1", status="completed", summary="Found the root cause",
    )})

    agent.tool_complete_callback.assert_called_once()
    call_args = agent.tool_complete_callback.call_args
    assert call_args.args[0] == "task-1"
    assert call_args.args[1] == "claude_subagent_task"
    assert call_args.args[2] == {"task_id": "task-1", "description": "Investigate"}
    import json
    result = json.loads(call_args.args[3])
    assert result == {
        "task_id": "task-1", "status": "completed",
        "summary": "Found the root cause", "is_error": False,
    }


def test_task_updated_killed_with_no_notification_still_completes():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    bridge({"type": "raw_message", "message": TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    )})
    bridge({"type": "raw_message", "message": TaskUpdatedMessage(
        task_id="task-1", status="killed",
    )})

    agent.tool_complete_callback.assert_called_once()
    import json
    result = json.loads(agent.tool_complete_callback.call_args.args[3])
    assert result["status"] == "killed"
    assert result["is_error"] is True


def test_task_result_block_still_suppressed_after_completion():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    bridge({"type": "raw_message", "message": AssistantMessage(
        content=[ToolUseBlock(id="tu_1", name="Task", input={})],
    )})
    bridge({"type": "raw_message", "message": TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    )})
    bridge({"type": "raw_message", "message": TaskNotificationMessage(
        task_id="task-1", status="completed", summary="done",
    )})
    bridge({"type": "raw_message", "message": AssistantMessage(
        content=[ToolResultBlock(tool_use_id="tu_1", content="done")],
    )})

    agent.tool_progress_callback.assert_not_called()
```

- [ ] **Step 15: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_claude_code_sdk_subagent_bridge.py -v`
Expected: FAIL with `NameError: name '_finish_task' is not defined`

- [ ] **Step 16: Implement `_finish_task`**

Add to `hermes-agent/agent/claude_code_runtime.py`, immediately after `_handle_task_message`:

```python
    def _finish_task(task_id: str, state: dict) -> None:
        _persist_subagent_transcript(task_id)
        is_error = state.get("status") in {"failed", "stopped", "killed"}
        cb = getattr(agent, "tool_complete_callback", None)
        if cb is not None:
            result_payload = {
                "task_id": task_id,
                "status": state.get("status"),
                "summary": state.get("summary") or "",
                "is_error": is_error,
            }
            try:
                cb(
                    task_id,
                    "claude_subagent_task",
                    {"task_id": task_id, "description": state.get("description", "")},
                    json.dumps(result_payload, ensure_ascii=False)[:4000],
                )
            except Exception:
                logger.debug(
                    "tool_complete_callback raised on claude_subagent_task "
                    "completion for %s", task_id, exc_info=True,
                )
        tool_use_id = state.get("tool_use_id")
        if tool_use_id:
            tool_use_id_to_task_id.pop(tool_use_id, None)
            task_tool_use_ids.discard(tool_use_id)
```

Also update `_handle_task_message`'s `TaskNotificationMessage` branch (add at the end, after the existing `state = subagent_state.setdefault(...)` block from Step 12):

```python
        if message_type == "TaskNotificationMessage":
            status = getattr(message, "status", None) or "completed"
            state["status"] = status
            state["summary"] = getattr(message, "summary", "") or ""
            _finish_task(task_id, state)
            return
```

Add `import json` to the top of `hermes-agent/agent/claude_code_runtime.py` (Phase 1's version only imports `logging` and `typing` names — confirm with `grep -n "^import json" agent/claude_code_runtime.py`; if absent, add it alongside the existing `import logging` line).

- [ ] **Step 17: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_claude_code_sdk_subagent_bridge.py -v`
Expected: PASS (10 tests total)

- [ ] **Step 18: Run the full Phase 1 + Phase 3 bridge test files together to confirm no regressions**

Run:
```bash
cd hermes-agent
./scripts/run_tests.sh \
  tests/agent/test_claude_code_sdk_event_bridge.py \
  tests/agent/test_claude_code_sdk_subagent_bridge.py \
  -v
```
Expected: PASS (14 tests total, 0 failures)

- [ ] **Step 19: Commit**

```bash
cd hermes-agent
git add agent/claude_code_runtime.py tests/agent/test_claude_code_sdk_subagent_bridge.py
git commit -m "feat: project Claude Code Task sub-agent lifecycle into claude_subagent_task tool_call"
```

---

### Task 3: `subagent_transcript.get` gateway RPC

**Files:**
- Modify: `hermes-agent/tui_gateway/server.py` (new `@method("subagent_transcript.get")` handler, placed near the other `.get`-suffixed methods such as `config.get` at line 13127)
- Test: `hermes-agent/tests/tui_gateway/test_subagent_transcript_rpc.py`

**Interfaces:**
- Consumes: `SessionDB.get_subagent_transcript(session_id, task_id) -> Optional[Dict[str, Any]]` (Task 1); `tui_gateway.server._get_db() -> Optional[SessionDB]` (existing, real, `tui_gateway/server.py:1047`); `_ok(rid, result)` / `_err(rid, code, msg)` (existing, real, `tui_gateway/server.py:1512-1516`); `method(name)` decorator (existing, real, `tui_gateway/server.py:1520`).
- Produces: RPC method `subagent_transcript.get`, params `{session_id: str, task_id: str}`, result `{found: bool, task_id, tool_use_id, description, status, events: list[dict], summary, updated_at}` on success or `{found: false}` when not recorded.

- [ ] **Step 1: Write the failing tests**

```python
# hermes-agent/tests/tui_gateway/test_subagent_transcript_rpc.py
"""Tests for the subagent_transcript.get RPC (Phase 3, sub-agent
visibility). Mirrors tests/tui_gateway/test_billing_rpc.py's direct
_methods[...] invocation pattern."""
from __future__ import annotations

import tui_gateway.server as srv
from hermes_state import SessionDB


def _call(method: str, params: dict) -> dict:
    envelope = srv._methods[method](1, params)
    return envelope


def test_subagent_transcript_get_returns_not_found_when_missing(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(srv, "_get_db", lambda: db)

    envelope = _call("subagent_transcript.get", {"session_id": "s1", "task_id": "task-1"})

    assert envelope["result"]["found"] is False


def test_subagent_transcript_get_returns_persisted_transcript(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="cli")
    db.upsert_subagent_transcript(
        "s1", "task-1", tool_use_id="tu_1", description="Investigate",
        status="completed",
        events=[{"type": "text", "text": "found it"}],
        summary="Root cause found",
    )
    monkeypatch.setattr(srv, "_get_db", lambda: db)

    envelope = _call("subagent_transcript.get", {"session_id": "s1", "task_id": "task-1"})
    result = envelope["result"]

    assert result["found"] is True
    assert result["task_id"] == "task-1"
    assert result["tool_use_id"] == "tu_1"
    assert result["description"] == "Investigate"
    assert result["status"] == "completed"
    assert result["events"] == [{"type": "text", "text": "found it"}]
    assert result["summary"] == "Root cause found"


def test_subagent_transcript_get_requires_session_id_and_task_id():
    envelope = _call("subagent_transcript.get", {"session_id": "", "task_id": ""})
    assert "error" in envelope


def test_subagent_transcript_get_fails_soft_when_db_unavailable(monkeypatch):
    monkeypatch.setattr(srv, "_get_db", lambda: None)

    envelope = _call("subagent_transcript.get", {"session_id": "s1", "task_id": "task-1"})

    assert envelope["result"]["found"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/tui_gateway/test_subagent_transcript_rpc.py -v`
Expected: FAIL with `KeyError: 'subagent_transcript.get'`

- [ ] **Step 3: Write the minimal implementation**

Add to `hermes-agent/tui_gateway/server.py`, immediately after the `config.get` handler block (which ends right before the `if key == "profile":`/subsequent branches conclude — insert this new, separate `@method` block right after `config.get`'s function ends, before the next unrelated `@method(...)`):

```python
@method("subagent_transcript.get")
def _(rid, params: dict) -> dict:
    """Full transcript detail for one Claude Code Task-tool sub-agent
    invocation, fetched by Desktop's subagent-task.tsx on expand. Always
    sourced from the persisted subagent_transcripts table (Task 1 of
    docs/plans/2026-07-23-claude-code-cli-subagent-visibility-plan.md), so
    it works identically whether the session is still live or was reloaded
    from history."""
    session_id = str(params.get("session_id") or "")
    task_id = str(params.get("task_id") or "")
    if not session_id or not task_id:
        return _err(rid, 5701, "session_id and task_id are required")

    db = _get_db()
    if db is None:
        return _ok(rid, {"found": False})

    try:
        row = db.get_subagent_transcript(session_id, task_id)
    except Exception as e:
        return _err(rid, 5702, str(e))

    if row is None:
        return _ok(rid, {"found": False})

    return _ok(rid, {
        "found": True,
        "task_id": row["task_id"],
        "tool_use_id": row.get("tool_use_id"),
        "description": row.get("description"),
        "status": row.get("status"),
        "events": row.get("events", []),
        "summary": row.get("summary"),
        "updated_at": row.get("updated_at"),
    })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/tui_gateway/test_subagent_transcript_rpc.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
cd hermes-agent
git add tui_gateway/server.py tests/tui_gateway/test_subagent_transcript_rpc.py
git commit -m "feat: add subagent_transcript.get gateway RPC"
```

---

### Task 4: Desktop `SubagentTask` component + `message-parts.tsx` wiring

**Files:**
- Create: `hermes-agent/apps/desktop/src/components/assistant-ui/tool/subagent-task.tsx`
- Test: `hermes-agent/apps/desktop/src/components/assistant-ui/tool/subagent-task.test.tsx`
- Modify: `hermes-agent/apps/desktop/src/components/assistant-ui/thread/message-parts.tsx` (`ChainToolFallback`, lines 39-51)

**Interfaces:**
- Consumes: `useSessionView().$runtimeId` (existing, real, `apps/desktop/src/app/chat/session-view`); `$gateway` nanostore + `gateway.request<T>(method, params)` (existing, real, `apps/desktop/src/store/gateway`); `toolPartDisclosureId(part)` / `$toolDisclosureOpen(id)` / `setToolDisclosureOpen(id, open)` (existing, real, `apps/desktop/src/components/assistant-ui/tool/fallback-model/targets.ts`, `apps/desktop/src/store/tool-view.ts`); `parseMaybeObject(value)` (existing, real, `apps/desktop/src/components/assistant-ui/tool/fallback-model/format.ts`); `subagent_transcript.get` RPC (Task 3); `ToolCallMessagePartProps` (from `@assistant-ui/react`, existing).
- Produces: `SubagentTask: FC<ToolCallMessagePartProps>`; one new `if (props.toolName === 'claude_subagent_task') return <SubagentTask {...props} />` branch in `ChainToolFallback`.

- [ ] **Step 1: Write the failing tests**

```tsx
// hermes-agent/apps/desktop/src/components/assistant-ui/tool/subagent-task.test.tsx
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'
import { $gateway } from '@/store/gateway'
import { $activeSessionId } from '@/store/session'

import { SubagentTask } from './subagent-task'
import type { ToolPart } from './fallback-model'

function args(overrides: Record<string, unknown> = {}) {
  return { task_id: 'task-1', description: 'Investigate flaky test', ...overrides }
}

function part(overrides: Partial<ToolPart> = {}): ToolPart {
  return {
    args: args(),
    toolCallId: 'task-1',
    toolName: 'claude_subagent_task',
    type: 'tool-call',
    ...overrides
  } as unknown as ToolPart
}

function mockGateway(response: unknown) {
  const request = vi.fn().mockResolvedValue(response)
  $gateway.set({ request } as unknown as HermesGateway)

  return request
}

afterEach(() => {
  cleanup()
  $activeSessionId.set(null)
  $gateway.set(null)
})

describe('SubagentTask', () => {
  it('renders the collapsed header with the task description', () => {
    render(<SubagentTask {...(part() as any)} />)

    expect(screen.getByText('Investigate flaky test')).toBeTruthy()
  })

  it('shows a running state before a result arrives', () => {
    render(<SubagentTask {...(part({ result: undefined }) as any)} />)

    expect(screen.getByText(/running/i)).toBeTruthy()
  })

  it('shows a completed state once the compact result lands', () => {
    render(
      <SubagentTask
        {...(part({
          result: JSON.stringify({
            task_id: 'task-1',
            status: 'completed',
            summary: 'Found the root cause',
            is_error: false
          })
        }) as any)}
      />
    )

    expect(screen.getByText('Found the root cause')).toBeTruthy()
  })

  it('fetches the full transcript via subagent_transcript.get on expand', async () => {
    $activeSessionId.set('sess-1')
    const request = mockGateway({
      found: true,
      task_id: 'task-1',
      description: 'Investigate flaky test',
      status: 'completed',
      events: [
        { type: 'text', text: 'looking at logs' },
        { type: 'tool_use', name: 'Bash', input: { command: 'pytest -k flaky' } }
      ],
      summary: 'Found the root cause'
    })

    render(
      <SubagentTask
        {...(part({
          result: JSON.stringify({
            task_id: 'task-1', status: 'completed',
            summary: 'Found the root cause', is_error: false
          })
        }) as any)}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: /Investigate flaky test/i }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('subagent_transcript.get', {
        session_id: 'sess-1',
        task_id: 'task-1'
      })
    })
    expect(await screen.findByText('looking at logs')).toBeTruthy()
    expect(screen.getByText(/pytest -k flaky/)).toBeTruthy()
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent/apps/desktop && npx vitest run --project ui src/components/assistant-ui/tool/subagent-task.test.tsx`
Expected: FAIL with `Failed to resolve import "./subagent-task"`

- [ ] **Step 3: Write the minimal implementation**

```tsx
// hermes-agent/apps/desktop/src/components/assistant-ui/tool/subagent-task.tsx
'use client'

import { type ToolCallMessagePartProps } from '@assistant-ui/react'
import { useStore } from '@nanostores/react'
import { type FC, useMemo, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { $gateway } from '@/store/gateway'
import { $toolDisclosureOpen, setToolDisclosureOpen } from '@/store/tool-view'

import { toolPartDisclosureId } from './fallback-model/targets'
import { parseMaybeObject } from './fallback-model/format'
import type { ToolPart } from './fallback-model/types'

// Copy is deliberately plain English literals, not routed through useI18n() —
// see this plan's Global Constraints "i18n is deliberately out of scope" note.
const COPY = {
  running: 'Running…',
  expand: 'View transcript'
}

interface SubagentTaskArgs {
  task_id?: string
  description?: string
}

interface SubagentTaskResult {
  task_id?: string
  status?: string
  summary?: string
  is_error?: boolean
}

interface SubagentTranscriptEvent {
  type: 'text' | 'tool_use' | 'tool_result'
  text?: string
  name?: string
  input?: Record<string, unknown>
  content?: string
  is_error?: boolean
}

interface SubagentTranscriptResponse {
  found: boolean
  events?: SubagentTranscriptEvent[]
  summary?: string
}

function readArgs(args: unknown): SubagentTaskArgs {
  const row = parseMaybeObject(args)

  return {
    task_id: typeof row.task_id === 'string' ? row.task_id : undefined,
    description: typeof row.description === 'string' ? row.description : undefined
  }
}

function readResult(result: unknown): SubagentTaskResult {
  if (result === undefined) {
    return {}
  }

  const row = parseMaybeObject(result)

  return {
    task_id: typeof row.task_id === 'string' ? row.task_id : undefined,
    status: typeof row.status === 'string' ? row.status : undefined,
    summary: typeof row.summary === 'string' ? row.summary : undefined,
    is_error: row.is_error === true
  }
}

function TranscriptEventRow({ event }: { event: SubagentTranscriptEvent }) {
  if (event.type === 'text') {
    return <p className="whitespace-pre-wrap text-(--ui-text-secondary)">{event.text}</p>
  }

  if (event.type === 'tool_use') {
    return (
      <p className="font-mono text-xs text-(--ui-text-tertiary)">
        {event.name}({JSON.stringify(event.input ?? {})})
      </p>
    )
  }

  return (
    <p
      className={event.is_error ? 'whitespace-pre-wrap text-destructive' : 'whitespace-pre-wrap text-(--ui-text-tertiary)'}
    >
      {event.content}
    </p>
  )
}

export const SubagentTask: FC<ToolCallMessagePartProps> = props => {
  const { args, result } = props
  const fromArgs = useMemo(() => readArgs(args), [args])
  const fromResult = useMemo(() => readResult(result), [result])
  const gateway = useStore($gateway)
  const sessionId = useStore(useSessionView().$runtimeId)

  const disclosureId = useMemo(() => toolPartDisclosureId(props as unknown as ToolPart), [props])
  const open = useStore(useMemo(() => $toolDisclosureOpen(disclosureId), [disclosureId])) ?? false

  const [transcript, setTranscript] = useState<SubagentTranscriptResponse | null>(null)
  const [loading, setLoading] = useState(false)

  const taskId = fromResult.task_id ?? fromArgs.task_id ?? ''
  const description = fromArgs.description ?? ''
  const status = fromResult.status ?? 'running'
  const summary = fromResult.summary

  const toggle = async () => {
    const next = !open
    setToolDisclosureOpen(disclosureId, next)

    if (next && !transcript && gateway && sessionId && taskId) {
      setLoading(true)
      try {
        const response = await gateway.request<SubagentTranscriptResponse>('subagent_transcript.get', {
          session_id: sessionId,
          task_id: taskId
        })
        setTranscript(response)
      } finally {
        setLoading(false)
      }
    }
  }

  return (
    <div className="my-1.5 rounded-md border border-primary/20 bg-(--ui-chat-surface-background) px-2.5 py-2 text-sm">
      <button aria-expanded={open} onClick={() => void toggle()} type="button">
        {description}
      </button>
      <p className="text-xs text-(--ui-text-tertiary)">
        {status === 'running' ? COPY.running : summary}
      </p>
      {open && (
        <div className="mt-2 grid gap-1 border-t border-(--ui-stroke-tertiary) pt-2">
          {loading && <p className="text-xs text-(--ui-text-tertiary)">{COPY.expand}…</p>}
          {transcript?.events?.map((event, index) => (
            <TranscriptEventRow event={event} key={index} />
          ))}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent/apps/desktop && npx vitest run --project ui src/components/assistant-ui/tool/subagent-task.test.tsx`
Expected: PASS (4 tests)

- [ ] **Step 5: Wire the dispatch branch in `message-parts.tsx`**

Edit `hermes-agent/apps/desktop/src/components/assistant-ui/thread/message-parts.tsx`. Add the import alongside the existing `ClarifyTool` import (line 9):

```tsx
import { ClarifyTool } from '@/components/assistant-ui/clarify-tool'
import { SubagentTask } from '@/components/assistant-ui/tool/subagent-task'
```

Edit `ChainToolFallback` (lines 39-51), adding the new branch immediately after the `clarify` branch:

```tsx
const ChainToolFallback: FC<ToolCallMessagePartProps> = props => {
  // todo parts are hoisted to a dedicated panel above the message content.
  if (props.toolName === 'todo') {
    return null
  }

  if (props.toolName === 'image_generate') {
    return <ImageGenerateTool {...props} />
  }

  if (props.toolName === 'clarify') {
    return <ClarifyTool {...props} />
  }

  if (props.toolName === 'claude_subagent_task') {
    return <SubagentTask {...props} />
  }

  return <ToolFallback {...props} />
}
```

- [ ] **Step 6: Run the full desktop UI test project to confirm no regressions**

Run: `cd hermes-agent/apps/desktop && npx vitest run --project ui`
Expected: PASS (all existing tests plus the 4 new ones, 0 failures)

- [ ] **Step 7: Commit**

```bash
cd hermes-agent
git add apps/desktop/src/components/assistant-ui/tool/subagent-task.tsx apps/desktop/src/components/assistant-ui/tool/subagent-task.test.tsx apps/desktop/src/components/assistant-ui/thread/message-parts.tsx
git commit -m "feat: render claude_subagent_task as an inline collapsed block in Desktop"
```

---

## Deliberate deviations from existing patterns

**Inline-collapsed vs. the existing sub-agent panel.** Hermes already has a full, separate sub-agent-visibility subsystem for its own `delegate_task`/kanban-worker mechanism — `apps/desktop/src/store/subagents.ts` (`SubagentProgress`/`SubagentNode`), rendered in a separate `apps/desktop/src/app/agents/index.tsx` panel, fed by dedicated gateway events (`subagent.spawn_requested`, `subagent.start`, etc. — confirmed live in `tui_gateway/server.py`'s `event_type.startswith("subagent.")` branch). This feature deliberately does **not** reuse that mechanism: it needs to persist inline with, and reload from, the durable message history the same way every other tool call does, whereas the kanban panel's store is ephemeral/session-scoped and panel-only — the architectural opposite of what inline-collapsed-in-conversation-history requires. This is why the new tool_call name and its persistence path are entirely separate from that system, not a variant of it.

## What's deliberately NOT in this plan

- **Multi-account (`agent/cli_accounts.py`, `switch_cli_account()`)** — Phase 2's plan, independent of this one; this plan never touches account resolution.
- **The SDK's `SessionStore` Protocol / on-disk `get_subagent_messages()`** — investigated and rejected (see "Investigation" above); not wired into `ClaudeAgentOptions` anywhere in this plan.
- **A "live" progress indicator while a sub-agent is still running** (e.g. streaming token counts into the collapsed header in real time) — `TaskProgressMessage.usage` is persisted into the transcript row's `status`/`updated_at` today, but the Desktop component only re-fetches on expand, not on a live subscription. A follow-up could wire a `subagent_transcript.updated` push event; out of scope here since the task's "what Phase 3 must cover" list only requires fetch-on-expand.
- **Localization of the new Desktop copy strings** — explicit scope decision, see Global Constraints.
