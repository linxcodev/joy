"""Session persistence and recovery for context compaction.

This module provides:
- SessionSnapshot: Structured snapshot of agent state with compaction context
- SessionManager: Atomic save/load with atomic writes and recovery
- SessionRecovery: Recovery from corrupted or incomplete sessions
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any
from enum import Enum

logger = logging.getLogger(__name__)


class SessionState(Enum):
    """Session state indicators."""
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CORRUPTED = "corrupted"


@dataclass
class CompactionMetadata:
    """Metadata about compaction operation."""
    compaction_count: int = 0
    last_compaction_time: Optional[str] = None
    last_compaction_token_reduction: float = 0.0
    total_tokens_saved: float = 0.0
    is_compacted: bool = False


@dataclass
class TaskStatus:
    """Current task status information."""
    current_task: Optional[str] = None
    subtasks: list[str] = field(default_factory=list)
    completed_tasks: list[str] = field(default_factory=list)
    failed_tasks: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)


@dataclass
class SessionSnapshot:
    """Atomic snapshot of agent session with compaction state."""
    session_id: str
    created_at: str
    updated_at: str
    state: str  # SessionState enum value
    
    # Core context
    conversation_history: list[dict]
    preserved_context: Optional[dict] = None
    system_prompt: str = ""
    
    # Task and goal information
    user_goal: Optional[str] = None
    task_status: dict = field(default_factory=dict)
    
    # Compaction metadata
    compaction_metadata: dict = field(default_factory=dict)
    
    # Configuration
    model: str = ""
    context_window_size: int = 128000
    
    # Metadata
    turn_count: int = 0
    tool_call_count: int = 0
    iteration_count: int = 0
    last_tool_call: Optional[str] = None
    
    # Checksum for validation
    checksum: Optional[str] = None
    
    # File references
    modified_files: dict = field(default_factory=dict)  # path -> change_type
    
    # Error tracking
    last_error: Optional[str] = None
    error_count: int = 0
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "SessionSnapshot":
        """Create from dictionary."""
        return cls(**data)
    
    def compute_checksum(self) -> str:
        """Compute checksum for validation."""
        import hashlib
        # Include critical fields for validation
        critical_data = {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "turn_count": self.turn_count,
            "conversation_length": len(self.conversation_history),
            "state": self.state,
        }
        data_str = json.dumps(critical_data, sort_keys=True)
        return hashlib.sha256(data_str.encode()).hexdigest()[:16]


class SessionManager:
    """Manages session persistence with atomic writes and recovery."""
    
    DEFAULT_SESSION_DIR = Path.home() / ".joy" / "sessions"
    
    def __init__(self, session_dir: Optional[Path] = None):
        """Initialize session manager.
        
        Args:
            session_dir: Directory for storing session files (default: ~/.joy/sessions)
        """
        self.session_dir = Path(session_dir) if session_dir else self.DEFAULT_SESSION_DIR
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._current_session_id: Optional[str] = None
        self._session_index_file = self.session_dir / "index.json"
    
    def create_session(
        self,
        model: str,
        user_goal: Optional[str] = None,
        system_prompt: str = "",
        context_window_size: int = 128000,
    ) -> str:
        """Create new session with generated ID.
        
        Args:
            model: Model name
            user_goal: User's main objective
            system_prompt: Agent system instructions
            context_window_size: Context window for this model
            
        Returns:
            session_id
        """
        with self._lock:
            session_id = self._generate_session_id()
            now = datetime.now(timezone.utc).isoformat()
            
            snapshot = SessionSnapshot(
                session_id=session_id,
                created_at=now,
                updated_at=now,
                state=SessionState.ACTIVE.value,
                conversation_history=[],
                user_goal=user_goal,
                system_prompt=system_prompt,
                model=model,
                context_window_size=context_window_size,
                compaction_metadata=asdict(CompactionMetadata()),
                task_status=asdict(TaskStatus()),
            )
            
            self.save_snapshot(snapshot)
            self._current_session_id = session_id
            logger.info(f"Created session: {session_id}")
            
            return session_id
    
    def save_snapshot(self, snapshot: SessionSnapshot) -> bool:
        """Save session snapshot atomically.
        
        Uses atomic write pattern:
        1. Write to temporary file
        2. Compute and set checksum
        3. Rename to final location
        4. Update session index
        
        Args:
            snapshot: SessionSnapshot to save
            
        Returns:
            True if successful, False otherwise
        """
        with self._lock:
            try:
                # Compute checksum before saving
                snapshot.checksum = snapshot.compute_checksum()
                
                session_file = self.session_dir / f"{snapshot.session_id}.json"
                temp_file = self.session_dir / f"{snapshot.session_id}.tmp"
                
                # Write to temporary file
                with open(temp_file, "w") as f:
                    json.dump(snapshot.to_dict(), f, indent=2)
                
                # Atomic rename
                temp_file.replace(session_file)
                
                # Update index
                self._update_session_index(snapshot.session_id)
                
                logger.debug(f"Saved session: {snapshot.session_id}")
                return True
                
            except Exception as e:
                logger.error(f"Failed to save session: {e}")
                # Clean up temporary file if it exists
                try:
                    temp_file.unlink(missing_ok=True)
                except:
                    pass
                return False
    
    def load_snapshot(self, session_id: str) -> Optional[SessionSnapshot]:
        """Load session snapshot.
        
        Args:
            session_id: Session ID to load
            
        Returns:
            SessionSnapshot if found and valid, None otherwise
        """
        with self._lock:
            try:
                session_file = self.session_dir / f"{session_id}.json"
                
                if not session_file.exists():
                    logger.warning(f"Session file not found: {session_id}")
                    return None
                
                with open(session_file, "r") as f:
                    data = json.load(f)
                
                snapshot = SessionSnapshot.from_dict(data)
                
                # Validate checksum
                if snapshot.checksum:
                    computed = snapshot.compute_checksum()
                    if computed != snapshot.checksum:
                        logger.warning(f"Checksum mismatch for session {session_id}")
                        # Mark as corrupted but still return
                        snapshot.state = SessionState.CORRUPTED.value
                
                self._current_session_id = session_id
                logger.debug(f"Loaded session: {session_id}")
                
                return snapshot
                
            except Exception as e:
                logger.error(f"Failed to load session {session_id}: {e}")
                return None
    
    def update_snapshot(self, snapshot: SessionSnapshot) -> bool:
        """Update existing session snapshot.
        
        Args:
            snapshot: Updated snapshot
            
        Returns:
            True if successful
        """
        snapshot.updated_at = datetime.now(timezone.utc).isoformat()
        return self.save_snapshot(snapshot)
    
    def list_sessions(self) -> list[dict]:
        """List all available sessions.
        
        Returns:
            List of session metadata dicts
        """
        with self._lock:
            try:
                if not self._session_index_file.exists():
                    return []
                
                with open(self._session_index_file, "r") as f:
                    return json.load(f)
                    
            except Exception as e:
                logger.error(f"Failed to load session index: {e}")
                return []
    
    def delete_session(self, session_id: str) -> bool:
        """Delete session file.
        
        Args:
            session_id: Session ID to delete
            
        Returns:
            True if successful
        """
        with self._lock:
            try:
                session_file = self.session_dir / f"{session_id}.json"
                if session_file.exists():
                    session_file.unlink()
                    logger.info(f"Deleted session: {session_id}")
                    return True
                return False
            except Exception as e:
                logger.error(f"Failed to delete session {session_id}: {e}")
                return False
    
    def get_current_session(self) -> Optional[str]:
        """Get current session ID.
        
        Returns:
            Current session ID or None
        """
        return self._current_session_id
    
    def set_current_session(self, session_id: str) -> bool:
        """Set current session ID (loads and validates it).
        
        Args:
            session_id: Session ID to set as current
            
        Returns:
            True if session exists and is valid
        """
        snapshot = self.load_snapshot(session_id)
        if snapshot:
            self._current_session_id = session_id
            return True
        return False
    
    @staticmethod
    def _generate_session_id() -> str:
        """Generate unique session ID."""
        import uuid
        return f"joy_{uuid.uuid4().hex[:12]}"
    
    def _update_session_index(self, session_id: str) -> None:
        """Update session index file."""
        try:
            index_list = []
            if self._session_index_file.exists():
                with open(self._session_index_file, "r") as f:
                    index_list = json.load(f)
            
            # Find and update existing entry or add new
            found = False
            for entry in index_list:
                if entry.get("session_id") == session_id:
                    entry["updated_at"] = datetime.utcnow().isoformat()
                    found = True
                    break
            
            if not found:
                index_list.append({
                    "session_id": session_id,
                    "updated_at": datetime.utcnow().isoformat(),
                })
            
            # Write atomically
            temp_index = self.session_dir / "index.tmp"
            with open(temp_index, "w") as f:
                json.dump(index_list, f, indent=2)
            temp_index.replace(self._session_index_file)
            
        except Exception as e:
            logger.error(f"Failed to update session index: {e}")


class SessionRecovery:
    """Recovery mechanism for corrupted or incomplete sessions."""
    
    def __init__(self, manager: SessionManager):
        """Initialize recovery mechanism.
        
        Args:
            manager: SessionManager instance
        """
        self.manager = manager
    
    def validate_snapshot(self, snapshot: SessionSnapshot) -> tuple[bool, list[str]]:
        """Validate session snapshot.
        
        Args:
            snapshot: SessionSnapshot to validate
            
        Returns:
            Tuple of (is_valid, list of issues)
        """
        issues = []
        
        # Check required fields
        if not snapshot.session_id:
            issues.append("Missing session_id")
        
        if not snapshot.created_at:
            issues.append("Missing created_at")
        
        if not snapshot.system_prompt and snapshot.state != SessionState.CORRUPTED.value:
            issues.append("Missing system_prompt")
        
        # Check conversation is list
        if not isinstance(snapshot.conversation_history, list):
            issues.append("conversation_history is not a list")
        
        # Check compaction metadata
        if snapshot.compaction_metadata and not isinstance(snapshot.compaction_metadata, dict):
            issues.append("compaction_metadata is not a dict")
        
        # Check state is valid
        try:
            SessionState(snapshot.state)
        except ValueError:
            issues.append(f"Invalid state: {snapshot.state}")
        
        # Check checksums match
        if snapshot.checksum:
            computed = snapshot.compute_checksum()
            if computed != snapshot.checksum:
                issues.append("Checksum mismatch")
        
        return len(issues) == 0, issues
    
    def recover_session(
        self,
        session_id: str,
        strict: bool = False,
    ) -> Optional[SessionSnapshot]:
        """Recover session, fixing issues if possible.
        
        Args:
            session_id: Session ID to recover
            strict: If True, fail on any issues; if False, repair what possible
            
        Returns:
            Recovered SessionSnapshot or None if unrecoverable
        """
        snapshot = self.manager.load_snapshot(session_id)
        
        if not snapshot:
            logger.error(f"Session file not found: {session_id}")
            return None
        
        is_valid, issues = self.validate_snapshot(snapshot)
        
        if is_valid:
            logger.info(f"Session {session_id} is valid")
            return snapshot
        
        logger.warning(f"Session {session_id} has issues: {issues}")
        
        if strict:
            logger.error("Strict mode: failing on validation issues")
            return None
        
        # Attempt to repair
        return self._repair_snapshot(snapshot, issues)
    
    def _repair_snapshot(
        self,
        snapshot: SessionSnapshot,
        issues: list[str],
    ) -> Optional[SessionSnapshot]:
        """Attempt to repair corrupted snapshot.
        
        Args:
            snapshot: SessionSnapshot to repair
            issues: List of issues found
            
        Returns:
            Repaired snapshot or None if unrecoverable
        """
        logger.info(f"Attempting to repair snapshot {snapshot.session_id}")
        
        try:
            repairs = []
            
            # Fix missing session_id
            if not snapshot.session_id:
                snapshot.session_id = SessionManager._generate_session_id()
                repairs.append("Generated new session_id")
            
            # Fix missing timestamps
            if not snapshot.created_at:
                snapshot.created_at = datetime.utcnow().isoformat()
                repairs.append("Generated created_at")
            
            snapshot.updated_at = datetime.utcnow().isoformat()
            
            # Fix conversation history
            if not isinstance(snapshot.conversation_history, list):
                logger.warning("conversation_history is not a list, resetting")
                snapshot.conversation_history = []
                repairs.append("Reset conversation_history")
            
            # Fix compaction metadata
            if not isinstance(snapshot.compaction_metadata, dict):
                snapshot.compaction_metadata = asdict(CompactionMetadata())
                repairs.append("Reset compaction_metadata")
            
            # Fix task status
            if not isinstance(snapshot.task_status, dict):
                snapshot.task_status = asdict(TaskStatus())
                repairs.append("Reset task_status")
            
            # Fix invalid state
            try:
                SessionState(snapshot.state)
            except ValueError:
                snapshot.state = SessionState.CORRUPTED.value
                repairs.append(f"Set state to CORRUPTED")
            
            # Clear invalid checksum (will be recomputed)
            snapshot.checksum = None
            
            logger.info(f"Repairs applied: {repairs}")
            
            # Save repaired snapshot
            if self.manager.save_snapshot(snapshot):
                logger.info(f"Saved repaired snapshot: {snapshot.session_id}")
                return snapshot
            else:
                logger.error(f"Failed to save repaired snapshot")
                return None
                
        except Exception as e:
            logger.error(f"Failed to repair snapshot: {e}")
            return None
    
    def recover_from_backup(self, session_id: str) -> Optional[SessionSnapshot]:
        """Recover from backup file if original is corrupted.
        
        Args:
            session_id: Session ID to recover
            
        Returns:
            Recovered SessionSnapshot or None
        """
        # Look for backup file (e.g., .bak, .prev)
        backup_files = [
            self.manager.session_dir / f"{session_id}.bak",
            self.manager.session_dir / f"{session_id}.prev",
        ]
        
        for backup_file in backup_files:
            if not backup_file.exists():
                continue
            
            try:
                with open(backup_file, "r") as f:
                    data = json.load(f)
                
                snapshot = SessionSnapshot.from_dict(data)
                is_valid, issues = self.validate_snapshot(snapshot)
                
                if is_valid or len(issues) < 3:  # Accept minor issues
                    logger.info(f"Recovered from backup: {backup_file}")
                    return snapshot
                    
            except Exception as e:
                logger.warning(f"Failed to recover from backup {backup_file}: {e}")
        
        return None
