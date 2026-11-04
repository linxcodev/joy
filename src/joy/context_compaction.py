"""Context Compaction Engine for Joy Agent.

This module provides:
- CompactionStrategy: Abstract base for compaction strategies
- LLMSummarizer: LLM-based conversation summarization
- CompactionEngine: Orchestrates context compaction process
- Structured summaries with information preservation

When conversation history grows too large, this module creates structured summaries
that preserve critical information without storing the full conversation history.

Example usage:
    from joy.context_compaction import CompactionEngine
    
    engine = CompactionEngine(model="gpt-4")
    result = engine.maybe_compact(
        conversation=messages,
        system_prompt=SYSTEM_PROMPT,
        token_counter=counter,
        config=context_config
    )
    
    if result.compacted:
        messages = result.new_conversation
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from openai import OpenAI

from joy.token_counter import TokenCounter, item_text
from joy.context_config import get_model_config, ContextConfig
from joy.context_preservation import ContextPreserver

logger = logging.getLogger(__name__)

RECENT_ITEMS_KEPT = 10  # conversation items kept verbatim after the summary
SUMMARY_PREFIX = "[CONTEXT SUMMARY]"
MAX_SUMMARY_TOKENS = 4000  # cap on the summary text itself
SUMMARY_REASONING_BUDGET = 8000  # extra output budget a reasoning model spends thinking
SUMMARY_TIMEOUT = 60
FIT_TARGET = 0.8  # compacted history must end below this share of the threshold
PRESERVED_START, PRESERVED_END = "[PRESERVED CONTEXT]", "[END PRESERVED CONTEXT]"


def is_summary(item: dict) -> bool:
    """Whether an item is a summary message written by an earlier compaction."""
    return item.get("role") == "user" and item_text(item).startswith(SUMMARY_PREFIX)


def _as_input_item(item: dict) -> Optional[dict]:
    """Rewrite a kept item so it is valid stand-alone Responses API input.

    Reasoning items are dropped and server ids removed: once earlier items are
    summarized away, an id-bearing item could reference reasoning that is gone.
    """
    kind = item.get("type")
    if kind == "reasoning":
        return None
    if kind == "function_call":
        return {"type": "function_call", "call_id": item.get("call_id"),
                "name": item.get("name"), "arguments": item.get("arguments", "")}
    if kind == "function_call_output":
        return {"type": "function_call_output", "call_id": item.get("call_id"),
                "output": item.get("output", "")}
    if item.get("role") in ("user", "assistant", "developer"):
        return {"role": item["role"], "content": item_text(item)}
    return None


def recent_tail(conversation: list[dict], keep: int) -> list[dict]:
    """Last `keep` non-system items as valid input, never splitting a tool call from its result."""
    items = [m for m in conversation if m.get("role") != "system"]
    start = max(0, len(items) - keep)
    # Move the cut back to the earliest call whose output is kept, so no output
    # loses its call. Calls precede their outputs, so one pass is enough.
    call_index = {m.get("call_id"): i for i, m in enumerate(items) if m.get("type") == "function_call"}
    orphan_calls = [call_index[m.get("call_id")] for m in items[start:]
                    if m.get("type") == "function_call_output"
                    and call_index.get(m.get("call_id"), start) < start]
    if orphan_calls:
        start = min(orphan_calls)
    tail = [converted for m in items[start:] if (converted := _as_input_item(m))]
    # Drop any output whose call is still missing (call was never in the history).
    calls = {m["call_id"] for m in tail if m.get("type") == "function_call"}
    return [m for m in tail if m.get("type") != "function_call_output" or m["call_id"] in calls]


def carried_history(conversation: list[dict]) -> list[dict]:
    """The conversation as history for the next turn: everything but the system prompt.

    Tool calls and their results stay (as id-free input items, reasoning dropped),
    so the next turn sees what was actually done after the summary was written,
    not just the final reply.
    """
    return recent_tail(conversation, len(conversation))


def preserver_view(conversation: list[dict]) -> list[dict]:
    """The conversation in the shape ContextPreserver's extractors read.

    They expect {"type": "text", "content": str} messages and tool results under
    "content"; Joy's real items use plain role messages, content-part lists and
    "output". Reasoning items and earlier summaries are left out.
    """
    view = []
    for item in conversation:
        kind = item.get("type")
        if kind == "reasoning" or is_summary(item):
            continue
        if kind == "function_call":
            view.append(item)
        elif kind == "function_call_output":
            text = item_text(item)
            view.append({**item, "content": text, "output": text})
        elif item.get("role"):
            view.append({"type": "text", "role": item["role"], "content": item_text(item)})
    return view


def _truncate(text: str, cap: int) -> str:
    return text[:cap] + f"\n[... {len(text) - cap} characters truncated to fit the context window]"


def fit_to_budget(conversation: list[dict], counter: TokenCounter, budget: int) -> list[dict]:
    """Shrink a compacted conversation until it counts at most `budget` tokens.

    In order: cut oversized tool outputs, drop the oldest items (never the system
    prompt or a summary, and never a call without its output), then cut long
    message texts. Returns a new list; may still exceed the budget if the system
    prompt and summary alone do.
    """
    conv = [dict(m) for m in conversation]

    def over() -> bool:
        return counter.count_conversation(conv).total_tokens > budget

    def protected(item: dict) -> bool:
        return item.get("role") == "system" or is_summary(item)

    cap = 20_000
    while over() and cap >= 500:
        for m in conv:
            out = m.get("output")
            if m.get("type") == "function_call_output" and isinstance(out, str) and len(out) > cap:
                m["output"] = _truncate(out, cap)
        cap //= 2

    while over():
        prunable = [i for i, m in enumerate(conv) if not protected(m)]
        if len(prunable) <= 1:  # keep the latest item so the model has something to act on
            break
        dropped = conv.pop(prunable[0])
        if dropped.get("type") == "function_call":
            conv = [m for m in conv if not (m.get("type") == "function_call_output"
                                            and m.get("call_id") == dropped.get("call_id"))]
        # A call whose output was dropped first cannot happen: calls precede outputs.

    cap = 20_000
    while over() and cap >= 500:
        for m in conv:
            content = m.get("content")
            if not protected(m) and isinstance(content, str) and len(content) > cap:
                m["content"] = _truncate(content, cap)
        cap //= 2
    return conv


def _without_preserved_block(text: str) -> str:
    start = text.find(PRESERVED_START)
    end = text.find(PRESERVED_END)
    if start == -1 or end == -1 or end < start:
        return text
    return (text[:start] + text[end + len(PRESERVED_END):]).strip()


def _summary_body(item: dict) -> str:
    text = item_text(item).removeprefix(SUMMARY_PREFIX)
    return text.split("[END SUMMARY]")[0].strip()


def _system_message(conversation: list[dict]) -> Optional[dict]:
    msg = next((m for m in conversation if m.get("role") == "system"), None)
    return {"role": "system", "content": item_text(msg)} if msg else None


@dataclass
class CompactionResult:
    """Result of compaction attempt."""
    compacted: bool  # Whether compaction was performed
    original_tokens: int  # Tokens before compaction
    new_tokens: int  # Tokens after compaction (0 if not compacted)
    summary: Optional[str] = None  # Text of summary if created
    error: Optional[str] = None  # Error message if failed
    new_conversation: list[dict] = field(default_factory=list)  # Updated conversation


@dataclass
class StructuredSummary:
    """Structured summary of conversation progress."""
    user_goal: str  # User's main objective
    important_instructions: list[str]  # Still-applicable instructions
    technical_decisions: list[str]  # Key technical choices made
    files_modified: dict[str, str]  # {"path": "type_of_change"}
    code_changes: list[str]  # High-level descriptions of code changes
    test_results: str  # Current test status
    unresolved_errors: list[str]  # Outstanding issues
    current_task: str  # What agent is currently doing
    next_steps: list[str]  # Recommended next actions


def _strip_code_fence(text: str) -> str:
    """Remove a Markdown code fence around a reply (```json ... ```), if any."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```") and stripped.count("\n") >= 1:
        body = stripped[3:-3]
        return body.split("\n", 1)[1].strip() if "\n" in body else body.strip()
    return text


