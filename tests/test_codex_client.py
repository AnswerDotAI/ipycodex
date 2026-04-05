import asyncio,json

import ipycodex.codex_client as cc


class FakeClient:
    def __init__(self): self.calls = []

    async def start_thread(self, **kwargs):
        self.calls.append(("start_thread", kwargs))
        return "thread_1"

    async def turn_stream(self, thread_id, prompt, **kwargs):
        self.calls.append(("turn_stream", thread_id, prompt, kwargs))
        yield "first "
        yield "second"


async def _aiter(*items):
    for o in items: yield o


async def test_asyncchat_ephemeral(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(cc, "_client", fake)
    chat = cc.AsyncChat(model="gpt-5.4", sp="system")

    res = await chat("prompt", think="m")

    assert str(res) == "first second"
    assert fake.calls[0] == ("start_thread", dict(model="gpt-5.4", sp="system", ephemeral=True))
    assert fake.calls[1][1] == "thread_1"
    assert fake.calls[1][2] == "prompt"
    assert fake.calls[1][3]["think"] == "m"


def test_dynamic_tools_maps_openai_style_schema():
    tools = [dict(type="function", function=dict(name="demo", description="Demo tool", parameters=dict(type="object",
        properties=dict(x=dict(type="integer")))))]

    assert cc._dynamic_tools(tools) == [dict(name="demo", description="Demo tool", inputSchema=dict(type="object",
        properties=dict(x=dict(type="integer"))))]


async def test_call_tool_handles_async_and_json_result():
    async def demo(x): return dict(total=x + 1)

    res = await cc._call_tool(dict(demo=demo), "demo", dict(x=4))

    assert res["success"] is True
    assert json.loads(res["contentItems"][0]["text"]) == dict(total=5)


def test_completed_dynamic_tool_produces_compact_format():
    client = cc._CodexAppServer()
    item = dict(type="dynamicToolCall", id="call_1", tool="greet", arguments=dict(name="world"), success=True,
        contentItems=[dict(type="inputText", text="hello world")])
    text = client._completed_item_text(item, set(), {})
    assert text == "\n\n🔧 greet(name='world') => hello world\n"


def test_completed_command_produces_compact_format():
    client = cc._CodexAppServer()
    item = dict(type="commandExecution", id="cmd_1", command="echo hi", cwd="/tmp", exitCode=0, aggregatedOutput="hi\n")
    text = client._completed_item_text(item, set(), {})
    assert text == "\n\n🔧 echo hi => hi\n"


async def test_consume_turn_emits_command_stream_events():
    client = cc._CodexAppServer()
    client.events = asyncio.Queue()
    thread_id,turn_id = "thread_1","turn_1"
    msgs = [
        dict(method="item/started", params=dict(threadId=thread_id, turnId=turn_id,
            item=dict(type="commandExecution", id="cmd_1", command="printf hi", cwd="/tmp"))),
        dict(method="item/commandExecution/outputDelta", params=dict(threadId=thread_id, turnId=turn_id, itemId="cmd_1", delta="hi\n")),
        dict(method="item/completed", params=dict(threadId=thread_id, turnId=turn_id,
            item=dict(type="commandExecution", id="cmd_1", command="printf hi", cwd="/tmp", status="completed", exitCode=0))),
        dict(method="turn/completed", params=dict(threadId=thread_id, turnId=turn_id, turn=dict(id=turn_id)))]
    for msg in msgs: await client.events.put(msg)

    chunks = [o async for o in client._consume_turn(thread_id, turn_id, {})]

    assert chunks[:2] == [dict(kind="command_start", id="cmd_1", command="printf hi", cwd="/tmp"),
        dict(kind="command_delta", id="cmd_1", delta="hi\n", command="printf hi", cwd="/tmp")]
    assert chunks[2]["kind"] == "command_complete"
    assert "\n\n🔧 printf hi => hi" in chunks[2]["text"]


async def test_consume_turn_emits_thinking_events():
    client = cc._CodexAppServer()
    client.events = asyncio.Queue()
    thread_id,turn_id = "thread_1","turn_1"
    msgs = [dict(method="item/started", params=dict(threadId=thread_id, turnId=turn_id, item=dict(type="reasoning", id="r_1"))),
        dict(method="item/reasoning/textDelta", params=dict(threadId=thread_id, turnId=turn_id, itemId="r_1", contentIndex=0, delta="let me ")),
        dict(method="item/reasoning/textDelta", params=dict(threadId=thread_id, turnId=turn_id, itemId="r_1", contentIndex=0, delta="think")),
        dict(method="item/completed", params=dict(threadId=thread_id, turnId=turn_id, item=dict(type="reasoning", id="r_1"))),
        dict(method="item/started", params=dict(threadId=thread_id, turnId=turn_id, item=dict(type="agentMessage", id="msg_1"))),
        dict(method="item/agentMessage/delta", params=dict(threadId=thread_id, turnId=turn_id, itemId="msg_1", delta="Hello")),
        dict(method="turn/completed", params=dict(threadId=thread_id, turnId=turn_id, turn=dict(id=turn_id)))]
    for msg in msgs: await client.events.put(msg)

    chunks = [o async for o in client._consume_turn(thread_id, turn_id, {})]

    assert chunks[0] == dict(kind="thinking_start")
    assert chunks[1] == dict(kind="thinking_delta", delta="let me ")
    assert chunks[2] == dict(kind="thinking_delta", delta="think")
    assert chunks[3] == dict(kind="thinking_end")
    assert chunks[4] == "Hello"


async def test_async_stream_formatter_thinking_blockquotes_display_and_stores_tags():
    fmt = cc.AsyncStreamFormatter()
    fmt.is_tty = True
    seen = []
    stream = _aiter(dict(kind="thinking_start"), dict(kind="thinking_delta", delta="hmm"), dict(kind="thinking_end"), "Hello")
    async for _ in fmt.format_stream(stream): seen.append(fmt.display_text)

    assert any("> hmm" in o for o in seen)
    assert "<thinking>\nhmm\n</thinking>" in fmt.final_text
    assert "Hello" in fmt.final_text


async def test_async_stream_formatter_streams_live_command_text_only_to_display():
    final = "\n\n🔧 printf hi => hi\n"
    fmt = cc.AsyncStreamFormatter()
    fmt.is_tty = True
    seen = []
    stream = _aiter(dict(kind="command_start", id="cmd_1", command="printf hi", cwd="/tmp"),
        dict(kind="command_delta", id="cmd_1", delta="hi\n", command="printf hi", cwd="/tmp"),
        dict(kind="command_complete", id="cmd_1", text=final))
    async for _ in fmt.format_stream(stream): seen.append(fmt.display_text)

    assert any("⌛ <code>printf hi</code>" in o for o in seen[:2])
    assert any("hi\n" in o for o in seen[:2])
    assert fmt.final_text == final
    assert "⌛" not in fmt.final_text
