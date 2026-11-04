"""Token counting for conversation history and context usage tracking.

This module provides:
- TokenCounter: Estimates or counts tokens using tiktoken or fallback estimation
- Per-message token calculation (system, user, assistant, tool calls, results)
- Caching to avoid repeated calculations
- Support for all message formats used by Joy Agent
"""

import hashlib
from dataclasses import dataclass
from typing import Optional

# Try to import tiktoken for accurate token counting
try:
    import tiktoken
    HAS_TIKTOKEN = True
except ImportError:
    HAS_TIKTOKEN = False


def _load_encoding(model: str):
    """tiktoken encoding for `model`, or None to fall back to estimation.

    Models tiktoken does not know get o200k_base, the encoding of current OpenAI
    models. Loading can also fail offline (the encoding file is downloaded on
    first use); estimation is the fallback then.
    """
    try:
        return tiktoken.encoding_for_model(model.removeprefix("openai:"))
    except KeyError:
        pass
    except Exception:
        return None
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


def item_text(item: dict) -> str:
    """Plain text of a conversation item in any format Joy produces.

    Handles {"role", "content": str}, Responses API message items whose content is
    a list of parts, function_call_output items, and reasoning summaries.
    """
    if item.get("type") == "reasoning":
        return "".join(str(s.get("text", "")) for s in item.get("summary", []) if isinstance(s, dict))
    content = item.get("content", item.get("output", ""))
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part)
                       for part in content)
    return "" if content is None else str(content)


@dataclass
class TokenStats:
    """Statistics about token usage in a conversation."""
    total_tokens: int
    system_tokens: int
    conversation_tokens: int
    tool_tokens: int
    estimated: bool  # True if using fallback estimation


