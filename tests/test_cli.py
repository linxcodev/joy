"""Tests for the Joy TUI and its Responses agent loop, using mocked HTTP streams."""

import asyncio
import json
import os
import re
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace as NS
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import openai

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import DummyOutput
from rich.console import Console

import joy.cli as cli
import joy.llm as llm
import joy.tools as tools

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
USER_STYLE = "38;2;255;255;255;48;2;57;57;57"
ENV = {"OPENAI_API_KEY": "test-key", "LLM_MODEL": "gpt-test",
       "OPENAI_BASE_URL": "http://localhost:9/v1"}


def plain(text: str) -> str:
    """Strip ANSI codes and Rich's trailing width padding."""
    return "\n".join(line.rstrip() for line in ANSI.sub("", text).split("\n"))



# --- mocked Responses API -------------------------------------------------------

def sse(kind, **data) -> bytes:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode()


def delta(text, kind="response.output_text.delta"):
    return sse(kind, delta=text, item_id="msg", output_index=0, content_index=0)


def message(text):
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def reasoning(n=1):
    return {"type": "reasoning", "id": f"rs_{n}", "summary": [], "encrypted_content": f"enc{n}"}


def call(name, n=1, **args):
    return {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": name,
            "arguments": json.dumps(args), "status": "completed"}


def done(*output, status="completed", kind="response.completed", **extra):
    response = {"id": "resp", "object": "response", "status": status, "output": list(output),
                **extra}
    return sse(kind, response=response)


def answer(text):
    """A complete streamed reply: reasoning, text deltas, then response.completed."""
    return [delta(text), done(reasoning(), message(text))]


class Stall:
    """A point in a stream where iteration blocks until the gate opens."""

    def __init__(self, gate):
        self.gate = gate


class Server:
    """Fake HTTP endpoint. Each queued reply is a list of SSE chunks (Stall pauses)
    or an httpx.Response; `gate` delays the first byte of the next streams."""

    def __init__(self, replies, gate=None):
        self.replies, self.gate, self.requests = list(replies), gate, []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        reply = self.replies.pop(0)
        if isinstance(reply, httpx.Response):
            if self.gate is not None:
                self.gate.wait(5)
            return reply
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=self.body(reply))

    def body(self, chunks):
        if self.gate is not None:
            self.gate.wait(5)
        for item in chunks:
            if isinstance(item, Stall):
                item.gate.wait(5)
            else:
                yield item

    def client(self, **kwargs):
        http = httpx.Client(transport=httpx.MockTransport(self.handle))
        return openai.OpenAI(http_client=http, **kwargs)

    def inputs(self, index):
        return self.requests[index]["input"]


def api_error(status, code="bad", param=None):
    return httpx.Response(status, json={"error": {
        "message": "raw provider text sk-secret", "type": "invalid_request_error",
        "code": code, "param": param}})


