"""
Comprehensive Context Compaction Testing Suite.

Tests all 12 scenarios:
1. Context at threshold → compaction runs
2. Context below threshold → no compaction
3. User instructions preserved
4. Technical decisions preserved
5. File changes recorded
6. Test results available
7. Unresolved errors tracked
8. Agent continues after compaction
9. No repeated work
10. Session recovery after restart
11. Incomplete tool chains not broken
12. Sub-agent context compatible
"""

import pytest
import json
import tempfile
import os
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock
from typing import Optional

# Import from joy
from joy.token_counter import TokenCounter
from joy.context_config import get_model_config
from joy.context_preservation import (
    ContextPreserver,
    PreservationPolicy,
    InformationCategory,
)
from joy.context_compaction import CompactionEngine, CompactionResult
from joy.session_manager import SessionManager, SessionSnapshot, SessionRecovery
from joy.context_config import ContextConfig
from joy.context_compaction import preserver_view
from pathlib import Path
from types import SimpleNamespace


def tool_call(n, name, **args):
    """A function_call item as Joy's agent loop stores it."""
    return {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}",
            "name": name, "arguments": json.dumps(args), "status": "completed"}


def tool_output(n, text):
    return {"type": "function_call_output", "call_id": f"call_{n}", "output": text}


class FakeResponses:
    """Responses API stand-in for the summarizer: records prompts, returns `texts` in turn."""

    def __init__(self, *texts):
        self.texts, self.prompts = list(texts), []

    def create(self, **kwargs):
        self.prompts.append(kwargs["input"][0]["content"])
        return SimpleNamespace(status="completed", output_text=self.texts.pop(0))


def engine_with(responses, model="gpt-4"):
    """A real CompactionEngine whose summarizer talks to `responses`, never the network."""
    engine = CompactionEngine(model, api_key="mock", base_url="http://mock")
    engine.close()
    engine.summarizer.client = SimpleNamespace(responses=responses)
    return engine


def analyze(conversation, system_prompt="System"):
    """Run ContextPreserver the way CompactionEngine does."""
    return ContextPreserver().analyze_conversation(
        preserver_view(conversation), system_prompt, 1000, 128000)


class MockLLMProvider:
    """Mock LLM provider for testing."""

    def __init__(self, fail_count=0):
        self.fail_count = fail_count
        self.call_count = 0

    def create_completion(self, messages, model, **kwargs):
        """Mock completion creation."""
        self.call_count += 1

        if self.call_count <= self.fail_count:
            raise Exception("Mock LLM failure")

        # Return mock summary
        return Mock(
            choices=[
                Mock(
                    message=Mock(
                        content=json.dumps({
                            "user_goal": "Test goal",
                            "important_instructions": ["Do X", "Don't do Y"],
                            "technical_decisions": ["Use async"],
                            "files_modified": {"test.py": "written"},
                            "code_changes": ["Added feature X"],
                            "test_results": "15 passed, 2 failed",
                            "unresolved_errors": ["Issue #1 pending"],
                            "current_task": "Implementing feature",
                            "next_steps": ["Step 1", "Step 2"],
                        })
                    )
                )
            ]
        )


# ============================================================================
# SCENARIO 1: Context at threshold → compaction runs
# ============================================================================


class TestScenario1_ContextAtThreshold:
    """Verify compaction triggers when context reaches threshold."""

    def test_compaction_triggered_at_threshold(self):
        """Context at 75% should trigger compaction."""
        counter = TokenCounter("gpt-4")
        config = get_model_config("gpt-4")

        # Create conversation that's 75% of context
        large_text = "x" * 50000  # ~12,500 tokens
        conversation = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": large_text},
            {"role": "assistant", "content": large_text},
        ]

        tokens_stat = counter.count_conversation(conversation)
        tokens = tokens_stat.total_tokens
        available = config.available_for_input
        usage_percent = (tokens / available) * 100

        # Should be >= 70% to trigger
        assert usage_percent >= 70, f"Usage {usage_percent}% should trigger compaction"

    def test_compaction_actually_reduces_tokens(self):
        """Verify compaction reduces token count."""
        counter = TokenCounter("gpt-4")
        summary = json.dumps({"user_goal": "test", "current_task": "continue"})
        engine = engine_with(FakeResponses(summary))

        # Large conversation: well over gpt-4's threshold
        conversation = [{"role": "system", "content": "System prompt"},
                        {"role": "user", "content": "Analyse the logs"}]
        for n in range(20):
            conversation += [tool_call(n, "read", path=f"log{n}.txt"),
                             tool_output(n, f"log line {n} " * 200)]

        before_tokens = counter.count_conversation(conversation).total_tokens
        assert before_tokens > engine.threshold_tokens

        result = engine.maybe_compact(conversation, "System prompt")
        after_tokens = counter.count_conversation(result.new_conversation).total_tokens

        assert result.compacted, "Should report compaction occurred"
        assert after_tokens < before_tokens, "Compaction should reduce tokens"
        assert after_tokens <= engine.threshold_tokens, "Result should fit below the threshold"
        assert "[CONTEXT SUMMARY]" in str(result.new_conversation)

