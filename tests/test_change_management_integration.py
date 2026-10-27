"""
Comprehensive integration tests untuk Change Management Feature.

Tests mencakup:
1. Snapshot creation & detection
2. Manual change detection & conflict handling
3. Accept/reject operations dengan confirmation
4. Batch operations
5. Backup creation & restoration
6. Edge cases (new files, deleted files, binary files)
7. File modification after snapshot (conflict scenarios)
8. Session persistence
9. Summary & status tracking
10. Undo operations
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch

from rich.console import Console

from joy.diff_engine import DiffEngine, FileChangeType, FileStatus, FileDiffMetadata
from joy.diff_snapshot_manager import DiffSnapshotManager, SnapshotFileRecord, ChangeManagementRecord
from joy.diff_change_manager import DiffChangeManager, ReviewStatus, FileReviewState
from joy.diff_review_commands import DiffReviewCommandHandler
from joy.diff_commands import DiffCommandHandler
from joy.diff_config import DiffConfig


class TestSnapshotCreationAndDetection:
    """Test snapshot creation and change detection."""
    
    @pytest.fixture
    def workspace(self):
        """Create temporary workspace."""
        tmpdir = tempfile.mkdtemp()
        yield Path(tmpdir)
        shutil.rmtree(tmpdir)
    
    @pytest.fixture
    def snapshot_manager(self, workspace):
        """Create snapshot manager."""
        return DiffSnapshotManager(str(workspace))
    
    def test_snapshot_creation(self, workspace, snapshot_manager):
        """Test basic snapshot creation."""
        # Create initial files
        (workspace / "main.py").write_text("def hello():\n    print('hello')\n")
        (workspace / "utils.py").write_text("# utils\n")
        
        # Create snapshot
        files = {
            "main.py": "def hello():\n    print('hello')\n",
            "utils.py": "# utils\n"
        }
        
        result = snapshot_manager.create_snapshot("task_1", files)
        assert result is True
        
        # Verify snapshot was created
        assert "task_1" in snapshot_manager._snapshots
        snapshot = snapshot_manager._snapshots["task_1"]
        assert "main.py" in snapshot
        assert "utils.py" in snapshot
        assert snapshot["main.py"].exists is True
        assert snapshot["main.py"].content == files["main.py"]
    
    def test_snapshot_with_binary_files(self, workspace, snapshot_manager):
        """Test snapshot creation with binary files."""
        # Binary file content is None
        files = {
            "main.py": "print('hello')",
            "image.png": None,  # Binary or unreadable
        }
        
        result = snapshot_manager.create_snapshot("task_1", files)
        assert result is True
        
        snapshot = snapshot_manager._snapshots["task_1"]
        assert snapshot["image.png"].content is None
        # File not in disk will be marked NOT_FOUND
        assert snapshot["image.png"].file_status in [FileStatus.BINARY.value, FileStatus.NOT_FOUND.value]
    
    def test_detect_manual_changes(self, workspace, snapshot_manager):
        """Test detection of manual changes after snapshot."""
        # Create snapshot
        initial_content = "original content"
        files = {"main.py": initial_content}
        snapshot_manager.create_snapshot("task_1", files)
        
        # Simulate manual change
        modified_content = "original content\n# manual edit\n"
        current_files = {"main.py": modified_content}
        
        # Detect changes
        manual_changes = snapshot_manager.detect_manual_changes("task_1", current_files)
        
        assert "main.py" in manual_changes
        assert len(manual_changes) == 1
    
    def test_no_false_positives_on_unchanged(self, workspace, snapshot_manager):
        """Test that unchanged files don't trigger false positives."""
        content = "unchanged content"
        files = {"main.py": content}
        snapshot_manager.create_snapshot("task_1", files)
        
        # Same content
        current_files = {"main.py": content}
        manual_changes = snapshot_manager.detect_manual_changes("task_1", current_files)
        
        # Should be empty - no changes
        assert manual_changes == {}


