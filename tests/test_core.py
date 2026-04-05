import asyncio,io,json,os,shutil,sqlite3,sys,tempfile
from types import SimpleNamespace

import pytest
from IPython.core.inputtransformer2 import TransformerManager

TEST_HOME = tempfile.mkdtemp(prefix="ipycodex-tests-")
os.environ["XDG_CONFIG_HOME"] = TEST_HOME

import ipycodex.core as core
from ipycodex.core import (DEFAULT_CODE_THEME, DEFAULT_LOG_EXACT, DEFAULT_SEARCH, DEFAULT_SYSTEM_PROMPT, DEFAULT_THINK, EXTENSION_NS,
    IPyAIExtension, LAST_PROMPT, LAST_RESPONSE, RESET_LINE_NS, _extract_code_blocks, _format_var_xml, _git_repo_root, _list_sessions,
    _shell_names, _shell_refs, _thinking_to_blockquote, _run_shell_refs, _var_names, _var_refs, astream_to_stdout, prompt_from_lines,
    resume_session, transform_dots, transform_prompt_mode)

class DummyAsyncFormatter:
    async def format_stream(self, stream):
        async for o in stream: yield o

class DisplayStateFormatter:
    def __init__(self):
        self.display_text = ""
        self.final_text = ""

    async def format_stream(self, stream):
        async for o in stream:
            self.display_text = o["display"]
            self.final_text = o["final"]
            yield o.get("chunk", "")

class TTYStringIO(io.StringIO):
    def isatty(self): return True

class DummyMarkdown:
    def __init__(self, text, **kwargs): self.text,self.kwargs = text,kwargs

class DummyConsole:
    instances = []
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.printed = []
        type(self).instances.append(self)

    def print(self, obj):
        self.printed.append(obj)
        self.kwargs["file"].write(f"RICH:{obj.text}")

class DummyLive:
    instances = []
    def __init__(self, renderable, **kwargs):
        self.kwargs = kwargs
        self.renderables = [renderable]
        type(self).instances.append(self)

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): self.kwargs["console"].print(self.renderables[-1])
    def update(self, renderable, refresh=False): self.renderables.append(renderable)

class DummyCodexClient:
    def __init__(self):
        self.threads = {}
        self.turns = []
        self._next_thread_id = 0

    async def start_thread(self, *, model=None, sp="", tools=None, search=None, ephemeral=False):
        self._next_thread_id += 1
        tid = f"thread_{self._next_thread_id}"
        self.threads[tid] = dict(model=model, sp=sp, tools=tools, search=search, ephemeral=ephemeral)
        return tid

    async def resume_thread(self, thread_id, *, sp=""): return thread_id

    async def turn_stream(self, thread_id, prompt, *, ns=None, think=None, output_schema=None):
        self.turns.append(dict(thread_id=thread_id, prompt=prompt, ns=ns, think=think))
        yield "first "
        yield "second"

class DummyHistory:
    def __init__(self, session_number=1):
        self.session_number = session_number
        self.db = sqlite3.connect(":memory:")
        self.entries = {}
        with self.db:
            self.db.execute("""CREATE TABLE sessions (
                session INTEGER PRIMARY KEY,
                start TIMESTAMP,
                end TIMESTAMP,
                num_cmds INTEGER DEFAULT 0,
                remark TEXT)""")
            self.db.execute("""CREATE TABLE history (
                session INTEGER,
                line INTEGER,
                source TEXT)""")
            self.db.execute("INSERT INTO sessions (session, start, end, num_cmds, remark) VALUES (?, CURRENT_TIMESTAMP, NULL, 0, ?)",
                (session_number, None))

    def add(self, line, source, output=None): self.entries[line] = (source, output)
    def get_range(self, session=0, start=1, stop=None, raw=True, output=False):
        if stop is None: stop = max(self.entries, default=0) + 1
        for i in range(start, stop):
            if i not in self.entries: continue
            src,out = self.entries[i]
            yield (0, i, (src, out) if output else src)

class DummyDisplayPublisher:
    def __init__(self): self._is_publishing = False

class DummyInputTransformerManager:
    def __init__(self): self.cleanup_transforms = []

class DummyShell:
    def __init__(self):
        self.input_transformer_manager = DummyInputTransformerManager()
        self.user_ns = {}
        self.magics = []
        self.history_manager = DummyHistory()
        self.display_pub = DummyDisplayPublisher()
        self.execution_count = 2
        self.ran_cells = []
        self.loop_runner = asyncio.run
        self.prompts = None

    def register_magics(self, magics): self.magics.append(magics)
    def set_custom_exc(self, *args): pass

    def run_cell(self, source, store_history=False):
        self.ran_cells.append((source, store_history))
        if store_history:
            self.history_manager.add(self.execution_count, source)
            self.execution_count += 1
        try: exec(compile(source, f'<cell-{self.execution_count}>', 'exec'), self.user_ns)
        except Exception: pass
        return SimpleNamespace(success=True)

    async def run_cell_async(self, source, store_history=False, transformed_cell=None):
        return self.run_cell(transformed_cell or source, store_history=store_history)