def _response_text(response) -> str:
    """Text of a completed Responses API reply, streamed or not.

    Raises RuntimeError unless the response completed: an incomplete one holds
    truncated JSON, not a usable summary.
    """
    # A stream iterates events and has no output_text; a response object has it.
    if not hasattr(response, "__next__") or hasattr(response, "output_text"):
        status = getattr(response, "status", "completed")
        if status != "completed":
            raise RuntimeError(f"summary response {status}")
        text = response.output_text
        return text if isinstance(text, str) else ""

    deltas: list[str] = []
    try:
        for event in response:
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta":
                deltas.append(getattr(event, "delta", "") or "")
            elif kind == "response.completed":
                final = getattr(event, "response", None)
                try:
                    text = final.output_text if final is not None else ""
                except TypeError:  # proxies may send a completed response without output
                    text = ""
                # Some proxies omit the output on the completed event; the deltas have it.
                return text if isinstance(text, str) and text else "".join(deltas)
            elif kind in ("response.incomplete", "response.failed"):
                raise RuntimeError(f"summary {kind.removeprefix('response.')}")
            elif kind == "error":
                raise RuntimeError(f"summary stream error (code: {getattr(event, 'code', None)})")
        raise RuntimeError("summary stream ended before completing")
    finally:
        close = getattr(response, "close", None)
        if close:
            close()