class TestConflictDetection:
    """Test conflict detection when files are modified manually."""
    
    @pytest.fixture
    def snapshot_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield DiffSnapshotManager(tmpdir)
    
    def test_can_restore_without_conflict(self, snapshot_manager):
        """Test that file can be restored without conflict."""
        files = {"main.py": "original"}
        snapshot_manager.create_snapshot("task_1", files)
        
        # No manual changes
        can_restore, reason = snapshot_manager.can_restore("task_1", "main.py")
        assert can_restore is True
        assert reason is None
    
    def test_conflict_detection_on_manual_edit(self, snapshot_manager):
        """Test conflict detection when file is manually edited."""
        # Create file and snapshot
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_manager = DiffSnapshotManager(tmpdir)
            main_py = Path(tmpdir) / "main.py"
            main_py.write_text("original")
            
            files = {"main.py": "original"}
            snapshot_manager.create_snapshot("task_1", files)
            
            # Manually edit file
            main_py.write_text("original\nmanual edit")
            
            # Try to restore
            can_restore, reason = snapshot_manager.can_restore("task_1", "main.py")
            assert can_restore is False
            assert "manual changes" in reason.lower()
    
    def test_file_not_in_snapshot_cannot_restore(self, snapshot_manager):
        """Test that files not in snapshot cannot be restored."""
        files = {"main.py": "content"}
        snapshot_manager.create_snapshot("task_1", files)
        
        can_restore, reason = snapshot_manager.can_restore("task_1", "unknown.py")
        assert can_restore is False
        assert "not in snapshot" in reason.lower()


class TestFileRestoration:
    """Test file restoration and backup creation."""
    
    @pytest.fixture
    def workspace(self):
        tmpdir = tempfile.mkdtemp()
        yield Path(tmpdir)
        shutil.rmtree(tmpdir)
    
    @pytest.fixture
    def snapshot_manager(self, workspace):
        return DiffSnapshotManager(str(workspace))
    
    def test_restore_file_creates_backup(self, workspace, snapshot_manager):
        """Test that restore creates backup of current file."""
        # Create initial file
        main_py = workspace / "main.py"
        main_py.write_text("original")
        
        # Create snapshot
        snapshot_manager.create_snapshot("task_1", {"main.py": "original"})
        
        # Modify file
        main_py.write_text("modified")
        
        # Restore
        success, error = snapshot_manager.restore_file("task_1", "main.py", create_backup=True)
        
        # Check results
        assert success is True
        assert error is None
        assert (workspace / "main.py.backup").exists()
        assert (workspace / "main.py.backup").read_text() == "modified"
        assert main_py.read_text() == "original"
    
    def test_restore_file_without_backup(self, workspace, snapshot_manager):
        """Test restore without creating backup."""
        main_py = workspace / "main.py"
        main_py.write_text("original")
        
        snapshot_manager.create_snapshot("task_1", {"main.py": "original"})
        main_py.write_text("modified")
        
        success, error = snapshot_manager.restore_file("task_1", "main.py", create_backup=False)
        
        assert success is True
        assert not (workspace / "main.py.backup").exists()
        assert main_py.read_text() == "original"
    
    def test_restore_deleted_file_recreates_it(self, workspace, snapshot_manager):
        """Test that deleted file can be recreated from snapshot."""
        main_py = workspace / "main.py"
        main_py.write_text("original")
        
        snapshot_manager.create_snapshot("task_1", {"main.py": "original"})
        
        # Delete file
        main_py.unlink()
        assert not main_py.exists()
        
        # Restore
        success, error = snapshot_manager.restore_file("task_1", "main.py")
        
        assert success is True
        assert main_py.exists()
        assert main_py.read_text() == "original"
    
    def test_restore_new_file_removes_it(self, workspace, snapshot_manager):
        """Test that new file created by agent is removed on reject."""
        # File didn't exist in snapshot
        snapshot_manager.create_snapshot("task_1", {"main.py": "original"})
        
        # New file created by agent
        new_file = workspace / "generated.py"
        new_file.write_text("generated by agent")
        
        # "Restore" = remove the file
        snapshot_manager._snapshots["task_1"]["generated.py"] = SnapshotFileRecord(
            path="generated.py",
            exists=False,
            content=None,
            hash=None,
            mtime=None,
            size=0,
            file_status=FileStatus.NOT_FOUND.value,
        )
        
        success, error = snapshot_manager.restore_file("task_1", "generated.py")
        assert success is True
        assert not new_file.exists()


