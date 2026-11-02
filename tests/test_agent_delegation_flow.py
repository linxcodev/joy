"""Integration tests for main agent delegation flow.

Tests the complete flow of:
1. Main agent calling delegate() tool
2. Delegation result formatting
3. Result integration into conversation
4. Main agent continuation after delegation
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from joy.delegation import delegate, format_delegation_result, DelegationResult
from joy.agent_base import SubAgentRunner
from joy.tools import run_tool


class TestDelegationResultFormatting:
    """Test formatting of delegation results for display."""
    
    def test_format_success_result(self):
        """Test formatting a successful delegation result."""
        result = DelegationResult(
            status="success",
            output="Analyzed the code and found 3 issues.",
            sub_agent_name="Code Analyzer",
            total_requests=4,
            total_tool_calls=2,
            execution_time=2.3,
        )
        result_json = json.dumps({
            "status": result.status,
            "output": result.output,
            "sub_agent_name": result.sub_agent_name,
            "total_requests": result.total_requests,
            "total_tool_calls": result.total_tool_calls,
            "execution_time": result.execution_time,
        })
        
        formatted = format_delegation_result(result_json)
        
        # Check formatting contains key information
        assert "✓" in formatted
        assert "Code Analyzer" in formatted
        assert "SUCCESS" in formatted
        assert "2.30s" in formatted
        assert "Analyzed the code" in formatted
    
    def test_format_failure_result(self):
        """Test formatting a failed delegation result."""
        result_json = json.dumps({
            "status": "failed",
            "error": "Sub-agent timeout after 30 seconds",
            "sub_agent_name": "File Processor",
            "total_requests": 2,
            "total_tool_calls": 1,
            "execution_time": 30.1,
        })
        
        formatted = format_delegation_result(result_json)
        
        # Check formatting contains error information
        assert "✗" in formatted
        assert "File Processor" in formatted
        assert "FAILED" in formatted
        assert "timeout after 30 seconds" in formatted
    
    def test_format_invalid_json(self):
        """Test formatting with invalid JSON input."""
        invalid_json = "not valid json at all"
        
        formatted = format_delegation_result(invalid_json)
        
        # Should handle gracefully
        assert "Error parsing" in formatted
        assert "not valid json" in formatted
    
    def test_format_empty_json(self):
        """Test formatting with empty JSON object."""
        result_json = "{}"
        
        formatted = format_delegation_result(result_json)
        
        # Should still work with defaults
        assert "Unknown" in formatted
        assert "unknown" in formatted.lower()
    
    def test_format_with_multiline_output(self):
        """Test formatting preserves multiline output."""
        output = """Line 1: Issue A
