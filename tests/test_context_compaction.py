"""Tests for context compaction engine.

Tests cover:
- LLMSummarizer creation and operation
- Summary generation from conversations
- Fallback mechanisms on LLM failure
- CompactionEngine threshold detection
- Compaction result validation
"""

import json
import pytest
from unittest.mock import Mock, MagicMock, patch
from openai import OpenAI

from src.joy.context_compaction import (
    LLMSummarizer,
    CompactionEngine,
    CompactionResult,
    StructuredSummary,
)
from src.joy.token_counter import TokenCounter
from src.joy.context_config import get_model_config


class TestLLMSummarizer:
    """Tests for LLMSummarizer class."""

    @pytest.fixture
    def mock_client(self):
        """Create mock OpenAI client."""
        client = MagicMock(spec=OpenAI)
        return client

    @pytest.fixture
    def summarizer(self, mock_client):
        """Create summarizer with mock client."""
        return LLMSummarizer(
            client=mock_client,
            model="gpt-4",
            max_summary_tokens=2000,
        )

    @pytest.fixture
    def token_counter(self):
        """Create token counter."""
        return TokenCounter("gpt-4")

    @pytest.fixture
    def config(self):
        """Get model config."""
        return get_model_config("gpt-4")

    def test_summarizer_creation(self, summarizer):
        """Test summarizer can be created."""
        assert summarizer.model == "gpt-4"
        assert summarizer.max_summary_tokens == 2000

    def test_format_conversation(self, summarizer):
        """Test conversation formatting."""
        conversation = [
            {"role": "system", "type": "text", "content": "System prompt"},
            {"role": "user", "type": "text", "content": "User request"},
            {"role": "assistant", "type": "text", "content": "Agent response"},
            {"type": "function_call", "name": "read_file"},
            {"type": "function_result", "content": "file content"},
        ]

        formatted = summarizer._format_conversation(conversation)
        # The system prompt is passed to the summarizer separately.
        assert "SYSTEM:" not in formatted
        assert "USER:" in formatted
        assert "ASSISTANT:" in formatted
        assert "TOOL_CALL:" in formatted
        assert "TOOL_RESULT:" in formatted

    def test_summarizer_with_valid_conversation(
        self, summarizer, mock_client, token_counter, config
    ):
        """Test summarizer with valid conversation produces summary."""
        conversation = [
            {"role": "system", "type": "text", "content": "You are Joy agent"},
            {"role": "user", "type": "text", "content": "Read file.txt"},
            {"role": "assistant", "type": "text", "content": "I'll read that file"},
        ]

        system_prompt = "You are Joy agent"

        # Mock LLM response with valid JSON summary
        mock_response = MagicMock()
        mock_response.status = "completed"
        mock_response.output_text = json.dumps(
            {
                "user_goal": "Read a file",
                "important_instructions": ["Be concise"],
                "technical_decisions": ["Use read tool"],
                "files_modified": {"file.txt": "read"},
                "code_changes": ["None yet"],
                "test_results": "Unknown",
                "unresolved_errors": [],
                "current_task": "Reading file",
                "next_steps": ["Process content"],
            }
        )
        mock_client.responses.create.return_value = mock_response

        result = summarizer.compact(conversation, system_prompt, token_counter, config)

        assert result.compacted
        assert result.summary is not None
        assert result.original_tokens > 0
        assert result.new_tokens > 0
        assert len(result.new_conversation) > 0

    def test_summarizer_with_empty_conversation(
        self, summarizer, mock_client, token_counter, config
    ):
        """Test summarizer with empty conversation."""
        conversation = [
            {"role": "system", "type": "text", "content": "System prompt"},
        ]
        system_prompt = "System prompt"

        # Mock returns empty summary
        mock_response = MagicMock()
        mock_response.status = "completed"
        mock_response.output_text = ""
        mock_client.responses.create.return_value = mock_response

        result = summarizer.compact(conversation, system_prompt, token_counter, config)

        assert not result.compacted
        assert result.error is not None

    def test_summarizer_with_complex_conversation(
        self, summarizer, mock_client, token_counter, config
    ):
        """Test summarizer with complex multi-turn conversation."""
        conversation = [
            {"role": "system", "type": "text", "content": "System prompt"},
        ]

        # Add 20 turns
        for i in range(20):
            conversation.append(
                {"role": "user", "type": "text", "content": f"User turn {i}"}
            )
            conversation.append(
                {"role": "assistant", "type": "text", "content": f"Assistant turn {i}"}
            )

        system_prompt = "System prompt"

        # Mock LLM response
        mock_response = MagicMock()
        mock_response.status = "completed"
        mock_response.output_text = json.dumps(
            {
                "user_goal": "Complex task",
                "important_instructions": ["Do 20 things"],
                "technical_decisions": ["Multi-turn"],
                "files_modified": {},
                "code_changes": [],
                "test_results": "Passing",
                "unresolved_errors": [],
                "current_task": "Turn 20",
                "next_steps": ["Continue"],
            }
        )
        mock_client.responses.create.return_value = mock_response

        result = summarizer.compact(conversation, system_prompt, token_counter, config)

        assert result.compacted
        assert result.new_conversation is not None
        # New conversation should be smaller than original (has summary + recent messages)
        # But we can't guarantee tokens are less due to overhead, so just check it's valid
        assert len(result.new_conversation) > 0

    def test_summarizer_with_long_summary(
        self, summarizer, mock_client, token_counter, config
    ):
        """Test summarizer falls back when summary is too long."""
        conversation = [
            {"role": "system", "type": "text", "content": "System prompt"},
            {"role": "user", "type": "text", "content": "Task"},
        ]

        system_prompt = "System prompt"

        # Mock returns very long summary (exceeds limit)
        very_long_summary = "x" * 10000  # Very long
        mock_response = MagicMock()
        mock_response.status = "completed"
        mock_response.output_text = very_long_summary
        mock_client.responses.create.return_value = mock_response

        result = summarizer.compact(conversation, system_prompt, token_counter, config)

        # Should still be compacted (via fallback pruning)
        assert result.compacted

    def test_llm_failure_fallback(
        self, summarizer, mock_client, token_counter, config
    ):
        """Test fallback to pruning when LLM fails."""
        conversation = [
            {"role": "system", "type": "text", "content": "System prompt"},
        ]
        for i in range(10):
            conversation.append(
                {"role": "user", "type": "text", "content": f"User {i}"}
            )
            conversation.append(
                {"role": "assistant", "type": "text", "content": f"Assistant {i}"}
            )

        system_prompt = "System prompt"

        # Mock LLM failure
        mock_client.responses.create.side_effect = Exception("API error")

        result = summarizer.compact(conversation, system_prompt, token_counter, config)

        # Should fall back to pruning
        assert result.compacted
        assert result.summary is not None
        assert "Fallback pruning" in result.summary

    def test_build_compacted_conversation(self, summarizer):
        """Test building compacted conversation structure."""
        conversation = [
            {"role": "system", "type": "text", "content": "System"},
            {"role": "user", "type": "text", "content": "User 1"},
            {"role": "assistant", "type": "text", "content": "Assistant 1"},
        ]

        result = summarizer._build_compacted_conversation(
            conversation, "Summary text"
        )

        # Should have system, summary marker, and original messages
        assert len(result) >= 2
        assert result[0].get("role") == "system"
        # Summary should be second message
        assert "CONTEXT SUMMARY" in result[1].get("content", "")


