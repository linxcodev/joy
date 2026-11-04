"""Unit tests for token counter and context configuration."""

import pytest
from joy.token_counter import TokenCounter, TokenStats
from joy.context_config import (
    ContextConfig, get_model_config, calculate_context_usage,
    format_context_config
)


class TestTokenCounter:
    """Test TokenCounter class."""

    def test_token_counter_initialization(self):
        """Test TokenCounter initialization."""
        counter = TokenCounter("gpt-4")
        assert counter.model == "gpt-4"
        # tiktoken availability depends on installation
        assert isinstance(counter.uses_tiktoken, bool)

    def test_token_counter_empty_text(self):
        """Test counting tokens in empty text."""
        counter = TokenCounter()
        assert counter.count_tokens("") == 0

    def test_token_counter_single_character(self):
        """Test minimum token count."""
        counter = TokenCounter()
        result = counter.count_tokens("a")
        assert result >= 1

    def test_token_counter_longer_text(self):
        """Test token counting for longer text."""
        counter = TokenCounter()
        text = "Hello, this is a longer text with multiple words."
        result = counter.count_tokens(text)
        assert result > 0
        # For fallback estimation: ~1 token per 4 chars
        # This text is ~47 chars, so expect ~10-15 tokens
        assert result > 5

    def test_count_system_message(self):
        """Test counting a system message."""
        counter = TokenCounter()
        message = {
            "type": "text",
            "role": "system",
            "content": "You are a helpful assistant."
        }
        tokens = counter.count_message(message)
        assert tokens > 0

    def test_count_user_message(self):
        """Test counting a user message."""
        counter = TokenCounter()
        message = {
            "type": "text",
            "role": "user",
            "content": "Please help me with this task."
        }
        tokens = counter.count_message(message)
        assert tokens > 0

    def test_count_assistant_message(self):
        """Test counting an assistant message."""
        counter = TokenCounter()
        message = {
            "type": "text",
            "role": "assistant",
            "content": "I'll help you with that."
        }
        tokens = counter.count_message(message)
        assert tokens > 0

    def test_count_function_call(self):
        """Test counting a function call."""
        counter = TokenCounter()
        message = {
            "type": "function_call",
            "name": "read",
            "arguments": '{"path": "src/joy/llm.py", "limit": 100}'
        }
        tokens = counter.count_message(message)
        assert tokens > 0

    def test_count_tool_result(self):
        """Test counting a tool result."""
        counter = TokenCounter()
        message = {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": "File contents here..."
        }
        tokens = counter.count_message(message)
        assert tokens > 0

    def test_count_empty_message(self):
        """Test counting an empty message."""
        counter = TokenCounter()
        message = {"type": "text", "role": "user", "content": ""}
        tokens = counter.count_message(message)
        assert tokens > 0  # At least 1 for structure

    def test_count_conversation(self):
        """Test counting a full conversation."""
        counter = TokenCounter()
        conversation = [
            {
                "type": "text",
                "role": "user",
                "content": "Hello"
            },
            {
                "type": "text",
                "role": "assistant",
                "content": "Hi, how can I help?"
            }
        ]
        stats = counter.count_conversation(conversation)
        assert isinstance(stats, TokenStats)
        assert stats.total_tokens > 0
        assert stats.conversation_tokens > 0
        assert stats.system_tokens == 0
        assert stats.tool_tokens == 0

    def test_count_conversation_with_system(self):
        """Test counting conversation that includes system prompt."""
        counter = TokenCounter()
        conversation = [
            {"type": "text", "role": "user", "content": "Hello"}
        ]
        system_prompt = "You are helpful assistant."
        stats = counter.count_conversation_with_system(conversation, system_prompt)
        
        assert stats.system_tokens > 0
        assert stats.total_tokens > stats.system_tokens
        assert not stats.estimated or counter.uses_tiktoken

    def test_count_conversation_with_tools(self):
        """Test counting conversation that includes tool calls."""
        counter = TokenCounter()
        conversation = [
            {"type": "text", "role": "user", "content": "Read a file"},
            {"type": "function_call", "name": "read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "content"}
        ]
        stats = counter.count_conversation(conversation)
        
        assert stats.tool_tokens > 0
        assert stats.total_tokens > stats.tool_tokens

    def test_estimate_response_tokens(self):
        """Test response token budget calculation."""
        counter = TokenCounter("gpt-4")
        conversation = [
            {"type": "text", "role": "user", "content": "Hello"}
        ]
        system_prompt = "You are helpful."
        
        result = counter.estimate_response_tokens(conversation, system_prompt)
        
        assert "input_tokens" in result
        assert "available_output_tokens" in result
        assert "context_window" in result
        assert result["input_tokens"] > 0
        assert result["available_output_tokens"] > 0
        assert "is_approaching_limit" in result

    def test_format_stats(self):
        """Test token stats formatting."""
        counter = TokenCounter()
        stats = TokenStats(
            total_tokens=100,
            system_tokens=10,
            conversation_tokens=70,
            tool_tokens=20,
            estimated=True
        )
        formatted = counter.format_stats(stats)
        assert "Token Usage" in formatted
        assert "100" in formatted
        assert "estimated" in formatted


class TestContextConfig:
    """Test ContextConfig and context configuration."""

    def test_context_config_defaults(self):
        """Test ContextConfig default values."""
        config = ContextConfig()
        assert config.context_window_size == 8192
        assert config.max_output_tokens == 2048
        assert config.safety_margin == 500
        assert config.warning_threshold == 0.8

    def test_context_config_effective_size(self):
        """Test effective context size calculation."""
        config = ContextConfig(context_window_size=8192, safety_margin=500)
        assert config.effective_context_size == 7692

    def test_context_config_available_for_input(self):
        """Test available input tokens calculation."""
        config = ContextConfig(
            context_window_size=8192,
            max_output_tokens=2048,
            safety_margin=500
        )
        # effective = 8192 - 500 = 7692
        # available = 7692 - 2048 = 5644
        assert config.available_for_input == 5644

    def test_context_config_warning_threshold(self):
        """Test warning threshold calculation."""
        config = ContextConfig(
            context_window_size=8192,
            safety_margin=500,
            warning_threshold=0.8
        )
        # effective = 7692, threshold = 7692 * 0.8 = 6153.6
        assert config.warning_threshold_tokens == 6153

    def test_context_config_is_approaching_limit(self):
        """Test approaching limit detection."""
        config = ContextConfig(
            context_window_size=1000,
            safety_margin=100,
            warning_threshold=0.8
        )
        # effective = 900, threshold = 720
        assert not config.is_approaching_limit(500)
        assert config.is_approaching_limit(750)

    def test_get_model_config_exact_match(self):
        """Test getting config with exact model name match."""
        config = get_model_config("gpt-4")
        assert config.context_window_size == 8192

    def test_get_model_config_gpt4_turbo(self):
        """Test getting config for GPT-4 Turbo."""
        config = get_model_config("gpt-4-turbo")
        assert config.context_window_size == 128000

    def test_get_model_config_gpt4_32k(self):
        """Test getting config for GPT-4 32K."""
        config = get_model_config("gpt-4-32k")
        assert config.context_window_size == 32768

    def test_get_model_config_gpt35_turbo(self):
        """Test getting config for GPT-3.5 Turbo."""
        config = get_model_config("gpt-3.5-turbo")
        assert config.context_window_size == 4096

    def test_get_model_config_gpt35_16k(self):
        """Test getting config for GPT-3.5 Turbo 16K."""
        config = get_model_config("gpt-3.5-turbo-16k")
        assert config.context_window_size == 16384

    def test_get_model_config_gpt4o(self):
        """Test getting config for GPT-4o."""
        config = get_model_config("gpt-4o")
        assert config.context_window_size == 128000

    def test_get_model_config_partial_match(self):
        """Test partial model name matching."""
        # Should match pattern even with extra text
        config = get_model_config("gpt-4-turbo-2024-04-09")
        assert config.context_window_size == 128000

    def test_get_model_config_unknown_model(self):
        """Test handling unknown model name."""
        config = get_model_config("unknown-model-xyz")
        # Should return the default, sized for current models (not a 4K window,
        # which made compaction fire on nearly every request)
        assert config.context_window_size == 128000

    def test_get_model_config_case_insensitive(self):
        """Test case-insensitive model name matching."""
        config1 = get_model_config("GPT-4")
        config2 = get_model_config("gpt-4")
        assert config1.context_window_size == config2.context_window_size

    def test_format_context_config(self):
        """Test context config formatting."""
        config = ContextConfig(context_window_size=8192, max_output_tokens=2048)
        formatted = format_context_config(config, "gpt-4")
        
        assert "Context Configuration" in formatted
        assert "8192" in formatted
        assert "2048" in formatted

    def test_calculate_context_usage(self):
        """Test context usage calculation."""
        config = ContextConfig(
            context_window_size=8192,
            max_output_tokens=2048,
            safety_margin=500
        )
        # available = 5644
        
        usage = calculate_context_usage(2000, config)
        
        assert "input_tokens" in usage
        assert "available_tokens" in usage
        assert "usage_percent" in usage
        assert usage["input_tokens"] == 2000
        assert usage["available_tokens"] == 3644  # 5644 - 2000
        assert not usage["is_warning"]
        assert not usage["is_critical"]

    def test_calculate_context_usage_warning(self):
        """Test context usage warning threshold."""
        config = ContextConfig(
            context_window_size=1000,
            max_output_tokens=200,
            safety_margin=100,
            warning_threshold=0.7
        )
        # effective = 900, available = 700, warning = 630
        
        usage = calculate_context_usage(650, config)
        assert usage["is_warning"]
        assert not usage["is_critical"]

    def test_calculate_context_usage_critical(self):
        """Test context usage critical state."""
        config = ContextConfig(
            context_window_size=1000,
            max_output_tokens=200,
            safety_margin=100
        )
        # available = 700
        
        usage = calculate_context_usage(750, config)
        assert usage["is_critical"]

    def test_calculate_context_usage_percentages(self):
        """Test usage percentage calculations."""
        config = ContextConfig(
            context_window_size=1000,
            max_output_tokens=200,
            safety_margin=100
        )
        # available = 700
        
        usage = calculate_context_usage(350, config)
        # 350 / 700 = 50%
        assert 40 < usage["usage_percent"] < 60
        assert 0 <= usage["context_percent"] <= 100


class TestIntegration:
    """Integration tests for token tracking."""

    def test_full_conversation_tracking(self):
        """Test tracking a realistic conversation."""
        counter = TokenCounter("gpt-4")
        config = get_model_config("gpt-4")
        
        conversation = [
            {"type": "text", "role": "user", "content": "Read src/joy/llm.py"},
            {"type": "function_call", "name": "read", "arguments": '{"path": "src/joy/llm.py"}'},
            {"type": "function_call_output", "call_id": "c1", "output": "File content..."},
            {"type": "text", "role": "assistant", "content": "I read the file."},
        ]
        
        system_prompt = "You are Joy, a coding assistant."
        stats = counter.count_conversation_with_system(conversation, system_prompt)
        
        # All components should be counted
        assert stats.system_tokens > 0
        assert stats.tool_tokens > 0
        assert stats.conversation_tokens > 0
        assert stats.total_tokens > 0
        
        # Check if we can fit in context
        assert stats.total_tokens < config.context_window_size

    def test_response_budget_calculation(self):
        """Test response budget calculation for long conversation."""
        counter = TokenCounter("gpt-4-turbo")
        config = get_model_config("gpt-4-turbo")
        
        # Create a longer conversation
        conversation = [
            {"type": "text", "role": "user", "content": f"Message {i}"}
            for i in range(10)
        ]
        
        budget = counter.estimate_response_tokens(
            conversation,
            "You are helpful.",
            max_output_tokens=4096
        )
        
        assert budget["context_window"] == 128000
        assert not budget["is_approaching_limit"]  # Should have plenty of space


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
