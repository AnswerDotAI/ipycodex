# ipycodex

`ipycodex` is an IPython extension that turns any input starting with `.` into an AI prompt.

It is aimed at terminal IPython, not notebook frontends.

## Install

```bash
pip install ipycodex
```

`ipycodex` now talks to the local `codex app-server`, so you also need the `codex` CLI installed and authenticated on the machine running IPython. Set `IPYAI_CODEX_CMD` if `codex` is not on your `PATH` or you want to point at a different binary.

## CLI

`ipycodex` provides a standalone command that launches IPython with `ipycodex` and `ipythonng` extensions pre-loaded and output history enabled:

```bash
ipycodex
```

Resume a previous session:

```bash
ipycodex -r        # interactive session picker
ipycodex -r 43     # resume session 43 directly
```

On exit, `ipycodex` prints the session ID so you can resume later.

## Load As Extension

```python
%load_ext ipycodex
```

If you change the package in a running shell:

```python
%reload_ext ipycodex
```

## How To Auto-Load `ipycodex`

Add this to an `ipython_config.py` file used by terminal `ipython`:

```python
c.TerminalIPythonApp.extensions = ["ipycodex.core"]
```

Good places for that file include:

- env-local: `{sys.prefix}/etc/ipython/ipython_config.py`
- user-local: `~/.ipython/profile_default/ipython_config.py`

In a virtualenv, the env-local path is usually `.venv/etc/ipython/ipython_config.py`.

To see which config paths your current `ipython` is searching, run:

```bash
ipython --debug -c 'exit()' 2>&1 | grep Searching
```

## Usage

Only the leading period is special. There is no closing delimiter.

Single line:

```python
.write a haiku about sqlite
```

Multiline paste:

```python
.summarize this module:
focus on state management
and persistence behavior
```

Backslash-Enter continuation in the terminal:

```python
.draft a migration plan \
with risks and rollback steps
```

`ipycodex` also provides a line and cell magic named `%ipycodex` / `%%ipycodex`.

Note: `.01 * 3` and similar expressions starting with `.` followed by a digit will be interpreted as prompts. Write `0.01 * 3` instead.

## Notes

Any IPython cell containing only a string literal is treated as a "note". Notes provide context to the AI without being executable code:

```python
"This is a note explaining what I'm about to do"
```

Notes appear in the AI context as `<note>` blocks rather than `<code>` blocks. When saving a session, notes are stored as markdown cells in the startup notebook.

## `%ipycodex` Commands

```python
%ipycodex
%ipycodex model gpt-5.4
%ipycodex completion_model gpt-5.4-mini
%ipycodex think m
%ipycodex search h
%ipycodex code_theme monokai
%ipycodex log_exact true
%ipycodex save
%ipycodex reset
```

- `%ipycodex` — show current settings and config file paths
- `%ipycodex model ...` / `completion_model ...` / `think ...` / `search ...` / `code_theme ...` / `log_exact ...` — change settings for the current session
- `%ipycodex save` — save the current session (code, notes, and AI history) to `startup.ipynb`
- `%ipycodex reset` — clear AI prompt history for the current session
- `%ipycodex sessions` — list resumable sessions for the current directory (falls back to git repo root)

## Tools

Expose a function from the active IPython namespace as a tool by referencing it with `&`name`` in the prompt:

```python
def weather(city): return f"Sunny in {city}"

.use &`weather` to answer the question about Brisbane
```

Callable objects and async callables are also supported.

Tools are discovered from multiple sources beyond direct `&`name`` mentions in prompts:

- **Skills**: tools listed in `allowed-tools` frontmatter or referenced with `&`name`` in the skill body
- **Notes**: string-literal cells can contain `&`name`` references or YAML frontmatter with `allowed-tools`
- **Tool responses**: when a tool result starts with YAML frontmatter containing `allowed-tools` or `eval: true`, any `&`name`` references and `allowed-tools` entries in that result are also added

All discovered tools that exist as callables in the IPython namespace are included in the AI's tool schema.

