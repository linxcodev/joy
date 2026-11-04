"""Tests for Context Preservation module.

Tests ensure that critical information is preserved during context compaction
and that preservation mechanisms work correctly.
"""

import pytest
from joy.context_preservation import (
    InformationCategory,
    PreservationPolicy,
    ExtractedInformation,
    AnalysisResult,
    PreservationAnalyzer,
    ContextPreserver,
)


class TestInformationCategory:
    """Test information categorization."""

    def test_category_values(self):
        """Test that categories have correct values."""
        assert InformationCategory.PERMANENT.value == "permanent"
        assert InformationCategory.ACTIVE.value == "active"
        assert InformationCategory.IRRELEVANT.value == "irrelevant"


class TestPreservationPolicy:
    """Test preservation policy configuration."""

    def test_default_policy(self):
        """Test default policy values."""
        policy = PreservationPolicy()
        assert policy.preserve_recent_turns == 5
        assert policy.preserve_permanent_info is True
        assert policy.preserve_active_tasks is True
        assert policy.preserve_incomplete_chains is True
        assert policy.preserve_error_messages is True
        assert policy.preserve_file_changes is True
        assert policy.preserve_test_results is True

    def test_custom_policy(self):
        """Test custom policy configuration."""
        policy = PreservationPolicy(
            preserve_recent_turns=10,
            preserve_incomplete_chains=False,
            max_preserved_tokens=1000,
        )
        assert policy.preserve_recent_turns == 10
        assert policy.preserve_incomplete_chains is False
        assert policy.max_preserved_tokens == 1000


class TestExtractedInformation:
    """Test extracted information structure."""

    def test_empty_extraction(self):
        """Test creating empty extraction."""
        extracted = ExtractedInformation()
        assert extracted.user_goal is None
        assert extracted.system_instructions is None
        assert extracted.active_tasks == []
        assert extracted.files_modified == {}
        assert extracted.unresolved_errors == []

    def test_extraction_with_data(self):
        """Test extraction with populated data."""
        extracted = ExtractedInformation(
            user_goal="Implement feature X",
            active_tasks=["Task 1", "Task 2"],
            unresolved_errors=["Error A", "Error B"],
        )
        assert extracted.user_goal == "Implement feature X"
        assert len(extracted.active_tasks) == 2
        assert len(extracted.unresolved_errors) == 2


class TestPreservationAnalyzer:
    """Test conversation analysis and information extraction."""

    def test_analyzer_initialization(self):
        """Test analyzer can be created."""
        analyzer = PreservationAnalyzer()
        assert analyzer is not None

    def test_analyze_empty_conversation(self):
        """Test analyzing empty conversation."""
        analyzer = PreservationAnalyzer()
        result = analyzer.analyze(
            conversation=[],
            system_prompt="You are a helpful assistant.",
            token_count=100,
            context_limit=4000,
        )
        assert result.turn_count == 0
        assert result.tool_call_count == 0
        assert not result.has_incomplete_operations

    def test_analyze_simple_conversation(self):
        """Test analyzing simple conversation."""
        analyzer = PreservationAnalyzer()
        conversation = [
            {"role": "user", "type": "text", "content": "Implement a feature"},
            {"role": "assistant", "type": "text", "content": "I will help with that."},
        ]
        result = analyzer.analyze(
            conversation=conversation,
            system_prompt="You are Joy agent.",
            token_count=200,
            context_limit=4000,
        )
        assert result.turn_count == 1
        assert result.extracted.user_goal == "Implement a feature"

    def test_extract_file_changes(self):
        """Test extracting file changes from messages."""
        analyzer = PreservationAnalyzer()
        conversation = [
            {"role": "user", "type": "text", "content": "Edit file.py"},
            {
                "type": "function_call",
                "name": "edit",  # Joy's edit tool
                "call_id": "123",
                "arguments": '{"path": "src/example.py"}',
            },
            {
                "type": "function_call_output",
                "call_id": "123",
                "output": "File edited successfully",
            },
        ]
        result = analyzer.analyze(
            conversation=conversation,
            system_prompt="",
            token_count=300,
            context_limit=4000,
        )
        assert "src/example.py" in result.extracted.files_modified or any(
            "example.py" in path for path in result.extracted.files_modified
        )

    def test_extract_errors(self):
        """Test extracting error messages."""
        analyzer = PreservationAnalyzer()
        conversation = [
            {
                "role": "assistant",
                "type": "text",
                "content": "Error: File not found: /missing/file.txt",
            },
        ]
        result = analyzer.analyze(
            conversation=conversation,
            system_prompt="",
            token_count=200,
            context_limit=4000,
        )
        assert len(result.extracted.unresolved_errors) > 0
        assert any(
            "File not found" in err or "missing" in err.lower()
            for err in result.extracted.unresolved_errors
        )

    def test_detect_incomplete_tool_chains(self):
        """Test detecting incomplete tool chains."""
        analyzer = PreservationAnalyzer()
        conversation = [
            {
                "type": "function_call",
                "name": "read_ide",
                "call_id": "call_1",
                "arguments": '{"path": "file.py"}',
            },
            # No corresponding function_call_output
            {
                "type": "function_call",
                "name": "write_ide",
                "call_id": "call_2",
                "arguments": '{"path": "new.py"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_2",
                "output": "File written",
            },
        ]
        result = analyzer.analyze(
            conversation=conversation,
            system_prompt="",
            token_count=300,
            context_limit=4000,
        )
        assert result.has_incomplete_operations
        assert len(result.extracted.incomplete_tool_chains) >= 1

    def test_extract_test_results(self):
        """Test extracting test results."""
        analyzer = PreservationAnalyzer()
        conversation = [
            {
                "role": "assistant",
                "type": "text",
                "content": "Test results: 15 passed, 2 failed ✅ ❌",
            },
        ]
        result = analyzer.analyze(
            conversation=conversation,
            system_prompt="",
            token_count=200,
            context_limit=4000,
        )
        assert result.extracted.test_results is not None

    def test_approaching_limit_detection(self):
        """Test detection of approaching context limit."""
        analyzer = PreservationAnalyzer()
        conversation = [
            {"role": "user", "type": "text", "content": "Help me with code"},
        ]
        result = analyzer.analyze(
            conversation=conversation,
            system_prompt="",
            token_count=3500,  # 87.5% of 4000
            context_limit=4000,
        )
        assert result.is_approaching_limit