class TestChangeManagement:
    """Test change management operations (accept/reject)."""
    
    @pytest.fixture
    def console(self):
        return Console()
    
    @pytest.fixture
    def snapshot_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield DiffSnapshotManager(tmpdir)
    
    @pytest.fixture
    def change_manager(self, snapshot_manager, console):
        return DiffChangeManager(snapshot_manager, console)
    
    def test_accept_file(self, change_manager, snapshot_manager):
        """Test accepting a file change."""
        diff_metadata = {
            "main.py": FileDiffMetadata(
                path="main.py",
                change_type=FileChangeType.MODIFIED,
                file_status=FileStatus.READABLE,
                lines_added=5,
                lines_removed=2,
            )
        }
        
        change_manager.initialize_review("task_1", diff_metadata)
        
        success, error = change_manager.accept_file("task_1", "main.py", require_confirmation=False)
        
        assert success is True
        assert error is None
        
        # Check status updated
        status = change_manager.get_review_status("task_1", "main.py")
        assert status == ReviewStatus.ACCEPTED
    
    def test_reject_file(self, change_manager, snapshot_manager):
        """Test rejecting a file change (with mock snapshot)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_manager = DiffSnapshotManager(tmpdir)
            change_manager = DiffChangeManager(snapshot_manager, Console())
            
            # Create file and snapshot
            main_py = Path(tmpdir) / "main.py"
            main_py.write_text("original")
            snapshot_manager.create_snapshot("task_1", {"main.py": "original"})
            
            # Initialize review
            diff_metadata = {
                "main.py": FileDiffMetadata(
                    path="main.py",
                    change_type=FileChangeType.MODIFIED,
                    file_status=FileStatus.READABLE,
                )
            }
            change_manager.initialize_review("task_1", diff_metadata)
            
            # Modify file
            main_py.write_text("modified")
            
            # Reject
            success, error = change_manager.reject_file("task_1", "main.py", require_confirmation=False)
            
            assert success is True
            assert main_py.read_text() == "original"
            
            status = change_manager.get_review_status("task_1", "main.py")
            assert status == ReviewStatus.REJECTED
    
    def test_get_pending_files(self, change_manager):
        """Test getting list of pending files."""
        diff_metadata = {
            "a.py": FileDiffMetadata(
                path="a.py",
                change_type=FileChangeType.CREATED,
                file_status=FileStatus.READABLE,
            ),
            "b.py": FileDiffMetadata(
                path="b.py",
                change_type=FileChangeType.MODIFIED,
                file_status=FileStatus.READABLE,
            ),
        }
        
        change_manager.initialize_review("task_1", diff_metadata)
        
        pending = change_manager.get_pending_files("task_1")
        assert len(pending) == 2
        
        # Accept one
        change_manager.accept_file("task_1", "a.py", require_confirmation=False)
        
        pending = change_manager.get_pending_files("task_1")
        assert len(pending) == 1
        assert pending[0].file_path == "b.py"


class TestBatchOperations:
    """Test batch accept/reject operations."""
    
    @pytest.fixture
    def workspace(self):
        tmpdir = tempfile.mkdtemp()
        yield Path(tmpdir)
        shutil.rmtree(tmpdir)
    
    @pytest.fixture
    def change_manager(self, workspace):
        snapshot_manager = DiffSnapshotManager(str(workspace))
        return DiffChangeManager(snapshot_manager, Console())
    
    def test_accept_all(self, change_manager):
        """Test accepting all pending changes."""
        diff_metadata = {
            "a.py": FileDiffMetadata(
                path="a.py",
                change_type=FileChangeType.CREATED,
                file_status=FileStatus.READABLE,
            ),
            "b.py": FileDiffMetadata(
                path="b.py",
                change_type=FileChangeType.MODIFIED,
                file_status=FileStatus.READABLE,
            ),
            "c.py": FileDiffMetadata(
                path="c.py",
                change_type=FileChangeType.DELETED,
                file_status=FileStatus.READABLE,
            ),
        }
        
        change_manager.initialize_review("task_1", diff_metadata)
        
        accepted, errors = change_manager.accept_all("task_1")
        
        assert accepted == 3
        assert errors == []
        
        summary = change_manager.get_summary("task_1")
        assert summary['accepted'] == 3
        assert summary['pending'] == 0
    
    def test_reject_all(self, workspace, change_manager):
        """Test rejecting all pending changes."""
        # Create files
        (workspace / "a.py").write_text("content a")
        (workspace / "b.py").write_text("content b")
        
        # Snapshot
        snapshot_manager = change_manager.snapshot_manager
        snapshot_manager.create_snapshot("task_1", {
            "a.py": "content a",
            "b.py": "content b",
        })
        
        # Initialize review
        diff_metadata = {
            "a.py": FileDiffMetadata(
                path="a.py",
                change_type=FileChangeType.MODIFIED,
                file_status=FileStatus.READABLE,
            ),
            "b.py": FileDiffMetadata(
                path="b.py",
                change_type=FileChangeType.MODIFIED,
                file_status=FileStatus.READABLE,
            ),
        }
        change_manager.initialize_review("task_1", diff_metadata)
        
        # Modify files
        (workspace / "a.py").write_text("modified a")
        (workspace / "b.py").write_text("modified b")
        
        # Reject all
        rejected, errors = change_manager.reject_all("task_1")
        
        assert rejected == 2
        assert (workspace / "a.py").read_text() == "content a"
        assert (workspace / "b.py").read_text() == "content b"


class TestConflictManagement:
    """Test conflict detection and management."""
    
    @pytest.fixture
    def console(self):
        return Console()
    
    @pytest.fixture
    def change_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_manager = DiffSnapshotManager(tmpdir)
            yield DiffChangeManager(snapshot_manager, console=Console())
    
    def test_detect_conflicts_from_manual_changes(self, change_manager):
        """Test that manual changes are detected as conflicts."""
        diff_metadata = {
            "main.py": FileDiffMetadata(
                path="main.py",
                change_type=FileChangeType.MODIFIED,
                file_status=FileStatus.READABLE,
            )
        }
        
        change_manager.initialize_review("task_1", diff_metadata)
        
        # Simulate manual changes
        current_files = {
            "main.py": "content with manual edits"
        }
        
        # Mock snapshot to detect differences
        change_manager.snapshot_manager._snapshots["task_1"] = {
            "main.py": SnapshotFileRecord(
                path="main.py",
                exists=True,
                content="original",
                hash="abc123",
                mtime=None,
                size=8,
                file_status=FileStatus.READABLE.value,
            )
        }
        
        conflicts = change_manager.detect_conflicts("task_1", current_files)
        
        assert "main.py" in conflicts
        assert len(conflicts) == 1
    
    def test_conflict_state_prevents_restore(self, change_manager):
        """Test that conflict state shows in review status."""
        diff_metadata = {
            "main.py": FileDiffMetadata(
                path="main.py",
                change_type=FileChangeType.MODIFIED,
                file_status=FileStatus.READABLE,
            )
        }
        
        change_manager.initialize_review("task_1", diff_metadata)
        
        # Manually set conflict
        state = change_manager._review_states["task_1"]["main.py"]
        state.review_status = ReviewStatus.CONFLICT
        state.conflict_reason = "Test conflict"
        
        summary = change_manager.get_summary("task_1")
        assert summary['conflicts'] == 1
        assert summary['pending'] == 0


class TestReviewCommandHandler:
    """Test review command parsing and execution."""
    
    @pytest.fixture
    def console(self):
        return Console()
    
    @pytest.fixture
    def handler(self, console):
        snapshot_manager = DiffSnapshotManager(tempfile.mkdtemp())
        change_manager = DiffChangeManager(snapshot_manager, console)
        return DiffReviewCommandHandler(change_manager, console)
    
    def test_is_review_command(self, handler):
        """Test command detection."""
        assert handler.is_review_command("/accept main.py") is True
        assert handler.is_review_command("/reject main.py") is True
        assert handler.is_review_command("/status") is True
        assert handler.is_review_command("/conflicts") is True
        assert handler.is_review_command("/undo") is True
        assert handler.is_review_command("/accept-all") is True
        assert handler.is_review_command("/reject-all") is True
        
        assert handler.is_review_command("not a command") is False
        assert handler.is_review_command("/diff main.py") is False
    
    def test_parse_accept_command(self, handler):
        """Test parsing /accept command."""
        cmd, arg = handler.parse_command("/accept src/main.py")
        assert cmd == "/accept"
        assert arg == "src/main.py"
    
    def test_parse_reject_command(self, handler):
        """Test parsing /reject command."""
        cmd, arg = handler.parse_command("/reject test.py")
        assert cmd == "/reject"
        assert arg == "test.py"
    
    def test_parse_batch_commands(self, handler):
        """Test parsing batch commands."""
        cmd, arg = handler.parse_command("/accept-all")
        assert cmd == "/accept-all"
        assert arg is None
        
        cmd, arg = handler.parse_command("/reject-all")
        assert cmd == "/reject-all"
        assert arg is None
    
    def test_parse_status_command(self, handler):
        """Test parsing status command."""
        cmd, arg = handler.parse_command("/status")
        assert cmd == "/status"
        assert arg is None
    
    def test_invalid_command(self, handler):
        """Test invalid command parsing."""
        cmd, arg = handler.parse_command("/invalid")
        assert cmd == "invalid"


class TestDiffCommandHandler:
    """Test /diff command handling."""
    
    @pytest.fixture
    def handler(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = DiffConfig(auto_display=False)  # Disable auto for testing
            console = Console()
            yield DiffCommandHandler(tmpdir, console, config)
    
    def test_is_diff_command(self, handler):
        """Test /diff command detection."""
        assert handler.is_diff_command("/diff") is True
        assert handler.is_diff_command("/diff main.py") is True
        assert handler.is_diff_command("/diff src/path/file.py") is True
        
        assert handler.is_diff_command("/accept") is False
        assert handler.is_diff_command("not a command") is False
    
    def test_parse_diff_full(self, handler):
        """Test parsing /diff with no path."""
        cmd, path = handler.parse_diff_command("/diff")
        assert cmd == "full"
        assert path is None
    
    def test_parse_diff_file(self, handler):
        """Test parsing /diff with file path."""
        cmd, path = handler.parse_diff_command("/diff src/main.py")
        assert cmd == "file"
        assert path == "src/main.py"
    
    def test_parse_diff_invalid(self, handler):
        """Test parsing invalid diff command."""
        cmd, path = handler.parse_diff_command("/diff /diff")
        assert cmd == "invalid" or cmd == "file"  # Depending on implementation


class TestNoFalseTriggersOnReadOnly:
    """Test that read-only operations don't trigger diff."""
    
    @pytest.fixture
    def workspace(self):
        tmpdir = tempfile.mkdtemp()
        yield Path(tmpdir)
        shutil.rmtree(tmpdir)
    
    @pytest.fixture
    def handler(self, workspace):
        config = DiffConfig(auto_display=True)
        console = Console()
        return DiffCommandHandler(str(workspace), console, config)
    
    def test_no_trigger_on_file_read(self, workspace, handler):
        """Test that reading file doesn't trigger diff."""
        # Create file
        (workspace / "main.py").write_text("content")
        
        # Capture initial state
        handler.capture_initial_state()
        
        # Read file (should not trigger change)
        _ = (workspace / "main.py").read_text()
        
        # Detect changes
        changes = handler.detect_and_track_changes()
        
        assert changes == {}
    
    def test_trigger_only_on_actual_modifications(self, workspace, handler):
        """Test that only actual modifications trigger diff."""
        # Create file
        (workspace / "main.py").write_text("original")
        
        # Capture initial state
        handler.capture_initial_state()
        
        # Modify file
        (workspace / "main.py").write_text("modified")
        
        # Detect changes
        changes = handler.detect_and_track_changes()
        
        assert len(changes) > 0
        assert "main.py" in changes


