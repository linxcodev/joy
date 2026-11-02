"""Task delegation tool for main agent to delegate work to sub-agents.

This module provides:
- DelegationConfig: Configure sub-agent behavior and limits
- delegate(): Tool function registered with Joy's tool registry
- DelegationContext: Track delegation depth and prevent infinite recursion
- DelegationManager: Manage sub-agent lifecycle and enforce limits
"""

import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from joy.agent_base import AgentConfig, AgentStatus, SubAgentRunner


# Global context for tracking delegation depth per request
_delegation_lock = threading.Lock()
_delegation_stack = threading.local()


class DelegationError(Exception):
    """Delegation-specific error for invalid operations."""


@dataclass
class DelegationConfig:
    """Configuration for delegating a task to a sub-agent."""
    
    # Task specification
    task: str
    """The task instruction to delegate."""
    
    system_instruction: Optional[str] = None
    """System prompt for the sub-agent. If None, inherits from main agent."""
    
    # Allowed tools for sub-agent (must be subset of: read, write, edit, bash)
    allowed_tools: list[str] = field(default_factory=lambda: ["read", "write", "edit", "bash"])
    """Tools the sub-agent can use."""
    
    # Context to provide (avoid sending entire conversation history)
    context: Optional[str] = None
    """Relevant context/docs for the task (not full conversation history)."""
    
    # Sub-agent iteration limits
    max_requests: int = 8
    """Maximum LLM requests the sub-agent can make."""
    
    max_tool_calls: int = 4
    """Maximum tool calls per LLM response."""
    
    timeout_seconds: Optional[float] = None
    """Timeout for sub-agent execution."""
    
    # Delegation limits (prevent infinite recursion)
    max_delegation_depth: int = 2
    """Maximum levels of nested delegation (0 = no delegations allowed)."""
    
    max_delegations_per_request: int = 3
    """Maximum delegations allowed in a single top-level request."""


@dataclass
class DelegationResult:
    """Result from delegating a task to a sub-agent."""
    
    status: str
    """Status: 'success', 'failed', 'timeout', or 'rejected'."""
    
    output: str = ""
    """Sub-agent's output/answer."""
    
    error: Optional[str] = None
    """Error message if status is 'failed' or 'rejected'."""
    
    sub_agent_name: str = "sub_agent"
    """Name of the sub-agent that executed the task."""
    
    total_requests: int = 0
    """Number of LLM requests made by sub-agent."""
    
    total_tool_calls: int = 0
    """Number of tool calls executed by sub-agent."""
    
    execution_time: float = 0.0
    """Wall-clock execution time in seconds."""


def format_delegation_result(result_json: str) -> str:
    """Format delegation result JSON for display in conversation.
    
    Takes the JSON result from delegate() tool and returns a formatted
    string suitable for display to the user and continuation by the
    main agent.
    
    Args:
        result_json: JSON string from delegate() tool
        
    Returns:
        Formatted result string for conversation
    """
    try:
        result = json.loads(result_json)
    except (json.JSONDecodeError, TypeError) as e:
        return f"❌ Error parsing delegation result: {e}\n\nRaw: {result_json}"
    
    status = result.get("status", "unknown")
    success = status == "success"
    icon = "✓" if success else "✗"
    
    # Format header
    header = f"{'═' * 50}\n{icon} Delegation Result\n{'═' * 50}"
    
    # Format metadata
    sub_agent = result.get("sub_agent_name", "Unknown")
    duration = result.get("execution_time", 0)
    requests = result.get("total_requests", 0)
    tool_calls = result.get("total_tool_calls", 0)
    
    metadata = f"""
Sub-agent: {sub_agent}
Status: {status.upper()}
Duration: {duration:.2f}s
Requests: {requests}
Tool calls: {tool_calls}
"""
    
    # Format output or error
    if success:
        content = f"""
Result:
{'-' * 50}
{result.get('output', '(no output)')}
{'-' * 50}"""
    else:
        error_msg = result.get("error", result.get("output", "(unknown error)"))
        content = f"""
Error:
{'-' * 50}
{error_msg}
{'-' * 50}"""
    
    footer = f"{'═' * 50}"
    
    return f"{header}{metadata}{content}\n{footer}"