@pytest.fixture(autouse=True)
def _config_paths(tmp_path):
    if core.CONFIG_DIR.exists(): shutil.rmtree(core.CONFIG_DIR)
    core.CONFIG_DIR.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def dummy_codex(monkeypatch):
    client = DummyCodexClient()
    monkeypatch.setattr(core, "get_codex_client", lambda: client)
    async def _fake_astream_to_stdout(stream, **kwargs): return "".join([o async for o in stream])
    monkeypatch.setattr(core, "astream_to_stdout", _fake_astream_to_stdout)
    return client


def test_prompt_from_lines_drops_continuation_backslashes():
    lines = [".plan this work\\\n", "with two lines\n"]
    assert prompt_from_lines(lines) == "plan this work\nwith two lines\n"


def test_transform_dots_executes_ai_magic_call():
    seen = {}
    class DummyIPython:
        def run_cell_magic(self, magic, line, cell): seen.update(magic=magic, line=line, cell=cell)
    code = "".join(transform_dots([".hello\n", "world\n"]))
    exec(code, {"get_ipython": lambda: DummyIPython()})
    assert seen == dict(magic="ipycodex", line="", cell="hello\nworld\n")


async def _chunks(*items):
    for o in items: yield o


def run_stream(*items, **kwargs): return asyncio.run(astream_to_stdout(_chunks(*items), formatter_cls=DummyAsyncFormatter, **kwargs))


def _strip_ids(nb): return {**nb, "cells": [{k:v for k,v in c.items() if k != "id"} for c in nb.get("cells", [])]}


def mk_ext(load=True, **kwargs):
    shell = DummyShell()
    ext = IPyAIExtension(shell=shell, **kwargs)
    return shell, ext.load() if load else ext


def test_astream_to_stdout_collects_streamed_text():
    out = io.StringIO()
    text = run_stream("a", "b", out=out)
    assert text == "ab"
    assert out.getvalue() == "ab\n"


def test_astream_to_stdout_uses_live_markdown_for_tty_and_returns_full_text():
    tool_text = "\n\n🔧 f() => ok\n"
    DummyConsole.instances = []
    DummyLive.instances = []
    out = TTYStringIO()
    text = run_stream(tool_text, out=out, console_cls=DummyConsole, markdown_cls=DummyMarkdown, live_cls=DummyLive)
    assert text == tool_text
    assert "🔧 f() => ok" in DummyLive.instances[-1].renderables[-1].text


def test_astream_to_stdout_uses_rich_markdown_options_for_live_updates():
    DummyConsole.instances = []
    DummyLive.instances = []
    out = TTYStringIO()
    text = run_stream("`x`", out=out, code_theme="github-dark", console_cls=DummyConsole, markdown_cls=DummyMarkdown, live_cls=DummyLive)

    assert text == "`x`"
    md = DummyLive.instances[-1].renderables[-1]
    assert md.text == "`x`"
    assert md.kwargs == dict(code_theme="github-dark", inline_code_theme="github-dark", inline_code_lexer="python")


def test_astream_to_stdout_updates_live_markdown_as_chunks_arrive():
    DummyConsole.instances = []
    DummyLive.instances = []
    out = TTYStringIO()
    text = run_stream("a", "b", out=out, console_cls=DummyConsole, markdown_cls=DummyMarkdown, live_cls=DummyLive)

    assert text == "ab"
    assert [o.text for o in DummyLive.instances[-1].renderables] == ["a", "ab"]
    assert out.getvalue() == "RICH:ab"


def test_astream_to_stdout_tty_uses_formatter_display_and_final_text():
    DummyConsole.instances = []
    DummyLive.instances = []
    out = TTYStringIO()
    stream = _chunks(dict(display="⌛ running", final=""), dict(display="final answer", final="stored response"))
    text = asyncio.run(astream_to_stdout(stream,
        formatter_cls=DisplayStateFormatter, out=out, console_cls=DummyConsole, markdown_cls=DummyMarkdown, live_cls=DummyLive))

    assert text == "stored response"
    assert [o.text for o in DummyLive.instances[-1].renderables] == ["⌛ running", "final answer"]
    assert out.getvalue() == "RICH:final answer"


async def test_extension_load_is_idempotent_and_tracks_last_response(dummy_codex):
    shell,ext = mk_ext()
    ext.load()
    assert shell.input_transformer_manager.cleanup_transforms == [transform_dots]
    assert len(shell.magics) == 1
    assert shell.user_ns[EXTENSION_NS] is ext

    await ext.run_prompt("tell me something")

    assert len(dummy_codex.turns) == 1
    assert dummy_codex.turns[0]["prompt"] == "<user-request>tell me something</user-request>"
    assert dummy_codex.turns[0]["think"] == DEFAULT_THINK
    assert shell.user_ns[LAST_PROMPT] == "tell me something"
    assert shell.user_ns[LAST_RESPONSE] == "first second"
    assert ext.prompt_rows() == [("tell me something", "first second")]
    assert ext.prompt_records()[0][3] == 1


