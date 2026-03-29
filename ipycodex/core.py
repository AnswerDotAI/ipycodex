import argparse,ast,asyncio,atexit,json,os,re,signal,sys,uuid
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
from typing import Callable

from fastcore.xdg import xdg_config_home
from fastcore.xtras import frontmatter
from IPython import get_ipython
from IPython.core.inputtransformer2 import leading_empty_lines
from IPython.core.magic import Magics, cell_magic, line_magic, magics_class
from .codex_client import AsyncChat,AsyncStreamFormatter,get_codex_client,_history_xml
from rich.console import Console
from rich.file_proxy import FileProxy
from rich.live import Live
from rich.markdown import Markdown, TableDataElement

# Fix Rich bug: FileProxy.isatty doesn't delegate to underlying file
FileProxy.isatty = lambda self: self.rich_proxied_file.isatty()

# Fix Rich bug: TableDataElement.on_text clobbers syntax-highlight spans
def _tde_on_text(self, context, text):
    if isinstance(text, str): self.content.append(text, context.current_style)
    else: self.content.append_text(text)
TableDataElement.on_text = _tde_on_text
from toolslm.funccall import get_schema_nm

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_THINK = "l"
DEFAULT_SEARCH = "l"
DEFAULT_CODE_THEME = "monokai"
DEFAULT_LOG_EXACT = False
DEFAULT_PROMPT_MODE = False
DEFAULT_COMPLETION_MODEL = "gpt-5.4-mini"
_COMPLETION_SP = "You are a code completion engine for IPython. Return ONLY the completion text that should be inserted at the cursor position. No explanation, no markdown, no code fences, no prefix repetition."
DEFAULT_SYSTEM_PROMPT = """You are an AI assistant running inside IPython.

The user interacts with you through `ipycodex`, an IPython extension that turns input starting with a period into an AI prompt.

You may receive:
- a `<context>` XML block containing recent IPython code, outputs, and notes
- a `<user-request>` XML block containing the user's actual request

Inside `<context>`, entries tagged `<code>` are executed Python cells. Entries tagged `<note>` are user-written notes (cells whose only content is a string literal). Notes provide context and intent but are not executable code.

Earlier user turns in the chat history may also contain their own `<context>` blocks. When answering questions about what you have seen in the IPython session, consider the full chat history, not only the latest `<context>` block.

You can respond in Markdown. Your final visible output in terminal IPython will be rendered with Rich, so normal Markdown formatting, fenced code blocks, lists, and tables are appropriate when useful.

The user can attach context to their prompt using backtick references:
- `$`name`` exposes a variable's current value, shown as `<variable name="..." type="...">value</variable>` above the user's request
- `!`cmd`` runs a shell command and includes its output, shown as `<shell cmd="...">output</shell>` above the user's request

You can also use Codex app-server's built-in shell and file actions for normal workspace operations. Those server-side actions can inspect and modify files in the working directory sandbox, but they cannot access the live IPython namespace.

Use tools when they will materially improve correctness or completeness; otherwise answer directly. Prefer server-side shell and file actions for repository work. Use `pyrun` and `bash` when you need live Python state or the active IPython namespace. The user can register additional tools from the IPython namespace via `%ipycodex tool <name>`.

You have these dynamic tools available by default:
- `pyrun(code)`: Execute Python code in a sandboxed environment with access to the user's namespace. Use `pyrun('dir(...)')` to discover what's available on a module, object, or class. Use `pyrun('doc(...)')` to get its signature and docstring. Run `pyrun('doc(pyrun)')` to learn what's available in the sandbox.
- `bash(cmd)`: Run the local allowlisted shell helper when you specifically need it from the user's namespace.

Only dynamic tools can see live IPython objects. Server-side shell and file actions cannot.

Assume you are helping an interactive Python user. Prefer concise, accurate, practical responses. When writing code, default to Python unless the user asks for something else.
"""
MAGIC_NAME = "ipycodex"
LAST_PROMPT = "_ai_last_prompt"
LAST_RESPONSE = "_ai_last_response"
EXTENSION_NS = "_ipycodex"
EXTENSION_ATTR = "_ipycodex_extension"
RESET_LINE_NS = "_ipycodex_reset_line"
PROMPTS_TABLE = "codex_prompts"
PROMPTS_COLS = ["id", "session", "prompt", "response", "history_line"]
_PROMPTS_SQL = f"""CREATE TABLE IF NOT EXISTS {PROMPTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    response TEXT NOT NULL,
    history_line INTEGER NOT NULL DEFAULT 0)"""
_TOOLS_SQL = """CREATE TABLE IF NOT EXISTS codex_tools (
    session INTEGER NOT NULL,
    toolname TEXT NOT NULL,
    PRIMARY KEY (session, toolname))"""
_SESSIONS_SQL = """CREATE TABLE IF NOT EXISTS codex_sessions (
    session INTEGER PRIMARY KEY,
    thread_id TEXT NOT NULL)"""

def _ensure_codex_tables(db):
    if db is None: return
    with db:
        db.execute(_PROMPTS_SQL)
        cols = [o[1] for o in db.execute(f"PRAGMA table_info({PROMPTS_TABLE})")]
        if cols and cols != PROMPTS_COLS:
            db.execute(f"DROP TABLE {PROMPTS_TABLE}")
            db.execute(_PROMPTS_SQL)
        db.execute(f"CREATE INDEX IF NOT EXISTS idx_{PROMPTS_TABLE}_session_id ON {PROMPTS_TABLE} (session, id)")
        db.execute(_TOOLS_SQL)
        db.execute(_SESSIONS_SQL)

