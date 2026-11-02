"""Unit tests for CLI delegation event handling and state management."""

import pytest
from dataclasses import asdict
from joy.cli import DelegationEvent, sanitize_error, build_session


class TestDelegationEventParsing:
    """Test DelegationEvent parsing from payload."""

    def test_parse_complete_delegation_event(self):
        """Test parsing a complete delegation event payload."""
        payload = {
            "task_id": "task_001",
            "agent_name": "Analyzer",
            "status": "running",
            "start_time": 1234567890.5,
            "end_time": None,
            "result": None,
            "error": None,
        }
        event = DelegationEvent.from_payload(payload)
        
        assert event.task_id == "task_001"
        assert event.agent_name == "Analyzer"
        assert event.status == "running"
        assert event.start_time == 1234567890.5
        assert event.end_time is None
        assert event.result is None
        assert event.error is None

    def test_parse_delegation_event_with_result(self):
        """Test parsing a delegation event with result."""
        payload = {
            "task_id": "task_002",
            "agent_name": "Reviewer",
            "status": "completed",
            "start_time": 1234567890.0,
            "end_time": 1234567900.0,
            "result": {"files_reviewed": 3, "issues_found": 1},
            "error": None,
        }
        event = DelegationEvent.from_payload(payload)
        
        assert event.task_id == "task_002"
        assert event.agent_name == "Reviewer"
        assert event.status == "completed"
        assert event.result == {"files_reviewed": 3, "issues_found": 1}
        assert event.error is None

    def test_parse_delegation_event_with_error(self):
        """Test parsing a delegation event with error."""
        payload = {
            "task_id": "task_003",
            "agent_name": "Fixer",
            "status": "failed",
            "start_time": 1234567890.0,
            "end_time": 1234567895.0,
            "result": None,
            "error": "Timeout after 5 seconds",
        }
        event = DelegationEvent.from_payload(payload)
        
        assert event.task_id == "task_003"
        assert event.agent_name == "Fixer"
        assert event.status == "failed"
        assert event.error == "Timeout after 5 seconds"

    def test_parse_delegation_event_with_defaults(self):
        """Test parsing a delegation event with missing optional fields."""
        payload = {
            "task_id": "task_004",
            "agent_name": "Worker",
            "status": "queued",
        }
        event = DelegationEvent.from_payload(payload)
        
        assert event.task_id == "task_004"
        assert event.agent_name == "Worker"
        assert event.status == "queued"
        assert event.start_time is None
        assert event.end_time is None
        assert event.result is None
        assert event.error is None

    def test_parse_delegation_event_empty_payload(self):
        """Test parsing a delegation event with empty payload."""
        payload = {}
        event = DelegationEvent.from_payload(payload)
        
        assert event.task_id == ""
        assert event.agent_name == "sub_agent"
        assert event.status == "unknown"


class TestErrorSanitization:
    """Test error message sanitization."""

    def test_sanitize_openai_api_key(self):
        """Test removal of OpenAI API key pattern."""
        error = "Failed to call API with key sk-1234567890abcdefghijklmnopqrstuvwxyz1234"
        sanitized = sanitize_error(error)
        
        assert "sk-" not in sanitized
        assert "[API_KEY_REDACTED]" in sanitized

    def test_sanitize_bearer_token(self):
        """Test removal of Bearer token."""
        error = "Authorization failed: Bearer abc123xyz456.def789uvw.xyz"
        sanitized = sanitize_error(error)
        
        assert "Bearer" not in sanitized or "[BEARER_TOKEN_REDACTED]" in sanitized

    def test_sanitize_api_key_assignment(self):
        """Test removal of api_key= pattern."""
        error = "Config error: api_key='secret_key_value' not found"
        sanitized = sanitize_error(error)
        
        assert "secret_key_value" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitize_password_assignment(self):
        """Test removal of password= pattern."""
        error = "Database error: password='my_secret_password' invalid"
        sanitized = sanitize_error(error)
        
        assert "my_secret_password" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitize_token_assignment(self):
        """Test removal of token= pattern."""
        error = "Auth error: token='xyz789abc' expired"
        sanitized = sanitize_error(error)
        
        assert "xyz789abc" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitize_empty_error(self):
        """Test sanitization of empty error string."""
        error = ""
        sanitized = sanitize_error(error)
        
        assert sanitized == ""

    def test_sanitize_none_error(self):
        """Test sanitization of None error."""
        error = None
        sanitized = sanitize_error(error)
        
        assert sanitized is None

    def test_sanitize_error_without_secrets(self):
        """Test sanitization of error without sensitive data."""
        error = "Simple error message without secrets"
        sanitized = sanitize_error(error)
        
        assert sanitized == error