class TestContextPreserver:
    """Test context preservation functionality."""

    def test_preserver_initialization(self):
        """Test preserver can be created."""
        preserver = ContextPreserver()
        assert preserver is not None
        assert preserver.analyzer is not None

    def test_analyze_conversation(self):
        """Test analyzing conversation through preserver."""
        preserver = ContextPreserver()
        conversation = [
            {"role": "user", "type": "text", "content": "Implement a REST API"},
            {
                "role": "assistant",
                "type": "text",
                "content": "I'll create the REST API now.",
            },
        ]
        result = preserver.analyze_conversation(
            conversation=conversation,
            system_prompt="You are a coding assistant.",
            token_count=200,
            context_limit=4000,
        )
        assert isinstance(result, AnalysisResult)
        assert result.extracted.user_goal is not None

    def test_apply_preservation_with_default_policy(self):
        """Test applying preservation with default policy."""
        preserver = ContextPreserver()
        conversation = [
            {"role": "user", "type": "text", "content": "Implement feature"},
            {
                "role": "assistant",
                "type": "text",
                "content": "Error: Something failed",
            },
        ]
        analysis = preserver.analyze_conversation(
            conversation=conversation,
            system_prompt="You are helpful.",
            token_count=200,
            context_limit=4000,
        )
        preserved = preserver.apply_preservation(analysis, conversation)
        assert preserved.user_goal == "Implement feature"
        assert preserved.system_instructions is not None

    def test_apply_preservation_with_custom_policy(self):
        """Test applying preservation with custom policy."""
        preserver = ContextPreserver()
        conversation = [
            {"role": "user", "type": "text", "content": "Task"},
            {
                "type": "function_call",
                "name": "read_ide",
                "call_id": "c1",
                "arguments": '{"path": "file.py"}',
            },
        ]
        analysis = preserver.analyze_conversation(
            conversation=conversation,
            system_prompt="Assistant.",
            token_count=200,
            context_limit=4000,
        )
        policy = PreservationPolicy(preserve_incomplete_chains=False)
        preserved = preserver.apply_preservation(analysis, conversation, policy)
        assert preserved.incomplete_tool_chains == []

    def test_validate_preservation_success(self):
        """Test validation of successful preservation."""
        preserver = ContextPreserver()
        original = ExtractedInformation(
            user_goal="Build app",
            system_instructions="Be helpful.",
            unresolved_errors=["Error 1", "Error 2"],
            incomplete_tool_chains=[{"call_id": "c1", "name": "read_ide"}],
        )
        preserved = ExtractedInformation(
            user_goal="Build app",
            system_instructions="Be helpful.",
            unresolved_errors=["Error 1", "Error 2"],
            incomplete_tool_chains=[{"call_id": "c1", "name": "read_ide"}],
        )
        assert preserver.validate_preservation(original, preserved)

    def test_validate_preservation_failure_missing_goal(self):
        """Test validation fails when user goal is missing."""
        preserver = ContextPreserver()
        original = ExtractedInformation(user_goal="Build app")
        preserved = ExtractedInformation(user_goal=None)
        assert not preserver.validate_preservation(original, preserved)

    def test_validate_preservation_failure_lost_errors(self):
        """Test validation fails when errors are lost."""
        preserver = ContextPreserver()
        original = ExtractedInformation(
            user_goal="Task",
            system_instructions="Assist.",
            unresolved_errors=["Error 1", "Error 2"],
        )
        preserved = ExtractedInformation(
            user_goal="Task",
            system_instructions="Assist.",
            unresolved_errors=["Error 1"],  # Lost one error
        )
        assert not preserver.validate_preservation(original, preserved)

    def test_validate_preservation_failure_lost_chains(self):
        """Test validation fails when incomplete chains are lost."""
        preserver = ContextPreserver()
        original = ExtractedInformation(
            user_goal="Task",
            system_instructions="Assist.",
            incomplete_tool_chains=[
                {"call_id": "c1", "name": "read_ide"},
                {"call_id": "c2", "name": "write_ide"},
            ],
        )
        preserved = ExtractedInformation(
            user_goal="Task",
            system_instructions="Assist.",
            incomplete_tool_chains=[{"call_id": "c1", "name": "read_ide"}],  # Lost one
        )
        assert not preserver.validate_preservation(original, preserved)

    def test_format_preserved_context(self):
        """Test formatting preserved information."""
        preserver = ContextPreserver()
        extracted = ExtractedInformation(
            user_goal="Build REST API",
            active_tasks=["Implement endpoints", "Add validation"],
            files_modified={"app.py": "edited", "api.py": "written"},
            unresolved_errors=["Missing dependency"],
            incomplete_tool_chains=[{"call_id": "c1", "name": "read_ide"}],
            next_steps=["Test the API", "Document endpoints"],
        )
        formatted = preserver.format_preserved_context(extracted)
        assert "[PRESERVED CONTEXT]" in formatted
        assert "[END PRESERVED CONTEXT]" in formatted
        assert "Build REST API" in formatted
        assert "Implement endpoints" in formatted
        assert "app.py" in formatted
        assert "Missing dependency" in formatted


