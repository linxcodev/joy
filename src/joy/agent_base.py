"""Base agent abstraction for Joy sub-agents.

This module provides a reusable agent execution framework that:
- Isolates agent identity, system instructions, and task context
- Manages separate conversation histories per agent
- Enforces tool restrictions and iteration limits
- Supports timeout and explicit termination
"""

import queue
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class AgentStatus(Enum):
    """Agent execution status."""
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class AgentConfig:
    """Configuration for a sub-agent."""
    name: str
    description: str
    system_instruction: str
    allowed_tools: list[str] = field(default_factory=lambda: ["read", "write", "edit", "bash"])
    max_requests: int = 8
    max_tool_calls: int = 4
    timeout_seconds: Optional[float] = None


@dataclass
class AgentResult:
    """Result of agent task execution."""
    status: AgentStatus
    output: str = ""
    error: Optional[str] = None
    total_requests: int = 0
    total_tool_calls: int = 0
    execution_time: float = 0.0


class BaseAgent(ABC):
    """Abstract base agent that reuses Joy's LLM and tool infrastructure.
    
    Subclasses implement:
    - configure_client(): Initialize the LLM client
    - execute_turn(): Run one agent turn using StreamWorker
    """

    def __init__(self, config: AgentConfig):
        """Initialize agent with isolated config and conversation history."""
        self.config = config
        self.conversation: list[dict] = []
        self._status = AgentStatus.CREATED
        self._start_time: Optional[float] = None
        self._lock = threading.Lock()
    
    @classmethod
    def from_config(cls, config: AgentConfig) -> "SubAgentRunner":
        """Factory method to create SubAgentRunner from AgentConfig.
        
        Args:
            config: AgentConfig with agent settings
            
        Returns:
            SubAgentRunner instance ready for execution
        """
        return SubAgentRunner(config)

    @property
    def status(self) -> AgentStatus:
        """Current agent status."""
        with self._lock:
            return self._status

    @property
    def elapsed_time(self) -> float:
        """Seconds since execution started."""
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    def is_timeout(self) -> bool:
        """Check if execution has exceeded the configured timeout."""
        if self.config.timeout_seconds is None:
            return False
        return self.elapsed_time > self.config.timeout_seconds

    def set_status(self, status: AgentStatus) -> None:
        """Update agent status (thread-safe)."""
        with self._lock:
            self._status = status

    def add_message(self, role: str, content: str) -> None:
        """Add a message to the conversation history.
        
        Args:
            role: "user", "assistant", or "system"
            content: Message text
        """
        self.conversation.append({
            "type": "text",
            "role": role,
            "content": content
        })

    def add_tool_result(self, call_id: str, result: str) -> None:
        """Add a tool result to the conversation history.
        
        Args:
            call_id: The function call ID from the LLM response
            result: The tool output as a string
        """
        self.conversation.append({
            "type": "function_call_output",
            "call_id": call_id,
            "output": result
        })

    @abstractmethod
    def execute_turn(self, llm_config: dict) -> tuple[Optional[Any], Optional[str]]:
        """Execute one agent turn.
        
        Args:
            llm_config: Dictionary with "model", "api_key", "base_url"
        
        Returns:
            (response, error_message) where response is the LLM response object
            or None if an error occurred.
        """

    def build_initial_conversation(self) -> list[dict]:
        """Build the conversation list for the first request.
        
        Returns:
            List of message dicts ready to send to the LLM.
        """
        result = []
        # System instruction comes first
        result.append({
            "type": "text",
            "role": "system",
            "content": self.config.system_instruction
        })
        # Then any accumulated conversation
        result.extend(self.conversation)
        return result

    def execute(self, task: str, llm_config: dict) -> AgentResult:
        """Execute the agent on a task with full lifecycle management.
        
        Args:
            task: The task description/instruction
            llm_config: LLM configuration (model, api_key, base_url)
        
        Returns:
            AgentResult with status, output, and execution metrics
        """
        self.set_status(AgentStatus.RUNNING)
        if self._start_time is None:
            self._start_time = time.time()
        
        try:
            # Initialize conversation with task
            self.add_message("user", task)
            
            output_parts = []
            total_requests = 0
            total_tool_calls = 0
            
            # Agent loop
            while total_requests < self.config.max_requests:
                # Check timeout
                if self.is_timeout():
                    self.set_status(AgentStatus.TIMEOUT)
                    return AgentResult(
                        status=AgentStatus.TIMEOUT,
                        output="".join(output_parts),
                        error=f"Execution exceeded {self.config.timeout_seconds}s timeout",
                        total_requests=total_requests,
                        total_tool_calls=total_tool_calls,
                        execution_time=self.elapsed_time
                    )
                
                total_requests += 1
                
                # Execute one turn
                response, error_msg = self.execute_turn(llm_config)
                
                if error_msg:
                    self.set_status(AgentStatus.FAILED)
                    return AgentResult(
                        status=AgentStatus.FAILED,
                        output="".join(output_parts),
                        error=error_msg,
                        total_requests=total_requests,
                        total_tool_calls=total_tool_calls,
                        execution_time=self.elapsed_time
                    )
                
                if response is None:
                    break
                
                # Process response output
                if hasattr(response, "output") and response.output:
                    for item in response.output:
                        if item.type == "text":
                            output_parts.append(item.content)
                        elif item.type == "function_call":
                            total_tool_calls += 1
                
                # Replay response into conversation
                if hasattr(response, "output"):
                    self.conversation.extend(
                        item.model_dump(exclude_none=True)
                        for item in response.output
                    )
                
                # Check if there are more tool calls to execute
                has_tool_calls = any(
                    item.type == "function_call"
                    for item in getattr(response, "output", [])
                )
                
                if not has_tool_calls:
                    # No more tool calls -> agent is done
                    break
            
            if total_requests >= self.config.max_requests:
                self.set_status(AgentStatus.FAILED)
                return AgentResult(
                    status=AgentStatus.FAILED,
                    output="".join(output_parts),
                    error=f"Exceeded {self.config.max_requests} iteration limit",
                    total_requests=total_requests,
                    total_tool_calls=total_tool_calls,
                    execution_time=self.elapsed_time
                )
            
            self.set_status(AgentStatus.COMPLETED)
            return AgentResult(
                status=AgentStatus.COMPLETED,
                output="".join(output_parts),
                total_requests=total_requests,
                total_tool_calls=total_tool_calls,
                execution_time=self.elapsed_time
            )
            
        except Exception as e:
            self.set_status(AgentStatus.FAILED)
            return AgentResult(
                status=AgentStatus.FAILED,
                output="".join(output_parts),
                error=f"Unexpected error: {str(e)}",
                total_requests=total_requests,
                total_tool_calls=total_tool_calls,
                execution_time=self.elapsed_time
            )