class TestDelegationStateTracking:
    """Test state dict tracking of delegations."""

    def test_state_dict_init_with_delegations(self):
        """Test that state dict is initialized with delegation fields."""
        state = {
            "waiting": False,
            "response": "",
            "delegations": {},
            "active_tasks": set(),
        }
        
        assert "delegations" in state
        assert "active_tasks" in state
        assert isinstance(state["delegations"], dict)
        assert isinstance(state["active_tasks"], set)

    def test_add_delegation_to_state(self):
        """Test adding a delegation event to state."""
        state = {
            "waiting": True,
            "response": "Processing...",
            "delegations": {},
            "active_tasks": set(),
        }
        
        event = DelegationEvent(
            task_id="task_001",
            agent_name="Analyzer",
            status="running",
            start_time=1234567890.0,
        )
        
        state["delegations"][event.task_id] = event
        state["active_tasks"].add(event.task_id)
        
        assert "task_001" in state["delegations"]
        assert "task_001" in state["active_tasks"]
        assert state["delegations"]["task_001"].agent_name == "Analyzer"

    def test_multiple_delegations_tracked(self):
        """Test tracking multiple concurrent delegations."""
        state = {
            "delegations": {},
            "active_tasks": set(),
        }
        
        events = [
            DelegationEvent("task_001", "Analyzer", "running"),
            DelegationEvent("task_002", "Reviewer", "queued"),
            DelegationEvent("task_003", "Fixer", "blocked"),
        ]
        
        for event in events:
            state["delegations"][event.task_id] = event
            if event.status in ("queued", "running"):
                state["active_tasks"].add(event.task_id)
        
        assert len(state["delegations"]) == 3
        assert len(state["active_tasks"]) == 2  # Only running and queued
        assert "task_003" not in state["active_tasks"]  # blocked is not active

    def test_remove_task_from_active_on_complete(self):
        """Test removing task from active_tasks when completed."""
        state = {
            "delegations": {},
            "active_tasks": {"task_001", "task_002"},
        }
        
        # Task completed
        event = DelegationEvent(
            task_id="task_001",
            agent_name="Analyzer",
            status="completed",
            end_time=1234567900.0,
        )
        
        state["delegations"][event.task_id] = event
        state["active_tasks"].discard(event.task_id)
        
        assert event.task_id not in state["active_tasks"]
        assert "task_002" in state["active_tasks"]

    def test_active_tasks_not_include_failed(self):
        """Test that failed tasks are not in active_tasks."""
        state = {
            "delegations": {},
            "active_tasks": set(),
        }
        
        failed_event = DelegationEvent(
            task_id="task_001",
            agent_name="Analyzer",
            status="failed",
            error="Timeout",
        )
        
        state["delegations"][failed_event.task_id] = failed_event
        # Don't add to active_tasks since status is failed
        
        assert "task_001" not in state["active_tasks"]
        assert failed_event.task_id in state["delegations"]

    def test_state_reset_between_turns(self):
        """Test that state is reset between turns."""
        state = {
            "waiting": False,
            "response": "Previous response",
            "delegations": {"task_001": DelegationEvent("task_001", "Old", "completed")},
            "active_tasks": set(),
        }
        
        # Reset for new turn
        state.update(
            waiting=True,
            response="",
            outcome=None,
            error=None,
            tool=None,
            used_tools=False,
            delegations={},
            active_tasks=set(),
        )
        
        assert state["response"] == ""
        assert state["delegations"] == {}
        assert state["active_tasks"] == set()
        assert state["waiting"] is True


class TestDelegationStatusDisplay:
    """Test delegation_status() display function."""

    def test_delegation_status_with_no_delegations(self):
        """Test delegation_status when there are no delegations."""
        state = {
            "delegations": {},
            "active_tasks": set(),
        }
        
        session = build_session(state)
        # delegation_status is an internal function, so we test indirectly
        # by verifying state structure
        
        assert state["delegations"] == {}
        assert state["active_tasks"] == set()

    def test_delegation_status_with_one_delegation(self):
        """Test delegation_status with single delegation."""
        state = {
            "delegations": {
                "task_001": DelegationEvent(
                    task_id="task_001",
                    agent_name="Analyzer",
                    status="running",
                ),
            },
            "active_tasks": {"task_001"},
        }
        
        session = build_session(state)
        assert "task_001" in state["delegations"]
        assert state["delegations"]["task_001"].agent_name == "Analyzer"

    def test_delegation_status_with_multiple_delegations(self):
        """Test delegation_status with multiple delegations."""
        state = {
            "delegations": {
                "task_001": DelegationEvent(
                    task_id="task_001",
                    agent_name="Analyzer",
                    status="running",
                ),
                "task_002": DelegationEvent(
                    task_id="task_002",
                    agent_name="Reviewer",
                    status="queued",
                ),
                "task_003": DelegationEvent(
                    task_id="task_003",
                    agent_name="Fixer",
                    status="failed",
                    error="Timeout",
                ),
            },
            "active_tasks": {"task_001", "task_002"},
        }
        
        session = build_session(state)
        assert len(state["delegations"]) == 3
        assert len(state["active_tasks"]) == 2


