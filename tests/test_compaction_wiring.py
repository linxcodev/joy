"""Compaction is wired into the agent loop and produces valid Responses API input."""

import os
import sys

import httpx
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import joy.cli as cli
import joy.llm as llm
from joy.context_compaction import (CompactionEngine, CompactionResult, LLMSummarizer, fit_to_budget,
                                    preserver_view, recent_tail, carried_history)
from joy.session_manager import CompactionMetadata, SessionManager
from joy.token_counter import TokenCounter
import test_cli
from test_cli import ENV, Server, Turn, Workspace, api_error, answer, call, delta, done, drain, message, reasoning


def tool_round(n):
    """One response's items as the loop stores them: reasoning, a call, its output."""
    return [
        {"type": "reasoning", "id": f"rs_{n}", "summary": []},
        {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}",
         "name": "read", "arguments": "{}", "status": "completed"},
        {"type": "function_call_output", "call_id": f"call_{n}", "output": f"out {n}"},
    ]


class RecentTailTest(unittest.TestCase):
    def test_never_orphans_a_tool_output(self):
        items = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        for n in range(6):
            items += tool_round(n)
        for keep in range(1, len(items)):
            tail = recent_tail(items, keep)
            calls = {m["call_id"] for m in tail if m.get("type") == "function_call"}
            outputs = [m for m in tail if m.get("type") == "function_call_output"]
            self.assertTrue(all(o["call_id"] in calls for o in outputs), keep)

    def test_contiguous_tool_rounds_are_still_trimmed(self):
        items = [{"role": "user", "content": "go"}]
        for n in range(10):  # no reasoning items between rounds
            items += [{"type": "function_call", "call_id": f"c{n}", "name": "read", "arguments": "{}"},
                      {"type": "function_call_output", "call_id": f"c{n}", "output": "x"}]
        tail = recent_tail(items, 4)
        self.assertEqual([m["call_id"] for m in tail], ["c8", "c8", "c9", "c9"])
        # A cut between a call and its output moves back by just that call.
        self.assertEqual([m["call_id"] for m in recent_tail(items, 3)], ["c8", "c8", "c9", "c9"])

    def test_drops_reasoning_ids_and_system(self):
        items = [{"role": "system", "content": "sys"},
                 {"type": "message", "id": "msg_1", "role": "assistant",
                  "content": [{"type": "output_text", "text": "hi", "annotations": []}]},
                 *tool_round(1)]
        tail = recent_tail(items, 10)
        self.assertEqual(tail[0], {"role": "assistant", "content": "hi"})
        self.assertFalse(any(m.get("type") == "reasoning" for m in tail))
        self.assertFalse(any("id" in m for m in tail))
        self.assertFalse(any(m.get("role") == "system" for m in tail))


class SummarizerOutputTest(unittest.TestCase):
    def setUp(self):
        self.summarizer = LLMSummarizer(client=None, model="gpt-test")

    def test_summary_message_is_valid_input(self):
        conversation = [{"role": "system", "content": "sys"},
                        {"role": "user", "content": "task"}, *tool_round(1)]
        new = self.summarizer._build_compacted_conversation(conversation, '{"goal": "x"}')
        self.assertEqual(new[0], {"role": "system", "content": "sys"})
        self.assertEqual(set(new[1]), {"role", "content"})
        self.assertIn("[CONTEXT SUMMARY]", new[1]["content"])
        # Recent tool work survives verbatim.
        self.assertIn({"type": "function_call_output", "call_id": "call_1", "output": "out 1"}, new)

    def test_format_reads_real_message_shapes(self):
        conversation = [{"role": "user", "content": "fix the bug"},
                        {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "on it"}]},
                        *tool_round(1)]
        text = self.summarizer._format_conversation(conversation)
        self.assertIn("USER: fix the bug", text)
        self.assertIn("ASSISTANT: on it", text)
        self.assertIn("TOOL_CALL: read", text)
        self.assertIn("TOOL_RESULT: out 1", text)


class TokenCounterShapesTest(unittest.TestCase):
    def test_counts_list_content(self):
        counter = TokenCounter("gpt-test")
        text = "word " * 400
        plain = counter.count_message({"role": "assistant", "content": text})
        parts = counter.count_message({"type": "message", "role": "assistant",
                                       "content": [{"type": "output_text", "text": text}]})
        self.assertGreater(plain, 100)
        self.assertEqual(plain, parts)


SUMMARY = "[CONTEXT SUMMARY]\nsummary\n[END SUMMARY]"
BIG = "x " * 8000  # over FakeEngine's threshold


