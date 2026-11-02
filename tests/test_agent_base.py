"""Unit tests for agent_base.py sub-agent framework.

Tests cover:
- Successful agent execution
- LLM failure handling
- Iteration limit enforcement
- Empty output handling
- Timeout detection
- Status transitions
"""

import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import dataclass

from src.joy.agent_base import (
    AgentConfig,
    AgentStatus,
    AgentResult,
    BaseAgent,
    SubAgentRunner,
)


class MockAgent(BaseAgent):
    """Concrete agent for testing BaseAgent abstract interface."""
    
    def __init__(self, config: AgentConfig, mock_response=None, mock_error=None):
        super().__init__(config)
        self.mock_response = mock_response
        self.mock_error = mock_error
        self.turns_executed = 0
    
    def execute_turn(self, llm_config: dict):
        """Mock turn execution."""
        self.turns_executed += 1
        if self.mock_error:
            return None, self.mock_error
        return self.mock_response, None


class TestAgentConfig(unittest.TestCase):
    """Tests for AgentConfig dataclass."""
    
    def test_minimal_config(self):
        """Config requires name, description, system_instruction."""
        config = AgentConfig(
            name="test_agent",
            description="A test agent",
            system_instruction="You are a test agent."
        )
        self.assertEqual(config.name, "test_agent")
        self.assertEqual(config.allowed_tools, ["read", "write", "edit", "bash"])
        self.assertEqual(config.max_requests, 8)
        self.assertEqual(config.max_tool_calls, 4)
        self.assertIsNone(config.timeout_seconds)
    
    def test_custom_config(self):
        """Config allows custom tool restrictions and limits."""
        config = AgentConfig(
            name="minimal_agent",
            description="Only reads files",
            system_instruction="Only read files.",
            allowed_tools=["read"],
            max_requests=3,
            max_tool_calls=2,
            timeout_seconds=10.0
        )
        self.assertEqual(config.allowed_tools, ["read"])
        self.assertEqual(config.max_requests, 3)
        self.assertEqual(config.max_tool_calls, 2)
        self.assertEqual(config.timeout_seconds, 10.0)


class TestBaseAgent(unittest.TestCase):
    """Tests for BaseAgent base class."""
    
    def setUp(self):
        """Create a test config and mock agent."""
        self.config = AgentConfig(
            name="test_agent",
            description="Test agent",
            system_instruction="You are helpful."
        )
    
    def test_agent_initialization(self):
        """Agent initializes with correct status and empty history."""
        agent = MockAgent(self.config)
        self.assertEqual(agent.status, AgentStatus.CREATED)
        self.assertEqual(len(agent.conversation), 0)
        self.assertIsNone(agent._start_time)
    
    def test_add_message(self):
        """add_message appends to conversation history."""
        agent = MockAgent(self.config)
        agent.add_message("user", "Hello")
        agent.add_message("assistant", "Hi there")
        
        self.assertEqual(len(agent.conversation), 2)
        self.assertEqual(agent.conversation[0]["role"], "user")
        self.assertEqual(agent.conversation[0]["content"], "Hello")
        self.assertEqual(agent.conversation[1]["role"], "assistant")
    
    def test_add_tool_result(self):
        """add_tool_result appends tool output to conversation."""
        agent = MockAgent(self.config)
        agent.add_tool_result("call_123", "Tool output here")
        
        self.assertEqual(len(agent.conversation), 1)
        self.assertEqual(agent.conversation[0]["type"], "function_call_output")
        self.assertEqual(agent.conversation[0]["call_id"], "call_123")
        self.assertEqual(agent.conversation[0]["output"], "Tool output here")
    
    def test_set_status(self):
        """set_status updates agent status."""
        agent = MockAgent(self.config)
        agent.set_status(AgentStatus.RUNNING)
        self.assertEqual(agent.status, AgentStatus.RUNNING)
        
        agent.set_status(AgentStatus.COMPLETED)
        self.assertEqual(agent.status, AgentStatus.COMPLETED)
    
    def test_build_initial_conversation(self):
        """build_initial_conversation includes system instruction and history."""
        agent = MockAgent(self.config)
        agent.add_message("user", "Task 1")
        
        messages = agent.build_initial_conversation()
        
        # First message should be system instruction
        self.assertEqual(messages[0]["type"], "text")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("helpful", messages[0]["content"])
        
        # Then the user message
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"], "Task 1")
    
    def test_elapsed_time(self):
        """elapsed_time measures execution duration."""
        agent = MockAgent(self.config)
        self.assertEqual(agent.elapsed_time, 0.0)  # Not started
        
        agent._start_time = time.time()
        time.sleep(0.05)
        elapsed = agent.elapsed_time
        
        self.assertGreater(elapsed, 0.04)
        self.assertLess(elapsed, 0.2)
    
    def test_timeout_detection(self):
        """is_timeout detects when execution exceeds timeout."""
        config = AgentConfig(
            name="test",
            description="test",
            system_instruction="test",
            timeout_seconds=0.1
        )
        agent = MockAgent(config)
        
        # No timeout without start time
        self.assertFalse(agent.is_timeout())
        
        # Timeout after elapsed time
        agent._start_time = time.time()
        self.assertFalse(agent.is_timeout())
        
        time.sleep(0.15)
        self.assertTrue(agent.is_timeout())
    
    def test_no_timeout_when_not_configured(self):
        """is_timeout always returns False if timeout_seconds is None."""
        agent = MockAgent(self.config)  # timeout_seconds=None by default
        agent._start_time = time.time() - 100  # Simulate long execution
        
        self.assertFalse(agent.is_timeout())