async def test_run_prompt_suppresses_ipython_output_history_while_streaming(dummy_codex, monkeypatch):
    shell,ext = mk_ext(load=False)
    seen = []

    async def _fake_astream_to_stdout(stream, **kwargs):
        seen.append(shell.display_pub._is_publishing)
        return "".join([o async for o in stream])
    monkeypatch.setattr(core, "astream_to_stdout", _fake_astream_to_stdout)

    await ext.run_prompt("tell me something")

    assert seen == [True]
    assert shell.display_pub._is_publishing is False


async def test_run_prompt_stores_cleaned_response_for_output_history(dummy_codex, monkeypatch):
    shell,ext = mk_ext(load=False)
    ng = SimpleNamespace(_pty_output=None)
    shell._ipythonng_extension = ng

    async def _fake_astream_to_stdout(stream, **kwargs): return "<thinking>\nhmm\n</thinking>\n\nHello world"
    monkeypatch.setattr(core, "astream_to_stdout", _fake_astream_to_stdout)

    await ext.run_prompt("test")

    assert ng._pty_output == "> hmm\n\nHello world"


def test_unexpected_prompt_table_schema_is_recreated():
    shell = DummyShell()
    with shell.history_manager.db:
        shell.history_manager.db.execute("CREATE TABLE codex_prompts (id INTEGER PRIMARY KEY AUTOINCREMENT, session INTEGER NOT NULL, "
            "prompt TEXT NOT NULL, response TEXT NOT NULL, history_line INTEGER NOT NULL DEFAULT 0, "
            "prompt_line INTEGER NOT NULL DEFAULT 0)")
        shell.history_manager.db.execute("INSERT INTO codex_prompts (session, prompt, response, history_line, prompt_line) VALUES "
            "(1, 'p', 'r', 1, 2)")

    ext = IPyAIExtension(shell=shell)

    assert ext.prompt_records() == []
    cols = [o[1] for o in shell.history_manager.db.execute("PRAGMA table_info(codex_prompts)")]
    assert cols == ["id", "session", "prompt", "response", "history_line"]


def test_codex_tables_are_created():
    shell = DummyShell()
    ext = IPyAIExtension(shell=shell).load()
    db = shell.history_manager.db
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "codex_prompts" in tables
    assert "codex_tools" in tables
    assert "sessions" in tables


def test_config_file_is_created_and_loaded():
    _,ext = mk_ext(load=False)

    assert core.CONFIG_PATH.exists()
    assert core.SYSP_PATH.exists()
    data = json.loads(core.CONFIG_PATH.read_text())
    assert data["model"] == ext.model
    assert data["think"] == DEFAULT_THINK
    assert data["search"] == DEFAULT_SEARCH
    assert data["code_theme"] == DEFAULT_CODE_THEME
    assert data["log_exact"] == DEFAULT_LOG_EXACT
    assert core.SYSP_PATH.read_text() == DEFAULT_SYSTEM_PROMPT
    assert ext.system_prompt == DEFAULT_SYSTEM_PROMPT


def test_existing_sysp_file_is_loaded():
    sysp_path = core.SYSP_PATH
    sysp_path.write_text("custom sysp")
    _,ext = mk_ext(load=False)

    assert ext.system_prompt == "custom sysp"


async def test_config_values_drive_model_think_and_search(dummy_codex):
    core.CONFIG_PATH.write_text(json.dumps(dict(model="cfg-model", think="m", search="h", log_exact=True)))
    shell,ext = mk_ext()

    await ext.run_prompt("tell me something")

    assert ext.model == "cfg-model"
    assert ext.think == "m"
    assert ext.search == "h"
    assert ext.log_exact is True
    assert dummy_codex.turns[0]["think"] == "m"


def test_handle_line_can_report_and_set_model(capsys):
    _,ext = mk_ext(load=False, model="old-model", think="m", search="h", code_theme="github-dark", log_exact=True)

    ext.handle_line("")
    assert capsys.readouterr().out == (
        f"self.model='old-model'\nself.completion_model='{core.DEFAULT_COMPLETION_MODEL}'\n"
        f"self.think='m'\nself.search='h'\nself.code_theme='github-dark'\nself.log_exact=True\n"
        f"CONFIG_PATH={core.CONFIG_PATH!r}\nSYSP_PATH={core.SYSP_PATH!r}\n"
        f"LOG_PATH={core.LOG_PATH!r}\n")

    ext.handle_line("model new-model")
    assert ext.model == "new-model"
    assert capsys.readouterr().out == "self.model='new-model'\n"

    ext.handle_line("think l")
    assert ext.think == "l"
    assert capsys.readouterr().out == "self.think='l'\n"

    ext.handle_line("search m")
    assert ext.search == "m"
    assert capsys.readouterr().out == "self.search='m'\n"

    ext.handle_line("code_theme ansi_dark")
    assert ext.code_theme == "ansi_dark"
    assert capsys.readouterr().out == "self.code_theme='ansi_dark'\n"

    ext.handle_line("log_exact false")
    assert ext.log_exact is False
    assert capsys.readouterr().out == "self.log_exact=False\n"