class FakeEngine:
    """Stands in for CompactionEngine: compacts when forced or over ~5000
    characters, records calls, and returns a short history."""

    instances = []
    delay = None  # an Event to wait on, to simulate a slow summarizer

    def __init__(self, **kwargs):
        self.calls, self.forced = [], []
        FakeEngine.instances.append(self)

    def maybe_compact(self, conversation, system_prompt, force=False):
        self.calls.append(list(conversation))
        self.forced.append(force)
        if FakeEngine.delay is not None:
            FakeEngine.delay.wait(5)
        if not force and len(str(conversation)) - len(system_prompt) < 5000:
            return CompactionResult(compacted=False, original_tokens=100, new_tokens=0,
                                    new_conversation=conversation)
        new = [{"role": "system", "content": system_prompt},
               {"role": "user", "content": SUMMARY},
               {"role": "user", "content": "(latest request)"}]
        return CompactionResult(compacted=True, original_tokens=10_000, new_tokens=50,
                                summary="summary", new_conversation=new)

    def close(self):
        pass


class LoopWiringTest(Workspace, unittest.TestCase):
    def tearDown(self):
        FakeEngine.delay = None

    def run_worker(self, replies, env, content=BIG):
        FakeEngine.instances.clear()
        server = Server(replies)
        with patch.dict(os.environ, env, clear=True), \
                patch.object(llm, "OpenAI", server.client), \
                patch.object(llm, "CompactionEngine", FakeEngine):
            worker = llm.StreamWorker([{"role": "system", "content": llm.SYSTEM_PROMPT},
                                       {"role": "user", "content": content}])
            worker._thread.join(10)
        self.assertFalse(worker._thread.is_alive())
        return worker, server, drain(worker)

    def test_loop_compacts_before_request_over_threshold(self):
        worker, server, events = self.run_worker([answer("Done.")], ENV)
        engine = FakeEngine.instances[0]
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(worker.compaction_count, 1)
        self.assertIn("notice", [kind for kind, _ in events])
        self.assertFalse(any(kind == "text" and "compaction" in str(payload).lower()
                             for kind, payload in events))
        # Compacted history for the CLI: summary, request, final reply.
        self.assertEqual(worker.history, [{"role": "user", "content": SUMMARY},
                                          {"role": "user", "content": "(latest request)"},
                                          {"role": "assistant", "content": "Done."}])
        # The request sent to the model carries the compacted history.
        sent = str(server.requests[0]["input"])
        self.assertIn("[CONTEXT SUMMARY]", sent)
        self.assertNotIn("x x x x", sent)
        self.assertEqual(events[-1], ("done", None))

    def test_disabled_compaction_leaves_history_alone(self):
        env = {**ENV, "JOY_COMPACTION_ENABLED": "false"}
        worker, server, events = self.run_worker([answer("Done.")], env)
        self.assertEqual(FakeEngine.instances, [])
        self.assertEqual(worker.compaction_count, 0)
        self.assertIsNone(worker.history)
        self.assertIn("x x x x", str(server.requests[0]["input"]))

    def test_context_overflow_forces_compaction_and_retries_once(self):
        # Small enough to pass the threshold; the API still rejects it.
        replies = [api_error(400, "context_length_exceeded"), answer("Done.")]
        worker, server, events = self.run_worker(replies, ENV, content="short request")
        engine = FakeEngine.instances[0]
        self.assertEqual(engine.forced, [False, True])  # normal check, then forced
        self.assertEqual(len(server.requests), 2)
        self.assertIn("[CONTEXT SUMMARY]", str(server.requests[1]["input"]))
        self.assertEqual(events[-1], ("done", None))

    def test_second_overflow_is_reported(self):
        replies = [api_error(400, "context_length_exceeded"),
                   api_error(400, "context_length_exceeded")]
        worker, server, events = self.run_worker(replies, ENV, content="short request")
        self.assertEqual(len(server.requests), 2)  # one retry only
        self.assertEqual(events[-1][0], "error")
        self.assertIn("context_length_exceeded", events[-1][1])

    def test_proxy_overflow_wording_is_recognised(self):
        # As an OpenAI-compatible proxy in front of Anthropic reported it (smoke test).
        body = {"error": {"message": '[400]: {"type":"error","error":{"type":"invalid_request_error",'
                                     '"message":"prompt is too long: 308513 tokens > 200000 maximum"}}',
                          "type": "invalid_request_error", "code": "bad_request"}}
        replies = [httpx.Response(400, json=body), answer("Done.")]
        worker, server, events = self.run_worker(replies, ENV, content="short request")
        self.assertEqual(FakeEngine.instances[0].forced, [False, True])
        self.assertEqual(events[-1], ("done", None))

    def test_unrelated_bad_request_is_not_overflow(self):
        body = {"error": {"message": "Unsupported parameter: reasoning.effort",
                          "type": "invalid_request_error", "code": "bad_request"}}
        worker, server, events = self.run_worker([httpx.Response(400, json=body)], ENV,
                                                 content="short request")
        self.assertEqual(FakeEngine.instances[0].forced, [False])
        self.assertEqual(events[-1][0], "error")

    def test_other_errors_do_not_trigger_compaction(self):
        worker, server, events = self.run_worker([api_error(401, "invalid_api_key")], ENV,
                                                 content="short request")
        self.assertEqual(FakeEngine.instances[0].forced, [False])
        self.assertEqual(events[-1][0], "error")

    def test_cancel_during_compaction_returns_promptly(self):
        FakeEngine.delay = threading.Event()  # summarizer "hangs" until set
        server = Server([answer("never")])
        with patch.dict(os.environ, ENV, clear=True), \
                patch.object(llm, "OpenAI", server.client), \
                patch.object(llm, "CompactionEngine", FakeEngine):
            FakeEngine.instances.clear()
            worker = llm.StreamWorker([{"role": "user", "content": BIG}])
            for _ in range(50):  # wait until compaction has started
                if FakeEngine.instances and FakeEngine.instances[0].calls:
                    break
                threading.Event().wait(0.02)
            worker.cancel()
            worker._thread.join(1)
            alive = worker._thread.is_alive()
            FakeEngine.delay.set()
        self.assertFalse(alive)  # did not wait for the 5 s "summarizer"
        self.assertEqual(server.requests, [])  # no request after cancelling

    def test_worker_does_not_create_a_session_per_turn(self):
        with patch.object(llm, "SessionManager") as manager:
            self.run_worker([answer("Done.")], ENV)
        manager.assert_not_called()