class CompactionStrategy(ABC):
    """Abstract base for conversation compaction strategies."""

    @abstractmethod
    def compact(
        self,
        conversation: list[dict],
        system_prompt: str,
        token_counter: TokenCounter,
        config: ContextConfig,
    ) -> CompactionResult:
        """Compact conversation history.

        Args:
            conversation: Full conversation message history
            system_prompt: System instruction prompt
            token_counter: Token counter instance
            config: Context configuration with limits

        Returns:
            CompactionResult with compacted conversation or original if failed
        """
        pass


class LLMSummarizer(CompactionStrategy):
    """Summarize conversation using LLM."""

    def __init__(
        self,
        client: OpenAI,
        model: str,
        max_summary_tokens: int = 2000,
    ):
        """Initialize LLM summarizer.

        Args:
            client: OpenAI API client
            model: Model to use for summarization
            max_summary_tokens: Maximum tokens allowed in summary
        """
        self.client = client
        self.model = model
        self.max_summary_tokens = max_summary_tokens

    def compact(
        self,
        conversation: list[dict],
        system_prompt: str,
        token_counter: TokenCounter,
        config: ContextConfig,
        preserved_context: str = "",
    ) -> CompactionResult:
        """Create structured summary of conversation.

        `preserved_context` (ContextPreserver's formatted facts) is given to the
        summarizer as must-keep facts, and kept by the pruning fallback.
        """
        original_tokens = token_counter.count_conversation(conversation).total_tokens

        try:
            # Build summarization prompt; about half the input window, in characters.
            max_chars = max(20_000, min(400_000, config.available_for_input * 2))
            conv_text = self._format_conversation(conversation, max_chars=max_chars)
            summary_text = self._call_summarizer(
                system_prompt=system_prompt,
                conversation_text=conv_text,
                previous_summary=self._previous_summary(conversation),
                preserved_context=preserved_context,
            )

            if not summary_text or len(summary_text) < 10:
                logger.warning("Summarizer returned empty or too-short summary")
                return CompactionResult(
                    compacted=False,
                    original_tokens=original_tokens,
                    new_tokens=0,
                    error="Empty summary from LLM",
                    new_conversation=conversation,
                )

            # Validate summary doesn't exceed limit
            summary_tokens = token_counter.count_tokens(summary_text)
            if summary_tokens > self.max_summary_tokens:
                logger.warning(
                    f"Summary too long: {summary_tokens} > {self.max_summary_tokens} tokens"
                )
                return self._fallback_compact(
                    conversation,
                    token_counter,
                    config,
                    "Summary exceeded token limit, using pruning",
                    preserved_context,
                )

            # Build new conversation with summary marker
            new_conversation = self._build_compacted_conversation(
                conversation, summary_text
            )

            new_tokens = token_counter.count_conversation(new_conversation).total_tokens

            logger.info(
                f"Successfully compacted: {original_tokens} → {new_tokens} tokens "
                f"({summary_tokens} summary)"
            )

            return CompactionResult(
                compacted=True,
                original_tokens=original_tokens,
                new_tokens=new_tokens,
                summary=summary_text,
                new_conversation=new_conversation,
            )

        except Exception as exc:
            logger.error(f"LLM summarization failed: {exc}")
            return self._fallback_compact(
                conversation, token_counter, config, f"LLM failure: {exc}", preserved_context
            )

    def _format_conversation(self, conversation: list[dict], max_chars: int = 60_000) -> str:
        """Format conversation for summarization prompt.

        Keeps the newest lines that fit in `max_chars`; anything older is covered
        by the previous summary when there is one.
        """
        lines = []
        for msg in conversation:
            kind = msg.get("type")
            if kind == "reasoning" or msg.get("role") == "system" or is_summary(msg):
                continue
            if kind == "function_call":
                lines.append(f"TOOL_CALL: {msg.get('name', 'unknown')} {str(msg.get('arguments', ''))[:300]}")
            elif kind in ("function_call_output", "function_result"):
                lines.append(f"TOOL_RESULT: {item_text(msg)[:500]}")
            elif msg.get("role"):
                lines.append(f"{msg['role'].upper()}: {item_text(msg)[:1000]}")

        kept, used = [], 0
        for line in reversed(lines):
            if used + len(line) + 1 > max_chars:
                kept.append(f"[... {len(lines) - len(kept)} earlier items omitted]")
                break
            kept.append(line)
            used += len(line) + 1
        return "\n".join(reversed(kept))

    @staticmethod
    def _previous_summary(conversation: list[dict]) -> str:
        """Full text of summaries from earlier compactions, oldest first."""
        return "\n\n".join(item_text(m) for m in conversation if is_summary(m))

    def _call_summarizer(
        self, system_prompt: str, conversation_text: str, previous_summary: str = "",
        preserved_context: str = "",
    ) -> str:
        """Call LLM to create structured summary."""
        previous = ""
        if previous_summary:
            previous = f"""
PREVIOUS SUMMARY (from an earlier compaction; the conversation below continues
from it). Merge it into the new summary: keep every fact that still applies, and
update anything the conversation has since changed.
{previous_summary}
"""
        preserved = ""
        if preserved_context:
            preserved = f"""
FACTS EXTRACTED FROM THE FULL CONVERSATION (keep every one that still applies):
{preserved_context}
"""
        prompt = f"""Analyze this coding agent conversation and create a STRUCTURED JSON summary.

SYSTEM: {system_prompt}
{previous}{preserved}
CONVERSATION (last portion):
{conversation_text}

Create a JSON object with these exact keys:
- user_goal: What the user asked the agent to do
- important_instructions: List of still-applicable rules or constraints
- technical_decisions: List of key technical choices made
- files_modified: Dict of {{path: description}}
- code_changes: List of high-level code change descriptions
- test_results: Current test status or "unknown"
- unresolved_errors: List of outstanding issues
- current_task: What is currently being worked on
- next_steps: List of recommended next actions

Return ONLY valid JSON, no other text."""

        try:
            # Same API and reasoning-model parameters as the agent loop: chat
            # completions with max_tokens/temperature fails on reasoning models
            # and on Responses-only providers.
            # Streamed, like the agent loop: some OpenAI-compatible proxies always
            # answer with an event stream, which a non-streaming call cannot parse.
            response = self.client.responses.create(
                model=self.model,
                input=[{"role": "user", "content": prompt}],
                reasoning={"effort": "low"},
                max_output_tokens=self.max_summary_tokens + SUMMARY_REASONING_BUDGET,
                store=False,
                stream=True,
                timeout=SUMMARY_TIMEOUT,
            )
            return _strip_code_fence(_response_text(response))
        except Exception as exc:
            logger.error(f"Summarizer API call failed: {exc}")
            raise

    def _build_compacted_conversation(
        self, conversation: list[dict], summary_text: str
    ) -> list[dict]:
        """Build new conversation with summary replacing old messages."""
        # System prompt, then the summary, then the most recent items verbatim
        # (tool calls and their results included, so in-flight work keeps its data).
        new_conversation = []
        system_msg = _system_message(conversation)
        if system_msg:
            new_conversation.append(system_msg)

        new_conversation.append(
            {
                "role": "user",
                "content": f"{SUMMARY_PREFIX}\n{summary_text}\n[END SUMMARY]\n\nContinuing from where we left off...",
            }
        )
        # The new summary already merges earlier ones; don't keep those twice.
        recent = [m for m in conversation if not is_summary(m)]
        new_conversation.extend(recent_tail(recent, RECENT_ITEMS_KEPT))
        return new_conversation

    def _fallback_compact(
        self,
        conversation: list[dict],
        token_counter: TokenCounter,
        config: ContextConfig,
        reason: str,
        preserved_context: str = "",
    ) -> CompactionResult:
        """Fallback: simple pruning instead of LLM summarization."""
        logger.info(f"Using fallback pruning: {reason}")

        # Drop the oldest items: keep system, any earlier summaries (pruning must
        # not discard what was already condensed), and the most recent 70% (at least 10).
        earlier = [m for m in conversation if is_summary(m)]
        if preserved_context:
            # One summary: earlier summaries (minus their stale extracted facts,
            # recomputed below) plus the facts extracted from this conversation.
            bodies = [_without_preserved_block(_summary_body(m)) for m in earlier]
            body = "\n\n".join([b for b in bodies if b] + [preserved_context])
            summaries = [{"role": "user", "content": f"{SUMMARY_PREFIX}\n{body}\n[END SUMMARY]"}]
        else:
            summaries = [{"role": "user", "content": item_text(m)} for m in earlier]
        rest = [m for m in conversation if not is_summary(m)]
        other_count = sum(1 for m in rest if m.get("role") != "system")
        keep_count = max(10, int(other_count * 0.7))

        new_conversation = []
        system_msg = _system_message(conversation)
        if system_msg:
            new_conversation.append(system_msg)

        new_conversation.extend(summaries)
        new_conversation.extend(recent_tail(rest, keep_count))

        new_tokens = token_counter.count_conversation(new_conversation).total_tokens
        original_tokens = token_counter.count_conversation(conversation).total_tokens

        return CompactionResult(
            compacted=True,
            original_tokens=original_tokens,
            new_tokens=new_tokens,
            summary=f"Fallback pruning applied: {reason}",
            new_conversation=new_conversation,
        )