async def test_second_prompt_stores_both_in_codex_prompts(dummy_codex):
    shell,ext = mk_ext()

    await ext.run_prompt("first prompt")
    shell.execution_count = 3
    await ext.run_prompt("second prompt")

    assert ext.prompt_rows() == [
        ("first prompt", "first second"),
        ("second prompt", "first second")]
    assert len(dummy_codex.turns) == 2


async def test_persistent_thread_reused_across_prompts(dummy_codex):
    shell,ext = mk_ext()

    await ext.run_prompt("first prompt")
    await ext.run_prompt("second prompt")

    assert len(dummy_codex.threads) == 1
    assert dummy_codex.turns[0]["thread_id"] == dummy_codex.turns[1]["thread_id"]


def test_reset_only_deletes_current_session_history(capsys):
    shell,ext = mk_ext()

    ext.save_prompt("s1 prompt", "s1 response", 1)
    shell.history_manager.session_number = 2
    shell.execution_count = 8
    ext.save_prompt("s2 prompt", "s2 response", 7)

    ext.handle_line("reset")

    assert capsys.readouterr().out == "Deleted 1 AI prompts from session 2.\n"
    assert ext.prompt_rows(session=1) == [("s1 prompt", "s1 response")]
    assert ext.prompt_rows(session=2) == []
    assert shell.user_ns[RESET_LINE_NS] == 7


def test_register_tool(capsys):
    shell,ext = mk_ext()
    def demo():
        "Demo tool."
        return "ok"
    shell.user_ns["demo"] = demo

    ext.handle_line("tool demo")
    assert "registered" in capsys.readouterr().out
    assert ext._tools_dirty is True
    assert "demo" in ext._get_tool_names()


def test_register_tool_missing_raises(capsys):
    _,ext = mk_ext()
    ext.handle_line("tool nonexistent")
    assert "not defined" in capsys.readouterr().out


def test_register_tool_noncallable_raises(capsys):
    shell,ext = mk_ext()
    shell.user_ns["x"] = 42
    ext.handle_line("tool x")
    assert "not callable" in capsys.readouterr().out


def test_get_tools_includes_auto_and_registered():
    pytest.importorskip("safepyrun")
    from safepyrun import RunPython
    shell = DummyShell()
    shell.user_ns["pyrun"] = RunPython(g=shell.user_ns)
    ext = IPyAIExtension(shell=shell).load()
    names = ext._get_tool_names()
    assert "pyrun" in names
    assert "bash" in names


def test_handle_line_tools_lists_tools(capsys):
    shell,ext = mk_ext()
    ext.handle_line("tools")
    out = capsys.readouterr().out
    assert "bash" in out or "No tools" in out


async def test_tool_migration_creates_new_thread(dummy_codex):
    shell,ext = mk_ext()
    def demo():
        "Demo tool."
        return "ok"
    shell.user_ns["demo"] = demo

    await ext.run_prompt("first prompt")
    assert len(dummy_codex.threads) == 1
    first_thread = dummy_codex.turns[0]["thread_id"]

    ext._register_tool("demo")
    await ext.run_prompt("second prompt after tool added")

    assert len(dummy_codex.threads) == 2
    assert dummy_codex.turns[-1]["thread_id"] != first_thread


def test_context_xml_includes_code_and_outputs_since_last_prompt():
    shell = DummyShell()
    shell.history_manager.add(1, "a = 1")
    shell.history_manager.add(2, "a", "1")
    ext = IPyAIExtension(shell=shell).load()

    ctx = ext.code_context(1, 3)
    assert "<context><code>a = 1</code><code>a</code><output>1</output></context>\n" == ctx


def test_code_context_uses_note_tag_for_string_literals():
    shell = DummyShell()
    shell.history_manager.add(1, '"This is a note"')
    shell.history_manager.add(2, 'x = 1')
    shell.history_manager.add(3, '"""multi\nline"""')
    ext = IPyAIExtension(shell=shell).load()

    ctx = ext.code_context(1, 4)
    assert ctx == '<context><note>This is a note</note><code>x = 1</code><note>multi\nline</note></context>\n'


def test_save_notebook_converts_notes_to_markdown_cells(tmp_path):
    shell = DummyShell()
    shell.history_manager.add(1, '"# My note"')
    shell.history_manager.add(2, 'x = 1')
    shell.execution_count = 3
    ext = IPyAIExtension(shell=shell).load()

    path, _, _ = ext.save_notebook(tmp_path / "test")
    assert path.suffix == ".ipynb"
    nb = json.loads(path.read_text())
    c0 = {k:v for k,v in nb["cells"][0].items() if k != "id"}
    assert c0 == dict(cell_type="markdown", source="# My note",
        metadata=dict(ipycodex=dict(kind="code", line=1, source='"# My note"')))
    assert nb["cells"][1]["cell_type"] == "code"
    assert nb["cells"][1]["source"] == "x = 1"


