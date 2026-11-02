"""Tests for sub-agent task execution and delegation.

Tests cover:
- Task executor creation and invocation
- Event emission during task execution
- Error handling and sanitization
- Parallel task execution with callbacks
"""

import unittest
import time
from unittest.mock import Mock, MagicMock, patch
from dataclasses import dataclass

from joy.execution import create_task_executor, sanitize_error
from joy.parallel_execution import TaskConfig, TaskResult, TaskStatus, TaskPool
from joy.agent_base import BaseAgent, AgentConfig


class TestSanitizeError(unittest.TestCase):
    """Tests for error sanitization."""
    
    def test_sanitize_openai_api_key(self):
        """Test OpenAI API key redaction."""
        error = "Error: api key sk-proj-1234567890abcdefghijklmnop failed"
        result = sanitize_error(error)
        self.assertNotIn("sk-proj-", result)
        self.assertIn("[API_KEY_REDACTED]", result)
    
    def test_sanitize_bearer_token(self):
        """Test Bearer token redaction."""
        error = "Auth failed: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = sanitize_error(error)
        self.assertNotIn("Bearer eyJ", result)
        self.assertIn("[BEARER_TOKEN_REDACTED]", result)
    
    def test_sanitize_api_key_assignment(self):
        """Test api_key= redaction."""
        error = 'Config error: api_key="my-secret-key-12345" is invalid'
        result = sanitize_error(error)
        self.assertNotIn("my-secret-key", result)
        self.assertIn("[REDACTED]", result)
    
    def test_sanitize_password_assignment(self):
        """Test password= redaction."""
        error = 'Auth failed: password="super_secret_password_123"'
        result = sanitize_error(error)
        self.assertNotIn("super_secret", result)
        self.assertIn("[REDACTED]", result)
    
    def test_sanitize_empty_error(self):
        """Test handling of empty error strings."""
        self.assertEqual(sanitize_error(""), "")
        self.assertEqual(sanitize_error(None), None)
    
    def test_sanitize_error_without_secrets(self):
        """Test error with no secrets is unchanged."""
        error = "File not found: /tmp/missing.txt"
        result = sanitize_error(error)
        self.assertEqual(result, error)