class DelegationContext:
    """Per-request context for tracking delegation depth and count."""
    
    def __init__(self):
        self.depth = 0
        """Current delegation depth (0 = main agent level)."""
        
        self.count = 0
        """Total delegations initiated at top level."""
    
    def increment_depth(self) -> None:
        """Increase nesting depth."""
        self.depth += 1
    
    def decrement_depth(self) -> None:
        """Decrease nesting depth."""
        self.depth -= 1
    
    def increment_count(self) -> None:
        """Increment total delegation count."""
        self.count += 1
    
    @staticmethod
    def get_current() -> "DelegationContext":
        """Get or create the current thread's delegation context."""
        if not hasattr(_delegation_stack, "context"):
            _delegation_stack.context = DelegationContext()
        return _delegation_stack.context
    
    @staticmethod
    def clear() -> None:
        """Clear the current thread's delegation context."""
        if hasattr(_delegation_stack, "context"):
            delattr(_delegation_stack, "context")


class DelegationManager:
    """Manages sub-agent creation, execution, and lifecycle."""
    
    @staticmethod
    def validate_config(config: DelegationConfig) -> Optional[str]:
        """Validate delegation config. Returns error message or None."""
        # Check tool allowlist
        valid_tools = {"read", "write", "edit", "bash"}
        for tool in config.allowed_tools:
            if tool not in valid_tools:
                return f"Invalid tool '{tool}'. Must be one of: {', '.join(valid_tools)}"
        
        # Check iteration limits
        if config.max_requests < 1 or config.max_requests > 32:
            return "max_requests must be between 1 and 32"
        
        if config.max_tool_calls < 1 or config.max_tool_calls > 16:
            return "max_tool_calls must be between 1 and 16"
        
        # Check delegation limits
        if config.max_delegation_depth < 0 or config.max_delegation_depth > 3:
            return "max_delegation_depth must be between 0 and 3"
        
        if config.max_delegations_per_request < 1 or config.max_delegations_per_request > 10:
            return "max_delegations_per_request must be between 1 and 10"
        
        return None
    
    @staticmethod
    def check_delegation_limits(config: DelegationConfig) -> Optional[str]:
        """Check if delegation is allowed based on current context. Returns error or None."""
        ctx = DelegationContext.get_current()
        
        # Check depth limit
        if ctx.depth >= config.max_delegation_depth:
            return (f"Delegation depth limit ({config.max_delegation_depth}) reached. "
                   f"Cannot delegate from depth {ctx.depth}.")
        
        # Check count limit
        if ctx.count >= config.max_delegations_per_request:
            return (f"Delegation count limit ({config.max_delegations_per_request}) reached. "
                   f"Already delegated {ctx.count} tasks.")
        
        return None
    
    @staticmethod
    def create_sub_agent(config: DelegationConfig) -> tuple[SubAgentRunner, str]:
        """Create a sub-agent from delegation config.
        
        Returns (agent, error_message). If error_message is not None, agent is None.
        """
        # Build system instruction
        if config.system_instruction:
            system_instr = config.system_instruction
        else:
            system_instr = (
                "You are a sub-agent delegated a specific task. "
                "Complete the task efficiently and report your results. "
                "If you need context, it will be provided. "
                "Stay focused on the assigned task only."
            )
        
        # Create agent config
        agent_config = AgentConfig(
            name="delegated_sub_agent",
            description="Sub-agent for delegated task",
            system_instruction=system_instr,
            allowed_tools=config.allowed_tools,
            max_requests=config.max_requests,
            max_tool_calls=config.max_tool_calls,
            timeout_seconds=config.timeout_seconds,
        )
        
        try:
            agent = SubAgentRunner(agent_config)
            return agent, None
        except Exception as e:
            return None, f"Failed to create sub-agent: {str(e)}"
    
    @staticmethod
    def execute_sub_agent(
        agent: SubAgentRunner,
        config: DelegationConfig,
        llm_config: dict,
    ) -> DelegationResult:
        """Execute sub-agent and return delegation result."""
        # Build task with context if provided
        if config.context:
            full_task = f"{config.task}\n\n[Context]\n{config.context}"
        else:
            full_task = config.task
        
        # Load LLM config from environment if not provided
        if not llm_config:
            from joy.llm import load_config
            try:
                llm_config = load_config()
            except Exception as e:
                return DelegationResult(
                    status="failed",
                    error=f"Cannot load LLM config: {str(e)}",
                )
        
        # Execute agent
        agent_result = agent.execute(full_task, llm_config)
        
        # Convert to delegation result
        return DelegationResult(
            status="success" if agent_result.status == AgentStatus.COMPLETED else agent_result.status.value,
            output=agent_result.output,
            error=agent_result.error,
            sub_agent_name="delegated_sub_agent",
            total_requests=agent_result.total_requests,
            total_tool_calls=agent_result.total_tool_calls,
            execution_time=agent_result.execution_time,
        )