def test_notebook_roundtrip_preserves_notes(tmp_path):
    shell = DummyShell()
    shell.history_manager.add(1, '"a note"')
    shell.history_manager.add(2, 'x = 1')
    shell.execution_count = 3
    ext = IPyAIExtension(shell=shell).load()
    ext.save_notebook(tmp_path / "test")

    shell2 = DummyShell()
    shell2.execution_count = 1
    ext2 = IPyAIExtension(shell=shell2).load()
    ext2.load_notebook(tmp_path / "test")
    assert shell2.ran_cells == [('"a note"', True), ('x = 1', True)]


def test_history_context_uses_lines_since_last_prompt_only():
    shell = DummyShell()
    shell.history_manager.add(1, "before = 1")
    shell.history_manager.add(2, ".first prompt")
    shell.history_manager.add(3, "after = 2")
    shell.execution_count = 3
    ext = IPyAIExtension(shell=shell).load()
    ext.save_prompt("first prompt", "first response", 2)

    prompt = ext.format_prompt("second prompt", ext.last_prompt_line()+1, 4)
    assert "before = 1" not in prompt
    assert "after = 2" in prompt


def test_load_notebook_replays_code_and_restores_prompts(tmp_path):
    cells = [dict(cell_type="code", source="import math", metadata=dict(ipycodex=dict(kind="code", line=1)), outputs=[], execution_count=None),
        dict(cell_type="markdown", source="hello", metadata=dict(ipycodex=dict(kind="prompt", line=3, history_line=2, prompt="hi"))),
        dict(cell_type="code", source="x = 1", metadata=dict(ipycodex=dict(kind="code", line=3)), outputs=[], execution_count=None)]
    nb_path = tmp_path / "test.ipynb"
    nb_path.write_text(json.dumps(dict(cells=cells, metadata=dict(ipycodex_version=1), nbformat=4, nbformat_minor=5)))
    shell = DummyShell()
    shell.execution_count = 1
    ext = IPyAIExtension(shell=shell).load()
    ext.load_notebook(nb_path)

    assert shell.ran_cells == [("import math", True), ("x = 1", True)]
    assert ext.prompt_rows() == [("hi", "hello")]
    assert ext.prompt_records()[0][3] == 2
    hist = ext.dialog_history()
    assert hist[0] == "<context><code>import math</code></context>\n<user-request>hi</user-request>"
    assert shell.execution_count == 4


def test_save_writes_notebook(tmp_path, capsys):
    shell = DummyShell()
    shell.history_manager.add(1, "import math")
    shell.history_manager.add(2, ".first prompt")
    shell.history_manager.add(3, "x = 1")
    shell.execution_count = 4
    ext = IPyAIExtension(shell=shell).load()
    ext.save_prompt("first prompt", "first response", 1)

    ext.handle_line(f"save {tmp_path / 'mysession'}")

    nb_path = tmp_path / "mysession.ipynb"
    assert f"Saved 2 code cells and 1 prompts to {nb_path}.\n" in capsys.readouterr().out
    nb = json.loads(nb_path.read_text())
    assert all("id" in c for c in nb["cells"])
    assert _strip_ids(nb) == dict(
        cells=[
            dict(cell_type="code", source="import math", metadata=dict(ipycodex=dict(kind="code", line=1)), outputs=[], execution_count=None),
            dict(cell_type="markdown", source="first response",
                metadata=dict(ipycodex=dict(kind="prompt", line=2, history_line=1, prompt="first prompt"))),
            dict(cell_type="code", source="x = 1", metadata=dict(ipycodex=dict(kind="code", line=3)), outputs=[], execution_count=None),
        ],
        metadata=dict(ipycodex_version=1), nbformat=4, nbformat_minor=5)


async def test_log_exact_writes_full_prompt_and_response(dummy_codex):
    log_path = core.LOG_PATH
    shell = DummyShell()
    shell.history_manager.add(1, "a = 1")
    shell.execution_count = 3
    ext = IPyAIExtension(shell=shell, log_exact=True).load()

    await ext.run_prompt("tell me something")

    rec = json.loads(log_path.read_text().strip())
    assert rec["session"] == 1
    assert rec["prompt"] == "<context><code>a = 1</code></context>\n<user-request>tell me something</user-request>"
    assert rec["response"] == "first second"


def test_cleanup_transform_prevents_help_syntax_interference():
    tm = TransformerManager()
    tm.cleanup_transforms.insert(1, transform_dots)

    code = tm.transform_cell(".I am testing my new AI prompt system.\\\nTell me do you see a newline in this prompt?")
    assert code == "get_ipython().run_cell_magic('ipycodex', '', 'I am testing my new AI prompt system.\\nTell me do you see a newline in this prompt?\\n')\n"
    assert tm.check_complete(".I am testing my new AI prompt system.\\") == ("incomplete", 0)
    assert tm.check_complete(".I am testing my new AI prompt system.\\\nTell me do you see a newline in this prompt?") == ("complete", None)


def test_frontmatter():
    from fastcore.xtras import frontmatter
    fm, body = frontmatter("---\nname: x\n---\nbody")
    assert fm == {"name": "x"}
    assert body == "body"

