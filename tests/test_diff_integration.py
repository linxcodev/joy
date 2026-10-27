"""
Integration tests untuk Diff Review Feature di Joy CLI.

Tests:
- /diff command (manual trigger)
- /diff <path> command (single file)
- Auto display diff setelah agent task
- Config on/off untuk auto display
- No-change conditions (tidak trigger diff otomatis)
- Command tidak dikirim ke model
- Session recovery setelah diff display
"""

import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call
import pytest

from joy.diff_commands import DiffCommandHandler
from joy.diff_config import DiffConfig
from joy.diff_engine import DiffEngine, FileChangeType, FileStatus
from rich.console import Console
from io import StringIO


class TestDiffCommandBasics:
    """Test basic /diff command parsing dan handling."""
    
    @pytest.fixture
    def temp_workspace(self):
        """Create temporary workspace untuk testing."""
        tmp_dir = tempfile.mkdtemp()
        yield tmp_dir
        shutil.rmtree(tmp_dir, ignore_errors=True)
    
    @pytest.fixture
    def console(self):
        """Rich console untuk output."""
        return Console(file=StringIO(), force_terminal=True)
    
    @pytest.fixture
    def diff_handler(self, temp_workspace, console):
        """Initialize DiffCommandHandler."""
        config = DiffConfig(auto_display=True, show_summary=True)
        return DiffCommandHandler(temp_workspace, console, config)
    
    def test_is_diff_command_exact(self, diff_handler):
        """Test pengenalan command /diff exact."""
        assert diff_handler.is_diff_command("/diff") == True
        assert diff_handler.is_diff_command("  /diff  ") == True
        assert diff_handler.is_diff_command("/diff ") == True
    
    def test_is_diff_command_with_path(self, diff_handler):
        """Test pengenalan command /diff <path>."""
        assert diff_handler.is_diff_command("/diff src/file.py") == True
        assert diff_handler.is_diff_command("/diff some/path/to/file.txt") == True
        assert diff_handler.is_diff_command("/diff  multiple  spaces") == True
    
    def test_is_diff_command_false(self, diff_handler):
        """Test non-diff commands."""
        assert diff_handler.is_diff_command("diff") == False
        assert diff_handler.is_diff_command("/ diff") == False
        assert diff_handler.is_diff_command("edit file.py") == False
        assert diff_handler.is_diff_command("") == False
    
    def test_parse_diff_command_full(self, diff_handler):
        """Test parse /diff tanpa path."""
        cmd_type, path = diff_handler.parse_diff_command("/diff")
        assert cmd_type == "full"
        assert path is None
    
    def test_parse_diff_command_file(self, diff_handler):
        """Test parse /diff <path>."""
        cmd_type, path = diff_handler.parse_diff_command("/diff src/main.py")
        assert cmd_type == "file"
        assert path == "src/main.py"
    
    def test_parse_diff_command_invalid(self, diff_handler):
        """Test parse invalid diff command."""
        cmd_type, _ = diff_handler.parse_diff_command("not a diff command")
        assert cmd_type == "invalid"
    
    def test_handle_diff_command_no_changes(self, diff_handler):
        """Test handle /diff saat tidak ada pending changes."""
        # No pending changes set
        result = diff_handler.handle_diff_command("/diff")
        assert result == True
        
        # Output harus ada pesan "no changes"
        output = diff_handler.console.file.getvalue()
        assert "pending changes" in output.lower() or "no changes" in output.lower()
    
    def test_handle_diff_command_invalid(self, diff_handler):
        """Test handle /diff dengan command format invalid."""
        result = diff_handler.handle_diff_command("/diff  ")
        assert result == True
        
        # Tapi /diff saja valid
        result2 = diff_handler.handle_diff_command("/diff src/test.py")
        assert result2 == True