CONFIG_DIR = xdg_config_home()/"ipycodex"
CONFIG_PATH = CONFIG_DIR/"config.json"
SYSP_PATH = CONFIG_DIR/"sysp.txt"
LOG_PATH = CONFIG_DIR/"exact-log.jsonl"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

__all__ = """EXTENSION_ATTR EXTENSION_NS LAST_PROMPT LAST_RESPONSE MAGIC_NAME PROMPTS_TABLE RESET_LINE_NS DEFAULT_MODEL DEFAULT_COMPLETION_MODEL IPyAIExtension
create_extension CONFIG_PATH SYSP_PATH LOG_PATH is_dot_prompt load_ipython_extension
prompt_from_lines astream_to_stdout transform_dots unload_ipython_extension""".split()

_prompt_template = """{context}<user-request>{prompt}</user-request>"""
_var_re = re.compile(r"\$`(\w+(?:\([^`]*\))?)`")
_shell_re = re.compile(r"(?<![\w`])!`([^`]+)`")
_status_attrs = "model completion_model think search code_theme log_exact".split()


def _extract_code_blocks(text):
    from mistletoe import Document
    from mistletoe.block_token import CodeFence
    return [child.children[0].content.strip() for child in Document(text).children
        if isinstance(child, CodeFence) and child.language in ('python', 'py') and child.children and child.children[0].content.strip()]


def is_dot_prompt(lines: list[str]) -> bool: return bool(lines) and lines[0].startswith(".")


def prompt_from_lines(lines: list[str]) -> str | None:
    if not is_dot_prompt(lines): return None
    first,*rest = lines
    return "".join([first[1:], *rest]).replace("\\\n", "\n")


def transform_dots(lines: list[str], magic: str=MAGIC_NAME) -> list[str]:
    prompt = prompt_from_lines(lines)
    if prompt is None: return lines
    return [f"get_ipython().run_cell_magic({magic!r}, '', {prompt!r})\n"]


def transform_prompt_mode(lines: list[str], magic: str=MAGIC_NAME) -> list[str]:
    if not lines: return lines
    first = lines[0]
    stripped = first.lstrip()
    if not stripped or stripped == '\n': return lines
    if stripped.startswith(('!', '%')): return lines
    if stripped.startswith(';'): return [first.replace(';', '', 1)] + lines[1:]
    text = "".join(lines).replace("\\\n", "\n")
    return [f"get_ipython().run_cell_magic({magic!r}, '', {text!r})\n"]


def _tag(name: str, content="", **attrs) -> str:
    ats = "".join(f' {k}="{v}"' for k,v in attrs.items())
    return f"<{name}{ats}>{content}</{name}>"


def _is_ipycodex_input(source: str) -> bool:
    src = source.lstrip()
    return src.startswith(".") or src.startswith("%ipycodex") or src.startswith("%%ipycodex")


def _is_note(source):
    try: tree = ast.parse(source)
    except SyntaxError: return False
    return (len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str))


def _note_str(source): return ast.parse(source).body[0].value.value


def _var_names(text: str) -> set[str]: return set(_var_re.findall(text or ""))


def _exposed_vars(text):
    "Extract var names from frontmatter exposed-vars and $`var` mentions."
    fm, body = frontmatter(text)
    names = _var_names(text)
    if fm:
        ev = fm.get('exposed-vars', '')
        if ev: names |= set(str(ev).split())
    return names


def _var_refs(prompt, hist, notes=None):
    names = _var_names(prompt)
    for o in hist: names |= _var_names(o["prompt"])
    for n in (notes or []): names |= _exposed_vars(n)
    return names


_MISSING = object()

def _eval_var(name, ns):
    if '(' in name:
        try: ast.parse(name, mode='eval')
        except SyntaxError: return _MISSING
        try: return eval(name, ns)
        except Exception: return _MISSING
    return ns.get(name, _MISSING)

def _format_var_xml(names, ns):
    parts = []
    for n in sorted(names):
        v = _eval_var(n, ns)
        if v is _MISSING: continue
        parts.append(f'<variable name="{n}" type="{type(v).__name__}">{str(v)}</variable>')
    return "".join(parts)


def _shell_names(text: str) -> set[str]: return set(_shell_re.findall(text or ""))


def _shell_cmds(text):
    "Extract shell commands from frontmatter shell-cmds and !`cmd` mentions."
    fm, body = frontmatter(text)
    names = _shell_names(text)
    if fm:
        sc = fm.get('shell-cmds', '')
        if sc: names |= set(str(sc).split('\n')) if '\n' in str(sc) else {str(sc)}
    return names


def _shell_refs(prompt, hist, notes=None):
    names = _shell_names(prompt)
    for o in hist: names |= _shell_names(o["prompt"])
    for n in (notes or []): names |= _shell_cmds(n)
    return names


def _run_shell_refs(cmds):
    if not cmds: return ""
    import subprocess
    parts = []
    for cmd in sorted(cmds):
        try: out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout.rstrip()
        except Exception as e: out = f"Error: {e}"
        parts.append(f'<shell cmd="{cmd}">{out}</shell>')
    return "".join(parts)


def _event_sort_key(o): return o.get("line", 0), 0 if o.get("kind") == "code" else 1


def _thinking_to_blockquote(text):
    def _bq(m):
        from .codex_client import _blockquote
        return _blockquote(m.group(1).strip()) + "\n"
    return re.sub(r'<thinking>\n(.*?)\n</thinking>\n*', _bq, text, flags=re.DOTALL)


def _display_text(text): return _thinking_to_blockquote(text)


def _markdown_renderable(text: str, code_theme: str, markdown_cls=Markdown):
    return markdown_cls(text, code_theme=code_theme, inline_code_theme=code_theme, inline_code_lexer="python")