def test_frontmatter_none():
    from fastcore.xtras import frontmatter
    fm, body = frontmatter("no frontmatter")
    assert fm == {}
    assert body == "no frontmatter"


def test_bash_injected_on_load():
    pytest.importorskip("safecmd")
    shell = DummyShell()
    ext = IPyAIExtension(shell=shell).load()
    assert callable(shell.user_ns.get("bash"))


## Prompt variable tests ($`var`)

def test_var_names_extracts_dollar_backtick(): assert _var_names("use $`x` and $`y`") == {"x", "y"}

def test_var_names_empty_on_no_match():
    assert _var_names("no vars here") == set()
    assert _var_names("") == set()

def test_var_refs_from_prompt_and_history():
    refs = _var_refs("use $`a`", [dict(prompt="use $`b`")])
    assert refs == {"a", "b"}

def test_var_refs_from_notes():
    refs = _var_refs("", [], notes=["---\nexposed-vars: x y\n---\nuse $`z`"])
    assert refs == {"x", "y", "z"}

def test_format_var_xml():
    ns = dict(x=42, name="hello")
    xml = _format_var_xml({"x", "name"}, ns)
    assert '<variable name="x" type="int">42</variable>' in xml
    assert '<variable name="name" type="str">hello</variable>' in xml

def test_format_var_xml_missing_returns_empty(): assert _format_var_xml({"missing"}, {}) == ""

async def test_var_in_prompt_adds_variable_xml(dummy_codex):
    shell,ext = mk_ext()
    shell.user_ns["myval"] = 99
    await ext.run_prompt("check $`myval`")
    prompt = dummy_codex.turns[0]["prompt"]
    assert '<variable name="myval" type="int">99</variable>' in prompt

async def test_missing_var_in_prompt_adds_warning(dummy_codex):
    shell,ext = mk_ext()
    await ext.run_prompt("check $`missing_var`")
    prompt = dummy_codex.turns[0]["prompt"]
    assert '<warnings>' in prompt
    assert 'missing_var' in prompt

async def test_var_from_history_included(dummy_codex):
    shell,ext = mk_ext()
    shell.user_ns["x"] = 10
    await ext.run_prompt("first prompt with $`x`")
    await ext.run_prompt("second prompt")
    prompt = dummy_codex.turns[-1]["prompt"]
    assert '<variable name="x" type="int">10</variable>' in prompt

## Shell ref tests (!`cmd`)

def test_shell_names_extracts_bang_backtick(): assert _shell_names("check !`uname -a` and !`ls`") == {"uname -a", "ls"}

def test_shell_names_empty_on_no_match(): assert _shell_names("no shell here") == set()

def test_shell_refs_from_prompt_and_history():
    refs = _shell_refs("run !`echo hi`", [dict(prompt="run !`date`")])
    assert refs == {"echo hi", "date"}

def test_shell_refs_from_notes():
    refs = _shell_refs("", [], notes=["---\nshell-cmds: git status\n---\nrun !`ls`"])
    assert refs == {"git status", "ls"}

def test_run_shell_refs_runs_commands():
    xml = _run_shell_refs({"echo hello"})
    assert '<shell cmd="echo hello">' in xml
    assert 'hello' in xml

def test_run_shell_refs_empty_for_no_cmds(): assert _run_shell_refs(set()) == ""

async def test_shell_in_prompt_adds_shell_xml(dummy_codex):
    shell,ext = mk_ext()
    await ext.run_prompt("check !`echo test123`")
    prompt = dummy_codex.turns[0]["prompt"]
    assert '<shell cmd="echo test123">' in prompt
    assert 'test123' in prompt

async def test_shell_from_history_included(dummy_codex):
    shell,ext = mk_ext()
    await ext.run_prompt("first with !`echo aaa`")
    await ext.run_prompt("second prompt")
    prompt = dummy_codex.turns[-1]["prompt"]
    assert '<shell cmd="echo aaa">' in prompt

def test_sysprompt_mentions_variables_and_shell():
    assert '$`' in DEFAULT_SYSTEM_PROMPT
    assert '!`' in DEFAULT_SYSTEM_PROMPT


## Prompt mode tests

def test_prompt_mode_wraps_input_as_magic():
    lines = transform_prompt_mode(["hello world\n"])
    assert "run_cell_magic" in lines[0]
    assert "ipycodex" in lines[0]
    assert "hello world" in lines[0]

def test_prompt_mode_passes_through_semicolon_as_python():
    lines = transform_prompt_mode([";x = 42\n"])
    assert lines == ["x = 42\n"]

def test_prompt_mode_passes_through_bang_as_shell():
    lines = transform_prompt_mode(["!ls\n"])
    assert lines == ["!ls\n"]

def test_prompt_mode_passes_through_percent_as_magic():
    lines = transform_prompt_mode(["%timeit 1+1\n"])
    assert lines == ["%timeit 1+1\n"]

def test_prompt_mode_passes_through_double_percent():
    lines = transform_prompt_mode(["%%bash\n", "echo hi\n"])
    assert lines == ["%%bash\n", "echo hi\n"]