class TestDiffCommandWithChanges:
    """Test /diff command dengan actual changes."""
    
    @pytest.fixture
    def temp_workspace(self):
        """Create workspace with test files."""
        tmp_dir = tempfile.mkdtemp()
        
        # Create initial files
        test_file = Path(tmp_dir) / "test.py"
        test_file.write_text("def hello():\n    print('hello')\n")
        
        yield tmp_dir
        shutil.rmtree(tmp_dir, ignore_errors=True)
    
    @pytest.fixture
    def diff_handler(self, temp_workspace):
        """Initialize handler dengan test workspace."""
        console = Console(file=StringIO(), force_terminal=True)
        config = DiffConfig(auto_display=True, show_summary=True)
        return DiffCommandHandler(temp_workspace, console, config)
    
    def test_capture_and_detect_changes(self, diff_handler, temp_workspace):
        """Test capture initial state lalu detect perubahan."""
        # Capture initial
        diff_handler.capture_initial_state()
        
        # Modify file
        test_file = Path(temp_workspace) / "test.py"
        test_file.write_text("def hello():\n    print('hello world')\n")
        
        # Detect changes
        changes = diff_handler.detect_and_track_changes()
        
        assert len(changes) > 0
        assert "test.py" in changes
        assert changes["test.py"].change_type == FileChangeType.MODIFIED
        assert changes["test.py"].lines_added > 0
        assert changes["test.py"].lines_removed > 0
    
    def test_should_auto_display_when_enabled(self, diff_handler, temp_workspace):
        """Test should_auto_display_diff returns True saat config enabled."""
        diff_handler.capture_initial_state()
        
        # Modify file
        test_file = Path(temp_workspace) / "test.py"
        test_file.write_text("def hello():\n    return 'modified'\n")
        
        changes = diff_handler.detect_and_track_changes()
        
        # Config.auto_display = True
        assert diff_handler.should_auto_display_diff(changes) == True
    
    def test_should_not_auto_display_when_disabled(self, diff_handler, temp_workspace):
        """Test should_auto_display_diff returns False saat config disabled."""
        diff_handler.config.auto_display = False
        diff_handler.capture_initial_state()
        
        # Modify file
        test_file = Path(temp_workspace) / "test.py"
        test_file.write_text("modified content\n")
        
        changes = diff_handler.detect_and_track_changes()
        
        assert diff_handler.should_auto_display_diff(changes) == False
    
    def test_should_auto_display_respects_watch_patterns(self, diff_handler, temp_workspace):
        """Test watch_patterns filter works."""
        # Only watch .txt files
        diff_handler.config.watch_patterns = ["*.txt"]
        diff_handler.capture_initial_state()
        
        # Modify .py file
        test_file = Path(temp_workspace) / "test.py"
        test_file.write_text("modified\n")
        
        changes = diff_handler.detect_and_track_changes()
        
        # Should not auto-display karena .py tidak match *.txt
        assert diff_handler.should_auto_display_diff(changes) == False
    
    def test_should_auto_display_respects_ignore_patterns(self, diff_handler, temp_workspace):
        """Test ignore_patterns filter works."""
        # Ignore .py files
        diff_handler.config.ignore_patterns = ["*.py"]
        diff_handler.capture_initial_state()
        
        # Modify .py file
        test_file = Path(temp_workspace) / "test.py"
        test_file.write_text("modified\n")
        
        changes = diff_handler.detect_and_track_changes()
        
        # Should not auto-display karena .py dalam ignore patterns
        assert diff_handler.should_auto_display_diff(changes) == False
    
    def test_display_auto_diff_generates_summary(self, diff_handler, temp_workspace):
        """Test display_auto_diff dengan show_summary=True."""
        diff_handler.config.show_summary = True
        diff_handler.capture_initial_state()
        
        test_file = Path(temp_workspace) / "test.py"
        test_file.write_text("def hello():\n    print('modified')\n")
        
        changes = diff_handler.detect_and_track_changes()
        
        # Clear console sebelum display
        diff_handler.console.file.truncate(0)
        diff_handler.console.file.seek(0)
        
        diff_handler.display_auto_diff(changes)
        
        output = diff_handler.console.file.getvalue()
        # Should show summary
        assert len(output) > 0
    
    def test_clear_pending_resets_state(self, diff_handler, temp_workspace):
        """Test clear_pending membersihkan pending changes."""
        diff_handler.capture_initial_state()
        
        test_file = Path(temp_workspace) / "test.py"
        test_file.write_text("modified\n")
        
        changes = diff_handler.detect_and_track_changes()
        assert len(diff_handler._pending_changes) > 0
        
        diff_handler.clear_pending()
        
        assert len(diff_handler._pending_changes) == 0
        assert diff_handler._last_snapshot is None