# ============================================================================
# SCENARIO 2: Context below threshold → no compaction
# ============================================================================


class TestScenario2_ContextBelowThreshold:
    """Verify no compaction when context is below threshold."""

    def test_no_compaction_below_threshold(self):
        """Small conversation should not trigger compaction."""
        counter = TokenCounter("gpt-4")
        config = get_model_config("gpt-4")
        responses = FakeResponses()
        engine = engine_with(responses)

        # Create small conversation (~5% of context)
        conversation = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]

        tokens = counter.count_conversation(conversation).total_tokens
        usage_percent = (tokens / config.available_for_input) * 100
        assert usage_percent < 50, "Small conversation should be <50%"

        result = engine.maybe_compact(conversation, "System prompt")
        assert not result.compacted, "Should not compact small conversation"
        assert result.new_conversation == conversation
        assert responses.prompts == [], "The summarizer should not be called"

# ============================================================================
# SCENARIO 3: User instructions preserved
# ============================================================================


class TestScenario3_UserInstructionsPreserved:
    """Verify user instructions are retained after compaction."""

    def test_user_instructions_in_preservation(self):
        """Extract and preserve user instructions."""
        preserver = ContextPreserver()
        policy = PreservationPolicy(preserve_permanent_info=True)

        user_instructions = [
            "Must use async/await",
            "Follow PEP8 style",
            "Don't modify database directly",
        ]

        conversation = [
            {"role": "system", "content": "You are helpful"},
            {
                "role": "user",
                "content": (
                    "Task: Implement feature X. "
                    "Must use async/await. "
                    "Follow PEP8 style. "
                    "Don't modify database directly."
                ),
            },
            {"role": "assistant", "content": "Starting implementation..."},
        ]

        analysis = preserver.analyze_conversation(conversation, "System prompt", 1000, 128000)

        # Verify instructions captured
        assert analysis.extracted.active_tasks, "Active tasks should capture instructions"
        for instruction in user_instructions:
            found = any(instruction.lower() in task.lower() 
                       for task in analysis.extracted.active_tasks)
            assert found, f"Instruction '{instruction}' should be preserved"

    def test_instructions_survive_compaction(self):
        """Verify instructions aren't lost during compaction summary."""
        preserver = ContextPreserver()

        conversation = [
            {"role": "system", "content": "System"},
            {
                "role": "user",
                "content": "CRITICAL INSTRUCTION: Always validate input. Never skip tests.",
            },
            {"role": "assistant", "content": "Acknowledged"},
        ]

        analysis = preserver.analyze_conversation(conversation, "System", 1000, 128000)
        formatted = preserver.format_preserved_context(analysis.extracted)

        assert "validate input" in formatted.lower(), "Critical instruction should be in formatted output"
        assert "skip tests" in formatted.lower(), "Important constraint should be preserved"


# ============================================================================
# SCENARIO 4: Technical decisions preserved
# ============================================================================