class TestCreateTaskExecutor(unittest.TestCase):
    """Tests for create_task_executor function."""
    
    def test_create_executor_returns_callable(self):
        """Test executor factory returns callable."""
        executor = create_task_executor()
        self.assertTrue(callable(executor))
    
    def test_create_executor_with_callback(self):
        """Test executor factory accepts event callback."""
        callback = Mock()
        executor = create_task_executor(event_emitter=callback)
        self.assertTrue(callable(executor))
    
    @patch('joy.agent_base.BaseAgent.from_config')
    def test_executor_creates_sub_agent(self, mock_agent_factory):
        """Test executor creates sub-agent from config."""
        # Setup mocks
        mock_runner = MagicMock()
        mock_runner.add_message = MagicMock()
        mock_runner.execute_turn.return_value = ({"text": "Result"}, None)
        mock_runner.total_requests = 2
        mock_runner.total_tool_calls = 1
        mock_runner.conversation = [{"role": "assistant", "content": "Result"}]
        mock_agent_factory.return_value = mock_runner
        
        # Create executor and execute task
        executor = create_task_executor()
        config = TaskConfig(
            task_id="test_123",
            task="Analyze code",
            system_instruction="You are a code analyzer",
            allowed_tools=["read", "edit"],
            max_requests=4,
        )
        
        result = executor(config)
        
        # Verify
        mock_agent_factory.assert_called_once()
        mock_runner.add_message.assert_called_once()
        mock_runner.execute_turn.assert_called_once()
        self.assertEqual(result.task_id, "test_123")
        self.assertEqual(result.status, TaskStatus.COMPLETED)
    
    @patch('joy.agent_base.BaseAgent.from_config')
    def test_executor_returns_success_result(self, mock_agent_factory):
        """Test executor returns proper success result."""
        mock_runner = MagicMock()
        mock_runner.add_message = MagicMock()
        mock_runner.execute_turn.return_value = (
            {"text": "Task completed successfully"},
            None
        )
        mock_runner.total_requests = 3
        mock_runner.total_tool_calls = 2
        mock_runner.conversation = [{"role": "assistant", "content": "Task completed successfully"}]
        mock_agent_factory.return_value = mock_runner
        
        executor = create_task_executor()
        config = TaskConfig(task_id="task_1", task="Do something")
        
        result = executor(config)
        
        self.assertEqual(result.task_id, "task_1")
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.output, "Task completed successfully")
        self.assertEqual(result.total_requests, 3)
        self.assertEqual(result.total_tool_calls, 2)
        self.assertIsNotNone(result.start_time)
        self.assertIsNotNone(result.end_time)
        self.assertGreaterEqual(result.end_time, result.start_time)
    
    @patch('joy.agent_base.BaseAgent.from_config')
    def test_executor_handles_timeout_error(self, mock_agent_factory):
        """Test executor handles TimeoutError."""
        mock_agent_factory.side_effect = TimeoutError("Task exceeded max time")
        
        executor = create_task_executor()
        config = TaskConfig(task_id="task_timeout", task="Long task")
        
        result = executor(config)
        
        self.assertEqual(result.task_id, "task_timeout")
        self.assertEqual(result.status, TaskStatus.TIMEOUT)
        self.assertIn("timeout", result.error.lower())
        self.assertIsNotNone(result.start_time)
        self.assertIsNotNone(result.end_time)
    
    @patch('joy.agent_base.BaseAgent.from_config')
    def test_executor_handles_general_exception(self, mock_agent_factory):
        """Test executor handles general exceptions."""
        mock_agent_factory.side_effect = RuntimeError("Sub-agent crashed")
        
        executor = create_task_executor()
        config = TaskConfig(task_id="task_crash", task="Bad task")
        
        result = executor(config)
        
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("error", result.error.lower())
    
    @patch('joy.agent_base.BaseAgent.from_config')
    def test_executor_with_event_callback(self, mock_agent_factory):
        """Test executor can accept event callbacks (for future use)."""
        mock_agent = MagicMock()
        mock_agent.add_message = MagicMock()
        mock_agent.execute_turn.return_value = ({"text": "Done"}, None)
        mock_agent.total_requests = 1
        mock_agent.total_tool_calls = 0
        mock_agent.conversation = [{"role": "assistant", "content": "Done"}]
        mock_agent_factory.return_value = mock_agent
        
        callback = Mock()
        executor = create_task_executor(event_emitter=callback)
        config = TaskConfig(task_id="task_cb", task="Test")
        
        result = executor(config)
        
        # Executor accepts callback and uses it during execution
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        callback.assert_called()  # Verify callback was invoked