In addition to those dynamic tools, the Codex app-server can use its own built-in shell and file actions against the current working directory sandbox. Those server-side actions cannot see live IPython objects, so use `&` tools, `pyrun`, and variable references when interpreter state matters.

## Skills

`ipycodex` supports [Agent Skills](https://agentskills.io/) — reusable instruction sets that the AI can load on demand. Skills are discovered at extension load time from:

- `.agents/skills/` in the current directory and every parent directory
- `~/.config/agents/skills/`

Each skill is a directory containing a `SKILL.md` file with YAML frontmatter (`name`, `description`) and markdown instructions. Skills can also declare `allowed-tools` in their frontmatter (space-delimited list of tool names) to pre-approve tools without requiring explicit `&`name`` mentions in prompts.

At the start of each conversation, the AI sees a list of available skill names and descriptions. When a request matches a skill, the AI calls the `load_skill` tool to read its full instructions before responding.

Python code blocks in skills that start with `#| eval: true` (nbdev/quarto syntax) are executed in the IPython namespace when the skill is loaded, allowing skills to define tool functions:

````markdown
```python
#| eval: true
def my_tool(x):
    "A skill-provided tool"
    return x * 2
```
````

See the [Agent Skills specification](https://agentskills.io/specification.md) for the full format.

## Keyboard Shortcuts

`ipycodex` registers prompt_toolkit keybindings:

| Shortcut | Action |
|---|---|
| **Alt-.** | AI inline completion (calls Haiku, shows as greyed suggestion — accept with right arrow, or **Alt-f** to accept one word at a time) |
| **Alt-Up/Down** | Jump through complete history entries (skips line-by-line in multiline inputs) |
| **Alt-Shift-W** | Insert all Python code blocks from the last AI response |
| **Alt-Shift-1** through **Alt-Shift-9** | Insert the Nth code block |
| **Alt-Shift-Up/Down** | Cycle through code blocks one at a time |

Code blocks are extracted from fenced markdown blocks tagged as `python` or `py`. Blocks tagged with other languages (bash, json, etc.) or untagged blocks are skipped.

Syntax highlighting is disabled while typing `.` prompts and `%%ipycodex` cells so natural language isn't coloured as Python.

## Startup Replay

`%ipycodex save` snapshots the current session to `~/.config/ipycodex/startup.ipynb`:

- code cells are saved as code cells (notes become markdown cells)
- AI prompts are saved with the response as markdown and the prompt in cell metadata

When `ipycodex` loads into a fresh session, saved code is replayed and saved prompts are restored into the conversation history. This primes new sessions with imports, helpers, tools, and prior AI context without re-running the prompts.

## Output Rendering

Responses are streamed and rendered as markdown in the terminal via Rich. Model reasoning is displayed as blockquoted text during streaming and stored in `<thinking>` blocks in the response. Server-side command stdout is streamed live in TTY sessions, while the stored transcript keeps the final compact tool block. Tool calls are compacted to a short form like `🔧 f(x=1) => 2`.

## Configuration

Config files live under `~/.config/ipycodex/` and are created on demand:

| File | Purpose |
|---|---|
| `config.json` | Model, think/search level, code theme, log flag |
| `sysp.txt` | System prompt |
| `startup.ipynb` | Saved session snapshot |
| `exact-log.jsonl` | Raw prompt/response log (when `log_exact` is enabled) |

`config.json` supports:

```json
{
  "model": "gpt-5.4",
  "completion_model": "gpt-5.4-mini",
  "think": "l",
  "search": "l",
  "code_theme": "monokai",
  "log_exact": false
}
```

- `model` defaults from the `IPYAI_MODEL` environment variable if set when the config is first created
- `completion_model` is the model used for Alt-. inline completions
- `think` and `search` must be one of `l`, `m`, or `h`

## See Also

[ipyai](https://github.com/AnswerDotAI/ipyai) is a sister project with the same interface but backed by litellm/Anthropic instead of the Codex app-server.

## Development

See [DEV.md](DEV.md) for project layout, architecture, persistence details, and development workflow.