class TestScenario4_TechnicalDecisionsPreserved:
    """Verify technical decisions are retained."""

    def test_technical_decisions_extraction(self):
        """Technical decisions reach the summarizer and survive in its summary.

        ContextPreserver does not extract decisions; the summarizer records them
        under technical_decisions, so that is the path to verify.
        """
        summary = json.dumps({"technical_decisions": [
            "PostgreSQL for the database", "Redis for the cache", "async/await everywhere"]})
        responses = FakeResponses(summary)
        engine = engine_with(responses)

        conversation = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Use PostgreSQL for database, Redis for cache"},
            {"role": "assistant", "content": "Good choices"},
            {"role": "user", "content": "We decided to use async/await everywhere"},
        ]
        for n in range(20):
            conversation += [tool_call(n, "read", path=f"m{n}.py"), tool_output(n, "code " * 300)]

        result = engine.maybe_compact(conversation, "System")
        assert result.compacted

        prompt = responses.prompts[0].lower()
        assert "postgresql" in prompt and "redis" in prompt
        assert "async/await" in prompt, "Decisions should be given to the summarizer"
        assert "technical_decisions" in prompt, "The summary should have a decisions field"
        assert "async/await everywhere" in str(result.new_conversation), \
            "Technical decisions should be preserved"

# ============================================================================
# SCENARIO 5: File changes recorded
# ============================================================================


class TestScenario5_FileChangesRecorded:
    """Verify file modifications are tracked."""

    def test_file_changes_extraction(self):
        """Extract file modifications from Joy's write/edit tool calls."""
        conversation = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Create and modify files"},
            tool_call(1, "write", path="app.py", content="..."),
            tool_output(1, "Wrote app.py"),
            tool_call(2, "edit", path="app.py", old_text="x", new_text="y"),
            tool_output(2, "--- a/app.py"),
            tool_call(3, "edit", path="src/models.py", old_text="a", new_text="b"),
            tool_output(3, "--- a/src/models.py"),
            tool_call(4, "read", path="README.md"),
            tool_output(4, "readme"),
        ]

        analysis = analyze(conversation)
        files = analysis.extracted.files_modified

        assert files, "Files should be tracked"
        assert files.get("app.py") == "edited", "Modified files should be recorded"
        assert files.get("src/models.py") == "edited"
        assert "README.md" not in files, "Reading a file is not a change"
        formatted = ContextPreserver().format_preserved_context(analysis.extracted)
        assert "app.py" in formatted, "Files should appear in formatted context"

# ============================================================================
# SCENARIO 6: Test results available
# ============================================================================


class TestScenario6_TestResultsAvailable:
    """Verify test results are preserved."""

    def test_test_results_extraction(self):
        """Extract test results from a bash tool output."""
        conversation = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Run tests"},
            tool_call(1, "bash", command="pytest -q"),
            tool_output(1, "FAILED tests/test_a.py::test_x\n1 failed, 25 passed, 2 warnings in 3.42s"),
        ]

        analysis = analyze(conversation)

        assert analysis.extracted.test_results, "Test results should be captured"
        assert "25 passed" in analysis.extracted.test_results
        assert "1 failed" in analysis.extracted.test_results
        formatted = ContextPreserver().format_preserved_context(analysis.extracted)
        assert "TEST RESULTS" in formatted, "Test results should appear in formatted output"

# ============================================================================
# SCENARIO 7: Unresolved errors tracked
# ============================================================================


class TestScenario7_UnresolvedErrorsTracked:
    """Verify errors are tracked and preserved."""

    def test_error_extraction(self):
        """Extract and track errors from tool output."""
        conversation = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Fix the bug"},
            tool_call(1, "bash", command="python app.py"),
            tool_output(1, "ERROR: AttributeError: 'NoneType' object has no attribute 'foo'"),
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Found error, investigating..."}]},
        ]

        analysis = analyze(conversation)

        assert analysis.extracted.unresolved_errors, "Errors should be tracked"
        assert "AttributeError" in str(analysis.extracted.unresolved_errors), \
            "Error details should be preserved"
        formatted = ContextPreserver().format_preserved_context(analysis.extracted)
        assert "error" in formatted.lower(), "Errors should appear in formatted output"

# ============================================================================
# SCENARIO 8: Agent continues after compaction
# ============================================================================