class CarriedHistoryTest(unittest.TestCase):
    def test_keeps_tool_work_as_valid_input(self):
        conversation = [{"role": "system", "content": "sys"}, {"role": "user", "content": SUMMARY},
                        *tool_round(1),
                        {"type": "message", "role": "assistant", "id": "m",
                         "content": [{"type": "output_text", "text": "done"}]}]
        # The write after the summary stays visible to the next turn.
        self.assertEqual(carried_history(conversation), [
            {"role": "user", "content": SUMMARY},
            {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "out 1"},
            {"role": "assistant", "content": "done"}])


class CliCompactionTest(Workspace, unittest.TestCase):
    run_chat = test_cli.ChatTest.run_chat  # reuse the driver without re-collecting ChatTest
    sent = test_cli.ChatTest.sent

    def setUp(self):
        super().setUp()
        sessions = tempfile.TemporaryDirectory()
        self.addCleanup(sessions.cleanup)
        self.session_dir = Path(sessions.name)
        FakeEngine.instances.clear()
        patches = [patch.object(cli, "SessionManager", lambda: SessionManager(self.session_dir)),
                   patch.object(llm, "CompactionEngine", FakeEngine)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_next_turn_starts_from_compacted_history(self):
        w1, w2 = threading.Event(), threading.Event()
        text, _, server = self.run_chat(
            [BIG + "\r", Turn(gate=w1), "more\r", Turn(gate=w2), "/exit\r"],
            [answer("Done."), answer("Again.")], gates={0: w1, 1: w2})
        self.assertIn("Context compaction", text)  # shown as a status line
        self.assertEqual(self.sent(server, 1), [
            ("user", SUMMARY), ("user", "(latest request)"),
            ("assistant", "Done."), ("user", "more")])
        self.assertNotIn("x x x x", str(server.inputs(1)))
        # Turn 2 started from the small history, so it did not compact again.
        self.assertEqual(sum(e.forced.count(False) for e in FakeEngine.instances), 2)
        self.assertEqual(FakeEngine.instances[1].calls[0][1]["content"], SUMMARY)

        saved = [SessionManager(self.session_dir).load_snapshot(info["session_id"])
                 for info in SessionManager(self.session_dir).list_sessions()]
        meta = cli.as_record(saved[0].compaction_metadata, CompactionMetadata)
        self.assertTrue(meta.is_compacted)
        self.assertEqual(meta.compaction_count, 1)
        self.assertNotIn("📦", str(saved[0].conversation_history))  # notice is not reply text

    def test_as_record_reads_stored_dicts(self):
        meta = cli.as_record({"is_compacted": True, "compaction_count": 2, "extra": 1},
                             CompactionMetadata)
        self.assertEqual((meta.is_compacted, meta.compaction_count), (True, 2))
        self.assertEqual(cli.as_record(None, CompactionMetadata), CompactionMetadata())


class ModelConfigTest(unittest.TestCase):
    def test_current_models_get_real_windows(self):
        from joy.context_config import get_model_config
        self.assertEqual(get_model_config(llm.DEFAULT_MODEL).context_window_size, 400000)
        self.assertEqual(get_model_config("gpt-4.1-mini").context_window_size, 1047576)
        self.assertEqual(get_model_config("o4-mini").context_window_size, 200000)
        self.assertEqual(get_model_config("gpt-4").context_window_size, 8192)
        self.assertGreaterEqual(get_model_config("some-new-model").context_window_size, 128000)

    def test_env_override(self):
        from joy.context_config import get_model_config
        with patch.dict(os.environ, {"JOY_CONTEXT_WINDOW": "32000"}):
            config = get_model_config("gpt-5")
        self.assertEqual(config.context_window_size, 32000)
        self.assertLessEqual(config.max_output_tokens, 16000)  # output reserve clamped
        with patch.dict(os.environ, {"JOY_CONTEXT_WINDOW": "nope"}):
            self.assertEqual(get_model_config("gpt-5").context_window_size, 400000)


class FakeResponses:
    def __init__(self, text, status="completed"):
        self.text, self.status, self.calls = text, status, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return NS(status=self.status, output_text=self.text)


class SummarizerApiTest(unittest.TestCase):
    def setUp(self):
        self.counter = TokenCounter("gpt-5")
        from joy.context_config import get_model_config
        self.config = get_model_config("gpt-5")

    def summarizer(self, responses):
        return LLMSummarizer(client=NS(responses=responses), model="gpt-5", max_summary_tokens=4000)

    def conversation(self, earlier_summary=None):
        items = [{"role": "system", "content": "sys"}]
        if earlier_summary:
            items.append({"role": "user", "content": earlier_summary})
        items.append({"role": "user", "content": "task"})
        for n in range(8):
            items += tool_round(n)
        return items

    def test_code_fenced_json_is_unwrapped(self):
        responses = FakeResponses('```json\n{"user_goal": "task"}\n```')
        result = self.summarizer(responses).compact(self.conversation(), "sys", self.counter, self.config)
        self.assertEqual(result.summary, '{"user_goal": "task"}')

    def test_uses_responses_api_with_reasoning_params(self):
        responses = FakeResponses('{"user_goal": "task"}')
        result = self.summarizer(responses).compact(self.conversation(), "sys", self.counter, self.config)
        self.assertTrue(result.compacted)
        self.assertEqual(result.summary, '{"user_goal": "task"}')  # the LLM summary, not pruning
        request = responses.calls[0]
        self.assertNotIn("max_tokens", request)
        self.assertNotIn("temperature", request)
        self.assertIs(request["store"], False)
        self.assertGreater(request["max_output_tokens"], 4000)  # room for reasoning

    def test_streamed_reply_without_output_uses_deltas(self):
        # As an OpenAI-compatible proxy sends it: deltas, then a completed
        # event whose response has no output list.
        class StreamingResponses(FakeResponses):
            def create(self, **kwargs):
                self.calls.append(kwargs)
                events = [NS(type="response.created"),
                          NS(type="response.output_text.delta", delta='{"user_goal": '),
                          NS(type="response.output_text.delta", delta='"task"}'),
                          NS(type="response.completed", response=NS(output_text=None))]
                return iter(events)

        responses = StreamingResponses("")
        result = self.summarizer(responses).compact(self.conversation(), "sys", self.counter, self.config)
        self.assertEqual(result.summary, '{"user_goal": "task"}')
        self.assertIs(responses.calls[0]["stream"], True)

    def test_incomplete_stream_falls_back_to_pruning(self):
        class IncompleteStream(FakeResponses):
            def create(self, **kwargs):
                return iter([NS(type="response.output_text.delta", delta='{"user_go'),
                             NS(type="response.incomplete")])

        result = self.summarizer(IncompleteStream("")).compact(
            self.conversation(), "sys", self.counter, self.config)
        self.assertTrue(result.compacted)
        self.assertIn("Fallback", result.summary)

    def test_incomplete_response_falls_back_to_pruning(self):
        responses = FakeResponses('{"user_goal": "tr', status="incomplete")
        result = self.summarizer(responses).compact(self.conversation(), "sys", self.counter, self.config)
        self.assertTrue(result.compacted)
        self.assertIn("Fallback", result.summary)

    def test_previous_summary_is_sent_whole_and_not_duplicated(self):
        earlier = "[CONTEXT SUMMARY]\n" + "decided: use sqlite. " * 200 + "\n[END SUMMARY]"
        responses = FakeResponses('{"user_goal": "merged"}')
        result = self.summarizer(responses).compact(
            self.conversation(earlier), "sys", self.counter, self.config)
        prompt = responses.calls[0]["input"][0]["content"]
        self.assertIn("PREVIOUS SUMMARY", prompt)
        self.assertIn(earlier, prompt)  # not cut at 1000 characters
        self.assertEqual(prompt.count("decided: use sqlite."), 200)  # not repeated in CONVERSATION
        summaries = [m for m in result.new_conversation if str(m.get("content", "")).startswith("[CONTEXT SUMMARY]")]
        self.assertEqual(len(summaries), 1)
        self.assertIn("merged", summaries[0]["content"])

    def test_fallback_keeps_earlier_summary(self):
        earlier = "[CONTEXT SUMMARY]\nold facts\n[END SUMMARY]"
        conversation = self.conversation(earlier)
        for n in range(8, 30):
            conversation += tool_round(n)
        result = self.summarizer(FakeResponses("", status="failed")).compact(
            conversation, "sys", self.counter, self.config)
        self.assertTrue(result.compacted)
        self.assertEqual(result.new_conversation[1], {"role": "user", "content": earlier})
        self.assertLess(len(result.new_conversation), len(conversation))


class FitToBudgetTest(unittest.TestCase):
    def setUp(self):
        self.counter = TokenCounter("gpt-5")

    def tokens(self, conversation):
        return self.counter.count_conversation(conversation).total_tokens

    def test_truncates_big_tool_outputs_first(self):
        conversation = [{"role": "system", "content": "sys"}, {"role": "user", "content": SUMMARY},
                        {"type": "function_call", "call_id": "c1", "name": "read", "arguments": "{}"},
                        {"type": "function_call_output", "call_id": "c1", "output": "line\n" * 20000}]
        fitted = fit_to_budget(conversation, self.counter, 3000)
        self.assertLessEqual(self.tokens(fitted), 3000)
        self.assertEqual(len(fitted), 4)  # nothing dropped: truncation was enough
        self.assertIn("truncated to fit the context window", fitted[3]["output"])
        self.assertEqual(conversation[3]["output"], "line\n" * 20000)  # input untouched

    def test_drops_oldest_but_keeps_system_summary_and_pairs(self):
        conversation = [{"role": "system", "content": "sys"}, {"role": "user", "content": SUMMARY}]
        for n in range(20):
            conversation += [{"type": "function_call", "call_id": f"c{n}", "name": "read", "arguments": "{}"},
                             {"type": "function_call_output", "call_id": f"c{n}", "output": "word " * 400}]
        conversation.append({"role": "user", "content": "latest"})
        fitted = fit_to_budget(conversation, self.counter, 1500)
        self.assertLessEqual(self.tokens(fitted), 1500)
        self.assertEqual(fitted[:2], conversation[:2])
        self.assertEqual(fitted[-1], {"role": "user", "content": "latest"})
        calls = {m["call_id"] for m in fitted if m.get("type") == "function_call"}
        outputs = {m["call_id"] for m in fitted if m.get("type") == "function_call_output"}
        self.assertEqual(calls, outputs)

    def test_already_small_is_unchanged(self):
        conversation = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        self.assertEqual(fit_to_budget(conversation, self.counter, 1000), conversation)


class EngineTest(unittest.TestCase):
    """CompactionEngine with a fake Responses API."""

    def engine(self, text='{"user_goal": "task"}', window="20000"):
        with patch.dict(os.environ, {"JOY_CONTEXT_WINDOW": window}):
            engine = CompactionEngine(model="gpt-5", api_key="k", base_url="http://localhost:1")
        engine.close()
        self.responses = FakeResponses(text)
        engine.summarizer.client = NS(responses=self.responses)
        return engine

    def conversation(self, rounds):
        items = [{"role": "system", "content": "sys"},
                 {"role": "user", "content": "Fix the login bug in auth.py"}]
        for n in range(rounds):
            items += [{"type": "function_call", "call_id": f"c{n}", "name": "bash",
                       "arguments": '{"command": "pytest"}'},
                      {"type": "function_call_output", "call_id": f"c{n}",
                       "output": "FAILED tests/test_auth.py::test_login - AssertionError: bad token\n" + "log " * 300}]
        return items

    def test_below_threshold_is_left_alone_unless_forced(self):
        engine = self.engine()
        below = self.conversation(10)  # under the threshold, but more than the kept tail
        self.assertLess(engine.token_counter.count_conversation(below).total_tokens,
                        engine.threshold_tokens)
        self.assertFalse(engine.maybe_compact(below, "sys").compacted)
        self.assertEqual(self.responses.calls, [])
        self.assertTrue(engine.maybe_compact(below, "sys", force=True).compacted)

    def test_preserved_facts_reach_the_summarizer(self):
        engine = self.engine()
        result = engine.maybe_compact(self.conversation(30), "sys")
        self.assertTrue(result.compacted)
        prompt = self.responses.calls[0]["input"][0]["content"]
        self.assertIn("FACTS EXTRACTED FROM THE FULL CONVERSATION", prompt)
        self.assertIn("USER GOAL: Fix the login bug in auth.py", prompt)
        self.assertIn("bad token", prompt)  # error extracted from a tool output

    def test_result_fits_below_threshold(self):
        engine = self.engine()
        result = engine.maybe_compact(self.conversation(30), "sys")
        self.assertTrue(result.compacted)
        self.assertLessEqual(result.new_tokens, engine.threshold_tokens)

    def test_no_recompaction_without_growth(self):
        engine = self.engine()
        engine._last_compacted_tokens = 10**9  # as if the last result stayed huge
        self.assertFalse(engine.maybe_compact(self.conversation(30), "sys").compacted)
        self.assertEqual(self.responses.calls, [])

    def test_fallback_keeps_preserved_facts_as_one_summary(self):
        engine = self.engine()
        engine.summarizer.client = NS(responses=FakeResponses("", status="failed"))
        earlier = "[CONTEXT SUMMARY]\nold decision: use JWT\n[PRESERVED CONTEXT]\nstale\n[END PRESERVED CONTEXT]\n[END SUMMARY]"
        conversation = self.conversation(30)
        conversation.insert(1, {"role": "user", "content": earlier})
        result = engine.maybe_compact(conversation, "sys")
        summaries = [m for m in result.new_conversation if str(m.get("content", "")).startswith("[CONTEXT SUMMARY]")]
        self.assertEqual(len(summaries), 1)
        self.assertIn("old decision: use JWT", summaries[0]["content"])
        self.assertNotIn("stale", summaries[0]["content"])
        self.assertIn("UNRESOLVED ERRORS", summaries[0]["content"])


class FormatterBudgetTest(unittest.TestCase):
    def test_keeps_newest_lines_within_budget(self):
        summarizer = LLMSummarizer(client=None, model="gpt-5")
        conversation = [{"role": "user", "content": f"message {n} " + "x" * 90} for n in range(500)]
        text = summarizer._format_conversation(conversation, max_chars=5000)
        self.assertLessEqual(len(text), 5100)
        self.assertIn("message 499", text)
        self.assertNotIn("message 0 ", text)
        self.assertIn("earlier items omitted", text)


class PreserverViewTest(unittest.TestCase):
    def test_normalizes_real_shapes(self):
        view = preserver_view([{"role": "user", "content": "go"},
                               {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
                               {"type": "reasoning", "summary": []},
                               {"role": "user", "content": SUMMARY},
                               {"type": "function_call_output", "call_id": "c", "output": "Error: boom happened"}])
        self.assertEqual(view[0], {"type": "text", "role": "user", "content": "go"})
        self.assertEqual(view[1], {"type": "text", "role": "assistant", "content": "ok"})
        self.assertEqual(len(view), 3)  # reasoning and summary left out
        self.assertEqual(view[2]["content"], "Error: boom happened")


class TiktokenTest(unittest.TestCase):
    def test_counts_with_tiktoken_for_current_and_unknown_models(self):
        for model in (llm.DEFAULT_MODEL, "some-proxy-model"):
            counter = TokenCounter(model)
            self.assertTrue(counter._use_tiktoken, model)
            self.assertEqual(counter.count_tokens("hello world"), 2)


if __name__ == "__main__":
    unittest.main()
