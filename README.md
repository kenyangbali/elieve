# elieve

A modular Python AI-agent framework: a ReAct loop with function calling,
sandboxed tools, and a production-hardened harness for long autonomous
runs. Provider-agnostic — it works with **any OpenAI-compatible chat
completions API**.

## Features

- **ReAct loop** (`elieve/loop.py`) — think → act → observe with tool
  calls, step cap, and resumable runs (`progress.json` per run).
- **Sandboxed tools** (`elieve/tools/`) — `read_file`, `search`,
  `exec`, plus `remember`/`task_update` memory & task tools. Tools are
  confined to a configurable workspace root; destructive command
  patterns are blocked by default.
- **Context compaction** (`elieve/compaction.py`) — multi-layer
  compression (micro-compaction per turn, threshold full compaction)
  that keeps prompt-cache prefixes stable to cut token cost on long
  sessions.
- **Cross-session memory** (`elieve/memory.py`) — a `MEMORY.md` per
  outdir with `remember`/`recall`, plus `autoDream` periodic cleanup
  (`--tidy`).
- **2-stage permission gate** (`elieve/permissions.py`) — optional,
  pluggable classifier (fast yes/no stage + reasoning stage) with a
  fail-closed regex layer underneath when the classifier is off.
- **Multi-agent orchestrator** (`elieve/orchestrator.py`) — optional
  manager/worker mode: one planner spawns parallel workers (max depth
  1) with restricted toolsets, then merges their reports.
- **Lifecycle hooks** (`elieve/hooks.py`) — deterministic events the
  model can't skip: `PreToolUse`, `PostToolUse`, `PreCompact`,
  `PostCompact`, `OnStop`, `OnError`.
- **Task tracking** (`elieve/tasks.py`) — structured per-run checklists
  (`tasks.json`) with a `task_update` tool, surviving compaction.
- **Token/cost accounting** (`elieve/accounting.py`) — per-model usage
  totals (`usage.json`), context-window warnings, and configurable
  run cost caps.

## Quickstart

### Install

```bash
git clone https://github.com/elieve-dev/elieve.git
cd elieve
pip install -e .
```

Or install directly from git:

```bash
pip install git+https://github.com/elieve-dev/elieve.git
```

### Configure the API key

```bash
export OPENAI_API_KEY="sk-..."
```

Never commit keys to the repo. The key is read from the environment
variable named in the config (`api_key_env`), never stored in config
files.

### Run

The `pip install` step registers a `elieve` console entry point:

```bash
elieve --task "Summarize the README of ./workspace/demo" --outdir ./run-1
```

Without installing (repo checkout), the equivalent module invocation:

```bash
python3 -m elieve.loop --task "Summarize the README of ./workspace/demo" --outdir ./run-1
```

Each run writes `OUT.md` (final report) and `progress.json`
(resumable state) into `--outdir`.

### Custom provider via config

```bash
elieve --config configs/example.yaml --task "..." --outdir ./run-1
```

Minimal `example.yaml` (see `configs/example.yaml` for all blocks):

```yaml
provider:
  base_url: https://api.openai.com/v1
  api_key_env: OPENAI_API_KEY
  model: gpt-4o-mini
  timeout_s: 180

workspace_root: ./workspace
language: en   # en | id
```

### Local model example (Ollama)

```bash
export OLLAMA_API_KEY=ollama   # Ollama ignores the key; any dummy value works
```

```yaml
provider:
  base_url: http://localhost:11434/v1
  api_key_env: OLLAMA_API_KEY
  model: qwen3:8b
  timeout_s: 300
```

Any other OpenAI-compatible server (e.g. vLLM) works the same way —
just point `base_url` at its `/v1` endpoint and set the model name.

### Tests

```bash
python3 -m unittest discover -s tests
```

Runs the full unit suite (no network/API key needed).

### CLI reference

```
elieve --task TASK --outdir OUTDIR [--model MODEL] [--max-steps MAX_STEPS]
       [--config CONFIG] [--system-prompt SYSTEM_PROMPT] [--workspace WORKSPACE]
       [--lang {en,id}] [--profile {default,hunter}] [--tidy] [--no-exec]
```

- `--model` overrides the config's model; `--max-steps` caps ReAct
  steps (default 40).
- `--lang` selects the default system prompt (`en` | `id`).
- `--profile` selects the prompt persona: `default` (generic agent) or
  `hunter` (strict bug-hunter persona, opt-in).
- `--tidy` runs `autoDream` cleanup on the outdir's `MEMORY.md` and
  exits (no task is run).
- `--no-exec` drops the `exec` tool for a read-only run (used by
  orchestrator workers).

## Configuration

The `provider:` block in a YAML config:

| Key | Meaning |
|-----|---------|
| `base_url` | OpenAI-compatible chat completions endpoint (e.g. `https://api.openai.com/v1`) |
| `api_key_env` | Name of the env var holding the API key (key itself never in the file) |
| `model` | Default model for the run (overridable with `--model` / top-level `model:`) |
| `timeout_s` | Per-request timeout in seconds |
| `key_provider` | Alternative key source (e.g. `9router` reads from a local 9router DB instead of env) |

Other top-level keys:

| Key | Meaning |
|-----|---------|
| `model_policy` | `allow:` / `forbid:` model prefix lists (trailing `*` wildcards; `forbid` wins). Empty = unrestricted. |
| `workspace_root` | Sandbox root for tools (relative = resolved from cwd; `/tmp` always allowed) |
| `language` | Default system-prompt language: `en` or `id` (CLI `--lang` overrides) |
| `profile` | Prompt persona: `default` (generic agent) or `hunter` (bug-hunter, opt-in). CLI `--profile` overrides. Explicit `system_prompt` still wins over both. |

Note: `configs/bug-hunter.yaml` is a personal, Indonesian-language
example profile (built around a local gateway setup), **not** the
default. Use `configs/example.yaml` as the generic starting point.

## License

MIT — see [LICENSE](LICENSE).

Forks may relicense or replace the license file; the original file
retains the MIT grant as published.

## Documentation

| File | Description |
|------|-------------|
| `docs/ARCHITECTURE.md` | Original design doc for the 4-phase harness upgrades (legacy, Indonesian) |
| `docs/PHASE1-COMPACTION.md` | Compaction design notes: 4 layers, micro-compaction details (legacy, Indonesian) |
| `docs/HOOKS.md` | Lifecycle hook system: events, semantics, config (legacy, Indonesian) |
| `docs/TASKS.md` | Structured task tracking: `tasks.json`, `task_update` tool (legacy, Indonesian) |
| `docs/ACCOUNTING.md` | Token & cost accounting: usage tracking, warnings, cost caps (legacy, Indonesian) |
| `docs/GAP-AUDIT.md` | Feature-gap audit vs. public Claude Code docs; operational context is personal (legacy, Indonesian) |
