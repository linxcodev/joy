"""Configuration for model context windows and compaction thresholds.

This module provides:
- MODEL_CONFIGS: Context window sizes for various OpenAI models
- ContextConfig: Configuration for context management
- get_model_config(): Retrieve config for a specific model
- Helper functions for context calculations
"""

import logging
import os
from dataclasses import dataclass, field, replace
from typing import Optional


@dataclass
class ContextConfig:
    """Configuration for context window management and token tracking.
    
    Attributes:
        context_window_size: Total tokens available in model's context window
        max_output_tokens: Maximum tokens reserved for model output
        compaction_threshold: Token count threshold to trigger compaction (0 = disabled)
        safety_margin: Buffer tokens below max to avoid API rejection
        warning_threshold: Percentage of context to trigger warning (0.8 = 80%)
    """
    context_window_size: int = 8192
    max_output_tokens: int = 2048
    compaction_threshold: int = 0  # Disabled by default for this implementation
    safety_margin: int = 500
    warning_threshold: float = 0.8

    @property
    def effective_context_size(self) -> int:
        """Actual usable context after subtracting safety margin."""
        return max(0, self.context_window_size - self.safety_margin)

    @property
    def available_for_input(self) -> int:
        """Available tokens for input (conversation + system)."""
        return max(0, self.effective_context_size - self.max_output_tokens)

    @property
    def warning_threshold_tokens(self) -> int:
        """Token count at which to warn about approaching limit."""
        return int(self.effective_context_size * self.warning_threshold)

    def is_approaching_limit(self, input_tokens: int) -> bool:
        """Check if input tokens are approaching the warning threshold.
        
        Args:
            input_tokens: Current input token count
            
        Returns:
            True if approaching threshold
        """
        return input_tokens > self.warning_threshold_tokens


# Model configurations mapping model names to context window sizes
# Based on OpenAI's official specifications
MODEL_CONFIGS = {
    # GPT-4 variants
    "gpt-4": ContextConfig(context_window_size=8192, max_output_tokens=2048),
    "gpt-4-turbo": ContextConfig(context_window_size=128000, max_output_tokens=4096),
    "gpt-4-turbo-preview": ContextConfig(context_window_size=128000, max_output_tokens=4096),
    "gpt-4-turbo-2024-04-09": ContextConfig(context_window_size=128000, max_output_tokens=4096),
    "gpt-4-0125-preview": ContextConfig(context_window_size=128000, max_output_tokens=4096),
    "gpt-4-1106-preview": ContextConfig(context_window_size=128000, max_output_tokens=4096),
    "gpt-4-vision": ContextConfig(context_window_size=128000, max_output_tokens=4096),
    "gpt-4-vision-preview": ContextConfig(context_window_size=128000, max_output_tokens=4096),
    "gpt-4-32k": ContextConfig(context_window_size=32768, max_output_tokens=4096),
    "gpt-4-32k-0314": ContextConfig(context_window_size=32768, max_output_tokens=4096),
    "gpt-4-0314": ContextConfig(context_window_size=8192, max_output_tokens=2048),
    "gpt-4-0613": ContextConfig(context_window_size=8192, max_output_tokens=2048),
    
    # GPT-5 family (reasoning models; Joy's default model is one of these)
    "gpt-5": ContextConfig(context_window_size=400000, max_output_tokens=128000),

    # GPT-4.1 family
    "gpt-4.1": ContextConfig(context_window_size=1047576, max_output_tokens=32768),

    # o-series reasoning models
    "o-series": ContextConfig(context_window_size=200000, max_output_tokens=100000),

    # GPT-4o (latest multimodal model)
    "gpt-4o": ContextConfig(context_window_size=128000, max_output_tokens=4096),
    "gpt-4o-2024-05-13": ContextConfig(context_window_size=128000, max_output_tokens=4096),
    "gpt-4o-2024-08-06": ContextConfig(context_window_size=128000, max_output_tokens=4096),
    
    # GPT-3.5-turbo variants
    "gpt-3.5-turbo": ContextConfig(context_window_size=4096, max_output_tokens=2048),
    "gpt-3.5-turbo-16k": ContextConfig(context_window_size=16384, max_output_tokens=4096),
    "gpt-3.5-turbo-0613": ContextConfig(context_window_size=4096, max_output_tokens=2048),
    "gpt-3.5-turbo-1106": ContextConfig(context_window_size=16384, max_output_tokens=4096),
    "gpt-3.5-turbo-0125": ContextConfig(context_window_size=16384, max_output_tokens=4096),
    
    # Unknown models: current models have at least this much. A tiny default
    # made compaction fire on nearly every request; set JOY_CONTEXT_WINDOW for
    # a model with a smaller window.
    "default": ContextConfig(context_window_size=128000, max_output_tokens=16384),
}