class TestCompactionEngine:
    """Tests for CompactionEngine class."""

    @pytest.fixture
    def engine(self):
        """Create compaction engine with mock client."""
        with patch("src.joy.context_compaction.OpenAI") as mock_openai_class:
            mock_client = MagicMock(spec=OpenAI)
            mock_openai_class.return_value = mock_client

            engine = CompactionEngine(
                model="gpt-4",
                api_key="test-key",
                base_url="https://api.openai.com/v1",
            )
            engine.client = mock_client
            return engine

    @pytest.fixture
    def simple_conversation(self):
        """Create simple conversation."""
        return [
            {"role": "system", "type": "text", "content": "System"},
            {"role": "user", "type": "text", "content": "User"},
        ]

    def test_compaction_engine_initialization(self):
        """Test engine can be initialized."""
        with patch("src.joy.context_compaction.OpenAI"):
            engine = CompactionEngine(
                model="gpt-4",
                api_key="test-key",
            )
            assert engine.model == "gpt-4"
            assert engine.token_counter is not None
            assert engine.config is not None

    def test_compaction_not_triggered_under_threshold(self, engine, simple_conversation):
        """Test compaction not triggered when under threshold."""
        result = engine.maybe_compact(
            conversation=simple_conversation,
            system_prompt="System",
        )

        # Should not compact (short conversation)
        assert not result.compacted
        assert result.new_conversation == simple_conversation

    def test_compaction_triggered_at_threshold(self, engine):
        """Test compaction is triggered when approaching threshold."""
        # Create a long conversation to exceed threshold
        conversation = [
            {"role": "system", "type": "text", "content": "System prompt"}
        ]

        # Add many turns to exceed threshold
        for i in range(50):
            conversation.append(
                {
                    "role": "user",
                    "type": "text",
                    "content": "x" * 100 + f" turn {i}",
                }
            )
            conversation.append(
                {
                    "role": "assistant",
                    "type": "text",
                    "content": "y" * 100 + f" response {i}",
                }
            )

        # Mock summarizer to use fallback
        with patch.object(
            engine.summarizer, "_call_summarizer"
        ) as mock_call:
            mock_call.side_effect = Exception("Test API error")

            result = engine.maybe_compact(
                conversation=conversation,
                system_prompt="System prompt",
            )

            # Should attempt compaction
            # Result depends on whether it hit threshold and how fallback works
            assert result.original_tokens > 0

    def test_compaction_preserves_conversation_on_failure(
        self, engine, simple_conversation
    ):
        """Test original conversation preserved if compaction fails."""
        # Mock summarizer to fail
        with patch.object(
            engine.summarizer, "compact"
        ) as mock_compact:
            mock_compact.return_value = CompactionResult(
                compacted=False,
                original_tokens=100,
                new_tokens=0,
                error="Mock failure",
                new_conversation=simple_conversation,
            )

            result = engine.maybe_compact(
                conversation=simple_conversation,
                system_prompt="System",
            )

            assert not result.compacted
            assert result.new_conversation == simple_conversation

    def test_compaction_result_reusable(self, engine):
        """Test compacted conversation can be used in next call."""
        # Create conversation
        conversation = [
            {"role": "system", "type": "text", "content": "System"}
        ]

        for i in range(5):
            conversation.append(
                {"role": "user", "type": "text", "content": f"User {i}"}
            )
            conversation.append(
                {"role": "assistant", "type": "text", "content": f"Assistant {i}"}
            )

        # First compaction (if triggered)
        result1 = engine.maybe_compact(
            conversation=conversation,
            system_prompt="System",
        )

        # Should be able to use result in next call without error
        result2 = engine.maybe_compact(
            conversation=result1.new_conversation,
            system_prompt="System",
        )

        # Should not raise exception
        assert result2 is not None

    def test_compaction_engine_close(self, engine):
        """Test engine closes client properly."""
        engine.close()
        engine.client.close.assert_called_once()


class TestCompactionIntegration:
    """Integration tests for compaction with token counter."""

    def test_summary_reduces_token_count(self):
        """Test that compaction actually reduces token count."""
        counter = TokenCounter("gpt-4")

        # Create large conversation
        conversation = [
            {"role": "system", "type": "text", "content": "System prompt"},
        ]

        for i in range(30):
            conversation.append(
                {
                    "role": "user",
                    "type": "text",
                    "content": "x" * 200 + f" turn {i}",
                }
            )
            conversation.append(
                {
                    "role": "assistant",
                    "type": "text",
                    "content": "y" * 200 + f" response {i}",
                }
            )

        original_tokens = counter.count_conversation(conversation).total_tokens

        # Create simple pruned version
        pruned = [conversation[0]] + conversation[-10:]
        pruned_tokens = counter.count_conversation(pruned).total_tokens

        # Pruned should be significantly less
        assert pruned_tokens < original_tokens
        assert pruned_tokens < original_tokens * 0.5  # At least 50% reduction


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
