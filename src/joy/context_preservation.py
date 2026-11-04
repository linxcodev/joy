"""Context Preservation for Joy Agent Compaction.

This module ensures critical information is preserved during context compaction:
- Information categorization (permanent, active, irrelevant)
- Extraction of important metadata from conversation
- Validation of preserved information
- Integration with compaction engine

The preservation layer operates between the compaction engine and conversation history,
ensuring no critical information is lost during summarization.

Example usage:
    from joy.context_preservation import ContextPreserver, PreservationPolicy
    
    preserver = ContextPreserver()
    analysis = preserver.analyze_conversation(conversation)
    
    # Customize preservation rules
    policy = PreservationPolicy(
        preserve_recent_turns=5,
        preserve_incomplete_chains=True,
        preserve_error_messages=True
    )
    
    preserved = preserver.apply_preservation(analysis, conversation, policy)
"""

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class InformationCategory(Enum):
    """Categories of information in conversation."""
    PERMANENT = "permanent"  # System rules, user goal, immutable instructions
    ACTIVE = "active"  # Current task, unresolved errors, incomplete operations
    IRRELEVANT = "irrelevant"  # Completed tasks, old messages, no longer needed


@dataclass
class PreservationPolicy:
    """Policy for what information to preserve during compaction."""
    preserve_recent_turns: int = 5  # Keep last N conversation turns
    preserve_permanent_info: bool = True  # Keep system instructions, goals
    preserve_active_tasks: bool = True  # Keep unfinished operations
    preserve_incomplete_chains: bool = True  # Don't break tool call chains
    preserve_error_messages: bool = True  # Keep unresolved errors
    preserve_file_changes: bool = True  # Keep track of modified files
    preserve_test_results: bool = True  # Keep test status
    max_preserved_tokens: Optional[int] = None  # Hard limit on preservation size


@dataclass
class ExtractedInformation:
    """Information extracted from conversation."""
    user_goal: Optional[str] = None
    system_instructions: Optional[str] = None
    active_tasks: list[str] = field(default_factory=list)
    files_modified: dict[str, str] = field(default_factory=dict)  # path: change_type
    recent_code_changes: list[str] = field(default_factory=list)
    test_results: Optional[str] = None
    unresolved_errors: list[str] = field(default_factory=list)
    incomplete_tool_chains: list[dict] = field(default_factory=list)
    sub_agent_contexts: dict[str, str] = field(default_factory=dict)  # name: context
    next_steps: list[str] = field(default_factory=list)
    categorization: dict[str, InformationCategory] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    """Result of conversation analysis."""
    extracted: ExtractedInformation
    turn_count: int  # Total number of turns
    tool_call_count: int  # Total tool calls
    has_incomplete_operations: bool  # Whether there are incomplete tool chains
    is_approaching_limit: bool  # Context approaching limit
    estimated_compaction_needed: bool  # Compaction recommended


# Joy tools that change files, with the change type recorded for each.
WRITE_TOOLS = {"write": "written", "edit": "edited"}