def get_model_config(model_name: str) -> ContextConfig:
    """Get context configuration for a model.
    
    Tries exact match first, then partial matching based on model name patterns.
    Falls back to conservative default if no match found.
    
    Args:
        model_name: OpenAI model identifier (e.g., "gpt-4-turbo")
        
    Returns:
        ContextConfig for the model
    """
    return _with_env_overrides(_lookup_model_config(model_name))


def _env_int(name: str) -> Optional[int]:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number <= 0:
        logging.getLogger(__name__).warning(f"Ignoring invalid {name}: {value!r}")
        return None
    return number


def _with_env_overrides(config: ContextConfig) -> ContextConfig:
    """Apply JOY_CONTEXT_WINDOW / JOY_MAX_OUTPUT_TOKENS when set."""
    window = _env_int("JOY_CONTEXT_WINDOW")
    output = _env_int("JOY_MAX_OUTPUT_TOKENS")
    if window is None and output is None:
        return config
    window = window or config.context_window_size
    output = output or config.max_output_tokens
    # Never let the output reserve swallow the window.
    output = min(output, window // 2)
    return replace(config, context_window_size=window, max_output_tokens=output)


def _lookup_model_config(model_name: str) -> ContextConfig:
    model_lower = model_name.lower().strip().removeprefix("openai:")

    # Try exact match first
    if model_lower in MODEL_CONFIGS:
        return MODEL_CONFIGS[model_lower]

    # Try partial matches based on common patterns
    if "gpt-5" in model_lower:
        return MODEL_CONFIGS["gpt-5"]
    elif "gpt-4.1" in model_lower:  # before "gpt-4", which it contains
        return MODEL_CONFIGS["gpt-4.1"]
    elif model_lower.split("-")[0] in ("o1", "o3", "o4"):
        return MODEL_CONFIGS["o-series"]
    elif "gpt-4o" in model_lower:
        return MODEL_CONFIGS["gpt-4o"]
    elif "gpt-4-turbo" in model_lower or "gpt-4-0125" in model_lower:
        return MODEL_CONFIGS["gpt-4-turbo"]
    elif "gpt-4-32k" in model_lower:
        return MODEL_CONFIGS["gpt-4-32k"]
    elif "gpt-4" in model_lower:
        return MODEL_CONFIGS["gpt-4"]
    elif "gpt-3.5-turbo-16k" in model_lower or "gpt-3.5-turbo-1106" in model_lower:
        return MODEL_CONFIGS["gpt-3.5-turbo-16k"]
    elif "gpt-3.5-turbo" in model_lower:
        return MODEL_CONFIGS["gpt-3.5-turbo"]
    
    # Default fallback
    return MODEL_CONFIGS["default"]


def format_context_config(config: ContextConfig, model_name: str = "unknown") -> str:
    """Format context config for display.
    
    Args:
        config: ContextConfig to display
        model_name: Model name for header
        
    Returns:
        Formatted string for display
    """
    lines = [
        f"Context Configuration: {model_name}",
        "─" * 50,
        f"  Context Window:      {config.context_window_size:6d} tokens",
        f"  Max Output:          {config.max_output_tokens:6d} tokens",
        f"  Safety Margin:       {config.safety_margin:6d} tokens",
        f"  Effective Size:      {config.effective_context_size:6d} tokens",
        f"  Available for Input: {config.available_for_input:6d} tokens",
        f"  Warning Threshold:   {config.warning_threshold_tokens:6d} tokens ({config.warning_threshold*100:.0f}%)",
        "─" * 50,
    ]
    return "\n".join(lines)


def calculate_context_usage(input_tokens: int, config: ContextConfig) -> dict:
    """Calculate current context usage and status.
    
    Args:
        input_tokens: Current input token count
        config: ContextConfig for the model
        
    Returns:
        Dict with usage percentages and status flags
    """
    effective_size = config.effective_context_size
    available = config.available_for_input
    
    usage_percent = (input_tokens / available * 100) if available > 0 else 100
    context_percent = (input_tokens / effective_size * 100) if effective_size > 0 else 100
    
    return {
        "input_tokens": input_tokens,
        "available_tokens": max(0, available - input_tokens),
        "usage_percent": min(100, usage_percent),
        "context_percent": min(100, context_percent),
        "is_warning": input_tokens > config.warning_threshold_tokens,
        "is_critical": input_tokens > available,
        "tokens_until_warning": max(0, config.warning_threshold_tokens - input_tokens),
        "tokens_until_limit": max(0, available - input_tokens),
    }