async def _astream_to_live_markdown(chunks, out, code_theme: str, formatter=None, partial=None, console_cls=Console, markdown_cls=Markdown,
    live_cls=Live) -> str:
    console = console_cls(file=out, force_terminal=True)
    text = ""
    live = None
    live_cm = None
    async for chunk in chunks:
        if chunk:
            text += chunk
            if partial is not None: partial.append(chunk)
        display_text = getattr(formatter, "display_text", None) if formatter is not None else None
        current = text if display_text is None else display_text
        if not current: continue
        renderable = _markdown_renderable(_display_text(current), code_theme, markdown_cls)
        if live is None:
            live_cm = live_cls(renderable, console=console, auto_refresh=False, transient=False, redirect_stdout=True, redirect_stderr=False,
                vertical_overflow="visible")
            live = live_cm.__enter__()
        else: live.update(renderable, refresh=True)
    if live_cm is not None: live_cm.__exit__(None, None, None)
    return getattr(formatter, "final_text", text)


async def astream_to_stdout(stream, formatter_cls: Callable[..., AsyncStreamFormatter]=AsyncStreamFormatter, out=None,
    code_theme: str=DEFAULT_CODE_THEME, partial=None, console_cls=Console, markdown_cls=Markdown, live_cls=Live) -> str:
    out = sys.stdout if out is None else out
    fmt = formatter_cls()
    is_tty = getattr(out, "isatty", lambda: False)()
    if hasattr(fmt, "is_tty"): fmt.is_tty = is_tty
    chunks = fmt.format_stream(stream)
    if is_tty: return await _astream_to_live_markdown(chunks, out, code_theme, formatter=fmt, partial=partial, console_cls=console_cls,
        markdown_cls=markdown_cls, live_cls=live_cls)
    res = []
    async for chunk in chunks:
        if not chunk: continue
        out.write(chunk)
        out.flush()
        res.append(chunk)
        if partial is not None: partial.append(chunk)
    written = "".join(res)
    if written and not written.endswith("\n"):
        out.write("\n")
        out.flush()
    return getattr(fmt, "final_text", written)


def _validate_level(name: str, value: str, default: str) -> str:
    value = (value or default).strip().lower()
    if value not in {"l", "m", "h"}: raise ValueError(f"{name} must be one of h/m/l, got {value!r}")
    return value


def _validate_bool(name: str, value, default: bool) -> bool:
    if value is None: return default
    if isinstance(value, bool): return value
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"1", "true", "yes", "on"}: return True
        if value in {"0", "false", "no", "off"}: return False
    raise ValueError(f"{name} must be a boolean, got {value!r}")


@contextmanager
def _suppress_output_history(shell):
    pub = getattr(shell, "display_pub", None)
    if pub is None or not hasattr(pub, "_is_publishing"):
        yield
        return
    old = pub._is_publishing
    pub._is_publishing = True
    try: yield
    finally: pub._is_publishing = old


def _default_config():
    return dict(model=os.environ.get("IPYAI_MODEL", DEFAULT_MODEL), completion_model=DEFAULT_COMPLETION_MODEL, think=DEFAULT_THINK,
        search=DEFAULT_SEARCH, code_theme=DEFAULT_CODE_THEME, log_exact=DEFAULT_LOG_EXACT, prompt_mode=DEFAULT_PROMPT_MODE)


def load_config(path=None) -> dict:
    path = Path(path or CONFIG_PATH)
    cfg = _default_config()
    if path.exists():
        data = json.loads(path.read_text())
        if not isinstance(data, dict): raise ValueError(f"Invalid config format in {path}")
        cfg.update({k:v for k,v in data.items() if k in cfg})
    else: path.write_text(json.dumps(cfg, indent=2) + "\n")
    cfg["model"] = str(cfg["model"]).strip() or DEFAULT_MODEL
    cfg["completion_model"] = str(cfg["completion_model"]).strip() or DEFAULT_COMPLETION_MODEL
    cfg["think"] = _validate_level("think", cfg["think"], DEFAULT_THINK)
    cfg["search"] = _validate_level("search", cfg["search"], DEFAULT_SEARCH)
    cfg["code_theme"] = str(cfg["code_theme"]).strip() or DEFAULT_CODE_THEME
    cfg["log_exact"] = _validate_bool("log_exact", cfg["log_exact"], DEFAULT_LOG_EXACT)
    cfg["prompt_mode"] = _validate_bool("prompt_mode", cfg["prompt_mode"], DEFAULT_PROMPT_MODE)
    return cfg


def load_sysp(path=None) -> str:
    path = Path(path or SYSP_PATH)
    if not path.exists(): path.write_text(DEFAULT_SYSTEM_PROMPT)
    return path.read_text()


def _cell_id(): return uuid.uuid4().hex[:8]


def _event_to_cell(o):
    if o.get("kind") == "code":
        source = o.get("source", "")
        if _is_note(source):
            return dict(id=_cell_id(), cell_type="markdown", source=_note_str(source),
                metadata=dict(ipycodex=dict(kind="code", line=o.get("line", 0), source=source)))
        return dict(id=_cell_id(), cell_type="code", source=source, metadata=dict(ipycodex=dict(kind="code", line=o.get("line", 0))),
            outputs=[], execution_count=None)
    if o.get("kind") == "prompt":
        meta = dict(kind="prompt", line=o.get("line", 0), history_line=o.get("history_line", 0), prompt=o.get("prompt", ""))
        return dict(id=_cell_id(), cell_type="markdown", source=o.get("response", ""), metadata=dict(ipycodex=meta))