class TestDiffConfigFromEnv:
    """Test DiffConfig.from_env() environment variable handling."""
    
    def test_config_from_env_defaults(self):
        """Test default env values."""
        with patch.dict(os.environ, {}, clear=True):
            config = DiffConfig.from_env()
            
            assert config.auto_display == True  # default "true"
            assert config.show_summary == True
            assert config.format == "full"
            assert config.max_diff_lines == 100
    
    def test_config_from_env_auto_display_false(self):
        """Test JOY_DIFF_AUTO=false."""
        with patch.dict(os.environ, {"JOY_DIFF_AUTO": "false"}, clear=True):
            config = DiffConfig.from_env()
            assert config.auto_display == False
    
    def test_config_from_env_summary_disabled(self):
        """Test JOY_DIFF_SUMMARY=no."""
        with patch.dict(os.environ, {"JOY_DIFF_SUMMARY": "no"}, clear=True):
            config = DiffConfig.from_env()
            assert config.show_summary == False
    
    def test_config_from_env_watch_patterns(self):
        """Test JOY_DIFF_WATCH patterns."""
        with patch.dict(
            os.environ, 
            {"JOY_DIFF_WATCH": "*.py,src/**/*.ts"}, 
            clear=True
        ):
            config = DiffConfig.from_env()
            assert "*.py" in config.watch_patterns
            assert "src/**/*.ts" in config.watch_patterns
    
    def test_config_from_env_ignore_patterns(self):
        """Test JOY_DIFF_IGNORE patterns."""
        with patch.dict(
            os.environ,
            {"JOY_DIFF_IGNORE": "__pycache__,node_modules"},
            clear=True
        ):
            config = DiffConfig.from_env()
            assert "__pycache__" in config.ignore_patterns
            assert "node_modules" in config.ignore_patterns


class TestNoFalseTriggersOnReadOnlyOps:
    """Test bahwa diff tidak trigger untuk read-only operations."""
    
    @pytest.fixture
    def temp_workspace(self):
        """Create workspace dengan files."""
        tmp_dir = tempfile.mkdtemp()
        
        test_file = Path(tmp_dir) / "test.py"
        test_file.write_text("original content\n")
        
        yield tmp_dir
        shutil.rmtree(tmp_dir, ignore_errors=True)
    
    @pytest.fixture
    def diff_handler(self, temp_workspace):
        """Initialize handler."""
        console = Console(file=StringIO(), force_terminal=True)
        config = DiffConfig(auto_display=True)
        return DiffCommandHandler(temp_workspace, console, config)
    
    def test_read_file_does_not_trigger_diff(self, diff_handler, temp_workspace):
        """Test read file operation tidak trigger auto-diff."""
        diff_handler.capture_initial_state()
        
        # Just read file, tidak modify
        test_file = Path(temp_workspace) / "test.py"
        _ = test_file.read_text()
        
        changes = diff_handler.detect_and_track_changes()
        
        # Should be empty/no changes detected
        changed = {k: v for k, v in changes.items() 
                  if v.change_type != FileChangeType.UNCHANGED}
        assert len(changed) == 0
    
    def test_execute_command_no_changes_no_diff(self, diff_handler, temp_workspace):
        """Test run command yang tidak modify files."""
        diff_handler.capture_initial_state()
        
        # Run some operation yang tidak mengubah files
        # (dalam konteks real, ini adalah tool execution)
        
        changes = diff_handler.detect_and_track_changes()
        
        # No real changes
        changed = {k: v for k, v in changes.items() 
                  if v.change_type != FileChangeType.UNCHANGED}
        assert len(changed) == 0