def test_prompt_mode_multiline():
    lines = transform_prompt_mode(["tell me\\\n", "about python\n"])
    assert "run_cell_magic" in lines[0]
    assert "tell me" in lines[0] and "about python" in lines[0]

def test_prompt_mode_empty_passthrough():
    assert transform_prompt_mode(["\n"]) == ["\n"]
    assert transform_prompt_mode([]) == []

def test_prompt_mode_toggle():
    shell,ext = mk_ext()
    assert not ext.prompt_mode
    ext.handle_line("prompt")
    assert ext.prompt_mode
    ext.handle_line("prompt")
    assert not ext.prompt_mode

def test_prompt_mode_flag():
    shell = DummyShell()
    ext = IPyAIExtension(shell=shell, prompt_mode=True).load()
    assert ext.prompt_mode

def test_prompt_mode_config_default():
    core.CONFIG_PATH.write_text('{"prompt_mode": true}')
    shell = DummyShell()
    ext = IPyAIExtension(shell=shell).load()
    assert ext.prompt_mode

def test_prompt_mode_config_with_flag_toggles():
    core.CONFIG_PATH.write_text('{"prompt_mode": true}')
    shell = DummyShell()
    ext = IPyAIExtension(shell=shell, prompt_mode=True).load()
    assert not ext.prompt_mode

def test_prompt_mode_registered_transformer():
    shell,ext = mk_ext()
    ext.handle_line("prompt")
    cts = shell.input_transformer_manager.cleanup_transforms
    assert transform_prompt_mode in cts
    ext.handle_line("prompt")
    assert transform_prompt_mode not in cts


def test_thinking_to_blockquote_converts(): assert _thinking_to_blockquote("<thinking>\nhmm\n</thinking>\n\nHello world") == "> hmm\n\nHello world"
def test_thinking_to_blockquote_no_thinking(): assert _thinking_to_blockquote("Hello world") == "Hello world"
def test_thinking_to_blockquote_multiline(): assert _thinking_to_blockquote("<thinking>\nline1\nline2\n</thinking>\n\nHi") == "> line1\n> line2\n\nHi"


def test_extract_code_blocks_python_only():
    text = "Here's some code:\n```python\nx = 1\ny = 2\n```\nAnd more:\n```\nz = 3\n```\nBash:\n```bash\necho hi\n```\nPy:\n```py\na = 4\n```"
    assert _extract_code_blocks(text) == ["x = 1\ny = 2", "a = 4"]


def test_extract_code_blocks_empty_response():
    assert _extract_code_blocks("") == []
    assert _extract_code_blocks("no code here") == []


# --- Session persistence tests ---

def _mk_sessions_db():
    "Create an in-memory DB with the IPython sessions, history, and codex_prompts tables."
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE sessions (session INTEGER PRIMARY KEY AUTOINCREMENT, start TEXT, end TEXT, num_cmds INTEGER, remark TEXT)")
    db.execute("CREATE TABLE history (session INTEGER, line INTEGER, source TEXT, source_raw TEXT)")
    db.execute("CREATE TABLE codex_prompts (id INTEGER PRIMARY KEY AUTOINCREMENT, session INTEGER, prompt TEXT, response TEXT, history_line INTEGER)")
    return db

def test_git_repo_root(tmp_path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert _git_repo_root(str(sub)) == str(tmp_path)

def test_git_repo_root_none(tmp_path):
    assert _git_repo_root(str(tmp_path)) is None or _git_repo_root(str(tmp_path)) != str(tmp_path)

def test_list_sessions_exact_match():
    db = _mk_sessions_db()
    db.execute("INSERT INTO sessions VALUES (1, '2025-01-01', '2025-01-01', 5, ?)", (json.dumps(dict(cwd="/home/user/project", provider="codex")),))
    db.execute("INSERT INTO sessions VALUES (2, '2025-01-02', '2025-01-02', 3, '/home/user/other')")
    rows = _list_sessions(db, "/home/user/project")
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][5] is None

def test_list_sessions_with_prompts():
    db = _mk_sessions_db()
    db.execute("INSERT INTO sessions VALUES (1, '2025-01-01', NULL, 5, ?)", (json.dumps(dict(cwd="/proj")),))
    db.execute("INSERT INTO codex_prompts (session, prompt, response, history_line) VALUES (1, 'first', 'r1', 0)")
    db.execute("INSERT INTO codex_prompts (session, prompt, response, history_line) VALUES (1, 'second', 'r2', 1)")
    rows = _list_sessions(db, "/proj")
    assert rows[0][5] == "second"

def test_list_sessions_git_fallback(tmp_path):
    (tmp_path / ".git").mkdir()
    db = _mk_sessions_db()
    sub = str(tmp_path / "sub")
    db.execute("INSERT INTO sessions VALUES (1, '2025-01-01', NULL, 5, ?)", (json.dumps(dict(cwd=str(tmp_path), provider="codex")),))
    db.execute("INSERT INTO sessions VALUES (2, '2025-01-02', NULL, 3, ?)", (json.dumps(dict(cwd=sub, provider="codex")),))
    rows = _list_sessions(db, sub)
    assert len(rows) == 1 and rows[0][0] == 2
    rows = _list_sessions(db, str(tmp_path / "newsub"))
    assert len(rows) == 1 and rows[0][0] == 1