class PreservationAnalyzer:
    """Analyzes conversation to extract information for preservation."""

    def __init__(self):
        """Initialize the analyzer."""
        self._file_patterns = [
            r"read_ide.*path['\"]?:\s*['\"]([^'\"]+)['\"]",
            r"write_ide.*path['\"]?:\s*['\"]([^'\"]+)['\"]",
            r"edit_ide.*path['\"]?:\s*['\"]([^'\"]+)['\"]",
            r"File: `([^`]+)`",
            r"Modified: ([^\s]+)",
        ]

    def analyze(
        self,
        conversation: list[dict],
        system_prompt: str,
        token_count: int,
        context_limit: int,
    ) -> AnalysisResult:
        """Analyze conversation to extract preserved information.

        Args:
            conversation: Full conversation message history
            system_prompt: System instruction prompt
            token_count: Current token count
            context_limit: Maximum context window

        Returns:
            AnalysisResult with extracted information
        """
        extracted = ExtractedInformation()

        # Extract system instructions
        extracted.system_instructions = system_prompt[:500] if system_prompt else None

        # Analyze each message
        tool_call_count = 0
        turn_count = 0
        seen_user = False

        for i, msg in enumerate(conversation):
            msg_type = msg.get("type", "")
            role = msg.get("role", "")

            # Count turns (user messages)
            if role == "user":
                turn_count += 1
                seen_user = True
                extracted = self._extract_from_user_message(msg, extracted)

            # Count tool calls
            if msg_type == "function_call":
                tool_call_count += 1

            # Extract file changes
            if msg_type in ("function_call", "function_call_output", "text"):
                extracted = self._extract_file_changes(msg, extracted)

            # Extract errors
            if msg_type in ("text", "function_call_output"):
                extracted = self._extract_errors(msg, extracted)

            # Extract test results (test runs arrive as tool output)
            if msg_type in ("text", "function_call_output"):
                extracted = self._extract_test_results(msg, extracted)

            # Extract tool chains
            if msg_type == "function_call":
                extracted = self._track_tool_call(msg, extracted, conversation, i)

            # Extract next steps
            if role == "assistant":
                extracted = self._extract_next_steps(msg, extracted)

        # Detect incomplete operations
        has_incomplete = self._has_incomplete_operations(extracted)

        # Check if approaching limit
        is_approaching = token_count > context_limit * 0.75

        return AnalysisResult(
            extracted=extracted,
            turn_count=turn_count,
            tool_call_count=tool_call_count,
            has_incomplete_operations=has_incomplete,
            is_approaching_limit=is_approaching,
            estimated_compaction_needed=is_approaching and turn_count > 5,
        )

    def _extract_from_user_message(
        self, msg: dict, extracted: ExtractedInformation
    ) -> ExtractedInformation:
        """Extract goal and instructions from user messages."""
        content = msg.get("content", "")

        # First user message often contains the goal
        if not extracted.user_goal and content:
            first_line = content.split("\n")[0]
            if len(first_line) < 200:
                extracted.user_goal = first_line[:100]

        # Extract any instructions
        if "must" in content.lower() or "should" in content.lower():
            for line in content.split("\n"):
                if len(line) > 10 and len(line) < 150:
                    if any(kw in line.lower() for kw in ["must", "should", "don't", "do not"]):
                        extracted.active_tasks.append(line.strip())

        return extracted

    def _extract_file_changes(
        self, msg: dict, extracted: ExtractedInformation
    ) -> ExtractedInformation:
        """Extract file paths and changes from messages."""
        content = msg.get("content", "")
        arguments = msg.get("arguments", "")

        # Joy's write/edit tools: the path is a JSON argument.
        if msg.get("type") == "function_call" and msg.get("name") in WRITE_TOOLS:
            try:
                path = json.loads(arguments or "{}").get("path")
            except (ValueError, AttributeError):
                path = None
            if isinstance(path, str) and path and len(path) < 200:
                extracted.files_modified[path] = WRITE_TOOLS[msg["name"]]
            return extracted
        
        # Combine content and arguments for extraction
        text_to_scan = str(content) + " " + str(arguments)
        if not text_to_scan.strip():
            return extracted

        # Determine change type from message type or content
        change_type = "modified"
        msg_type = msg.get("type", "")
        msg_name = msg.get("name", "")
        
        if "read" in msg_type.lower() or "read_ide" in msg_name.lower():
            change_type = "read"
        elif "write" in msg_type.lower() or "write_ide" in msg_name.lower():
            change_type = "written"
        elif "edit" in msg_type.lower() or "edit_ide" in msg_name.lower():
            change_type = "edited"

        for pattern in self._file_patterns:
            matches = re.findall(pattern, text_to_scan, re.IGNORECASE)
            for match in matches:
                if match and len(match) < 200:
                    if match not in extracted.files_modified:
                        extracted.files_modified[match] = change_type

        return extracted

    def _extract_errors(
        self, msg: dict, extracted: ExtractedInformation
    ) -> ExtractedInformation:
        """Extract error messages from conversation."""
        content = msg.get("content", "")
        if not content:
            return extracted

        content_str = str(content)

        # Look for error indicators
        error_patterns = [
            r"(?:Error|FAIL|FAILED|Exception|Failed):\s*([^\n]+)",
            r"(?:ERROR|FATAL):\s*([^\n]+)",
            r"(?:Traceback|Unresolved|Not found|Invalid):\s*([^\n]+)",
        ]

        for pattern in error_patterns:
            matches = re.findall(pattern, content_str)
            for match in matches:
                if match and len(match) > 5 and len(match) < 200:
                    if match not in extracted.unresolved_errors:
                        extracted.unresolved_errors.append(match)

        return extracted

    def _extract_test_results(
        self, msg: dict, extracted: ExtractedInformation
    ) -> ExtractedInformation:
        """Extract test results from conversation."""
        content = msg.get("content", "")
        if not content:
            return extracted

        content_str = str(content)

        # Look for test indicators
        if any(kw in content_str for kw in ["PASSED", "FAILED", "pytest", "test", "✅", "❌"]):
            # Extract key metrics; the latest run wins.
            # "25 passed", "25/25 tests passed", "25 ✅"
            passed = re.search(r"(\d+)(?:/\d+)?\s*(?:tests?\s+)?(?:passed|✅)", content_str, re.IGNORECASE)
            failed = re.search(r"(\d+)\s*failed", content_str, re.IGNORECASE)
            if passed or failed:
                parts = [f"{passed.group(1)} passed"] if passed else []
                if failed:
                    parts.append(f"{failed.group(1)} failed")
                extracted.test_results = "Test status: " + ", ".join(parts)

        return extracted

    def _track_tool_call(
        self, msg: dict, extracted: ExtractedInformation, conversation: list[dict], index: int
    ) -> ExtractedInformation:
        """Track tool calls and their completion status."""
        call_id = msg.get("call_id", "")
        tool_name = msg.get("name", "")

        # Look for corresponding result
        has_result = False
        for j in range(index + 1, min(index + 5, len(conversation))):
            result_msg = conversation[j]
            if (
                result_msg.get("type") == "function_call_output"
                and result_msg.get("call_id") == call_id
            ):
                has_result = True
                break

        # If no result found, it's incomplete
        if not has_result:
            extracted.incomplete_tool_chains.append({"call_id": call_id, "name": tool_name})

        return extracted

    def _extract_next_steps(
        self, msg: dict, extracted: ExtractedInformation
    ) -> ExtractedInformation:
        """Extract next steps from assistant messages."""
        content = msg.get("content", "")
        if not content:
            return extracted

        content_str = str(content)

        # Look for step patterns
        patterns = [
            r"next.*?:\s*(.+?)(?:\.|,|$)",
            r"then.*?:\s*(.+?)(?:\.|,|$)",
            r"will\s+(.+?)(?:\.|,|$)",
        ]

        for pattern in patterns:
            matches = re.findall(pattern, content_str, re.IGNORECASE)
            for match in matches:
                if match and 5 < len(match) < 150:
                    if match not in extracted.next_steps:
                        extracted.next_steps.append(match)

        return extracted

    def _has_incomplete_operations(self, extracted: ExtractedInformation) -> bool:
        """Check if there are incomplete operations."""
        return bool(
            extracted.incomplete_tool_chains
            or extracted.unresolved_errors
            or any(task for task in extracted.active_tasks if not task.endswith("✓"))
        )


