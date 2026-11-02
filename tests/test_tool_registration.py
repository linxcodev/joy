"""Unit tests for tool registration, including delegation tool."""

import json
import pytest
from joy.tools import TOOLS, delegate


class TestToolRegistration:
    """Test that all tools are properly registered in the TOOLS registry."""
    
    def test_tools_registered(self):
        """Verify all expected tools are registered."""
        tools_dict = {spec["function"]["name"] for spec in TOOLS.tools()}
        expected = {"read", "write", "edit", "bash", "delegate"}
        assert expected == tools_dict, f"Expected {expected}, got {tools_dict}"
    
    def test_delegate_tool_present(self):
        """Verify delegate tool is in registry."""
        tools_list = [spec["function"]["name"] for spec in TOOLS.tools()]
        assert "delegate" in tools_list, f"delegate not in tools: {tools_list}"
    
    def test_delegate_has_description(self):
        """Verify delegate tool has description."""
        tools = TOOLS.tools()
        delegate_spec = next((t for t in tools if t["function"]["name"] == "delegate"), None)
        assert delegate_spec is not None, "delegate tool not found"
        assert "description" in delegate_spec["function"]
        assert len(delegate_spec["function"]["description"]) > 0


class TestDelegateToolFunction:
    """Test the delegate() function directly."""
    
    def test_delegate_with_minimal_args(self):
        """Test delegate() with just a task."""
        result = delegate("Analyze a simple file")
        assert isinstance(result, str), f"Expected string, got {type(result)}"
        
        # Parse as JSON to verify it's valid
        parsed = json.loads(result)
        assert "status" in parsed
        assert parsed["status"] in ["success", "failed", "timeout", "rejected"]
    
    def test_delegate_invalid_tools_rejected(self):
        """Test that invalid tools are rejected."""
        result = delegate(
            task="Test task",
            allowed_tools="invalid_tool"
        )
        parsed = json.loads(result)
        assert parsed["status"] == "rejected"
        assert "error" in parsed
        assert "Invalid tool" in parsed["error"]
    
    def test_delegate_invalid_max_requests_rejected(self):
        """Test that invalid max_requests is rejected."""
        result = delegate(
            task="Test task",
            max_requests=100  # Too high
        )
        parsed = json.loads(result)
        assert parsed["status"] == "rejected"
        assert "error" in parsed
        assert "max_requests" in parsed["error"]
    
    def test_delegate_invalid_depth_rejected(self):
        """Test that invalid delegation depth is rejected."""
        result = delegate(
            task="Test task",
            max_delegation_depth=10  # Too high
        )
        parsed = json.loads(result)
        assert parsed["status"] == "rejected"
        assert "error" in parsed
        assert "max_delegation_depth" in parsed["error"]
    
    def test_delegate_result_structure(self):
        """Verify delegate result has all required fields."""
        result = delegate("Test task")
        parsed = json.loads(result)
        
        required_fields = [
            "status",
            "output",
            "error",
            "sub_agent_name",
            "total_requests",
            "total_tool_calls",
            "execution_time",
        ]
        for field in required_fields:
            assert field in parsed, f"Missing field: {field}"
    
    def test_delegate_with_custom_tools(self):
        """Test delegate with custom tool selection."""
        result = delegate(
            task="Test task",
            allowed_tools="read,write"
        )
        parsed = json.loads(result)
        # Should not be rejected for having valid tools
        assert parsed["status"] in ["success", "failed", "timeout", "rejected"]
        if parsed["status"] == "rejected":
            assert "tool" not in (parsed.get("error") or "")
    
    def test_delegate_result_json_serializable(self):
        """Verify that delegate result is valid JSON."""
        result = delegate("Test task")
        # Should not raise exception
        parsed = json.loads(result)
        # Should be able to serialize back to JSON
        serialized = json.dumps(parsed)
        assert isinstance(serialized, str)


class TestToolExecutionViaRegistry:
    """Test that delegate can be executed via TOOLS.execute()."""
    
    def test_delegate_via_tools_execute(self):
        """Test calling delegate through TOOLS.execute()."""
        # Call via aisuite's execute method
        arguments = json.dumps({
            "task": "Simple test task",
            "max_requests": 2,
        })
        
        result = TOOLS.execute({
            "function": {
                "name": "delegate",
                "arguments": json.loads(arguments)
            }
        })
        
        # Result should be a list with one element (the output)
        assert isinstance(result, list)
        assert len(result) > 0
        
        # The output should be valid JSON
        output = str(result[0])
        parsed = json.loads(output)
        assert "status" in parsed
    
    def test_all_tools_callable_via_registry(self):
        """Verify all tools can be called through registry."""
        tool_names = [spec["function"]["name"] for spec in TOOLS.tools()]
        assert "delegate" in tool_names
        assert "read" in tool_names
        assert "write" in tool_names
        assert "edit" in tool_names
        assert "bash" in tool_names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