def test_resume_session():
    db = _mk_sessions_db()
    db.execute("INSERT INTO sessions VALUES (5, '2025-01-01', '2025-01-01 12:00', 10, ?)", (json.dumps(dict(cwd="/proj", provider="codex")),))
    db.execute("INSERT INTO history VALUES (5, 1, 'x=1', 'x=1')")
    db.execute("INSERT INTO history VALUES (5, 2, 'y=2', 'y=2')")
    db.execute("INSERT INTO sessions VALUES (6, '2025-01-02', NULL, NULL, '')")
    shell = DummyShell()
    shell.history_manager.db = db
    shell.history_manager.session_number = 6
    shell.history_manager.input_hist_parsed = [""]
    shell.history_manager.input_hist_raw = [""]
    resume_session(shell, 5)
    assert shell.history_manager.session_number == 5
    assert shell.execution_count == 3
    assert db.execute("SELECT * FROM sessions WHERE session=6").fetchone() is None
    row = db.execute("SELECT end FROM sessions WHERE session=5").fetchone()
    assert row[0] is None
    assert len(shell.history_manager.input_hist_parsed) == 3


def test_reset_clears_provider_metadata_from_remark():
    shell = DummyShell()
    ext = IPyAIExtension(shell=shell).load()
    with shell.history_manager.db:
        shell.history_manager.db.execute("UPDATE sessions SET remark=? WHERE session=1",
            (json.dumps(dict(cwd=os.getcwd(), provider="codex", provider_session_id="thread_1")),))
    ext.reset_session_history()
    row = shell.history_manager.db.execute("SELECT remark FROM sessions WHERE session=1").fetchone()
    assert json.loads(row[0]) == {"cwd": os.getcwd()}


def test_resume_session_not_found():
    db = _mk_sessions_db()
    db.execute("INSERT INTO sessions VALUES (1, '2025-01-01', NULL, NULL, '')")
    shell = DummyShell()
    shell.history_manager.db = db
    shell.history_manager.session_number = 1
    with pytest.raises(ValueError, match="Session 99 not found"): resume_session(shell, 99)

def test_store_cwd_in_remark():
    shell,ext = mk_ext()
    hm = shell.history_manager
    with hm.db: hm.db.execute("UPDATE sessions SET remark=? WHERE session=?", (json.dumps(dict(cwd=os.getcwd())), hm.session_number))
    row = hm.db.execute("SELECT remark FROM sessions WHERE session=?", (hm.session_number,)).fetchone()
    assert json.loads(row[0]) == {"cwd": os.getcwd()}

def test_handle_line_sessions():
    shell,ext = mk_ext()
    hm = shell.history_manager
    hm.db.execute("UPDATE sessions SET remark=?, num_cmds=5 WHERE session=1", (json.dumps(dict(cwd=os.getcwd(), provider="codex")),))
    hm.db.execute("INSERT INTO codex_prompts (session, prompt, response, history_line) VALUES (1, 'hello world', 'hi', 0)")
    import io as _io
    buf = _io.StringIO()
    import sys as _sys
    old = _sys.stdout
    _sys.stdout = buf
    try: ext.handle_line("sessions")
    finally: _sys.stdout = old
    out = buf.getvalue()
    assert "1" in out
    assert "hello world" in out


def test_e2e_ipycodex_session(tmp_path):
    "E2E: drive ipycodex interactively via pexpect — prompt, response, session lifecycle."
    import pexpect
    hist_file = str(tmp_path / "hist.sqlite")
    env = {k: v for k, v in os.environ.items() if k != 'IPYTHONNG_FLAGS'}
    env['XDG_CONFIG_HOME'] = str(tmp_path / "config")
    env['IPYTHON_DIR'] = str(tmp_path / "ipython")

    args = ['-m', 'IPython', '--ext', 'ipycodex', f'--HistoryManager.hist_file={hist_file}',
        '--TerminalIPythonApp.display_banner=False', '--colors=NoColor']
    child = pexpect.spawn(sys.executable, args, env=env, timeout=60, encoding='utf-8')

    def wait_prompt(n): child.expect(f'In \\[{n}\\]')

    # Wait for first prompt
    wait_prompt(1)

    # Run some Python code
    child.sendline('x = 42')
    wait_prompt(2)

    # Send an AI prompt via the . prefix
    child.sendline(".respond 'ok' if you receive this")
    child.expect('ok', timeout=60)

    # Wait for next prompt (AI response finished)
    wait_prompt(3)

    # Check %ipycodex sessions lists something
    child.sendline('%ipycodex sessions')
    wait_prompt(4)

    # Exit and check for resume message
    child.sendline('exit()')
    child.expect('To resume')
    child.expect(pexpect.EOF)