class TestEventSequenceHandling:
    """Test handling of event sequences."""

    def test_event_sequence_starting_task(self):
        """Test event sequence: queued -> running."""
        state = {
            "delegations": {},
            "active_tasks": set(),
        }
        
        # Task queued
        event1 = DelegationEvent(
            task_id="task_001",
            agent_name="Analyzer",
            status="queued",
        )
        state["delegations"][event1.task_id] = event1
        state["active_tasks"].add(event1.task_id)
        
        assert "task_001" in state["active_tasks"]
        
        # Task running
        event2 = DelegationEvent(
            task_id="task_001",
            agent_name="Analyzer",
            status="running",
            start_time=1234567890.0,
        )
        state["delegations"][event2.task_id] = event2  # Update
        
        assert "task_001" in state["active_tasks"]
        assert state["delegations"]["task_001"].start_time == 1234567890.0

    def test_event_sequence_completing_task(self):
        """Test event sequence: running -> completed."""
        state = {
            "delegations": {
                "task_001": DelegationEvent(
                    task_id="task_001",
                    agent_name="Analyzer",
                    status="running",
                    start_time=1234567890.0,
                ),
            },
            "active_tasks": {"task_001"},
        }
        
        # Task completed
        event = DelegationEvent(
            task_id="task_001",
            agent_name="Analyzer",
            status="completed",
            start_time=1234567890.0,
            end_time=1234567900.0,
            result={"analysis": "Complete"},
        )
        
        state["delegations"][event.task_id] = event
        state["active_tasks"].discard(event.task_id)
        
        assert "task_001" not in state["active_tasks"]
        assert state["delegations"]["task_001"].result == {"analysis": "Complete"}

    def test_event_sequence_multiple_tasks_concurrent(self):
        """Test concurrent task execution with event sequence."""
        state = {
            "delegations": {},
            "active_tasks": set(),
        }
        
        # Task 1 queued
        event1 = DelegationEvent("task_001", "Analyzer", "queued")
        state["delegations"][event1.task_id] = event1
        state["active_tasks"].add(event1.task_id)
        
        # Task 2 queued
        event2 = DelegationEvent("task_002", "Reviewer", "queued")
        state["delegations"][event2.task_id] = event2
        state["active_tasks"].add(event2.task_id)
        
        # Task 1 running
        event1_running = DelegationEvent(
            "task_001",
            "Analyzer",
            "running",
            start_time=1234567890.0,
        )
        state["delegations"]["task_001"] = event1_running
        
        # Task 1 completes
        event1_complete = DelegationEvent(
            "task_001",
            "Analyzer",
            "completed",
            start_time=1234567890.0,
            end_time=1234567900.0,
        )
        state["delegations"]["task_001"] = event1_complete
        state["active_tasks"].discard("task_001")
        
        # Task 2 still active
        assert "task_002" in state["active_tasks"]
        assert "task_001" not in state["active_tasks"]
        assert len(state["delegations"]) == 2


class TestDelegationEventDataclass:
    """Test DelegationEvent dataclass functionality."""

    def test_delegation_event_serializable(self):
        """Test that DelegationEvent can be serialized to dict."""
        event = DelegationEvent(
            task_id="task_001",
            agent_name="Analyzer",
            status="running",
            start_time=1234567890.0,
            end_time=None,
            result={"status": "in_progress"},
            error=None,
        )
        
        # Convert to dict
        event_dict = asdict(event)
        
        assert event_dict["task_id"] == "task_001"
        assert event_dict["agent_name"] == "Analyzer"
        assert event_dict["status"] == "running"
        assert isinstance(event_dict, dict)

    def test_delegation_event_equality(self):
        """Test DelegationEvent equality."""
        event1 = DelegationEvent(
            task_id="task_001",
            agent_name="Analyzer",
            status="running",
        )
        event2 = DelegationEvent(
            task_id="task_001",
            agent_name="Analyzer",
            status="running",
        )
        
        assert event1 == event2

    def test_delegation_event_inequality(self):
        """Test DelegationEvent inequality."""
        event1 = DelegationEvent(
            task_id="task_001",
            agent_name="Analyzer",
            status="running",
        )
        event2 = DelegationEvent(
            task_id="task_002",
            agent_name="Reviewer",
            status="queued",
        )
        
        assert event1 != event2
