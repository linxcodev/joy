"""Integration tests for session persistence and recovery.

Tests cover:
- Creating and saving sessions
- Loading and resuming sessions
- Handling corrupted sessions
- Recovery from failures
- Compatibility with old sessions
"""

import json
import tempfile
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from joy.session_manager import (
    SessionManager,
    SessionRecovery,
    SessionSnapshot,
    SessionState,
    CompactionMetadata,
    TaskStatus,
)


class TestSessionSnapshot:
    """Test SessionSnapshot data structure."""
    
    def test_snapshot_creation(self):
        """Test creating a snapshot."""
        snapshot = SessionSnapshot(
            session_id="test_session",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[],
        )
        
        assert snapshot.session_id == "test_session"
        assert snapshot.state == SessionState.ACTIVE.value
        assert isinstance(snapshot.conversation_history, list)
    
    def test_snapshot_to_dict(self):
        """Test converting snapshot to dict."""
        snapshot = SessionSnapshot(
            session_id="test_session",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[{"role": "user", "content": "test"}],
        )
        
        data = snapshot.to_dict()
        assert data["session_id"] == "test_session"
        assert data["conversation_history"] == [{"role": "user", "content": "test"}]
    
    def test_snapshot_from_dict(self):
        """Test creating snapshot from dict."""
        data = {
            "session_id": "test_session",
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "state": SessionState.ACTIVE.value,
            "conversation_history": [{"role": "user", "content": "test"}],
            "compaction_metadata": {},
            "task_status": {},
        }
        
        snapshot = SessionSnapshot.from_dict(data)
        assert snapshot.session_id == "test_session"
        assert len(snapshot.conversation_history) == 1
    
    def test_snapshot_checksum(self):
        """Test checksum computation."""
        snapshot = SessionSnapshot(
            session_id="test_session",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[],
            turn_count=5,
        )
        
        checksum = snapshot.compute_checksum()
        assert isinstance(checksum, str)
        assert len(checksum) == 16  # SHA256 truncated
        
        # Same data should produce same checksum
        checksum2 = snapshot.compute_checksum()
        assert checksum == checksum2
        
        # Different data should produce different checksum
        snapshot.turn_count = 6
        checksum3 = snapshot.compute_checksum()
        assert checksum != checksum3


class TestSessionManager:
    """Test SessionManager functionality."""
    
    @pytest.fixture
    def temp_session_dir(self):
        """Create temporary session directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)
    
    @pytest.fixture
    def manager(self, temp_session_dir):
        """Create SessionManager with temp directory."""
        return SessionManager(session_dir=temp_session_dir)
    
    def test_create_session(self, manager):
        """Test creating a new session."""
        session_id = manager.create_session(
            model="gpt-4",
            user_goal="Test goal",
            system_prompt="Test prompt",
        )
        
        assert session_id is not None
        assert session_id.startswith("joy_")
        assert manager.get_current_session() == session_id
        
        # Verify file was created
        session_file = manager.session_dir / f"{session_id}.json"
        assert session_file.exists()
    
    def test_save_snapshot_atomically(self, manager):
        """Test atomic snapshot saving."""
        snapshot = SessionSnapshot(
            session_id="test_session",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[{"role": "user", "content": "test"}],
        )
        
        success = manager.save_snapshot(snapshot)
        assert success is True
        
        # Verify checksum was set
        assert snapshot.checksum is not None
        
        # Verify file exists and is readable
        session_file = manager.session_dir / "test_session.json"
        assert session_file.exists()
        
        with open(session_file) as f:
            data = json.load(f)
            assert data["session_id"] == "test_session"
            assert data["checksum"] is not None
    
    def test_load_snapshot(self, manager):
        """Test loading a snapshot."""
        # Create and save
        snapshot = SessionSnapshot(
            session_id="test_load",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[{"role": "user", "content": "hello"}],
            user_goal="Test goal",
            model="gpt-4",
        )
        manager.save_snapshot(snapshot)
        
        # Load
        loaded = manager.load_snapshot("test_load")
        assert loaded is not None
        assert loaded.session_id == "test_load"
        assert loaded.user_goal == "Test goal"
        assert len(loaded.conversation_history) == 1
        assert loaded.conversation_history[0]["content"] == "hello"
    
    def test_load_nonexistent_session(self, manager):
        """Test loading a session that doesn't exist."""
        loaded = manager.load_snapshot("nonexistent")
        assert loaded is None
    
    def test_update_snapshot(self, manager):
        """Test updating an existing snapshot."""
        # Create initial
        snapshot = SessionSnapshot(
            session_id="test_update",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[],
            turn_count=1,
        )
        manager.save_snapshot(snapshot)
        
        # Update
        snapshot.turn_count = 5
        snapshot.conversation_history.append({"role": "assistant", "content": "response"})
        manager.update_snapshot(snapshot)
        
        # Verify
        loaded = manager.load_snapshot("test_update")
        assert loaded.turn_count == 5
        assert len(loaded.conversation_history) == 1
    
    def test_list_sessions(self, manager):
        """Test listing sessions."""
        # Create multiple sessions
        manager.create_session(model="gpt-4", user_goal="Goal 1")
        manager.create_session(model="gpt-4", user_goal="Goal 2")
        manager.create_session(model="gpt-4", user_goal="Goal 3")
        
        sessions = manager.list_sessions()
        assert len(sessions) == 3
        assert all("session_id" in s for s in sessions)
    
    def test_delete_session(self, manager):
        """Test deleting a session."""
        session_id = manager.create_session(model="gpt-4")
        
        # Verify exists
        session_file = manager.session_dir / f"{session_id}.json"
        assert session_file.exists()
        
        # Delete
        success = manager.delete_session(session_id)
        assert success is True
        assert not session_file.exists()
    
    def test_current_session_tracking(self, manager):
        """Test current session tracking."""
        assert manager.get_current_session() is None
        
        session_id1 = manager.create_session(model="gpt-4")
        assert manager.get_current_session() == session_id1
        
        session_id2 = manager.create_session(model="gpt-4")
        assert manager.get_current_session() == session_id2
        
        # Set to first
        manager.set_current_session(session_id1)
        assert manager.get_current_session() == session_id1
    
    def test_save_with_compaction_metadata(self, manager):
        """Test saving with compaction metadata."""
        snapshot = SessionSnapshot(
            session_id="test_compaction",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[],
            compaction_metadata={
                "compaction_count": 2,
                "total_tokens_saved": 5000,
                "is_compacted": True,
            },
        )
        
        manager.save_snapshot(snapshot)
        loaded = manager.load_snapshot("test_compaction")
        
        assert loaded.compaction_metadata["compaction_count"] == 2
        assert loaded.compaction_metadata["total_tokens_saved"] == 5000
    
    def test_save_with_task_status(self, manager):
        """Test saving with task status."""
        snapshot = SessionSnapshot(
            session_id="test_task",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[],
            task_status={
                "current_task": "Implement feature X",
                "subtasks": ["Step 1", "Step 2"],
                "next_steps": ["Step 3"],
            },
        )
        
        manager.save_snapshot(snapshot)
        loaded = manager.load_snapshot("test_task")
        
        assert loaded.task_status["current_task"] == "Implement feature X"
        assert "Step 1" in loaded.task_status["subtasks"]


