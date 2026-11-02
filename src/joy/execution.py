"""Task executor for sub-agent delegation.

This module provides the task executor function that:
- Creates sub-agents from delegation config
- Executes tasks via SubAgentRunner
- Emits delegation events
- Handles errors and timeouts
"""

import time
import re
from typing import Callable, Optional
from dataclasses import dataclass

from joy.agent_base import BaseAgent, SubAgentRunner, AgentConfig
from joy.parallel_execution import TaskConfig, TaskResult, TaskStatus


def sanitize_error(error_str: str) -> str:
    """Remove API keys, tokens, and sensitive data from error messages."""
    if not error_str:
        return error_str
    
    patterns = [
        (r'sk-[A-Za-z0-9\-_]{20,}', '[API_KEY_REDACTED]'),
        (r'Bearer [A-Za-z0-9\-_.]+', '[BEARER_TOKEN_REDACTED]'),
        (r'api_key\s*=\s*[\'"][^\'"]+[\'"]', 'api_key=[REDACTED]'),
        (r'password\s*=\s*[\'"][^\'"]+[\'"]', 'password=[REDACTED]'),
        (r'token\s*=\s*[\'"][^\'"]+[\'"]', 'token=[REDACTED]'),
    ]
    
    result = error_str
    for pattern, replacement in patterns:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    
    return result


def create_task_executor(event_emitter: Optional[Callable] = None) -> Callable[[TaskConfig], TaskResult]:
    """Create a task executor function that uses SubAgentRunner.
    
    Args:
        event_emitter: Optional callback(event_type: str, payload: dict) for delegation events
    
    Returns:
        Function that executes TaskConfig and returns TaskResult
    """
    
    def executor(config: TaskConfig) -> TaskResult:
        """Execute a single delegated task.
        
        Args:
            config: TaskConfig with task description and agent settings
            
        Returns:
            TaskResult with execution status and output
        """
        start_time = time.time()
        
        try:
            # Emit start event
            if event_emitter:
                event_emitter("delegation", {
                    "task_id": config.task_id,
                    "status": "running",
                    "start_time": start_time,
                })
            
            # Create sub-agent configuration from task config
            agent_config = AgentConfig(
                name=config.name,
                description=f"Sub-agent for task {config.task_id}",
                system_instruction=config.system_instruction,
                allowed_tools=config.allowed_tools,
                max_requests=config.max_requests,
                max_tool_calls=config.max_tool_calls,
                timeout_seconds=config.timeout_seconds,
            )
            
            # Create sub-agent instance (now with proper from_config factory)
            runner = BaseAgent.from_config(agent_config)
            
            # Add task to conversation history (execute_turn uses what's already in conversation)
            runner.add_message("user", config.task)
            
            # Execute the task - returns (response_dict, error_msg)
            response_dict, error_msg = runner.execute_turn(llm_config={})
            
            # Handle execution error
            if error_msg:
                raise RuntimeError(error_msg)
            
            # Build result with output from sub-agent response
            output = ""
            if isinstance(response_dict, dict):
                output = response_dict.get("text", "")
            elif isinstance(response_dict, str):
                output = response_dict
            
            # Get conversation to extract actual output
            if hasattr(runner, "conversation") and runner.conversation:
                # Extract last assistant message
                for msg in reversed(runner.conversation):
                    if msg.get("role") == "assistant":
                        output = msg.get("content", output)
                        break
            
            end_time = time.time()
            
            # Emit completion event
            if event_emitter:
                event_emitter("delegation", {
                    "task_id": config.task_id,
                    "status": "completed",
                    "output": output[:500],
                    "start_time": start_time,
                    "end_time": end_time,
                })
            
            # Create successful result
            result = TaskResult(
                task_id=config.task_id,
                status=TaskStatus.COMPLETED,
                output=output,
                start_time=start_time,
                end_time=end_time,
                total_requests=getattr(runner, "total_requests", 1),
                total_tool_calls=getattr(runner, "total_tool_calls", 0),
            )
            
            return result
            
        except TimeoutError as e:
            end_time = time.time()
            error_msg = f"Task timeout: {str(e)}"
            
            # Emit failure event
            if event_emitter:
                event_emitter("delegation", {
                    "task_id": config.task_id,
                    "status": "timeout",
                    "error": sanitize_error(error_msg),
                    "end_time": end_time,
                })
            
            return TaskResult(
                task_id=config.task_id,
                status=TaskStatus.TIMEOUT,
                error=sanitize_error(error_msg),
                start_time=start_time,
                end_time=end_time,
            )
            
        except Exception as e:
            end_time = time.time()
            error_msg = f"Task execution error: {str(e)}"
            
            # Emit failure event
            if event_emitter:
                event_emitter("delegation", {
                    "task_id": config.task_id,
                    "status": "failed",
                    "error": sanitize_error(error_msg),
                    "end_time": end_time,
                })
            
            return TaskResult(
                task_id=config.task_id,
                status=TaskStatus.FAILED,
                error=sanitize_error(error_msg),
                start_time=start_time,
                end_time=end_time,
            )
    
    return executor