def delegate(
    task: str,
    system_instruction: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    context: Optional[str] = None,
    max_requests: int = 8,
    max_tool_calls: int = 4,
    timeout_seconds: Optional[float] = None,
    max_delegation_depth: int = 2,
    max_delegations_per_request: int = 3,
) -> str:
    """Delegate a task to a sub-agent.
    
    The main agent calls this tool to offload work. The sub-agent executes
    independently with its own conversation history and tool restrictions.
    Results are returned as a JSON summary.
    
    Args:
        task: The task instruction for the sub-agent.
        system_instruction: Custom system prompt. If omitted, uses default.
        allowed_tools: Comma-separated list of tools (read, write, edit, bash).
                      Defaults to all tools.
        context: Relevant context/docs for the task (not full conversation).
                Keeps sub-agent focused without huge input tokens.
        max_requests: Max LLM requests for sub-agent (1-32, default 8).
        max_tool_calls: Max tool calls per response (1-16, default 4).
        timeout_seconds: Timeout for sub-agent execution (seconds).
        max_delegation_depth: Max nesting levels (0-3, default 2).
                            0 = no delegations allowed.
                            1 = main can delegate, but sub-agents cannot.
                            2 = main delegates to sub, sub delegates to sub-sub.
        max_delegations_per_request: Max delegations per top-level request (1-10, default 3).
    
    Returns:
        JSON string with delegation result including:
        - status: 'success', 'failed', 'timeout', or 'rejected'
        - output: Sub-agent's output
        - error: Error message if failed
        - total_requests, total_tool_calls, execution_time
    
    Examples:
        # Simple delegation
        result = delegate("Analyze the test results in tests/ directory")
        
        # With custom tools and context
        result = delegate(
            task="Optimize the database query",
            allowed_tools="read,edit",
            context="The slow query is in src/db.py. Target: < 100ms",
            max_requests=6
        )
    """
    # Parse allowed_tools from comma-separated string
    if allowed_tools:
        tools_list = [t.strip() for t in allowed_tools.split(",")]
    else:
        tools_list = ["read", "write", "edit", "bash"]
    
    # Create delegation config
    config = DelegationConfig(
        task=task,
        system_instruction=system_instruction,
        allowed_tools=tools_list,
        context=context,
        max_requests=max_requests,
        max_tool_calls=max_tool_calls,
        timeout_seconds=timeout_seconds,
        max_delegation_depth=max_delegation_depth,
        max_delegations_per_request=max_delegations_per_request,
    )
    
    # Validate config
    validation_error = DelegationManager.validate_config(config)
    if validation_error:
        result = DelegationResult(
            status="rejected",
            error=validation_error,
        )
        return json.dumps({
            "status": result.status,
            "error": result.error,
            "output": "",
            "total_requests": 0,
            "total_tool_calls": 0,
            "execution_time": 0.0,
        })
    
    # Check delegation limits
    ctx = DelegationContext.get_current()
    limit_error = DelegationManager.check_delegation_limits(config)
    if limit_error:
        result = DelegationResult(
            status="rejected",
            error=limit_error,
        )
        return json.dumps({
            "status": result.status,
            "error": result.error,
            "output": "",
            "total_requests": 0,
            "total_tool_calls": 0,
            "execution_time": 0.0,
        })
    
    # Track delegation
    ctx.increment_count()
    ctx.increment_depth()
    
    try:
        # Create sub-agent
        agent, create_error = DelegationManager.create_sub_agent(config)
        if create_error:
            return json.dumps({
                "status": "rejected",
                "error": create_error,
                "output": "",
                "total_requests": 0,
                "total_tool_calls": 0,
                "execution_time": 0.0,
            })
        
        # Load LLM config
        from joy.llm import load_config
        try:
            llm_config = load_config()
        except Exception as e:
            return json.dumps({
                "status": "failed",
                "error": f"Cannot load LLM config: {str(e)}",
                "output": "",
                "total_requests": 0,
                "total_tool_calls": 0,
                "execution_time": 0.0,
            })
        
        # Execute sub-agent
        result = DelegationManager.execute_sub_agent(agent, config, llm_config)
        
        # Return JSON result
        return json.dumps({
            "status": result.status,
            "output": result.output,
            "error": result.error,
            "sub_agent_name": result.sub_agent_name,
            "total_requests": result.total_requests,
            "total_tool_calls": result.total_tool_calls,
            "execution_time": result.execution_time,
        })
        
    finally:
        ctx.decrement_depth()
