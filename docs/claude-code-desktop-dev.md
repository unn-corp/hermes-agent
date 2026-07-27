# Running the Desktop app in dev mode against the Claude Code integration

How to boot Hermes Desktop from a source checkout so it runs **your working
tree** — used to exercise the Claude Code CLI integration (Phases 1–3 of
`docs/design/claude-code-integration.md`) by hand, since the sub-agent
visibility work only shows up in the Desktop conversation view.

Nothing here is required for the test suite. `./scripts/run_tests.sh` covers
all three phases headlessly; this is for looking at the UI.

---

## How the dev build finds your code

`npm run dev` in `apps/desktop` runs two processes side by side:

| Process | What it does |
|---|---|
| `dev:renderer` | Vite dev server on `127.0.0.1:5174` (the React UI) |
| `dev:electron` | Compiles the Electron main process, waits for Vite, launches Electron |

**Electron spawns the Python backend itself** — you do not start a gateway
separately. It runs `<python> -m hermes_cli.main serve --host 127.0.0.1
--port 0` and discovers the port the child announces.

Which Python and which source tree it picks (`resolveHermesBackend` in
`apps/desktop/electron/main.ts`):

1. `HERMES_DESKTOP_HERMES_ROOT`, if set and it looks like a source root
   (contains `hermes_cli/main.py`). Honoured as-is — this is the "pin a
   worktree" escape hatch.
2. Otherwise, in a non-packaged build, `SOURCE_REPO_ROOT` — resolved as
   `apps/desktop/../..`, i.e. **the checkout you launched from**.
3. Otherwise the installed `~/.hermes/hermes-agent`.

Within the chosen root, `findPythonForRoot` takes `HERMES_DESKTOP_PYTHON` if
set, else `.venv/bin/python`, else `venv/bin/python`.

**Consequence:** running `npm run dev` from inside a worktree already uses
that worktree's code and its `.venv` — no env vars needed. Set
`HERMES_DESKTOP_HERMES_ROOT` only when launching from somewhere else, or to
be explicit in a script.

> Worktrees under `.worktrees/*` carry their own `.venv`; the submodule's main
> checkout does not. Launch from a worktree, or the backend resolution falls
> through to the installed copy and you will be testing the wrong code.

---

## Prerequisites

```bash
# 1. Node deps — install at the WORKSPACE ROOT, not apps/desktop.
#    apps/desktop/scripts/assert-root-install.mjs fails the dev script otherwise.
cd <checkout>            # e.g. hermes-agent/.worktrees/<branch>
npm install

# 2. Python env with the Claude Code extra.
uv pip install --python .venv/bin/python -e ".[all,dev]"
uv pip install --python .venv/bin/python "claude-agent-sdk==0.2.126"

# 3. The claude CLI itself — the SDK shells out to whatever is on PATH.
#    Floor is MIN_CLAUDE_VERSION = (2, 0, 0) in agent/transports/claude_code_sdk.py.
npm i -g @anthropic-ai/claude-code
claude --version
```

The venv's own `pip` is broken in these worktrees (`AttributeError: ... _log
has no attribute init_logging`) — use `uv pip install --python .venv/bin/python`
as above.

Verify before launching:

```bash
.venv/bin/python -c "import claude_agent_sdk; print(claude_agent_sdk.__version__)"  # 0.2.126
claude --version                                                                    # >= 2.0.0
node apps/desktop/scripts/assert-root-install.mjs && echo "root install ok"
```

---

## Use a throwaway HERMES_HOME

The Desktop honours `HERMES_HOME` (`apps/desktop/electron/main.ts`). Point it
at a scratch directory so experiments never touch your real
`~/.hermes/config.yaml`, sessions, or state DB:

```bash
export HERMES_HOME="$HOME/.hermes-dev-claudecode"
mkdir -p "$HERMES_HOME"
```

Everything below assumes that is set. Drop it only when you deliberately want
your real profile.

---

## Enable the `claude_code_sdk` runtime

Two conditions, both required (`_maybe_apply_claude_code_sdk_runtime` in
`hermes_cli/runtime_provider.py`) — the runtime is inert by default:

- provider resolves to `anthropic`
- `model.anthropic_runtime: claude_code_sdk`

```yaml
# $HERMES_HOME/config.yaml
model:
  default: claude-sonnet-5
  anthropic_runtime: claude_code_sdk   # unset or "auto" = normal Messages API