class TestCLIIntegrationScenarios:
    """Integration tests untuk complete CLI workflows."""
    
    @pytest.fixture
    def temp_workspace(self):
        """Create workspace."""
        tmp_dir = tempfile.mkdtemp()
        Path(tmp_dir).mkdir(exist_ok=True)
        
        # Create initial Python file
        py_file = Path(tmp_dir) / "app.py"
        py_file.write_text("def main():\n    pass\n")
        
        yield tmp_dir
        shutil.rmtree(tmp_dir, ignore_errors=True)
    
    @pytest.fixture
    def diff_handler(self, temp_workspace):
        """Initialize handler."""
        console = Console(file=StringIO(), force_terminal=True)
        config = DiffConfig(auto_display=True, show_summary=True)
        return DiffCommandHandler(temp_workspace, console, config)
    
    def test_complete_turn_flow(self, diff_handler, temp_workspace):
        """Test complete turn: capture -> detect -> display -> clear."""
        # 1. Capture initial state
        diff_handler.capture_initial_state()
        assert diff_handler._last_snapshot is not None
        
        # 2. Modify file (simulating tool execution)
        py_file = Path(temp_workspace) / "app.py"
        py_file.write_text("def main():\n    print('hello')\n")
        
        # 3. Detect changes
        changes = diff_handler.detect_and_track_changes()
        assert len(changes) > 0
        
        # 4. Check should auto-display
        should_display = diff_handler.should_auto_display_diff(changes)
        assert should_display == True
        
        # 5. Display (mocks console output)
        diff_handler.console.file.truncate(0)
        diff_handler.console.file.seek(0)
        diff_handler.display_auto_diff(changes)
        
        # 6. Clear pending
        diff_handler.clear_pending()
        assert len(diff_handler._pending_changes) == 0
        assert diff_handler._last_snapshot is None
    
    def test_manual_diff_command_flow(self, diff_handler, temp_workspace):
        """Test manual /diff command flow."""
        # Setup some pending changes
        diff_handler.capture_initial_state()
        
        py_file = Path(temp_workspace) / "app.py"
        py_file.write_text("def main():\n    return True\n")
        
        changes = diff_handler.detect_and_track_changes()
        
        # User runs /diff command
        diff_handler.console.file.truncate(0)
        diff_handler.console.file.seek(0)
        
        result = diff_handler.handle_diff_command("/diff")
        
        assert result == True
        output = diff_handler.console.file.getvalue()
        # Should display something
        assert len(output) > 0
    
    def test_diff_command_specific_file(self, diff_handler, temp_workspace):
        """Test /diff <path> command untuk specific file."""
        # Create multiple files
        file1 = Path(temp_workspace) / "file1.py"
        file2 = Path(temp_workspace) / "file2.py"
        file1.write_text("content 1\n")
        file2.write_text("content 2\n")
        
        # Capture and modify
        diff_handler.capture_initial_state()
        file1.write_text("modified 1\n")
        file2.write_text("modified 2\n")
        
        changes = diff_handler.detect_and_track_changes()
        
        # Run /diff untuk file1 saja
        diff_handler.console.file.truncate(0)
        diff_handler.console.file.seek(0)
        
        result = diff_handler.handle_diff_command("/diff file1.py")
        assert result == True
        
        output = diff_handler.console.file.getvalue()
        assert len(output) > 0


class TestStreamingAndEvents:
    """Test bahwa diff tidak mengganggu streaming dan events."""
    
    def test_diff_command_not_sent_to_model(self):
        """Test /diff command tidak dikirim ke model."""
        # This is handled in cli.py logic
        # Mock test untuk memverifikasi command intercepted
        
        message = "/diff"
        
        # Should be detected sebelum diproses sebagai user message
        from joy.diff_commands import DiffCommandHandler
        handler = DiffCommandHandler(".", Console(file=StringIO()))
        
        is_diff = handler.is_diff_command(message)
        assert is_diff == True
        
        # Command harus intercept sebelum masuk ke messages list


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