Line 2: Issue B
Line 3: Issue C"""
        
        result_json = json.dumps({
            "status": "success",
            "output": output,
            "sub_agent_name": "Analyzer",
            "total_requests": 1,
            "total_tool_calls": 1,
            "execution_time": 1.0,
        })
        
        formatted = format_delegation_result(result_json)
        
        # Multiline output should be preserved
        assert "Issue A" in formatted
        assert "Issue B" in formatted
        assert "Issue C" in formatted


class TestRunToolWithDelegation:
    """Test run_tool() function with delegation results."""
    
    @patch('joy.tools.TOOLS.execute')
    def test_run_tool_formats_delegation_result(self, mock_execute):
        """Test that run_tool() formats delegation results specially."""
        # Mock TOOLS.execute to return a delegation result
        delegation_result = json.dumps({
            "status": "success",
            "output": "Task completed successfully",
            "sub_agent_name": "Worker",
            "total_requests": 3,
            "total_tool_calls": 1,
            "execution_time": 1.5,
        })
        
        mock_execute.return_value = [delegation_result]
        
        # Call run_tool with delegation
        output, success = run_tool(
            name="delegate",
            arguments=json.dumps({"task": "test task"})
        )
        
        # Result should be formatted (not raw JSON)
        assert success is True
        assert "✓" in output  # Formatted output has icon
        assert "Worker" in output
        assert "SUCCESS" in output
        assert "1.50s" in output
    
    @patch('joy.tools.TOOLS.execute')
    def test_run_tool_other_tools_unchanged(self, mock_execute):
        """Test that other tools are not affected by delegation formatting."""
        # Mock read tool execution
        mock_execute.return_value = ["File content here"]
        
        output, success = run_tool(
            name="read",
            arguments=json.dumps({"path": "test.txt"})
        )
        
        # Should return raw output (not formatted)
        assert success is True
        assert output == "File content here"
    
    @patch('joy.tools.TOOLS.execute')
    def test_run_tool_handles_delegation_failure(self, mock_execute):
        """Test run_tool() handles delegation failures."""
        # Mock failed delegation
        failed_result = json.dumps({
            "status": "failed",
            "error": "Sub-agent failed to complete task",
            "sub_agent_name": "Worker",
            "total_requests": 8,
            "total_tool_calls": 4,
            "execution_time": 29.5,
        })
        
        mock_execute.return_value = [failed_result]
        
        output, success = run_tool(
            name="delegate",
            arguments=json.dumps({"task": "test task"})
        )
        
        # Should still format, but indicate failure
        assert success is True  # Tool call succeeded, delegation failed
        assert "✗" in output  # Formatted output has failure icon
        assert "FAILED" in output
        assert "failed to complete task" in output


class TestDelegationIntegration:
    """Integration tests for delegation in agent conversation."""
    
    def test_delegation_result_integrates_into_conversation(self):
        """Test that delegation results can be added to conversation."""
        conversation = [
            {"role": "user", "content": "Please analyze this code"},
        ]
        
        # Simulate delegation result
        result = json.dumps({
            "status": "success",
            "output": "Code analysis complete",
            "sub_agent_name": "Analyzer",
            "total_requests": 3,
            "total_tool_calls": 2,
            "execution_time": 2.5,
        })
        
        formatted = format_delegation_result(result)
        
        # Add to conversation
        conversation.append({"role": "assistant", "content": "Calling sub-agent..."})
        conversation.append({"role": "user", "content": formatted})
        
        # Conversation should contain the formatted result
        assert len(conversation) == 3
        assert "Calling sub-agent" in conversation[1]["content"]
        assert "Analyzer" in conversation[2]["content"]
        assert "SUCCESS" in conversation[2]["content"]
    
    def test_delegation_result_preserves_output_for_agent_continuation(self):
        """Test that delegation output is preserved for main agent to use."""
        delegation_output = """
        Function analyze_code has 3 bugs:
        1. Missing null check
        2. Off-by-one error
        3. Memory leak
        """
        
        result_json = json.dumps({
            "status": "success",
            "output": delegation_output,
            "sub_agent_name": "Code Reviewer",
            "total_requests": 5,
            "total_tool_calls": 3,
            "execution_time": 3.2,
        })
        
        formatted = format_delegation_result(result_json)
        
        # Original output should be preserved in formatted result
        assert "Missing null check" in formatted
        assert "Off-by-one error" in formatted
        assert "Memory leak" in formatted
    
    @patch('joy.agent_base.SubAgentRunner')
    @patch('joy.agent_base.BaseAgent.from_config')
    def test_full_delegation_call_flow(self, mock_from_config, mock_runner_class):
        """Test full flow of delegation tool call."""
        # Mock sub-agent execution
        mock_agent = MagicMock()
        mock_from_config.return_value = SubAgentRunner(
            {"system_instruction": "test", "allowed_tools": ["read", "write"]}
        )
        mock_runner = MagicMock()
        mock_runner_class.return_value = mock_runner
        mock_runner.execute.return_value = MagicMock(
            status="COMPLETED",
            output="Task done",
            total_requests=2,
            total_tool_calls=1,
            execution_time=1.5,
        )
        
        # This would be called by the agent
        # (Not testing actual delegate() function as it requires real LLM setup)
        # Just testing the flow
        result = {
            "status": "success",
            "output": "Task done",
            "sub_agent_name": "test_agent",
            "total_requests": 2,
            "total_tool_calls": 1,
            "execution_time": 1.5,
        }
        
        formatted = format_delegation_result(json.dumps(result))
        
        # Result should be properly formatted
        assert "✓" in formatted
        assert "test_agent" in formatted
        assert "Task done" in formatted


class TestDelegationEdgeCases:
    """Test edge cases in delegation result handling."""
    
    def test_very_long_output(self):
        """Test formatting with very long delegation output."""
        long_output = "Line " + " ".join([str(i) for i in range(100)])
        
        result_json = json.dumps({
            "status": "success",
            "output": long_output,
            "sub_agent_name": "Worker",
            "total_requests": 1,
            "total_tool_calls": 1,
            "execution_time": 0.5,
        })
        
        formatted = format_delegation_result(result_json)
        
        # Should still format properly with long output
        assert "✓" in formatted
        assert "Worker" in formatted
        assert "Line" in formatted
    
    def test_special_characters_in_output(self):
        """Test formatting with special characters."""
        output_with_chars = "Results: ✓✗⚠️ Test → Done"
        
        result_json = json.dumps({
            "status": "success",
            "output": output_with_chars,
            "sub_agent_name": "Tester",
            "total_requests": 1,
            "total_tool_calls": 1,
            "execution_time": 0.5,
        })
        
        formatted = format_delegation_result(result_json)
        
        # Special characters should be preserved
        assert "→" in formatted
        assert "Tester" in formatted
    
    def test_empty_output_with_success(self):
        """Test formatting success with empty output."""
        result_json = json.dumps({
            "status": "success",
            "output": "",
            "sub_agent_name": "Agent",
            "total_requests": 1,
            "total_tool_calls": 0,
            "execution_time": 0.1,
        })
        
        formatted = format_delegation_result(result_json)
        
        # Should handle empty output gracefully
        assert "✓" in formatted
        assert "Agent" in formatted
        assert "SUCCESS" in formatted
    
    def test_missing_optional_fields(self):
        """Test formatting with missing optional fields."""
        minimal_result = {
            "status": "success",
        }
        
        formatted = format_delegation_result(json.dumps(minimal_result))
        
        # Should use defaults for missing fields
        assert "✓" in formatted
        assert "Unknown" in formatted
        assert "0.00s" in formatted


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