```

Auth comes from the `claude` CLI's own subscription/OAuth login, **not** an
Anthropic API key — that is the whole point of this runtime. Make sure the CLI
is logged in (`claude` once, interactively) before launching.

To fall back, unset `model.anthropic_runtime`. Nothing else changes.

---

## Launch

```bash
cd <checkout>/apps/desktop
npm run dev
```

Useful variants:

| Command | Why |
|---|---|
| `npm run dev` | Normal dev boot |
| `npm run dev:fake-boot` | Fake boot sequence — UI work without a real backend |
| `npm run profile:main` | Same, with the Electron main process on `--inspect=9229` |

Explicit pin, if launching from outside the checkout:

```bash
HERMES_DESKTOP_HERMES_ROOT=/abs/path/to/checkout \
HERMES_DESKTOP_PYTHON=/abs/path/to/checkout/.venv/bin/python \
npm run dev
```

---

## Exercising each phase

### Phase 1 — the transport

Send any message. With the runtime on, the whole turn is handed to the
`claude` CLI: assistant text streams in, and its tool calls (Bash, Read, Edit…)
render as ordinary tool rows. If the runtime is off you are on the normal
Messages API and cannot tell the difference from the UI alone — check that
tool calls carry Claude Code's tool names.

Failure to start surfaces in-conversation as
`Claude Code SDK turn failed: … Fall back to default runtime by unsetting
model.anthropic_runtime.`

### Phase 2 — multi-account hot-swap

```bash
# Register an isolated CLAUDE_CONFIG_DIR as a named account.
.venv/bin/python -m hermes_cli.main accounts add personal \
  --provider claude_code_sdk --config-dir "$HOME/.claude-personal"

.venv/bin/python -m hermes_cli.main accounts list
```

`accounts add` probes before persisting: it checks the binary version **and**
that `<config-dir>/.credentials.json` holds an `accessToken`, so a directory
that was never logged into is rejected up front.

Then, mid-conversation, `/cli-account claude_code_sdk personal`. It takes
effect on the **next** turn — the switch tears down the live session and the
lazy-init block picks up the new `config_dir`. Conversation history is
preserved, because both transports pass Hermes' history as per-turn context
rather than owning a server-side thread.

`/cli-account` with no arguments lists registered accounts. Note the provider
literal is `claude_code_sdk`, never bare `claude_code`.

### Phase 3 — sub-agent visibility

Prompt something that makes Claude Code delegate to its own **Task** tool
(e.g. "use a subagent to investigate X"). You should see:

- a single collapsed `claude_subagent_task` block with the task description,
  **not** a generic `Task` tool row — the generic bubble is deliberately
  suppressed so the dedicated one replaces it rather than duplicating it
- a running state, then the summary once the task reaches a terminal status
- on expand, the full sub-agent transcript, fetched via the
  `subagent_transcript.get` RPC

The sub-agent's own text and inner tool calls must **not** appear in the main
conversation stream — they belong to the transcript only.

Confirm it persisted (durable, so it survives reload):

```bash
sqlite3 "$HERMES_HOME/state.db" \
  "SELECT task_id, status, substr(summary,1,60) FROM subagent_transcripts;"
```

Expanding a collapsed block re-reads that table, so it works identically for a
session reloaded from history. The fetch happens once per block — collapsing
and re-expanding does not re-hit the gateway.

---

## Notes

- **`⚠ A previous lazy-backend refresh may have left the venv unhealthy`** on
  CLI startup is pre-existing noise from the lazy-backend probe, unrelated to
  this integration. Harmless.
- The renderer is plain Vite HMR; React edits hot-reload. Changes under
  `electron/` need a restart. **Python changes need a backend restart** —
  the backend is a spawned child, so restart the app.
- Copy for the sub-agent block is deliberately un-localised English literals
  (see the i18n note in `subagent-task.tsx`); localisation is a follow-up.