def _cell_to_event(cell):
    meta = cell.get("metadata", {}).get("ipycodex", {})
    kind = meta.get("kind")
    if kind == "code":
        source = meta.get("source") or cell.get("source", "")
        return dict(kind="code", line=meta.get("line", 0), source=source)
    if kind == "prompt":
        return dict(kind="prompt", line=meta.get("line", 0), history_line=meta.get("history_line", 0),
            prompt=meta.get("prompt", ""), response=cell.get("source", ""))


def _load_notebook(path) -> list:
    "Load events from an ipycodex .ipynb file."
    path = Path(path)
    if not path.exists(): raise FileNotFoundError(f"Notebook not found: {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict): raise ValueError(f"Invalid notebook format in {path}")
    return [e for c in data.get("cells", []) if (e := _cell_to_event(c)) is not None]


def _git_repo_root(path):
    "Walk up from `path` looking for `.git`, return repo root or None."
    p = Path(path).resolve()
    for d in [p] + list(p.parents):
        if (d / ".git").exists(): return str(d)
    return None

_LIST_SQL = """SELECT s.session, s.start, s.end, s.num_cmds, s.remark,
    (SELECT prompt FROM codex_prompts WHERE session=s.session ORDER BY id DESC LIMIT 1)
    FROM sessions s WHERE s.remark{w} ORDER BY s.session DESC LIMIT 20"""

def _list_sessions(db, cwd):
    "Return recent sessions for `cwd`, falling back to git repo root exact match."
    rows = db.execute(_LIST_SQL.format(w="=?"), (cwd,)).fetchall()
    if not rows:
        repo = _git_repo_root(cwd)
        if repo and repo != cwd: rows = db.execute(_LIST_SQL.format(w="=?"), (repo,)).fetchall()
    return rows

def _fmt_session(sid, start, ncmds, last_prompt, max_prompt=60):
    "Format a session row as a display string."
    p = (last_prompt or '').replace('\n', ' ')[:max_prompt]
    if last_prompt and len(last_prompt) > max_prompt: p += '...'
    return f"{sid:>6}  {str(start or '')[:19]:20}  {ncmds or 0:>5}  {p}"

def _pick_session(rows):
    "Show an interactive session picker, return chosen session ID or None."
    from prompt_toolkit.shortcuts import radiolist_dialog
    values = [(sid, _fmt_session(sid, start, ncmds, lp)) for sid,start,end,ncmds,remark,lp in rows]
    return radiolist_dialog(title="Resume session", text="Select a session to resume:", values=values, default=values[0][0]).run()

def resume_session(shell, session_id):
    "Replace the current fresh session with an existing one."
    hm = shell.history_manager
    fresh_id = hm.session_number
    row = hm.db.execute("SELECT session FROM sessions WHERE session=?", (session_id,)).fetchone()
    if not row: raise ValueError(f"Session {session_id} not found")
    with hm.db:
        hm.db.execute("DELETE FROM sessions WHERE session=?", (fresh_id,))
        hm.db.execute("UPDATE sessions SET end=NULL WHERE session=?", (session_id,))
    hm.session_number = session_id
    max_line = hm.db.execute("SELECT MAX(line) FROM history WHERE session=?", (session_id,)).fetchone()[0]
    shell.execution_count = (max_line or 0) + 1
    hm.input_hist_parsed.extend([""] * (shell.execution_count - 1))
    hm.input_hist_raw.extend([""] * (shell.execution_count - 1))

@magics_class
class AIMagics(Magics):
    def __init__(self, shell, ext):
        super().__init__(shell)
        self.ext = ext

    @line_magic("ipycodex")
    def ipycodex_line(self, line: str=""): return self.ext.handle_line(line)

    @cell_magic("ipycodex")
    async def ipycodex_cell(self, line: str="", cell: str | None=None): await self.ext.run_prompt(cell)


class IPyAIExtension:
    def __init__(self, shell, model=None, completion_model=None, think=None, search=None, code_theme=None, log_exact=None,
        system_prompt=None, prompt_mode=None):
        self.shell,self.loaded = shell,False
        cfg = load_config(CONFIG_PATH)
        self.prompt_mode = cfg["prompt_mode"] ^ bool(prompt_mode)
        self.model = model or cfg["model"]
        self.completion_model = completion_model or cfg["completion_model"]
        self.think = _validate_level("think", think if think is not None else cfg["think"], DEFAULT_THINK)
        self.search = _validate_level("search", search if search is not None else cfg["search"], DEFAULT_SEARCH)
        self.code_theme = str(code_theme or cfg["code_theme"]).strip() or DEFAULT_CODE_THEME
        self.log_exact = _validate_bool("log_exact", log_exact if log_exact is not None else cfg["log_exact"], DEFAULT_LOG_EXACT)
        self.system_prompt = system_prompt if system_prompt is not None else load_sysp(SYSP_PATH)
        self._thread_id = None
        self._tools_dirty = False
        from safecmd import bash, ex, sed
        from pyskills import doc
        shell.user_ns.setdefault("bash", bash)
        shell.user_ns.setdefault("ex", ex)
        shell.user_ns.setdefault("sed", sed)
        shell.user_ns.setdefault("doc", doc)

    @property
    def history_manager(self): return getattr(self.shell, "history_manager", None)

    @property
    def session_number(self): return getattr(self.history_manager, "session_number", 0)

    @property
    def reset_line(self): return self.shell.user_ns.get(RESET_LINE_NS, 0)

    @property
    def db(self):
        hm = self.history_manager
        return None if hm is None else hm.db

    def ensure_tables(self): _ensure_codex_tables(self.db)

    def prompt_records(self, session: int | None=None) -> list:
        if self.db is None: return []
        self.ensure_tables()
        session = self.session_number if session is None else session
        cur = self.db.execute(f"SELECT id, prompt, response, history_line FROM {PROMPTS_TABLE} WHERE session=? ORDER BY id", (session,))
        return cur.fetchall()

    def prompt_rows(self, session: int | None=None) -> list: return [(p, r) for _,p,r,_ in self.prompt_records(session=session)]

    def last_prompt_line(self, session: int | None=None) -> int:
        rows = self.prompt_records(session=session)
        return rows[-1][3] if rows else self.reset_line

    def current_prompt_line(self) -> int:
        c = getattr(self.shell, "execution_count", 1)
        return max(c-1, 0)

    def current_input_line(self) -> int: return max(getattr(self.shell, "execution_count", 1), 1)

    def code_history(self, start: int, stop: int) -> list:
        hm = self.history_manager
        if hm is None or stop <= start: return []
        return list(hm.get_range(session=0, start=start, stop=stop, raw=True, output=True))

    def full_history(self) -> list: return self.code_history(1, self.current_input_line()+1)

    def code_context(self, start: int, stop: int) -> str:
        entries = self.code_history(start, stop)
        parts = []
        for _,line,pair in entries:
            source,output = pair
            if not source or _is_ipycodex_input(source): continue
            if _is_note(source): parts.append(_tag("note", _note_str(source)))
            else:
                parts.append(_tag("code", source))
                if output is not None: parts.append(_tag("output", output))
        if not parts: return ""
        return _tag("context", "".join(parts)) + "\n"

    def format_prompt(self, prompt: str, start: int, stop: int) -> str:
        ctx = self.code_context(start, stop)
        return _prompt_template.format(context=ctx, prompt=prompt.strip())

    def dialog_history(self) -> list:
        "Build serialized conversation history for thread migration."
        hist = []
        prev_line = self.reset_line
        for pid,prompt,response,history_line in self.prompt_records():
            if not response.strip(): response = "<system>user interrupted</system>"
            hist += [self.format_prompt(prompt, prev_line+1, history_line), response]
            prev_line = history_line
        return hist

    def note_strings(self, start, stop):
        "Return note string values from code history in range."
        return [_note_str(src) for _,_,pair in self.code_history(start, stop) if (src := pair[0]) and _is_note(src)]

    def _get_tool_names(self):
        "Get tool names: auto-tools + codex_tools table entries."
        ns = self.shell.user_ns
        names = set()
        for t in ("pyrun", "bash"):
            if callable(ns.get(t)): names.add(t)
        if self.db:
            for row in self.db.execute("SELECT toolname FROM codex_tools WHERE session=?", (self.session_number,)):
                if callable(ns.get(row[0])): names.add(row[0])
        return names

    def _get_tools(self):
        "Get tool schemas for the current tool set."
        ns = self.shell.user_ns
        return [dict(type="function", function=get_schema_nm(o, ns, pname="parameters")) for o in sorted(self._get_tool_names()) if callable(ns.get(o))]

    async def _ensure_thread(self):
        "Start or resume the codex thread for this session."
        if self._thread_id and not self._tools_dirty: return
        self.ensure_tables()
        client = get_codex_client()
        if self._tools_dirty and self._thread_id:
            await self._migrate_thread()
            return
        if self.db:
            row = self.db.execute("SELECT thread_id FROM codex_sessions WHERE session=?", (self.session_number,)).fetchone()
            if row:
                try:
                    self._thread_id = await client.resume_thread(row[0], sp=self.system_prompt)
                    return
                except Exception: pass
        tools = self._get_tools()
        self._thread_id = await client.start_thread(model=self.model, sp=self.system_prompt, tools=tools, search=self.search)
        if self.db:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO codex_sessions (session, thread_id) VALUES (?,?)",
                    (self.session_number, self._thread_id))

    async def _migrate_thread(self):
        "Start a new thread with updated tools, replaying conversation history."
        hist = self.dialog_history()
        tools = self._get_tools()
        client = get_codex_client()
        new_id = await client.start_thread(model=self.model, sp=self.system_prompt, tools=tools, search=self.search)
        if hist:
            migration_prompt = (_history_xml(hist)
                + "The above conversation has been migrated to this new thread. Respond with 'ok'.")
            async for _ in client.turn_stream(new_id, migration_prompt, ns=self.shell.user_ns, think="l"): pass
        self._thread_id = new_id
        self._tools_dirty = False
        if self.db:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO codex_sessions (session, thread_id) VALUES (?,?)",
                    (self.session_number, new_id))

    def _register_tool(self, name):
        "Register a tool from user_ns for the current session."
        ns = self.shell.user_ns
        if name not in ns: raise NameError(f"{name!r} is not defined")
        if not callable(ns[name]): raise TypeError(f"{name!r} is not callable")
        if self.db:
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO codex_tools (session, toolname) VALUES (?,?)",
                    (self.session_number, name))
        self._tools_dirty = True

    def save_prompt(self, prompt: str, response: str, history_line: int):
        if self.db is None: return
        self.ensure_tables()
        with self.db:
            self.db.execute(f"INSERT INTO {PROMPTS_TABLE} (session, prompt, response, history_line) VALUES (?, ?, ?, ?)",
                (self.session_number, prompt, response, history_line))

    def startup_events(self) -> list[dict]:
        events = []
        for _,line,pair in self.full_history():
            source,_ = pair
            if not source or _is_ipycodex_input(source): continue
            events.append(dict(kind="code", line=line, source=source))
        for pid,prompt,response,history_line in self.prompt_records():
            events.append(dict(kind="prompt", id=pid, line=history_line+1, history_line=history_line, prompt=prompt, response=response))
        return sorted(events, key=_event_sort_key)

    def save_notebook(self, path) -> tuple[int,int]:
        path = Path(path)
        if path.suffix != '.ipynb': path = path.with_suffix('.ipynb')
        events = [{k:v for k,v in o.items() if k != "id"} for o in self.startup_events()]
        nb = dict(cells=[_event_to_cell(e) for e in events], metadata=dict(ipycodex_version=1), nbformat=4, nbformat_minor=5)
        path.write_text(json.dumps(nb, indent=2) + "\n")
        return path, sum(o["kind"] == "code" for o in events), sum(o["kind"] == "prompt" for o in events)

    def _advance_execution_count(self):
        if hasattr(self.shell, "execution_count"): self.shell.execution_count += 1

    def load_notebook(self, path) -> tuple[int,int]:
        path = Path(path)
        if path.suffix != '.ipynb': path = path.with_suffix('.ipynb')
        events = _load_notebook(path)
        ncode = nprompt = 0
        for o in sorted(events, key=_event_sort_key):
            if o.get("kind") == "code":
                source = o.get("source", "")
                if not source: continue
                res = self.shell.run_cell(source, store_history=True)
                ncode += 1
                if getattr(res, "success", True) is False: break
            elif o.get("kind") == "prompt":
                history_line = int(o.get("history_line", max(o.get("line", 1)-1, 0)))
                self.save_prompt(o.get("prompt", ""), o.get("response", ""), history_line)
                self._advance_execution_count()
                nprompt += 1
        return path, ncode, nprompt

    def log_exact_exchange(self, prompt: str, response: str):
        if not self.log_exact: return
        rec = dict(ts=datetime.now(timezone.utc).isoformat(), session=self.session_number, prompt=prompt, response=response)
        with LOG_PATH.open("a") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def reset_session_history(self) -> int:
        if self.db is None: return 0
        self.ensure_tables()
        with self.db: cur = self.db.execute(f"DELETE FROM {PROMPTS_TABLE} WHERE session=?", (self.session_number,))
        self.shell.user_ns.pop(LAST_PROMPT, None)
        self.shell.user_ns.pop(LAST_RESPONSE, None)
        self.shell.user_ns[RESET_LINE_NS] = self.current_prompt_line()
        self._thread_id = None
        return cur.rowcount or 0

    def _register_keybindings(self):
        pt_app = getattr(self.shell, 'pt_app', None)
        if pt_app is None: return
        auto_suggest = pt_app.auto_suggest
        if auto_suggest:
            auto_suggest._ai_full_text = None
            _orig_get = auto_suggest.get_suggestion
            def _patched_get(buffer, document):
                from prompt_toolkit.auto_suggest import Suggestion
                text,ft = document.text,auto_suggest._ai_full_text
                if ft and ft.startswith(text) and len(ft) > len(text): return Suggestion(ft[len(text):])
                auto_suggest._ai_full_text = None
                return _orig_get(buffer, document)
            auto_suggest.get_suggestion = _patched_get
        ns = self.shell.user_ns
        def _get_blocks(): return _extract_code_blocks(ns.get(LAST_RESPONSE, ''))
        @pt_app.key_bindings.add('escape', 'W')
        def _paste_all(event):
            blocks = _get_blocks()
            if blocks: event.current_buffer.insert_text('\n'.join(blocks))
        for i,ch in enumerate('!@#$%^&*(', 1):
            @pt_app.key_bindings.add('escape', ch)
            def _paste_nth(event, n=i):
                blocks = _get_blocks()
                if len(blocks) >= n: event.current_buffer.insert_text(blocks[n-1])
        cycle = dict(idx=-1, resp='')
        def _cycle(event, delta):
            resp = ns.get(LAST_RESPONSE, '')
            blocks = _get_blocks()
            if not blocks: return
            if resp != cycle['resp']: cycle.update(idx=-1, resp=resp)
            cycle['idx'] = (cycle['idx'] + delta) % len(blocks)
            from prompt_toolkit.document import Document
            event.current_buffer.document = Document(blocks[cycle['idx']])
        @pt_app.key_bindings.add('escape', 's-up')
        def _cycle_down(event): _cycle(event, 1)
        @pt_app.key_bindings.add('escape', 's-down')
        def _cycle_up(event): _cycle(event, -1)
        @pt_app.key_bindings.add('escape', 'up')
        def _hist_back(event): event.current_buffer.history_backward()
        @pt_app.key_bindings.add('escape', 'down')
        def _hist_fwd(event): event.current_buffer.history_forward()
        @pt_app.key_bindings.add('escape', '.')
        def _ai_suggest(event):
            buf = event.current_buffer
            doc = buf.document
            if not doc.text.strip(): return
            app = event.app
            async def _do_complete():
                try:
                    text = await self._ai_complete(doc)
                    if text and buf.document == doc:
                        from prompt_toolkit.auto_suggest import Suggestion
                        if auto_suggest: auto_suggest._ai_full_text = doc.text + text
                        buf.suggestion = Suggestion(text)
                        app.invalidate()
                except Exception: pass
            app.create_background_task(_do_complete())
        @pt_app.key_bindings.add('escape', 'p')
        def _toggle_prompt(event):
            self._toggle_prompt_mode()
            from prompt_toolkit.formatted_text import PygmentsTokens
            pt_app.message = PygmentsTokens(self.shell.prompts.in_prompt_tokens())
            event.app.invalidate()

    async def _ai_complete(self, document):
        prefix,suffix = document.text_before_cursor,document.text_after_cursor
        ctx = self.code_context(self.last_prompt_line()+1, self.current_prompt_line())
        parts = []
        if ctx: parts.append(ctx)
        parts.append(f"<current-input>\n<prefix>{prefix}</prefix>")
        if suffix.strip(): parts.append(f"<suffix>{suffix}</suffix>")
        parts.append("</current-input>")
        parts.append("\nReturn ONLY the completion text to insert immediately after the prefix."
            " Do not repeat the prefix or include any explanation.")
        chat = AsyncChat(model=self.completion_model, sp=_COMPLETION_SP)
        res = await chat("\n".join(parts))
        return (res.content or "").strip()

    def _patch_lexer(self):
        from IPython.terminal.ptutils import IPythonPTLexer
        from prompt_toolkit.lexers import SimpleLexer
        _plain = SimpleLexer()
        _orig = IPythonPTLexer.lex_document
        ext = self
        def _lex_document(self, document):
            text = document.text.lstrip()
            if ext.prompt_mode and not text.startswith((';', '!', '%')): return _plain.lex_document(document)
            if text.startswith('.') or text.startswith('%%ipycodex'): return _plain.lex_document(document)
            return _orig(self, document)
        IPythonPTLexer.lex_document = _lex_document

    def load(self):
        if self.loaded: return self
        self.ensure_tables()
        cts = self.shell.input_transformer_manager.cleanup_transforms
        if self.prompt_mode:
            if transform_prompt_mode not in cts: cts.insert(0, transform_prompt_mode)
            self._swap_prompts()
        elif transform_dots not in cts:
            idx = 1 if cts and cts[0] is leading_empty_lines else 0
            cts.insert(idx, transform_dots)
        self.shell.register_magics(AIMagics(self.shell, self))
        self.shell.user_ns[EXTENSION_NS] = self
        self.shell.user_ns.setdefault(RESET_LINE_NS, 0)
        setattr(self.shell, EXTENSION_ATTR, self)
        self._register_keybindings()
        self._patch_lexer()
        self.loaded = True
        return self

    def unload(self):
        if not self.loaded: return self
        cts = self.shell.input_transformer_manager.cleanup_transforms
        if transform_dots in cts: cts.remove(transform_dots)
        if transform_prompt_mode in cts: cts.remove(transform_prompt_mode)
        if self.shell.user_ns.get(EXTENSION_NS) is self: self.shell.user_ns.pop(EXTENSION_NS, None)
        if getattr(self.shell, EXTENSION_ATTR, None) is self: delattr(self.shell, EXTENSION_ATTR)
        self.loaded = False
        return self

    def _show(self, attr): return print(f"self.{attr}={getattr(self, attr)!r}")

    def _set(self, attr, value):
        setattr(self, attr, value)
        return self._show(attr)

    def _toggle_prompt_mode(self):
        self.prompt_mode = not self.prompt_mode
        cts = self.shell.input_transformer_manager.cleanup_transforms
        if self.prompt_mode:
            if transform_prompt_mode not in cts: cts.insert(0, transform_prompt_mode)
            if transform_dots in cts: cts.remove(transform_dots)
        else:
            if transform_prompt_mode in cts: cts.remove(transform_prompt_mode)
            if transform_dots not in cts:
                idx = 1 if cts and cts[0] is leading_empty_lines else 0
                cts.insert(idx, transform_dots)
        self._swap_prompts()
        state = "ON" if self.prompt_mode else "OFF"
        print(f"Prompt mode {state}")

    def _swap_prompts(self):
        from IPython.terminal.prompts import Prompts, Token
        shell = self.shell
        if self.prompt_mode:
            if not hasattr(self, '_orig_prompts'): self._orig_prompts = shell.prompts
            class PromptModePrompts(Prompts):
                def in_prompt_tokens(self_p): return [(Token.Prompt, 'Pr ['), (Token.PromptNum, str(shell.execution_count)), (Token.Prompt, ']: ')]
            shell.prompts = PromptModePrompts(shell)
        elif hasattr(self, '_orig_prompts'): shell.prompts = self._orig_prompts

    def _show_help(self):
        cmds = [("(no args)", "Show current settings"), ("help", "Show this help"), ("model <name>", "Set model"), ("think <l|m|h>",
            "Set thinking level"), ("search <l|m|h>", "Set search level"), ("prompt", "Toggle prompt mode"),
            ("tool <name>", "Register a tool from user_ns"), ("tools", "List registered tools"),
            ("save <file>", "Save session to .ipynb"), ("load <file>", "Load session from .ipynb"),
            ("reset", "Clear AI prompts from current session"), ("sessions", "List previous sessions")]
        print("Usage: %ipycodex <command>\n")
        for cmd, desc in cmds: print(f"  {cmd:20s} {desc}")

    def handle_line(self, line: str):
        line = line.strip()
        if not line:
            for o in _status_attrs: self._show(o)
            print(f"{CONFIG_PATH=}")
            print(f"{SYSP_PATH=}")
            return print(f"{LOG_PATH=}")
        if line in _status_attrs: return self._show(line)
        if line == "prompt": return self._toggle_prompt_mode()
        if line == "reset":
            n = self.reset_session_history()
            return print(f"Deleted {n} AI prompts from session {self.session_number}.")
        if line == "tools":
            names = sorted(self._get_tool_names())
            return print(", ".join(names) if names else "No tools registered.")
        if line == "sessions":
            rows = _list_sessions(self.db, os.getcwd())
            if not rows: return print("No sessions found for this directory.")
            print(f"{'ID':>6}  {'Start':20}  {'Cmds':>5}  {'Last prompt'}")
            for sid,start,end,ncmds,remark,lp in rows: print(_fmt_session(sid, start, ncmds, lp))
            return
        cmd,_,arg = line.partition(" ")
        clean = arg.strip()
        if cmd == "tool":
            if not clean: return print("Usage: %ipycodex tool <name>")
            try:
                self._register_tool(clean)
                return print(f"Tool '{clean}' registered. Thread will be migrated on next prompt.")
            except (NameError, TypeError) as e: return print(str(e))
        if cmd == "save":
            if not clean: return print("Usage: %ipycodex save <filename>")
            path, ncode, nprompt = self.save_notebook(clean)
            return print(f"Saved {ncode} code cells and {nprompt} prompts to {path}.")
        if cmd == "load":
            if not clean: return print("Usage: %ipycodex load <filename>")
            try:
                path, ncode, nprompt = self.load_notebook(clean)
                return print(f"Loaded {ncode} code cells and {nprompt} prompts from {path}.")
            except FileNotFoundError as e: return print(str(e))
        if cmd == "help": return self._show_help()
        if clean:
            vals = dict(model=lambda: clean, completion_model=lambda: clean or DEFAULT_COMPLETION_MODEL, code_theme=lambda: clean or DEFAULT_CODE_THEME,
                think=lambda: _validate_level("think", clean, self.think), search=lambda: _validate_level("search", clean, self.search),
                log_exact=lambda: _validate_bool("log_exact", clean, self.log_exact))
            if cmd in vals: return self._set(cmd, vals[cmd]())
        return print(f"Unknown command: {line!r}. Run %ipycodex help for available commands.")

    async def run_prompt(self, prompt: str):
        prompt = (prompt or "").rstrip("\n")
        if not prompt.strip(): return None
        await self._ensure_thread()
        history_line = self.current_prompt_line()
        records = self.prompt_records()
        recs = [dict(prompt=p, history_line=hl) for _,p,_,hl in records]
        # Collect notes for var/shell refs
        notes = []
        prev_line = self.reset_line
        for o in recs:
            notes += self.note_strings(prev_line+1, o["history_line"])
            prev_line = o["history_line"]
        notes += self.note_strings(self.last_prompt_line()+1, history_line)
        ns = self.shell.user_ns
        var_names = _var_refs(prompt, recs, notes=notes)
        missing_vars = sorted(n for n in var_names if n not in ns)
        var_xml = _format_var_xml(var_names, ns)
        shell_cmds = _shell_refs(prompt, recs, notes=notes)
        shell_xml = _run_shell_refs(shell_cmds)
        warnings = ""
        if missing_vars: warnings = _tag("warnings", f"The following symbols were referenced but aren't defined in the interpreter: {', '.join(missing_vars)}") + "\n"
        prefix = var_xml + shell_xml
        full_prompt = self.format_prompt(prompt, self.last_prompt_line()+1, history_line)
        full_prompt = warnings + prefix + full_prompt
        self.shell.user_ns[LAST_PROMPT] = prompt
        client = get_codex_client()
        stream = client.turn_stream(self._thread_id, full_prompt, ns=ns, think=self.think)
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        loop.add_signal_handler(signal.SIGINT, task.cancel)
        partial = []
        try:
            with _suppress_output_history(self.shell): text = await astream_to_stdout(stream, code_theme=self.code_theme, partial=partial)
        except asyncio.CancelledError:
            text = "".join(partial) + "\n<system>user interrupted</system>"
            print("\nstopped")
        finally: loop.remove_signal_handler(signal.SIGINT)
        self.shell.user_ns[LAST_RESPONSE] = text
        ng = getattr(self.shell, '_ipythonng_extension', None)
        if ng: ng._pty_output = _thinking_to_blockquote(text)
        self.log_exact_exchange(full_prompt, text)
        self.save_prompt(prompt, text, history_line)
        return None