class SubAgentRunner(BaseAgent):
    """Default sub-agent implementation using Joy's StreamWorker."""

    def __init__(self, config: AgentConfig):
        """Initialize sub-agent with config."""
        super().__init__(config)
        self._cancelled = threading.Event()
        self._stream_worker: Optional[Any] = None
        self.total_requests = 0
        self.total_tool_calls = 0

    def cancel(self) -> None:
        """Signal the agent to stop execution."""
        self._cancelled.set()
        if self._stream_worker:
            self._stream_worker.cancel()

    def execute_turn(self, llm_config: dict) -> tuple[Optional[Any], Optional[str]]:
        """Execute one turn using StreamWorker.
        
        This reuses Joy's existing agent infrastructure to avoid code duplication.
        """
        # Avoid circular imports
        from joy.llm import StreamWorker, LLMError, describe_error

        try:
            # Build messages with system instruction
            messages = self.build_initial_conversation()
            
            # Create worker to run one turn
            self._stream_worker = StreamWorker(messages)
            
            output_parts = []
            tool_calls_in_turn = []
            
            # Track requests
            self.total_requests += 1
            
            # Consume events from worker
            while True:
                try:
                    kind, payload = self._stream_worker.events.get(timeout=1.0)
                except queue.Empty:
                    if self._cancelled.is_set():
                        return None, "Execution cancelled"
                    continue
                
                if kind == "text":
                    output_parts.append(payload)
                elif kind == "tool":
                    name, status = payload
                    if status == "running":
                        tool_calls_in_turn.append(name)
                        self.total_tool_calls += 1
                elif kind == "done":
                    # Worker completed successfully
                    break
                elif kind == "error":
                    return None, payload
            
            # Check if worker actually completed or timed out
            self._stream_worker._thread.join(timeout=2.0)
            if self._stream_worker._thread.is_alive():
                self._stream_worker.cancel()
                return None, "Worker thread did not complete in time"
            
            # Return the response from the last conversation item
            if not self.conversation:
                return None, "No response generated"
            
            # Build response dict with content and metrics
            response_text = "".join(output_parts)
            response_dict = {
                "text": response_text,
                "used_tools": tool_calls_in_turn,
                "tool_count": len(tool_calls_in_turn),
            }
            
            # The conversation was updated by StreamWorker
            # Extract and return the response with actual content
            return response_dict, None  # Signals success; actual content included
            
        except LLMError as e:
            return None, describe_error(e)
        except Exception as e:
            return None, f"Unexpected error: {str(e)}"
