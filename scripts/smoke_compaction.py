"""Smoke test context compaction against the real API.

Unit tests use fake servers; this checks the assumptions they cannot:
the provider accepts the summarizer request and the compacted history, and
the agent still remembers its work after compaction.

Usage (needs OPENAI_API_KEY; LLM_MODEL / OPENAI_BASE_URL as for Joy):
    .venv/bin/python scripts/smoke_compaction.py            # steps 1-3
    .venv/bin/python scripts/smoke_compaction.py --overflow # also step 4

Cost: steps 1-3 make roughly 10-25 small model requests. Step 4 sends one
request larger than the model's context window; providers normally reject it
before running the model, but check your provider's billing for rejected input.

Exit code 0 when every step passes.
"""

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SMALL_WINDOW = "24000"  # small enough that a few file reads trigger compaction


def report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n       {detail}" if detail else ""))
    return ok


def sample_conversation(system_prompt: str) -> list[dict]:
    """A realistic agent history: a goal, decisions, edits, a failing test run."""
    conversation = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Add rate limiting to the login endpoint in src/auth.py. "
                                    "You must use a Redis token bucket, 5 attempts per minute."},
    ]
    for n in range(12):
        conversation += [
            {"type": "function_call", "call_id": f"call_{n}", "name": "read",
             "arguments": json.dumps({"path": f"src/module_{n}.py"})},
            {"type": "function_call_output", "call_id": f"call_{n}",
             "output": "\n".join(f"{i:4}  def helper_{n}_{i}(value): return value * {i}"
                                 for i in range(150))},
        ]
    conversation += [
        {"type": "function_call", "call_id": "call_edit", "name": "edit",
         "arguments": json.dumps({"path": "src/auth.py", "old_text": "def login(",
                                  "new_text": "@rate_limited(5, 60)\ndef login("})},
        {"type": "function_call_output", "call_id": "call_edit", "output": "--- a/src/auth.py\n+++ b/src/auth.py"},
        {"type": "function_call", "call_id": "call_test", "name": "bash",
         "arguments": json.dumps({"command": "pytest -q tests/test_auth.py"})},
        {"type": "function_call_output", "call_id": "call_test",
         "output": "FAILED tests/test_auth.py::test_lockout - AssertionError: expected 429\n1 failed, 11 passed in 0.8s"},
        {"role": "assistant", "content": "The lockout test fails; next I will return 429 when the bucket is empty."},
    ]
    return conversation


def step_summarizer(config: dict, llm) -> tuple[bool, list[dict] | None]:
    """1. The real summarizer returns an LLM summary, not the pruning fallback."""
    from joy.context_compaction import CompactionEngine

    engine = CompactionEngine(model=config["model"], api_key=config["api_key"],
                              base_url=config["base_url"], config=config["context"])
    try:
        started = time.monotonic()
        result = engine.maybe_compact(sample_conversation(llm.SYSTEM_PROMPT), llm.SYSTEM_PROMPT,
                                      force=True)
        elapsed = time.monotonic() - started
    finally:
        engine.close()

    if not result.compacted:
        report("1. summarizer request", False, f"not compacted: {result.error}")
        return False, None
    if (result.summary or "").startswith("Fallback"):
        report("1. summarizer request", False,
               f"the API call failed, pruning was used instead: {result.summary}")
        return False, None
    try:
        summary = json.loads(result.summary)
    except ValueError:
        report("1. summarizer request", False, f"summary is not JSON: {result.summary[:300]!r}")
        return False, None
    text = json.dumps(summary).lower()
    kept = [fact for fact in ("redis", "auth.py", "429") if fact in text]
    ok = report("1. summarizer request", len(kept) == 3,
                f"{result.original_tokens} -> {result.new_tokens} tokens in {elapsed:.1f}s; "
                f"facts kept: {kept} of ['redis', 'auth.py', '429']")
    return ok, result.new_conversation


def step_compacted_history(config: dict, llm, history: list[dict]) -> bool:
    """2. The API accepts the compacted history as input."""
    from openai import OpenAI

    question = {"role": "user", "content": "In one sentence: what rate limit did I ask for, "
                                           "and what is the next step? Do not call tools."}
    client = OpenAI(api_key=config["api_key"], base_url=config["base_url"],
                    timeout=llm.TIMEOUT, max_retries=0)
    text: list[str] = []
    try:
        response = llm.stream_response(client, config["model"], history + [question],
                                       text.append, threading.Event())
    except Exception as exc:
        return report("2. API accepts compacted history", False, llm.describe_error(exc))
    finally:
        client.close()
    reply = "".join(text)
    calls = [item for item in response.output if item.type == "function_call"]
    remembers = "5" in reply and ("429" in reply or "minute" in reply.lower())
    return report("2. API accepts compacted history", bool(reply) and not calls and remembers,
                  f"reply: {reply[:300]!r}")