def create_extension(shell=None, resume=None, load=None, prompt_mode=False, **kwargs):
    shell = shell or get_ipython()
    if shell is None: raise RuntimeError("No active IPython shell found")
    _ensure_codex_tables(shell.history_manager.db)
    if resume is not None:
        if resume == -1:
            rows = _list_sessions(shell.history_manager.db, os.getcwd())
            if rows and (chosen := _pick_session(rows)): resume_session(shell, chosen)
            else: print("No sessions found for this directory.")
        else: resume_session(shell, resume)
    ext = getattr(shell, EXTENSION_ATTR, None)
    if ext is None: ext = IPyAIExtension(shell=shell, prompt_mode=prompt_mode, **kwargs)
    if not ext.loaded: ext.load()
    if load is not None:
        try:
            path, ncode, nprompt = ext.load_notebook(load)
            print(f"Loaded {ncode} code cells and {nprompt} prompts from {path}.")
        except FileNotFoundError as e: print(str(e))
    hm = shell.history_manager
    with hm.db: hm.db.execute("UPDATE sessions SET remark=? WHERE session=?", (os.getcwd(), hm.session_number))
    if not getattr(shell, '_ipycodex_atexit', False):
        sid = hm.session_number
        atexit.register(lambda: print(f"\nTo resume: ipycodex -r {sid}"))
        shell._ipycodex_atexit = True
    return ext


_ng_parser = argparse.ArgumentParser(add_help=False)
_ng_parser.add_argument('-r', type=int, nargs='?', const=-1, default=None)
_ng_parser.add_argument('-l', type=str, default=None)
_ng_parser.add_argument('-p', action='store_true', default=False)

def _parse_ng_flags():
    "Parse IPYTHONNG_FLAGS env var via argparse."
    raw = os.environ.pop("IPYTHONNG_FLAGS", "")
    if not raw: return _ng_parser.parse_args([])
    return _ng_parser.parse_args(raw.split())

def load_ipython_extension(ipython):
    flags = _parse_ng_flags()
    return create_extension(ipython, resume=flags.r, load=flags.l, prompt_mode=flags.p)


def unload_ipython_extension(ipython):
    ext = getattr(ipython, EXTENSION_ATTR, None)
    if ext is None: return
    ext.unload()