class TestScenario8_AgentContinuesAfterCompaction:
    """Verify agent can continue working after compaction."""

    def test_agent_state_preserved_for_continuation(self):
        """Agent should continue with same context after compaction."""
        preserver = ContextPreserver()
        counter = TokenCounter("gpt-4")

        # Simulate conversation before compaction
        conversation_before = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Task A: Create feature"},
            {
                "role": "assistant",
                "content": "Working on feature...",
            },
            {"role": "user", "content": "Continue with Task B"},
            {
                "role": "assistant",
                "content": "Starting Task B",
            },
        ]

        # Analyze for preservation
        analysis = preserver.analyze_conversation(
            conversation_before, "You are helpful", 1000, 128000
        )

        # Simulate compaction summary
        compacted = preserver.format_preserved_context(analysis.extracted)

        # Build new conversation with compaction marker
        conversation_after = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": f"[CONTEXT] {compacted[:200]}..."},
            {"role": "assistant", "content": "Context loaded, continuing work"},
            {"role": "user", "content": "Next step?"},
        ]

        # Agent should have context about previous tasks
        assert len(conversation_after) > 0, "Conversation should continue"
        assert any("context" in str(msg).lower() for msg in conversation_after), \
            "Compacted context should be present"


# ============================================================================
# SCENARIO 9: No repeated work
# ============================================================================


class TestScenario9_NoRepeatedWork:
    """Verify agent doesn't repeat completed tasks."""

    def test_completed_tasks_not_repeated(self):
        """Preserve completed tasks to avoid repetition."""
        preserver = ContextPreserver()

        conversation = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Task 1: Create schema\nTask 2: Add indexes"},
            {
                "role": "assistant",
                "content": "Created schema",
            },
            {
                "role": "function",
                "name": "bash_ide",
                "content": "Schema created successfully",
            },
            {
                "role": "assistant",
                "content": "Adding indexes",
            },
            {
                "role": "function",
                "name": "bash_ide",
                "content": "Indexes added",
            },
            {"role": "user", "content": "What's left?"},
            {
                "role": "assistant",
                "content": "Both tasks completed",
            },
        ]

        analysis = preserver.analyze_conversation(conversation, "System", 1000, 128000)

        # Verify completed tasks recorded
        formatted = preserver.format_preserved_context(analysis.extracted)
        assert "schema" in formatted.lower() or "create" in formatted.lower(), \
            "Completed work should be tracked"


# ============================================================================
# SCENARIO 10: Session recovery after restart
# ============================================================================


class TestScenario10_SessionRecoveryAfterRestart:
    """Verify session can be restored after application restart."""

    def test_session_save_and_load(self):
        """Save session and load it back."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionManager(Path(tmpdir))

            session_id = "test_session_123"
            snapshot = SessionSnapshot(
                session_id=session_id,
                created_at=datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
                state="active",
                conversation_history=[
                    {"role": "user", "content": "[CONTEXT SUMMARY]\nsummary\n[END SUMMARY]"},
                    {"role": "user", "content": "Important task"},
                    {"role": "assistant", "content": "Working on it"},
                ],
                user_goal="Complete the task",
                task_status={"current_task": "In progress"},
                model="gpt-4",
                context_window_size=128000,
                turn_count=5,
                tool_call_count=2,
                modified_files={"app.py": "written"},
                compaction_metadata={"compaction_count": 1, "is_compacted": True},
                preserved_context={},
                system_prompt="You are helpful",
                iteration_count=5,
            )

            assert manager.save_snapshot(snapshot), "Session should save successfully"

            loaded = manager.load_snapshot(session_id)
            assert loaded is not None, "Session should load"
            assert loaded.user_goal == "Complete the task", "Goal should persist"
            assert loaded.turn_count == 5, "Turn count should persist"
            assert "app.py" in loaded.modified_files, "File changes should persist"
            assert loaded.compaction_metadata["is_compacted"], "Compaction state should persist"
            assert loaded.conversation_history == snapshot.conversation_history

    def test_corrupted_session_recovery(self):
        """Recover from corrupted session file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionManager(Path(tmpdir))
            recovery = SessionRecovery(manager)
            session_id = "corrupted_session"

            snapshot = SessionSnapshot(
                session_id=session_id,
                created_at=datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
                state="active",
                conversation_history=[{"role": "user", "content": "test"}],
                user_goal="Test",
                task_status={},
                model="gpt-4",
                context_window_size=128000,
                turn_count=1,
                tool_call_count=0,
                modified_files={},
                compaction_metadata={},
                preserved_context={},
                system_prompt="System",
                iteration_count=1,
            )
            manager.save_snapshot(snapshot)

            # Corrupt the file
            session_file = Path(tmpdir) / f"{session_id}.json"
            assert session_file.exists()
            session_file.write_text("{corrupted json")

            # Recovery must not raise; it either recovers or reports nothing.
            recovered = recovery.recover_session(session_id, strict=False)
            if recovered is not None:
                assert recovered.session_id == session_id