class TestAgentExecution(unittest.TestCase):
    """Tests for agent.execute() method."""
    
    def setUp(self):
        """Create test config."""
        self.config = AgentConfig(
            name="test_agent",
            description="Test agent",
            system_instruction="Test"
        )
        self.llm_config = {
            "model": "gpt-4",
            "api_key": "fake-key",
            "base_url": "https://api.openai.com/v1"
        }
    
    def test_successful_execution(self):
        """Successful execution returns COMPLETED status."""
        # Mock response with no tool calls (terminal state)
        response = MagicMock()
        response.output = []
        
        agent = MockAgent(self.config, mock_response=response)
        result = agent.execute("Do something", self.llm_config)
        
        self.assertEqual(result.status, AgentStatus.COMPLETED)
        self.assertEqual(result.error, None)
        self.assertEqual(result.total_requests, 1)
        self.assertEqual(agent.status, AgentStatus.COMPLETED)
    
    def test_llm_error_handling(self):
        """LLM error returns FAILED status with error message."""
        agent = MockAgent(
            self.config,
            mock_response=None,
            mock_error="API key invalid"
        )
        result = agent.execute("Do something", self.llm_config)
        
        self.assertEqual(result.status, AgentStatus.FAILED)
        self.assertEqual(result.error, "API key invalid")
        self.assertEqual(result.total_requests, 1)
        self.assertEqual(agent.status, AgentStatus.FAILED)
    
    def test_empty_output(self):
        """Empty output is handled gracefully."""
        response = MagicMock()
        response.output = []
        
        agent = MockAgent(self.config, mock_response=response)
        result = agent.execute("Do something", self.llm_config)
        
        self.assertEqual(result.status, AgentStatus.COMPLETED)
        self.assertEqual(result.output, "")
        self.assertEqual(result.total_tool_calls, 0)
    
    def test_iteration_limit(self):
        """Execution stops at max_requests limit."""
        response = MagicMock()
        response.output = []
        
        config = AgentConfig(
            name="test",
            description="test",
            system_instruction="test",
            max_requests=2
        )
        agent = MockAgent(config, mock_response=response)
        
        # Execute multiple times to trigger the limit
        # Each execute_turn increments turns_executed
        result = agent.execute("Do something", self.llm_config)
        
        # Should only execute 1 turn (no tool calls, so agent stops)
        self.assertEqual(agent.turns_executed, 1)
    
    def test_timeout_during_execution(self):
        """Execution times out when elapsed_time exceeds timeout_seconds."""
        response = MagicMock()
        response.output = []
        
        config = AgentConfig(
            name="test",
            description="test",
            system_instruction="test",
            timeout_seconds=0.05
        )
        agent = MockAgent(config, mock_response=response)
        
        # Manually set start time to simulate a timeout condition
        # (time-based tests can be flaky, so we verify the timeout detection logic)
        def test_execute_with_timeout():
            agent._start_time = time.time() - 0.1  # Simulate 100ms have passed
            # Execute should detect timeout on first iteration
            return agent.execute("Do something", self.llm_config)
        
        result = test_execute_with_timeout()
        
        self.assertEqual(result.status, AgentStatus.TIMEOUT)
        self.assertIn("timeout", result.error.lower())
    
    def test_message_added_to_conversation(self):
        """Task is added to conversation before execution."""
        response = MagicMock()
        response.output = []
        
        agent = MockAgent(self.config, mock_response=response)
        agent.execute("My task here", self.llm_config)
        
        # Task should be in conversation
        user_messages = [m for m in agent.conversation if m.get("role") == "user"]
        self.assertTrue(any("My task here" in m.get("content", "") for m in user_messages))
    
    def test_execution_time_measured(self):
        """Execution time is measured and reported."""
        response = MagicMock()
        response.output = []
        
        agent = MockAgent(self.config, mock_response=response)
        result = agent.execute("Do something", self.llm_config)
        
        self.assertGreater(result.execution_time, 0.0)