class TestTaskPoolEventEmission(unittest.TestCase):
    """Tests for event emission from TaskPool."""
    
    def test_task_pool_with_event_callback(self):
        """Test TaskPool accepts event callback."""
        callback = Mock()
        pool = TaskPool(max_concurrent=2, event_callback=callback)
        self.assertEqual(pool.event_callback, callback)
    
    @patch('joy.agent_base.BaseAgent.from_config')
    def test_task_pool_emits_start_event(self, mock_agent_factory):
        """Test TaskPool emits event when task starts."""
        mock_agent = MagicMock()
        mock_agent.add_message = MagicMock()
        mock_agent.execute_turn.return_value = ({"text": "Done"}, None)
        mock_agent.total_requests = 1
        mock_agent.total_tool_calls = 0
        mock_agent.conversation = [{"role": "assistant", "content": "Done"}]
        mock_agent_factory.return_value = mock_agent
        
        events = []
        callback = lambda t, p: events.append((t, p))
        
        pool = TaskPool(max_concurrent=1, event_callback=callback)
        executor = create_task_executor()
        
        config = TaskConfig(task_id="t1", task="Test")
        task_id = pool.submit_task(config)
        
        # Run pool
        pool.execute(executor)
        
        # Should have emitted events
        self.assertGreater(len(events), 0)
        # First event should be start with status "running"
        event_types_and_statuses = [(t, p.get("status")) for t, p in events]
        self.assertIn("running", [s for t, s in event_types_and_statuses if t == "delegation"])
    
    @patch('joy.agent_base.BaseAgent.from_config')
    def test_task_pool_emits_completion_event(self, mock_agent_factory):
        """Test TaskPool emits event when task completes."""
        mock_agent = MagicMock()
        mock_agent.total_requests = 1
        mock_agent.total_tool_calls = 0
        mock_agent.conversation = []
        
        # Mock SubAgentRunner instance that execute_turn will be called on
        mock_runner = MagicMock()
        mock_runner.execute_turn.return_value = ({"text": "Completed"}, None)
        mock_runner.conversation = []
        
        # Make from_config return mock runner
        mock_agent_factory.return_value = mock_runner
        
        events = []
        callback = lambda t, p: events.append((t, p))
        
        pool = TaskPool(max_concurrent=1, event_callback=callback)
        executor = create_task_executor(event_emitter=callback)
        
        config = TaskConfig(task_id="t2", task="Complete test")
        pool.submit_task(config)
        pool.execute(executor)
        
        # Find completion event
        completion_events = [(t, p) for t, p in events if p.get("status") == "completed"]
        self.assertGreater(len(completion_events), 0)
        
        # Verify event payload
        _, payload = completion_events[0]
        self.assertEqual(payload["task_id"], "t2")
        self.assertEqual(payload["status"], "completed")
        self.assertIsNotNone(payload.get("start_time"))
        self.assertIsNotNone(payload.get("end_time"))
    
    @patch('joy.agent_base.BaseAgent.from_config')
    def test_task_pool_emits_failure_event(self, mock_agent_factory):
        """Test TaskPool emits event when task fails."""
        mock_agent_factory.side_effect = ValueError("Test failure")
        
        events = []
        callback = lambda t, p: events.append((t, p))
        
        pool = TaskPool(max_concurrent=1, event_callback=callback)
        executor = create_task_executor()
        
        config = TaskConfig(task_id="t3", task="Failing task")
        pool.submit_task(config)
        pool.execute(executor)
        
        # Find failure event
        failure_events = [(t, p) for t, p in events if p.get("status") == "failed"]
        self.assertGreater(len(failure_events), 0)
        
        _, payload = failure_events[0]
        self.assertEqual(payload["task_id"], "t3")
        self.assertEqual(payload["status"], "failed")
        self.assertIsNotNone(payload.get("error"))


class TestTaskResultTimings(unittest.TestCase):
    """Tests for task result timing metrics."""
    
    @patch('joy.agent_base.BaseAgent.from_config')
    @patch('joy.execution.SubAgentRunner')
    def test_task_result_has_timing_info(self, mock_runner_class, mock_agent_factory):
        """Test task result includes start/end times."""
        mock_agent = MagicMock()
        mock_agent.total_requests = 1
        mock_agent.total_tool_calls = 0
        mock_agent_factory.return_value = mock_agent
        
        mock_runner = MagicMock()
        mock_runner.execute_turn.return_value = ({"text": "Result"}, [])
        mock_runner_class.return_value = mock_runner
        
        executor = create_task_executor()
        config = TaskConfig(task_id="t_time", task="Test")
        
        before = time.time()
        result = executor(config)
        after = time.time()
        
        self.assertIsNotNone(result.start_time)
        self.assertIsNotNone(result.end_time)
        self.assertGreaterEqual(result.start_time, before - 0.1)
        self.assertLessEqual(result.end_time, after + 0.1)
        self.assertGreaterEqual(result.execution_time, 0)


if __name__ == "__main__":
    unittest.main()