def run_turn(llm, messages: list[dict], timeout: float = 600) -> dict:
    """Run one StreamWorker turn and collect its events."""
    worker = llm.StreamWorker(messages)
    events = {"text": [], "notice": [], "tool": [], "done": False, "error": None}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            kind, payload = worker.events.get(timeout=1)
        except Exception:
            continue
        if kind in ("text", "notice", "tool"):
            events[kind].append(payload)
        elif kind == "done":
            events["done"] = True
            break
        elif kind == "error":
            events["error"] = payload
            break
    else:
        worker.cancel()
        events["error"] = f"timed out after {timeout:.0f}s"
    worker._thread.join(5)
    events["worker"] = worker
    return events


def step_agent_loop(llm, tools) -> bool:
    """3. A real multi-tool turn compacts, finishes, and the next turn remembers."""
    with tempfile.TemporaryDirectory() as tmp, patch.object(tools, "ROOT", Path(tmp).resolve()), \
            patch.dict(os.environ, {"JOY_CONTEXT_WINDOW": SMALL_WINDOW}):
        for n in range(6):
            (Path(tmp) / f"data_{n}.txt").write_text(
                "\n".join(f"record {n}-{i}: value={i * 7 % 13}" for i in range(400)))
        task = ("Read each of data_0.txt to data_5.txt in full, one read per file. Then create "
                "summary.md listing each file name and its line count. Reply with one short sentence.")
        messages = [{"role": "system", "content": llm.SYSTEM_PROMPT}, {"role": "user", "content": task}]
        first = run_turn(llm, messages)
        worker = first["worker"]
        reply = "".join(first["text"])
        ok = report("3a. multi-tool turn with compaction",
                    first["done"] and not first["error"] and worker.compaction_count > 0,
                    f"tools: {len(first['tool']) // 2}, compactions: {worker.compaction_count}, "
                    f"notices: {first['notice']}, error: {first['error']}, reply: {reply[:200]!r}")
        made = (Path(tmp) / "summary.md").exists()
        ok &= report("3b. work finished after compaction", made,
                     "summary.md " + ("written" if made else "missing"))
        if not ok:
            return False

        # Next turn, as the CLI would send it: the compacted history plus a question.
        history = worker.history or [{"role": "user", "content": task},
                                     {"role": "assistant", "content": reply}]
        question = {"role": "user", "content": "Without using tools: which file did you create, "
                                               "and how many data files did you read?"}
        second = run_turn(llm, [{"role": "system", "content": llm.SYSTEM_PROMPT}, *history, question])
        answer = "".join(second["text"])
        return report("3c. next turn remembers the work",
                      second["done"] and "summary.md" in answer and ("6" in answer or "six" in answer.lower()),
                      f"used compacted history: {worker.history is not None}; reply: {answer[:300]!r}")


def step_overflow(config: dict, llm) -> bool:
    """4. The provider's 'input too long' error is recognised for the reactive retry."""
    from openai import OpenAI

    # Well past the configured window (about 1+ token per repeated word): the
    # real window may be larger than configured, and an accepted request is billed.
    words = config["context"].context_window_size * 2 + 50_000
    oversized = [{"role": "user", "content": "overflow " * words}]
    client = OpenAI(api_key=config["api_key"], base_url=config["base_url"],
                    timeout=120, max_retries=0)
    try:
        llm.stream_response(client, config["model"], oversized, lambda _: None, threading.Event())
    except Exception as exc:
        detail = (f"{type(exc).__name__}: status={getattr(exc, 'status_code', None)} "
                  f"code={getattr(exc, 'code', None)} message={str(getattr(exc, 'message', exc))[:200]!r}")
        return report("4. overflow error recognised", llm.is_context_overflow(exc), detail)
    finally:
        client.close()
    return report("4. overflow error recognised", False,
                  "the request was accepted: the model's window is larger than configured")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--overflow", action="store_true", help="also run step 4 (one oversized request)")
    args = parser.parse_args()

    import joy.llm as llm
    import joy.tools as tools
    from joy.context_config import get_model_config

    try:
        config = llm.load_config()
    except llm.LLMError as exc:
        print(exc)
        return 2
    print(f"model: {config['model']}  base_url: {config['base_url']}")

    with patch.dict(os.environ, {"JOY_CONTEXT_WINDOW": SMALL_WINDOW}):
        small = dict(config, context=get_model_config(config["model"]))
    results = []
    ok, history = step_summarizer(small, llm)
    results.append(ok)
    results.append(step_compacted_history(config, llm, history) if history else
                   report("2. API accepts compacted history", False, "skipped: step 1 failed"))
    results.append(step_agent_loop(llm, tools))
    if args.overflow:
        results.append(step_overflow(dict(config, context=get_model_config(config["model"])), llm))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} steps passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
