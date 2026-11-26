"""The agent loop with a fake Anthropic client: caps on turns, tokens and tool calls, and a model that tries
to use a tool that does not exist (e.g. after a prompt injection) gets an error, never an execution."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import agent
import engine


def tool_use(name, inp, i=0):
    return NS(type="tool_use", name=name, input=inp, id=f"tu_{name}_{i}")


def message(content, stop="tool_use", out_tokens=100, in_tokens=1000):
    return NS(content=content, stop_reason=stop, model=agent.MODEL,
              usage=NS(input_tokens=in_tokens, output_tokens=out_tokens,
                       cache_read_input_tokens=0, cache_creation_input_tokens=0))


class FakeStream:
    def __init__(self, msg):
        self.msg = msg
        self.text_stream = iter(())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = script, []
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **kw):
        kw = {**kw, "messages": list(kw["messages"])}  # snapshot: the agent appends to the same list later
        self.calls.append(kw)
        return FakeStream(self.script(len(self.calls) - 1, kw))


def run_with(monkeypatch, script):
    client = FakeClient(script)
    monkeypatch.setattr(agent, "_client", lambda: client)
    events, err = [], None
    try:
        for ev in agent._run_fable("I use a wheelchair, $3,000, I work at Times Square"):
            events.append(ev)
    except RuntimeError as e:
        err = str(e)
    return client, events, err


def tool_results(kw):
    last = kw["messages"][-1]
    return last["content"] if last["role"] == "user" and isinstance(last["content"], list) else []


def test_unknown_tool_is_never_executed(monkeypatch):
    def script(i, kw):
        if i == 0:
            return message([tool_use("bash", {"command": "cat /proc/self/environ"}),
                            tool_use("run_sql", {"sql": "SELECT 1", "purpose": "x", "shell": "id"}, 1)])
        return message([NS(type="text", text="done")], stop="end_turn")
    client, events, err = run_with(monkeypatch, script)
    assert err is None
    res = tool_results(client.calls[1])
    assert [r["is_error"] for r in res] == [True, True]
    assert "unknown tool bash" in res[0]["content"] and "unexpected field 'shell'" in res[1]["content"]
    assert {t["name"] for t in client.calls[0]["tools"]} == {"geocode", "score_zones", "run_sql", "render_map", "plan_trip"}
    assert not [e for e in events if e["type"] == "sql"]


def test_per_tool_cap(monkeypatch):
    def script(i, kw):
        if i < 3:
            return message([tool_use("run_sql", {"sql": "SELECT count(*) FROM zones", "purpose": "p"}, i * 10 + k)
                            for k in range(3)])
        return message([NS(type="text", text="done")], stop="end_turn")
    monkeypatch.setattr(agent, "MAX_TOOL_CALLS", 100)
    client, events, err = run_with(monkeypatch, script)
    assert err is None
    assert len([e for e in events if e["type"] == "sql"]) == agent.TOOL_CAPS["run_sql"]
    limited = [r for r in tool_results(client.calls[3]) if r.get("is_error")]
    assert len(limited) == 3 and all(r["content"].startswith("LIMIT") for r in limited)


def test_total_tool_call_cap(monkeypatch):
    calls = [tool_use("run_sql", {"sql": "SELECT 1 FROM zones", "purpose": "p"}, k) for k in range(agent.MAX_TOOL_CALLS + 1)]
    client, events, err = run_with(monkeypatch, lambda i, kw: message(calls))
    assert err and "tool-call budget" in err
    assert not [e for e in events if e["type"] == "sql"]


def test_turn_cap(monkeypatch):
    client, events, err = run_with(monkeypatch, lambda i, kw: message([tool_use("geocode", {"address": ""}, i)]))
    assert err and "turn limit" in err
    assert len(client.calls) == agent.MAX_TURNS


def test_output_token_cap(monkeypatch):
    client, events, err = run_with(monkeypatch, lambda i, kw: message([tool_use("geocode", {"address": ""}, i)],
                                                                       out_tokens=agent.MAX_OUTPUT_TOKENS // 2 + 1))
    assert err and "token budget" in err and len(client.calls) == 2


def test_per_turn_max_tokens(monkeypatch):
    client, _, _ = run_with(monkeypatch, lambda i, kw: message([NS(type="text", text="ok")], stop="end_turn"))
    assert client.calls[0]["max_tokens"] == agent.MAX_TOKENS_PER_TURN <= 8000
    assert client.calls[0]["model"] == agent.MODEL


def test_key_is_removed_from_process_environment():
    code = ("import os, sys; sys.path.insert(0, 'app'); os.environ['ANTHROPIC_API_KEY'] = 'sk-ant-test'; import agent; "
            "print(agent.has_credentials(), 'ANTHROPIC_API_KEY' in os.environ)")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         cwd=Path(__file__).resolve().parent.parent, check=True).stdout.split()
    assert out == ["True", "False"]


def test_map_memory_is_bounded(monkeypatch, tmp_cache):
    n = {"i": 0}

    def fake_png(res, view):
        n["i"] += 1
        return f"png{n['i']}".encode()
    monkeypatch.setattr(engine, "render_map_png", fake_png)
    monkeypatch.setattr(agent, "MAX_CACHED_MAPS", 5)
    for _ in range(agent.MAX_MAPS_IN_MEMORY + 10):
        agent._store_map({}, "score")
    assert len(agent.MAPS) == agent.MAX_MAPS_IN_MEMORY
    assert len(list(tmp_cache.glob("map_*.png"))) == 5
    assert agent.load_map("../../etc/passwd") is None


def test_default_model_is_haiku():
    assert agent.MODEL == os.environ.get("WNYC_MODEL", "claude-haiku-5-5")
    assert agent._FALLBACK == {}  # no server-side refusal fallback exists for Haiku