class TestPreservationWithComplex:
    """Test preservation with complex scenarios."""

    def test_preservation_with_multiple_tools(self):
        """Test preservation with multiple tool calls."""
        preserver = ContextPreserver()
        conversation = [
            {"role": "user", "type": "text", "content": "Refactor code"},
            {
                "type": "function_call",
                "name": "read_ide",
                "call_id": "c1",
                "arguments": '{"path": "old_code.py"}',
            },
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "File contents...",
            },
            {
                "type": "function_call",
                "name": "write_ide",
                "call_id": "c2",
                "arguments": '{"path": "new_code.py"}',
            },
            # c2 result missing (incomplete)
        ]
        result = preserver.analyze_conversation(
            conversation=conversation,
            system_prompt="Refactor assistant.",
            token_count=400,
            context_limit=4000,
        )
        assert result.tool_call_count >= 2
        assert result.has_incomplete_operations

    def test_preservation_with_errors_and_fixes(self):
        """Test preservation when errors occur then get fixed."""
        preserver = ContextPreserver()
        conversation = [
            {
                "role": "assistant",
                "type": "text",
                "content": "Error: Syntax error in line 5",
            },
            {
                "type": "function_call",
                "name": "edit_ide",
                "call_id": "c1",
                "arguments": '{"path": "file.py"}',
            },
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "Fixed",
            },
            {
                "role": "assistant",
                "type": "text",
                "content": "Tests passed ✅",
            },
        ]
        result = preserver.analyze_conversation(
            conversation=conversation,
            system_prompt="Debug assistant.",
            token_count=300,
            context_limit=4000,
        )
        # Error should still be recorded
        assert len(result.extracted.unresolved_errors) > 0 or result.extracted.test_results

    def test_preservation_long_conversation(self):
        """Test preservation with long conversation."""
        preserver = ContextPreserver()
        conversation = []
        for i in range(10):
            conversation.append(
                {
                    "role": "user",
                    "type": "text",
                    "content": f"Task {i}",
                }
            )
            conversation.append(
                {
                    "role": "assistant",
                    "type": "text",
                    "content": f"Working on task {i}",
                }
            )
        result = preserver.analyze_conversation(
            conversation=conversation,
            system_prompt="Multi-task assistant.",
            token_count=2000,
            context_limit=4000,
        )
        assert result.turn_count == 10
        assert result.extracted.user_goal is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