class CompactionEngine:
    """Orchestrates context compaction process."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        compaction_threshold_percent: float = 0.75,
        config: Optional[ContextConfig] = None,
    ):
        """Initialize compaction engine.

        Args:
            model: Model name for compaction
            api_key: OpenAI API key
            base_url: OpenAI base URL
            compaction_threshold_percent: Trigger compaction at this % of context (0.75 = 75%)
            config: Context limits to use; looked up from `model` when omitted
        """
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self.compaction_threshold_percent = compaction_threshold_percent
        self.preserver = ContextPreserver()
        self._last_compacted_tokens = 0
        self.token_counter = TokenCounter(model)
        self.config = config or get_model_config(model)
        self.summarizer = LLMSummarizer(
            client=self.client,
            model=model,
            max_summary_tokens=min(self.config.max_output_tokens // 2, MAX_SUMMARY_TOKENS),
        )

    @property
    def threshold_tokens(self) -> int:
        return int(self.config.available_for_input * self.compaction_threshold_percent)

    def _preserved_context(self, conversation: list[dict], system_prompt: str, tokens: int) -> str:
        """ContextPreserver's must-keep facts for this conversation, or ""."""
        try:
            analysis = self.preserver.analyze_conversation(
                conversation=preserver_view(conversation),
                system_prompt=system_prompt,
                token_count=tokens,
                context_limit=self.config.available_for_input,
            )
            text = self.preserver.format_preserved_context(analysis.extracted)
        except Exception as exc:  # extraction is best effort
            logger.warning(f"Context preservation failed: {exc}")
            return ""
        # A block with only its markers carries no facts.
        inner = text.replace(PRESERVED_START, "").replace(PRESERVED_END, "").strip()
        return text if inner else ""

    def maybe_compact(
        self,
        conversation: list[dict],
        system_prompt: str,
        force: bool = False,
    ) -> CompactionResult:
        """Compact conversation if approaching token limit.

        Args:
            conversation: Current conversation history
            system_prompt: System instruction text
            force: Compact regardless of the threshold, e.g. after the API
                rejected the request for exceeding the context window

        Returns:
            CompactionResult with potentially compacted conversation
        """
        # Calculate current token usage
        total_tokens = self.token_counter.count_conversation(conversation).total_tokens
        threshold = self.threshold_tokens

        logger.debug(
            f"Token check: {total_tokens} / {self.config.available_for_input} "
            f"(threshold: {threshold})"
        )

        # Check if compaction needed. If the last compaction could not get below
        # the threshold, wait for real growth instead of re-summarizing each request.
        barely_grew = total_tokens < self._last_compacted_tokens + 100
        if not force and (total_tokens <= threshold or barely_grew):
            return CompactionResult(
                compacted=False,
                original_tokens=total_tokens,
                new_tokens=0,
                new_conversation=conversation,
            )

        logger.info(f"Triggering compaction at {total_tokens} tokens (threshold: {threshold})")

        # Attempt compaction
        result = self.summarizer.compact(
            conversation=conversation,
            system_prompt=system_prompt,
            token_counter=self.token_counter,
            config=self.config,
            preserved_context=self._preserved_context(conversation, system_prompt, total_tokens),
        )

        # Make sure the result fits well below the threshold, so it neither
        # overflows the window nor triggers another compaction right away.
        if result.compacted and result.new_conversation:
            budget = int(threshold * FIT_TARGET)
            if result.new_tokens > budget:
                result.new_conversation = fit_to_budget(
                    result.new_conversation, self.token_counter, budget)
                result.new_tokens = self.token_counter.count_conversation(
                    result.new_conversation).total_tokens
            if result.new_tokens > self.config.available_for_input:
                logger.warning(f"Compacted history still exceeds the window: {result.new_tokens} tokens")

        # Verify result before returning
        if result.compacted:
            # Ensure new conversation is valid and shorter
            if not result.new_conversation:
                logger.error("Compaction produced empty conversation")
                return CompactionResult(
                    compacted=False,
                    original_tokens=total_tokens,
                    new_tokens=0,
                    error="Invalid compaction result",
                    new_conversation=conversation,
                )

            if result.new_tokens >= total_tokens:
                logger.warning(
                    f"Compaction ineffective: {result.new_tokens} >= {total_tokens}"
                )
                return CompactionResult(
                    compacted=False,
                    original_tokens=total_tokens,
                    new_tokens=0,
                    error="Compaction not effective",
                    new_conversation=conversation,
                )
            self._last_compacted_tokens = result.new_tokens

        return result

    def close(self):
        """Close API client."""
        self.client.close()