class Workspace:
    """Run tools in a temporary launch directory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        patcher = patch.object(tools, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)


class LLMTest(Workspace, unittest.TestCase):
    def start(self, replies, env=ENV, gate=None):
        server = Server(replies, gate)
        with patch.dict(os.environ, env, clear=True), patch.object(llm, "OpenAI", server.client):
            worker = llm.StreamWorker([{"role": "user", "content": "hi"}])
        return worker, server

    def collect(self, replies, env=ENV):
        worker, server = self.start(replies, env)
        worker._thread.join(10)
        self.assertFalse(worker._thread.is_alive())
        return drain(worker), server

    def test_streamed_text_and_request_shape(self):
        events, server = self.collect([[delta("Hel"), delta("lo"), delta("", ),
                                        done(reasoning(), message("Hello"))]])
        self.assertEqual(events, [("text", "Hel"), ("text", "lo"), ("done", None)])
        request = server.requests[0]
        self.assertEqual(request["model"], "gpt-test")
        self.assertEqual(request["reasoning"], {"effort": "medium"})
        self.assertEqual(request["include"], ["reasoning.encrypted_content"])
        self.assertIs(request["store"], False)
        self.assertTrue(request["stream"])
        self.assertEqual({t["name"] for t in request["tools"]}, {"read", "write", "edit", "bash"})
        self.assertTrue(all(t["strict"] is False for t in request["tools"]))

    def test_refusal_text(self):
        events, _ = self.collect([[delta("No.", "response.refusal.delta"), done()]])
        self.assertEqual(events, [("text", "No."), ("done", None)])

    def test_tool_results_replayed_with_reasoning(self):
        (self.root / "notes.txt").write_text("alpha\n")
        first = [delta("Checking."), done(reasoning(1), message("Checking."),
                                          call("read", 1, path="notes.txt"),
                                          call("write", 2, path="out/new.txt", content="hi\n"))]
        second = [done(reasoning(2), call("edit", 3, path="nope.txt", old_text="a",
                                          new_text="b"))]
        events, server = self.collect([first, second, answer("All set.")])
        self.assertEqual((self.root / "out/new.txt").read_text(), "hi\n")
        self.assertEqual(events, [
            ("text", "Checking."),
            ("tool", ("read", "running")), ("tool", ("read", "finished")),
            ("tool", ("write", "running")), ("tool", ("write", "finished")),
            ("tool", ("edit", "running")), ("tool", ("edit", "failed")),
            ("text", "\n\nAll set."), ("done", None)])
        replay = server.inputs(1)[1:]
        self.assertEqual([item["type"] for item in replay],
                         ["reasoning", "message", "function_call", "function_call",
                          "function_call_output", "function_call_output"])
        self.assertEqual(replay[0]["encrypted_content"], "enc1")
        self.assertEqual(replay[4]["call_id"], "call_1")
        self.assertIn("1\talpha", replay[4]["output"])
        self.assertEqual(replay[5]["call_id"], "call_2")
        error = server.inputs(2)[-1]
        self.assertEqual(error["call_id"], "call_3")
        self.assertIn("Error: File not found", error["output"])  # errors go back to the model
        self.assertEqual(server.inputs(2)[-3]["encrypted_content"], "enc2")

    def test_incomplete_streams_run_no_tools(self):
        target = self.root / "x.txt"
        writing = call("write", path="x.txt", content="no")
        cases = [
            ([delta("Partial")], "ended before completing"),
            ([done(writing, status="incomplete", kind="response.incomplete",
                   incomplete_details={"reason": "max_output_tokens"})], "max_output_tokens"),
            ([done(writing, status="failed", kind="response.failed",
                   error={"code": "server_error", "message": "raw"})], "server_error"),
            ([sse("error", code="overloaded", message="raw", sequence_number=1)], "overloaded"),
        ]
        for chunks, message_part in cases:
            events, server = self.collect([chunks])
            self.assertEqual(events[-1][0], "error")
            self.assertIn(message_part, events[-1][1])
            self.assertNotIn("raw", events[-1][1])
            self.assertFalse(any(kind == "tool" for kind, _ in events))
            self.assertEqual(len(server.requests), 1)
        self.assertFalse(target.exists())

    def test_stream_closed_on_every_exit(self):
        for items in ([], [NS(type="response.completed", response=NS(output=[]))]):
            stream = MagicMock()
            stream.__iter__.return_value = iter(items)
            client = NS(responses=NS(create=lambda **kw: stream))
            try:
                llm.stream_response(client, "m", [], lambda t: None, threading.Event())
            except llm.LLMError:
                pass
            stream.close.assert_called_once()

    def test_api_errors_are_diagnosed_without_secrets(self):
        events, _ = self.collect([api_error(401, "invalid_api_key")])
        self.assertEqual(events, [("error", "API error (status 401, type invalid_request_error, "
                                            "code invalid_api_key). Check OPENAI_API_KEY.")])
        events, _ = self.collect([api_error(400, "unsupported_value", "reasoning.effort")])
        self.assertIn("param reasoning.effort", events[0][1])
        self.assertNotIn("sk-secret", events[0][1])
        self.assertNotIn("test-key", events[0][1])

    def test_missing_credentials(self):
        events, server = self.collect([], env={})
        self.assertEqual(events[0][0], "error")
        self.assertIn("OPENAI_API_KEY", events[0][1])
        self.assertEqual(server.requests, [])

    def test_request_and_tool_call_limits(self):
        many = [call("read", n, path="none") for n in range(10)]
        events, server = self.collect([[done(*many)], answer("ok")])
        outputs = [i for i in server.inputs(1) if i.get("type") == "function_call_output"]
        self.assertEqual(len(outputs), 10)
        self.assertEqual(sum(1 for e in events if e == ("tool", ("read", "running"))), 8)
        self.assertIn("only 8 tool calls", outputs[-1]["output"])

        loops = [[done(call("read", n, path="none"))] for n in range(llm.MAX_REQUESTS)]
        events, server = self.collect(loops)
        self.assertEqual(len(server.requests), 16)
        self.assertIn("16 model requests", events[-1][1])

    def test_cancel_discards_late_events(self):
        gate = threading.Event()
        worker, server = self.start([answer("late")], gate=gate)
        worker.cancel()
        gate.set()
        worker._thread.join(5)
        self.assertTrue(worker.events.empty())

    def test_cancel_stops_running_and_further_tools(self):
        marker = self.root / "second"
        replies = [[done(call("bash", 1, command="sleep 30"),
                         call("write", 2, path="second", content="x"))], answer("never")]
        worker, server = self.start(replies)
        deadline = time.monotonic() + 5
        while worker.events.empty() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(worker.events.get(), ("tool", ("bash", "running")))
        worker.cancel()
        worker._thread.join(5)
        self.assertFalse(worker._thread.is_alive())  # sleep 30 was killed
        self.assertTrue(worker.used_tools)
        self.assertFalse(marker.exists())
        self.assertEqual(len(server.requests), 1)
        self.assertTrue(worker.events.empty())


def drain(worker):
    events = []
    while not worker.events.empty():
        events.append(worker.events.get())
    return events


class Turn:
    """Keys for a streaming prompt: type `keys`, open `gate` after 0.2 s, then type `later`."""

    def __init__(self, keys="", gate=None, later=""):
        self.keys, self.gate, self.later = keys, gate, later


def preview_text(session) -> str:
    return fragment_list_to_text(to_formatted_text(controls(session)[0].text()))


def controls(session):
    def walk(node):
        for child in getattr(node, "children", []):
            yield from walk(child)
        content = getattr(node, "content", None)
        if isinstance(content, FormattedTextControl):
            yield content
        elif content is not None and content is not node:
            yield from walk(content)
    return list(walk(session.layout.container))


class ChatTest(Workspace, unittest.TestCase):
    def run_chat(self, turns, replies, env=ENV, gates=()):
        """Drive main(). Plain prompts take a key string; streaming prompts take a Turn.

        `gates` pairs reply indexes with events that hold back that reply's first byte.
        """
        output = StringIO()
        console = Console(file=output, width=80, force_terminal=True, color_system="truecolor")
        original_prompt = PromptSession.prompt
        pending, defaults = list(turns), []
        server = GatedServer(replies, dict(gates))

        with create_pipe_input() as pipe:
            def prompt(session, *args, **kwargs):
                defaults.append(kwargs.get("default"))
                turn = pending.pop(0)
                start = kwargs.get("pre_run")
                if start is None:
                    pipe.send_text(turn)
                    return original_prompt(session, *args, **kwargs)
                self.assertIsInstance(turn, Turn)

                def feed():
                    start()
                    pipe.send_text(turn.keys)
                    loop = asyncio.get_running_loop()
                    if turn.gate is not None:
                        loop.call_later(0.2, turn.gate.set)
                    if turn.later:
                        loop.call_later(0.4, pipe.send_text, turn.later)
                kwargs["pre_run"] = feed
                return original_prompt(session, *args, **kwargs)

            with create_app_session(input=pipe, output=DummyOutput()), \
                    redirect_stdout(output), \
                    patch.object(PromptSession, "prompt", prompt), \
                    patch.dict(os.environ, env, clear=True), \
                    patch.object(llm, "OpenAI", server.client):
                cli.main(console=console)
        self.assertEqual(pending, [])
        return plain(output.getvalue()), defaults, server

    def sent(self, server, index):
        return [(m.get("role"), m.get("content")) for m in server.inputs(index)[1:]]

    def test_reply_rendered_and_history_sent(self):
        w1, w2 = threading.Event(), threading.Event()
        text, _, server = self.run_chat(
            ["hello\r", Turn(gate=w1), "more\r", Turn(gate=w2), "/exit\r"],
            [answer("Hello **World**\n\nSecond line"), answer("Again")], gates={0: w1, 1: w2})
        self.assertIn("\u203a hello", text)
        self.assertIn("\n Hello World\n", text)  # Markdown, one leading space per line
        self.assertIn("\n Second line\n", text)
        self.assertEqual(text.count("Hello World"), 1)  # no duplicate of the preview
        self.assertEqual(server.inputs(1)[0], {"role": "system", "content": llm.SYSTEM_PROMPT})
        self.assertEqual(self.sent(server, 1), [
            ("user", "hello"), ("assistant", "Hello **World**\n\nSecond line"), ("user", "more")])

    def test_draft_survives_reply_and_enter_waits(self):
        gate = threading.Event()
        text, defaults, server = self.run_chat(
            ["first\r", Turn("next idea\r", gate), "\x03", "/exit\r"], [answer("Hi")],
            gates={0: gate})
        self.assertEqual(defaults[2], "next idea")  # Enter during streaming was inert
        self.assertNotIn("\u203a next idea", text)
        self.assertEqual(len(server.requests), 1)

    def test_tool_turn_shows_status_and_reply(self):
        (self.root / "a.txt").write_text("hi\n")
        gate = threading.Event()
        replies = [[done(call("read", path="a.txt"))], answer("Read it.")]
        text, _, server = self.run_chat(["look\r", Turn(gate=gate), "/exit\r"], replies,
                                        gates={1: gate})
        self.assertIn(" Read it.", text)
        self.assertEqual(self.sent(server, 0), [("user", "look")])

    def test_cancel_keeps_partial_and_draft(self):
        gate = threading.Event()  # never opened: the stream stalls after "Part"
        stalled = [delta("Part"), Stall(gate), delta("never"), done(message("Partnever"))]
        w = threading.Event()
        text, defaults, server = self.run_chat(
            ["first\r", Turn("draft", later="\x03"), "\r", Turn(gate=w), "/exit\r"],
            [stalled, answer("Fresh")], gates={1: w})
        gate.set()  # let the abandoned worker finish; its events must go nowhere
        self.assertIn(" Part", text)
        self.assertIn("Response cancelled.", text)
        self.assertEqual(defaults[2], "draft")
        self.assertNotIn("never", text)
        self.assertEqual(self.sent(server, 1), [("user", "draft")])  # no tools: not kept
        self.assertIn("Fresh", text)

    def test_cancel_after_tool_use_keeps_note_in_history(self):
        replies = [[done(call("write", 1, path="made.txt", content="x"),
                         call("bash", 2, command="sleep 30"))], answer("Checked.")]
        w = threading.Event()
        text, _, server = self.run_chat(
            ["make it\r", Turn(later="\x03"), "retry\r", Turn(gate=w), "/exit\r"],
            replies, gates={1: w})
        self.assertEqual((self.root / "made.txt").read_text(), "x")  # writes are not undone
        self.assertIn("Response cancelled.", text)
        self.assertIn(llm.INTERRUPTED_NOTE, text)
        self.assertEqual(self.sent(server, 1), [
            ("user", "make it"), ("assistant", llm.INTERRUPTED_NOTE), ("user", "retry")])

    def test_api_error_preserves_draft(self):
        gate, w = threading.Event(), threading.Event()
        text, defaults, server = self.run_chat(
            ["hello\r", Turn("draft", gate), "\r", Turn(gate=w), "/exit\r"],
            [api_error(500, "server_error"), answer("ok")], gates={0: gate, 1: w})
        self.assertIn("API error (status 500", text)
        self.assertNotIn("sk-secret", text)
        self.assertEqual(defaults[2], "draft")
        self.assertEqual(self.sent(server, 1), [("user", "draft")])

    def test_incomplete_stream_keeps_partial_and_skips_history(self):
        gate, w = threading.Event(), threading.Event()
        text, _, server = self.run_chat(
            ["hello\r", Turn(gate=gate), "again\r", Turn(gate=w), "/exit\r"],
            [[delta("Partial")], answer("ok")], gates={0: gate, 1: w})
        self.assertIn(" Partial", text)
        self.assertIn("ended before completing", text)
        self.assertEqual(self.sent(server, 1), [("user", "again")])

    def test_missing_credentials_message(self):
        text, _, server = self.run_chat(["hi\r", Turn(), "/exit\r"], [], env={})
        self.assertIn("Missing OPENAI_API_KEY", text)
        self.assertNotIn("test-key", text)
        self.assertEqual(server.requests, [])


class GatedServer(Server):
    """A Server whose Nth reply waits for its own gate before sending anything."""

    def __init__(self, replies, gates):
        super().__init__(replies)
        self.gates = gates

    def handle(self, request):
        gate = self.gates.get(len(self.requests))
        if gate is not None:
            gate.wait(5)
        return super().handle(request)


class LayoutTest(unittest.TestCase):
    def test_heights_and_preview(self):
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            state = {"waiting": False, "response": ""}
            session = cli.build_session(state)
            container = session.layout.container
            self.assertEqual(container.preferred_height(80, 24).preferred, 3)
            border = session.style.get_attrs_for_style_str("class:frame.border")
            self.assertEqual((border.bgcolor, border.color), ("393939", "5c5c5c"))

            state["waiting"] = True  # Thinking line + blank line + input
            height = container.preferred_height(80, 24)
            self.assertEqual((height.min, height.preferred), (5, 5))
            self.assertIn("Thinking\u2026\n", preview_text(session))

            state["response"] = "one\ntwo"
            self.assertEqual(preview_text(session), "one\ntwo\n")
            self.assertEqual(container.preferred_height(80, 24).preferred, 3 + 3)
            state["response"] = "\n".join(map(str, range(100)))
            # Long replies shrink to fit; the input keeps its three rows.
            self.assertLessEqual(container.preferred_height(80, 24).min, 4)

    def test_tool_status_line(self):
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            state = {"waiting": True, "response": "", "tool": None}
            session = cli.build_session(state)
            container = session.layout.container
            self.assertEqual(container.preferred_height(80, 24).preferred, 5)
            state["tool"] = ("bash", "running")
            self.assertEqual(container.preferred_height(80, 24).preferred, 6)
            text = fragment_list_to_text(to_formatted_text(controls(session)[1].text()))
            self.assertIn("bash \u00b7 running", text)
            self.assertIn("Thinking\u2026\n", preview_text(session))  # label unchanged
            state["waiting"] = False
            self.assertEqual(container.preferred_height(80, 24).preferred, 3)

    def test_thinking_indicator_is_animated(self):
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            session = cli.build_session({"waiting": True, "response": ""})
            frames = set()
            for tick in range(20):
                with patch("joy.cli.time.monotonic", lambda: tick * 0.1):
                    frames.add(preview_text(session)[0])
            self.assertEqual(frames, set(cli.SPINNER))


if __name__ == "__main__":
    unittest.main()