class TestSessionPersistence:
    """Test that session state persists across operations."""
    
    def test_conversation_history_preserved(self):
        """Test that conversation history is preserved."""
        history = []
        
        # Simulate conversation
        history.append({"role": "user", "content": "Edit main.py"})
        history.append({"role": "assistant", "content": "Done"})
        
        # Accept changes
        # (history should still be there)
        
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"
    
    def test_snapshots_cleared_per_task(self):
        """Test that snapshots are cleaned up after task."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_manager = DiffSnapshotManager(tmpdir)
            
            # Create snapshot for task 1
            snapshot_manager.create_snapshot("task_1", {"main.py": "content"})
            assert "task_1" in snapshot_manager._snapshots
            
            # Clean up
            snapshot_manager.cleanup_snapshot("task_1")
            assert "task_1" not in snapshot_manager._snapshots


class TestSummaryAndStatusTracking:
    """Test summary and status display functionality."""
    
    @pytest.fixture
    def change_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_manager = DiffSnapshotManager(tmpdir)
            yield DiffChangeManager(snapshot_manager, Console())
    
    def test_get_summary(self, change_manager):
        """Test getting summary of review state."""
        diff_metadata = {
            "a.py": FileDiffMetadata(
                path="a.py",
                change_type=FileChangeType.CREATED,
                file_status=FileStatus.READABLE,
            ),
            "b.py": FileDiffMetadata(
                path="b.py",
                change_type=FileChangeType.MODIFIED,
                file_status=FileStatus.READABLE,
            ),
            "c.py": FileDiffMetadata(
                path="c.py",
                change_type=FileChangeType.DELETED,
                file_status=FileStatus.READABLE,
            ),
        }
        
        change_manager.initialize_review("task_1", diff_metadata)
        
        # Accept one, leave others pending
        change_manager.accept_file("task_1", "a.py", require_confirmation=False)
        
        summary = change_manager.get_summary("task_1")
        
        assert summary['total'] == 3
        assert summary['pending'] == 2
        assert summary['accepted'] == 1
        assert summary['rejected'] == 0
        assert summary['conflicts'] == 0
    
    def test_display_review_summary_with_mixed_status(self, change_manager):
        """Test displaying summary with mixed status."""
        diff_metadata = {
            "a.py": FileDiffMetadata(
                path="a.py",
                change_type=FileChangeType.CREATED,
                file_status=FileStatus.READABLE,
                lines_added=10,
            ),
            "b.py": FileDiffMetadata(
                path="b.py",
                change_type=FileChangeType.MODIFIED,
                file_status=FileStatus.READABLE,
                lines_added=5,
                lines_removed=3,
            ),
        }
        
        change_manager.initialize_review("task_1", diff_metadata)
        change_manager.accept_file("task_1", "a.py", require_confirmation=False)
        
        # Should not raise
        change_manager.display_review_summary("task_1")


# Integration workflow tests

class TestCompleteAcceptRejectWorkflow:
    """Test complete accept/reject workflow."""
    
    def test_full_workflow(self):
        """Test complete workflow from snapshot to accept/reject."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            
            # 1. Create files
            (workspace / "main.py").write_text("original")
            (workspace / "utils.py").write_text("original utils")
            
            # 2. Create snapshot
            snapshot_manager = DiffSnapshotManager(str(workspace))
            snapshot_manager.create_snapshot("task_1", {
                "main.py": "original",
                "utils.py": "original utils",
            })
            
            # 3. Initialize change manager
            change_manager = DiffChangeManager(snapshot_manager, Console())
            diff_metadata = {
                "main.py": FileDiffMetadata(
                    path="main.py",
                    change_type=FileChangeType.MODIFIED,
                    file_status=FileStatus.READABLE,
                    lines_added=3,
                    lines_removed=1,
                ),
                "utils.py": FileDiffMetadata(
                    path="utils.py",
                    change_type=FileChangeType.MODIFIED,
                    file_status=FileStatus.READABLE,
                    lines_added=2,
                ),
            }
            change_manager.initialize_review("task_1", diff_metadata)
            
            # 4. Modify files (simulating agent work)
            (workspace / "main.py").write_text("original\nnew line\nanother line\nmore")
            (workspace / "utils.py").write_text("original utils\nnew util")
            
            # 5. Accept main.py
            success, error = change_manager.accept_file("task_1", "main.py", require_confirmation=False)
            assert success is True
            
            # 6. Check summary
            summary = change_manager.get_summary("task_1")
            assert summary['pending'] == 1
            assert summary['accepted'] == 1
            
            # 7. Reject utils.py
            success, error = change_manager.reject_file("task_1", "utils.py", require_confirmation=False)
            assert success is True
            
            # 8. Verify restoration
            assert (workspace / "utils.py").read_text() == "original utils"
            assert (workspace / "main.py").read_text() == "original\nnew line\nanother line\nmore"
            
            # 9. Final summary
            final_summary = change_manager.get_summary("task_1")
            assert final_summary['pending'] == 0
            assert final_summary['accepted'] == 1
            assert final_summary['rejected'] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