class ContextPreserver:
    """Preserves critical information during context compaction."""

    def __init__(self):
        """Initialize the preserver."""
        self.analyzer = PreservationAnalyzer()

    def analyze_conversation(
        self,
        conversation: list[dict],
        system_prompt: str,
        token_count: int,
        context_limit: int,
    ) -> AnalysisResult:
        """Analyze conversation to extract preserved information.

        Args:
            conversation: Full conversation message history
            system_prompt: System instruction prompt
            token_count: Current token count
            context_limit: Maximum context window

        Returns:
            AnalysisResult with extracted information
        """
        return self.analyzer.analyze(conversation, system_prompt, token_count, context_limit)

    def apply_preservation(
        self,
        analysis: AnalysisResult,
        conversation: list[dict],
        policy: Optional[PreservationPolicy] = None,
    ) -> ExtractedInformation:
        """Apply preservation policy to ensure critical information is kept.

        Args:
            analysis: Conversation analysis result
            conversation: Full conversation message history
            policy: Preservation policy (uses defaults if None)

        Returns:
            ExtractedInformation with preservation applied
        """
        if policy is None:
            policy = PreservationPolicy()

        extracted = analysis.extracted

        # Apply categorization based on policy
        if policy.preserve_permanent_info:
            if not extracted.user_goal:
                extracted.user_goal = "unknown (not determined from conversation)"
            if not extracted.system_instructions:
                extracted.system_instructions = "(no system prompt captured)"

        if policy.preserve_active_tasks:
            # Ensure active tasks are not empty if there are incomplete operations
            if analysis.has_incomplete_operations and not extracted.active_tasks:
                extracted.active_tasks.append("(has incomplete operations)")

        if policy.preserve_error_messages:
            # Don't filter out errors based on policy
            pass

        if not policy.preserve_incomplete_chains:
            # Clear incomplete chains if policy says not to preserve
            extracted.incomplete_tool_chains = []

        # Categorize extracted information
        extracted.categorization = {
            "user_goal": InformationCategory.PERMANENT,
            "system_instructions": InformationCategory.PERMANENT,
            "active_tasks": InformationCategory.ACTIVE,
            "files_modified": InformationCategory.ACTIVE,
            "recent_code_changes": InformationCategory.ACTIVE,
            "test_results": InformationCategory.ACTIVE,
            "unresolved_errors": InformationCategory.ACTIVE,
            "incomplete_tool_chains": InformationCategory.ACTIVE,
            "next_steps": InformationCategory.ACTIVE,
            "sub_agent_contexts": InformationCategory.ACTIVE,
        }

        logger.info(
            f"Preservation applied: {len(extracted.files_modified)} files, "
            f"{len(extracted.unresolved_errors)} errors, "
            f"{len(extracted.incomplete_tool_chains)} incomplete chains"
        )

        return extracted

    def validate_preservation(
        self, original: ExtractedInformation, preserved: ExtractedInformation
    ) -> bool:
        """Validate that preservation didn't lose critical information.

        Args:
            original: Original extracted information
            preserved: Preserved extracted information

        Returns:
            True if preservation is valid, False otherwise
        """
        # Check permanent information is preserved
        if not preserved.user_goal or preserved.user_goal == "unknown":
            logger.warning("User goal was not preserved")
            return False

        if not preserved.system_instructions or preserved.system_instructions == "(no system prompt captured)":
            logger.warning("System instructions were not preserved")
            return False

        # Check active information is preserved
        original_error_count = len(original.unresolved_errors)
        preserved_error_count = len(preserved.unresolved_errors)

        if preserved_error_count < original_error_count:
            logger.warning(
                f"Unresolved errors lost: {original_error_count} → {preserved_error_count}"
            )
            return False

        # Check incomplete chains are preserved
        original_chain_count = len(original.incomplete_tool_chains)
        preserved_chain_count = len(preserved.incomplete_tool_chains)

        if preserved_chain_count < original_chain_count:
            logger.warning(
                f"Incomplete tool chains lost: {original_chain_count} → {preserved_chain_count}"
            )
            return False

        logger.info("Preservation validation passed")
        return True

    def format_preserved_context(self, extracted: ExtractedInformation) -> str:
        """Format preserved information as context string for LLM.

        Args:
            extracted: Extracted information to format

        Returns:
            Formatted string suitable for inclusion in LLM context
        """
        lines = []

        lines.append("[PRESERVED CONTEXT]")
        lines.append("")

        if extracted.user_goal:
            lines.append(f"USER GOAL: {extracted.user_goal}")
            lines.append("")

        if extracted.active_tasks:
            lines.append("ACTIVE TASKS:")
            for task in extracted.active_tasks[:5]:  # Limit to 5
                lines.append(f"  - {task}")
            lines.append("")

        if extracted.files_modified:
            lines.append("FILES MODIFIED:")
            for path, change_type in list(extracted.files_modified.items())[:10]:  # Limit to 10
                lines.append(f"  - {path} ({change_type})")
            lines.append("")

        if extracted.test_results:
            lines.append(f"TEST RESULTS: {extracted.test_results}")
            lines.append("")

        if extracted.unresolved_errors:
            lines.append("UNRESOLVED ERRORS:")
            for error in extracted.unresolved_errors[:5]:  # Limit to 5
                lines.append(f"  - {error}")
            lines.append("")

        if extracted.incomplete_tool_chains:
            lines.append("INCOMPLETE OPERATIONS:")
            for chain in extracted.incomplete_tool_chains[:3]:  # Limit to 3
                name = chain.get("name", "unknown")
                lines.append(f"  - {name} (call_id: {chain.get('call_id', 'unknown')})")
            lines.append("")

        if extracted.next_steps:
            lines.append("NEXT STEPS:")
            for step in extracted.next_steps[:3]:  # Limit to 3
                lines.append(f"  - {step}")
            lines.append("")

        lines.append("[END PRESERVED CONTEXT]")

        return "\n".join(lines)