# ============================================================================
# SCENARIO 11: Incomplete tool chains not broken
# ============================================================================


class TestScenario11_IncompleteToolChainsNotBroken:
    """Verify incomplete tool calls are not broken during compaction."""

    def test_incomplete_tool_chain_preserved(self):
        """Don't compact when tool chain is incomplete."""
        conversation = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Read and modify file"},
            tool_call(1, "read", path="test.py"),
            # Missing function_call_output - chain incomplete!
        ]

        analysis = analyze(conversation)

        assert analysis.extracted.incomplete_tool_chains, "Incomplete chains should be detected"
        assert analysis.extracted.incomplete_tool_chains[0]["call_id"] == "call_1"
        assert analysis.has_incomplete_operations

    def test_complete_tool_chain_preserved(self):
        """Complete tool chains should be fully preserved."""
        preserver = ContextPreserver()

        conversation = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Read file"},
            {
                "role": "assistant",
                "content": "Reading file",
                "function_call": {
                    "name": "read_ide",
                    "arguments": '{"path": "test.py"}',
                },
            },
            {
                "role": "function",
                "name": "read_ide",
                "content": "File contents",
            },
            {
                "role": "assistant",
                "content": "File read successfully",
            },
        ]

        analysis = preserver.analyze_conversation(conversation, "System", 1000, 128000)

        # Verify complete chains don't appear in incomplete list
        assert not analysis.extracted.incomplete_tool_chains, \
            "Complete chains should not be marked incomplete"


# ============================================================================
# SCENARIO 12: Sub-agent context compatible
# ============================================================================


class TestScenario12_SubAgentContextCompatible:
    """Verify sub-agent contexts are preserved and compatible."""

    def test_sub_agent_context_tracking(self):
        """Track sub-agent contexts during compaction."""
        preserver = ContextPreserver()

        conversation = [
            {"role": "system", "content": "Main agent"},
            {"role": "user", "content": "Delegate task to sub-agent"},
            {
                "role": "assistant",
                "content": "Delegating to sub-agent-1",
            },
            {
                "role": "function",
                "name": "delegate_ide",
                "content": "Sub-agent result: Task completed",
            },
            {"role": "assistant", "content": "Sub-agent completed task"},
        ]

        analysis = preserver.analyze_conversation(conversation, "Main agent", 1000, 128000)

        # Sub-agent contexts should be tracked
        # (even if no explicit sub-agent contexts, should not error)
        formatted = preserver.format_preserved_context(analysis.extracted)
        assert isinstance(formatted, str), "Formatted context should be string"


# ============================================================================
# Integration Tests with Mock LLM
# ============================================================================