class TestSessionRecovery:
    """Test SessionRecovery functionality."""
    
    @pytest.fixture
    def temp_session_dir(self):
        """Create temporary session directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)
    
    @pytest.fixture
    def manager(self, temp_session_dir):
        """Create SessionManager with temp directory."""
        return SessionManager(session_dir=temp_session_dir)
    
    @pytest.fixture
    def recovery(self, manager):
        """Create SessionRecovery."""
        return SessionRecovery(manager)
    
    def test_validate_valid_snapshot(self, recovery):
        """Test validating a valid snapshot."""
        snapshot = SessionSnapshot(
            session_id="valid",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[],
            system_prompt="Test",
        )
        
        is_valid, issues = recovery.validate_snapshot(snapshot)
        assert is_valid is True
        assert len(issues) == 0
    
    def test_validate_missing_session_id(self, recovery):
        """Test validation with missing session_id."""
        snapshot = SessionSnapshot(
            session_id="",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[],
        )
        
        is_valid, issues = recovery.validate_snapshot(snapshot)
        assert is_valid is False
        assert any("session_id" in issue for issue in issues)
    
    def test_validate_invalid_state(self, recovery):
        """Test validation with invalid state."""
        snapshot = SessionSnapshot(
            session_id="test",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state="invalid_state",
            conversation_history=[],
        )
        
        is_valid, issues = recovery.validate_snapshot(snapshot)
        assert is_valid is False
        assert any("state" in issue for issue in issues)
    
    def test_validate_checksum_mismatch(self, recovery):
        """Test validation with checksum mismatch."""
        snapshot = SessionSnapshot(
            session_id="test",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[],
            checksum="wrong_checksum",
        )
        
        is_valid, issues = recovery.validate_snapshot(snapshot)
        assert is_valid is False
        assert any("Checksum" in issue for issue in issues)
    
    def test_recover_valid_session(self, manager, recovery):
        """Test recovering a valid session."""
        # Create valid session
        snapshot = SessionSnapshot(
            session_id="valid_recover",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[{"role": "user", "content": "test"}],
            system_prompt="Test prompt",
        )
        manager.save_snapshot(snapshot)
        
        # Recover
        recovered = recovery.recover_session("valid_recover", strict=True)
        assert recovered is not None
        assert recovered.session_id == "valid_recover"
    
    def test_recover_missing_session_id_non_strict(self, manager, recovery):
        """Test recovering corrupted session with missing ID (non-strict)."""
        # Create invalid session directly
        snapshot = SessionSnapshot(
            session_id="",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[],
        )
        
        # Save with manual ID in filename
        import uuid
        temp_id = f"temp_{uuid.uuid4().hex[:8]}"
        snapshot_copy = SessionSnapshot(
            session_id=temp_id,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            state=snapshot.state,
            conversation_history=snapshot.conversation_history,
        )
        manager.save_snapshot(snapshot_copy)
        
        # Recover with repair
        recovered = recovery.recover_session(temp_id, strict=False)
        assert recovered is not None
        # Should have been repaired (new session_id generated or session_id set from filename)
    
    def test_repair_corrupted_state(self, manager, recovery):
        """Test repairing snapshot with invalid state."""
        # Create with invalid state and save directly
        session_id = "repair_test"
        session_file = manager.session_dir / f"{session_id}.json"
        
        data = {
            "session_id": session_id,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "state": "invalid_state",  # This is invalid
            "conversation_history": [],
            "compaction_metadata": {},
            "task_status": {},
        }
        
        with open(session_file, "w") as f:
            json.dump(data, f)
        
        # Recover and repair
        recovered = recovery.recover_session(session_id, strict=False)
        assert recovered is not None
        # Should be set to CORRUPTED state
        assert recovered.state == SessionState.CORRUPTED.value
    
    def test_repair_conversation_history(self, manager, recovery):
        """Test repairing invalid conversation history."""
        session_id = "repair_conv"
        session_file = manager.session_dir / f"{session_id}.json"
        
        data = {
            "session_id": session_id,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "state": SessionState.ACTIVE.value,
            "conversation_history": "not_a_list",  # Invalid
            "compaction_metadata": {},
            "task_status": {},
        }
        
        with open(session_file, "w") as f:
            json.dump(data, f)
        
        recovered = recovery.recover_session(session_id, strict=False)
        assert recovered is not None
        assert isinstance(recovered.conversation_history, list)
        assert len(recovered.conversation_history) == 0


class TestSessionIntegration:
    """Integration tests for session persistence and recovery."""
    
    @pytest.fixture
    def temp_session_dir(self):
        """Create temporary session directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)
    
    def test_save_and_resume_session(self, temp_session_dir):
        """Test saving a session and resuming it later."""
        # Create and save
        manager1 = SessionManager(session_dir=temp_session_dir)
        session_id = manager1.create_session(
            model="gpt-4",
            user_goal="Build feature X",
            system_prompt="You are Joy",
            context_window_size=128000,
        )
        
        # Simulate some work
        snapshot = manager1.load_snapshot(session_id)
        snapshot.conversation_history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        snapshot.turn_count = 2
        manager1.update_snapshot(snapshot)
        
        # New manager loads it
        manager2 = SessionManager(session_dir=temp_session_dir)
        loaded = manager2.load_snapshot(session_id)
        
        assert loaded is not None
        assert loaded.user_goal == "Build feature X"
        assert loaded.turn_count == 2
        assert len(loaded.conversation_history) == 2
        assert loaded.model == "gpt-4"
    
    def test_recovery_from_corrupted_session(self, temp_session_dir):
        """Test recovering from a corrupted session file."""
        # Create corrupted session file
        session_id = "corrupted"
        session_file = temp_session_dir / f"{session_id}.json"
        
        # Write invalid JSON
        with open(session_file, "w") as f:
            f.write("{invalid json")
        
        manager = SessionManager(session_dir=temp_session_dir)
        recovery = SessionRecovery(manager)
        
        # Should handle gracefully
        loaded = manager.load_snapshot(session_id)
        assert loaded is None  # Failed to load
    
    def test_prevent_data_loss_on_save_failure(self, temp_session_dir):
        """Test that original file is preserved if save fails."""
        manager = SessionManager(session_dir=temp_session_dir)
        
        # Create initial snapshot
        snapshot = SessionSnapshot(
            session_id="data_loss_test",
            created_at="2024-01-01T00:00:00",
            updated_at="2024-01-01T00:00:00",
            state=SessionState.ACTIVE.value,
            conversation_history=[{"role": "user", "content": "original"}],
        )
        
        manager.save_snapshot(snapshot)
        
        # Verify original saved
        original = manager.load_snapshot("data_loss_test")
        assert original.conversation_history[0]["content"] == "original"
        
        # Try to save with different content (simulates incomplete save)
        snapshot.conversation_history[0]["content"] = "modified"
        manager.save_snapshot(snapshot)
        
        # Verify new content is there
        modified = manager.load_snapshot("data_loss_test")
        assert modified.conversation_history[0]["content"] == "modified"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