class TokenCounter:
    """Count tokens in conversation history with caching support.
    
    Uses tiktoken for accurate OpenAI token counting if available, falls back
    to character-based estimation otherwise.
    """

    def __init__(self, model: str = "gpt-4"):
        """Initialize token counter for a specific model.
        
        Args:
            model: OpenAI model name (e.g., "gpt-4", "gpt-4-turbo", "gpt-3.5-turbo")
        """
        self.model = model
        self._encoding = None
        self._use_tiktoken = False
        self._cache_key = None
        self._cached_stats = None

        if HAS_TIKTOKEN:
            self._encoding = _load_encoding(model)
            self._use_tiktoken = self._encoding is not None

    @property
    def uses_tiktoken(self) -> bool:
        """Whether accurate token counting via tiktoken is available."""
        return self._use_tiktoken

    def encode(self, text: str) -> list[int]:
        """Encode text to tokens using tiktoken or return empty list for fallback."""
        if not self._use_tiktoken or not self._encoding:
            return []
        try:
            return self._encoding.encode(text)
        except Exception:
            return []

    def count_tokens(self, text: str) -> int:
        """Count tokens in a text string.
        
        Args:
            text: The text to count tokens for
            
        Returns:
            Number of tokens (estimated if tiktoken unavailable)
        """
        if not text:
            return 0

        if self._use_tiktoken and self._encoding:
            try:
                return len(self._encoding.encode(text))
            except Exception:
                pass

        # Fallback: character-based estimation
        # Rough approximation: 1 token ≈ 4 characters for English text
        return max(1, len(text) // 4)

    def count_message(self, message: dict) -> int:
        """Count tokens in a single message.
        
        Handles various message formats used by Joy:
        - System messages: {"type": "text", "role": "system", "content": "..."}
        - User messages: {"type": "text", "role": "user", "content": "..."}
        - Assistant messages: {"type": "text", "role": "assistant", "content": "..."}
        - Tool calls: {"type": "function_call", "name": "...", "arguments": "..."}
        - Tool results: {"type": "function_call_output", "call_id": "...", "output": "..."}
        
        Args:
            message: A message dict from conversation history
            
        Returns:
            Estimated token count for the message
        """
        message_type = message.get("type", "")
        tokens = 0

        if message_type == "text":
            # Text message: role + content
            role = message.get("role", "")
            content = message.get("content", "")
            # Rough: role adds ~1-2 tokens, content is counted
            tokens = 2 + self.count_tokens(content)

        elif message_type == "function_call":
            # Tool call: name + arguments
            name = message.get("name", "")
            arguments = message.get("arguments", "")
            # Function call overhead: ~4 tokens for structure
            tokens = 4 + self.count_tokens(name) + self.count_tokens(str(arguments))

        elif message_type == "function_call_output":
            # Tool result: output only
            output = message.get("output", "")
            # Result overhead: ~2 tokens
            tokens = 2 + self.count_tokens(str(output))

        else:
            # {"role", "content"} messages and Responses API items, whose content
            # may be a list of parts; reasoning items carry a summary list.
            text = item_text(message)
            tokens = self.count_tokens(text)
            if message.get("role"):
                tokens += 2  # role overhead, as for "text" messages

        return max(1, tokens)

    def count_conversation(self, conversation: list[dict], include_system: bool = True) -> TokenStats:
        """Count all tokens in a conversation history.
        
        Args:
            conversation: List of message dicts from agent conversation
            include_system: Whether to count system messages (usually True)
            
        Returns:
            TokenStats with breakdown and totals
        """
        system_tokens = 0
        conversation_tokens = 0
        tool_tokens = 0

        for message in conversation:
            msg_type = message.get("type", "")
            role = message.get("role", "")
            tokens = self.count_message(message)

            if msg_type == "text" and role == "system" and include_system:
                system_tokens += tokens
            elif msg_type in ("function_call", "function_call_output"):
                tool_tokens += tokens
            else:
                conversation_tokens += tokens

        total = system_tokens + conversation_tokens + tool_tokens

        return TokenStats(
            total_tokens=total,
            system_tokens=system_tokens,
            conversation_tokens=conversation_tokens,
            tool_tokens=tool_tokens,
            estimated=not self._use_tiktoken
        )

    def count_conversation_with_system(self, conversation: list[dict], 
                                       system_prompt: str) -> TokenStats:
        """Count tokens for a full conversation with system prompt.
        
        Args:
            conversation: List of message dicts
            system_prompt: System instruction text
            
        Returns:
            TokenStats with breakdown including system prompt
        """
        system_tokens = self.count_tokens(system_prompt) + 2  # +2 for role/overhead
        
        # Count rest of conversation
        rest_stats = self.count_conversation(conversation, include_system=False)
        
        return TokenStats(
            total_tokens=system_tokens + rest_stats.total_tokens,
            system_tokens=system_tokens,
            conversation_tokens=rest_stats.conversation_tokens,
            tool_tokens=rest_stats.tool_tokens,
            estimated=not self._use_tiktoken
        )

    def estimate_response_tokens(self, conversation: list[dict], 
                                 system_prompt: str,
                                 max_output_tokens: int = 4096) -> dict:
        """Estimate remaining budget for model response.
        
        Calculates:
        - Input tokens (conversation + system)
        - Available tokens for output
        - Whether we're approaching limits
        
        Args:
            conversation: Current conversation history
            system_prompt: System instruction
            max_output_tokens: Maximum output tokens allowed
            
        Returns:
            Dict with input_tokens, available_output_tokens, and is_approaching_limit
        """
        stats = self.count_conversation_with_system(conversation, system_prompt)
        
        context_window = self._estimate_context_window()

        input_tokens = stats.total_tokens
        available_output = max_output_tokens
        remaining_context = context_window - input_tokens - available_output
        
        return {
            "input_tokens": input_tokens,
            "system_tokens": stats.system_tokens,
            "conversation_tokens": stats.conversation_tokens,
            "tool_tokens": stats.tool_tokens,
            "reserved_output_tokens": available_output,
            # What the model can still write: the reserve, or less when input crowds it out.
            "available_output_tokens": max(0, min(available_output, context_window - input_tokens)),
            "context_window": context_window,
            "remaining_context": remaining_context,
            "is_approaching_limit": remaining_context < (context_window * 0.1),  # 10% threshold
            "estimated": stats.estimated
        }

    def _estimate_context_window(self) -> int:
        """Context window size for the model, from the shared model table."""
        from joy.context_config import get_model_config  # context_config never imports this module

        return get_model_config(self.model).context_window_size

    def format_stats(self, stats: TokenStats, width: int = 50) -> str:
        """Format token stats for display.
        
        Args:
            stats: TokenStats from count_conversation
            width: Width for formatting
            
        Returns:
            Formatted string for display
        """
        estimated_label = " (estimated)" if stats.estimated else " (accurate)"
        header = f"Token Usage{estimated_label}"
        
        lines = [
            header,
            "─" * width,
            f"  System:       {stats.system_tokens:5d} tokens",
            f"  Conversation: {stats.conversation_tokens:5d} tokens",
            f"  Tools:        {stats.tool_tokens:5d} tokens",
            "─" * width,
            f"  Total:        {stats.total_tokens:5d} tokens",
        ]
        
        return "\n".join(lines)