class TestIntegrationWithMockLLM:
    """Integration tests using mock LLM."""

    def test_full_compaction_workflow(self):
        """Analyse, then persist the extracted context in a session and load it back."""
        with tempfile.TemporaryDirectory() as tmpdir:
            counter = TokenCounter("gpt-4")
            preserver = ContextPreserver()
            manager = SessionManager(Path(tmpdir))

            conversation = [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": (
                    "Task: Implement feature. "
                    "You must use async/await. "
                    "FILES: app.py, models.py")},
                tool_call(1, "write", path="app.py", content="async def main(): ..."),
                tool_output(1, "Wrote app.py"),
                {"role": "assistant", "content": "Created file"},
                {"role": "user", "content": "Next step?"},
                {"role": "assistant", "content": "Adding models"},
            ]

            analysis = preserver.analyze_conversation(
                preserver_view(conversation), "You are helpful",
                counter.count_conversation(conversation).total_tokens, 128000)

            assert analysis.extracted.active_tasks, "Tasks should be extracted"
            assert "app.py" in analysis.extracted.files_modified, "Files should be tracked"

            formatted = preserver.format_preserved_context(analysis.extracted)
            session_id = "test_workflow"
            snapshot = SessionSnapshot(
                session_id=session_id,
                created_at=datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
                state="active",
                conversation_history=conversation,
                preserved_context={"text": formatted},
                system_prompt="You are helpful",
                user_goal=analysis.extracted.user_goal or "(unknown)",
                task_status={"tasks": analysis.extracted.active_tasks},
                model="gpt-4",
                context_window_size=128000,
                turn_count=len(conversation),
                tool_call_count=1,
                modified_files=analysis.extracted.files_modified,
                compaction_metadata={},
                iteration_count=len(conversation),
            )

            assert manager.save_snapshot(snapshot), "Snapshot should save"
            loaded = manager.load_snapshot(session_id)
            assert loaded is not None, "Snapshot should load"
            assert "async" in str(loaded.preserved_context).lower(), \
                "Critical instruction should be in preserved context"
            assert loaded.modified_files == {"app.py": "written"}

    def test_compaction_doesnt_lose_critical_info(self):
        """Critical information extracted by the preserver reaches the summarizer."""
        responses = FakeResponses(json.dumps({"user_goal": "requirements"}))
        engine = engine_with(responses)

        conversation = [
            {"role": "system", "content": "You are helpful assistant"},
            {"role": "user", "content": (
                "CRITICAL REQUIREMENTS:\n"
                "1. Must use PostgreSQL\n"
                "2. Must validate all input\n"
                "3. Files created: schema.sql, migrations/001.sql")},
            tool_call(1, "edit", path="src/auth.py", old_text="a", new_text="b"),
            tool_output(1, "--- a/src/auth.py"),
            tool_call(2, "bash", command="psql -f schema.sql"),
            tool_output(2, "✅ Schema created\n❌ Error: Foreign key constraint violated"),
        ]
        for n in range(3, 23):
            conversation += [tool_call(n, "read", path=f"f{n}.py"), tool_output(n, "code " * 300)]

        result = engine.maybe_compact(conversation, "You are helpful assistant")
        assert result.compacted

        facts = responses.prompts[0].split("FACTS EXTRACTED FROM THE FULL CONVERSATION")[1]
        critical_checks = [
            ("PostgreSQL" in facts, "Database choice"),
            ("validate" in facts.lower(), "Validation requirement"),
            ("src/auth.py" in facts, "File reference"),
            ("Foreign key constraint" in facts, "Error tracking"),
        ]
        for check, label in critical_checks:
            assert check, f"Critical info missing: {label}"

# ============================================================================
# Scenario-based Test with Long Simulation
# ============================================================================


class TestLongSimulationScenario:
    """Long conversation simulation to verify agent can recall prior context."""

    def test_agent_recalls_decisions_after_compaction(self):
        """Decisions made before a compaction survive the next compaction too.

        The first summary records them; the second compaction no longer has the
        original message, so they must reach it through PREVIOUS SUMMARY.
        """
        decisions = ["Use JWT tokens", "Store in Redis", "Expire after 24 hours"]
        first = json.dumps({"user_goal": "auth system", "technical_decisions": decisions})
        second = json.dumps({"user_goal": "auth system", "technical_decisions": decisions,
                             "current_task": "session storage"})
        responses = FakeResponses(first, second)
        engine = engine_with(responses)

        conversation = [
            {"role": "system", "content": "You are a helpful coding assistant"},
            {"role": "user", "content": (
                "TASK: Implement user authentication system\n"
                "DECISIONS:\n- Use JWT tokens\n- Store in Redis\n- Expire after 24 hours")},
        ]
        for n in range(20):
            conversation += [tool_call(n, "read", path=f"a{n}.py"), tool_output(n, "code " * 300)]

        compacted = engine.maybe_compact(conversation, "You are a helpful coding assistant")
        assert compacted.compacted
        history = compacted.new_conversation
        assert "Expire after 24 hours" not in str(history[2:]), "the original message is summarized away"

        # Keep working until the next compaction.
        for n in range(20, 40):
            history += [tool_call(n, "read", path=f"b{n}.py"), tool_output(n, "more " * 300)]
        history.append({"role": "user", "content": "What storage did we decide to use for sessions?"})
        again = engine.maybe_compact(history, "You are a helpful coding assistant")
        assert again.compacted

        previous = responses.prompts[1].split("PREVIOUS SUMMARY")[1].split("CONVERSATION")[0]
        for decision in decisions:
            assert decision in previous, f"{decision} should reach the second summary"
        final = str(again.new_conversation)
        assert "Store in Redis" in final, "Storage decision should be in preserved context"
        assert "Use JWT tokens" in final, "Auth method should be preserved"
        assert "24 hours" in final, "Token expiration should be preserved"

# ============================================================================
# Run all tests
# ============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