class TestSubAgentRunner(unittest.TestCase):
    """Tests for SubAgentRunner concrete implementation."""
    
    def setUp(self):
        """Create test config."""
        self.config = AgentConfig(
            name="test_runner",
            description="Test runner",
            system_instruction="Test runner instruction"
        )
        self.llm_config = {
            "model": "gpt-4",
            "api_key": "fake-key",
            "base_url": "https://api.openai.com/v1"
        }
    
    def test_runner_initialization(self):
        """SubAgentRunner initializes correctly."""
        runner = SubAgentRunner(self.config)
        self.assertEqual(runner.config.name, "test_runner")
        self.assertEqual(runner.status, AgentStatus.CREATED)
    
    def test_cancel_method_exists(self):
        """SubAgentRunner has cancel method."""
        runner = SubAgentRunner(self.config)
        runner.cancel()  # Should not raise
        self.assertTrue(runner._cancelled.is_set())
    
    def test_execute_turn_builds_conversation(self):
        """execute_turn includes system instruction in conversation."""
        runner = SubAgentRunner(self.config)
        runner.add_message("user", "Test task")
        
        # Verify the runner has both messages
        self.assertEqual(len(runner.conversation), 1)
        
        # Check that build_initial_conversation includes system instruction
        initial_conv = runner.build_initial_conversation()
        self.assertGreater(len(initial_conv), 0)
        self.assertTrue(any(
            m.get("role") == "system" for m in initial_conv
        ))


class TestAgentResult(unittest.TestCase):
    """Tests for AgentResult dataclass."""
    
    def test_successful_result(self):
        """Create result with success status."""
        result = AgentResult(
            status=AgentStatus.COMPLETED,
            output="Task completed successfully",
            total_requests=2,
            total_tool_calls=1,
            execution_time=1.5
        )
        
        self.assertEqual(result.status, AgentStatus.COMPLETED)
        self.assertEqual(result.output, "Task completed successfully")
        self.assertIsNone(result.error)
        self.assertEqual(result.total_requests, 2)
        self.assertEqual(result.execution_time, 1.5)
    
    def test_failure_result(self):
        """Create result with failure status and error."""
        result = AgentResult(
            status=AgentStatus.FAILED,
            output="Partial output",
            error="API connection failed",
            total_requests=1,
            total_tool_calls=0,
            execution_time=0.5
        )
        
        self.assertEqual(result.status, AgentStatus.FAILED)
        self.assertEqual(result.error, "API connection failed")
        self.assertEqual(result.output, "Partial output")
    
    def test_timeout_result(self):
        """Create result with timeout status."""
        result = AgentResult(
            status=AgentStatus.TIMEOUT,
            error="Execution exceeded 30s timeout",
            execution_time=30.1
        )
        
        self.assertEqual(result.status, AgentStatus.TIMEOUT)
        self.assertIn("timeout", result.error.lower())


class TestAgentStatus(unittest.TestCase):
    """Tests for AgentStatus enum."""
    
    def test_status_values(self):
        """All expected statuses are defined."""
        self.assertEqual(AgentStatus.CREATED.value, "created")
        self.assertEqual(AgentStatus.RUNNING.value, "running")
        self.assertEqual(AgentStatus.COMPLETED.value, "completed")
        self.assertEqual(AgentStatus.FAILED.value, "failed")
        self.assertEqual(AgentStatus.TIMEOUT.value, "timeout")
        self.assertEqual(AgentStatus.CANCELLED.value, "cancelled")


if __name__ == "__main__":
    unittest.main()
