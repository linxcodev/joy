# Next Steps: Session Persistence Integration

## Status Saat Ini
- ✅ SessionManager fully implemented (564 lines)
- ✅ SessionSnapshot dengan atomic writes
- ✅ SessionRecovery dengan error handling
- ✅ 25/25 tests passing (100%)
- ⏳ Integration dengan agent loop belum dilakukan

## Phase 1: Agent Loop Integration (30 min)

### Objectives
- Integrate SessionManager ke StreamWorker
- Auto-save session state setelah setiap iteration
- Support session resume pada startup

### Changes Required

#### File: `src/joy/llm.py`

**Imports (top of file):**
```python
from joy.session_manager import SessionManager, SessionSnapshot, SessionState
```

**StreamWorker.__init__() changes:**
```python
def __init__(self, messages: list[dict]):
    # ... existing code ...
    self.session_manager: Optional[SessionManager] = None
    self.session_id: Optional[str] = None
    self.session_start_time = datetime.utcnow().isoformat()
    self.model = ""
    self.tracked_files: dict = {}  # path -> change_type
    self.tool_call_count: int = 0
    self.error_count: int = 0
    self.last_error: Optional[str] = None
```

**Add new method: _save_session_snapshot()**
```python
def _save_session_snapshot(self) -> bool:
    """Save current session state to persistent storage."""
    if not self.session_manager or not self.session_id:
        return False
    
    try:
        snapshot = SessionSnapshot(
            session_id=self.session_id,
            created_at=self.session_start_time,
            updated_at=datetime.utcnow().isoformat(),
            state=SessionState.ACTIVE.value,
            conversation_history=self.conversation,
            system_prompt=SYSTEM_PROMPT,
            model=self.model,
            turn_count=self.iteration,
            tool_call_count=self.tool_call_count,
            error_count=self.error_count,
            last_error=self.last_error,
            modified_files=self.tracked_files,
            user_goal="",  # Extract from conversation if available
        )
        return self.session_manager.save_snapshot(snapshot)
    except Exception as e:
        logger.error(f"Failed to save session snapshot: {e}")
        return False
```

**Modify _run() method:**
- Call `_save_session_snapshot()` setelah setiap successful iteration
- Save sebelum API call juga, untuk resilience

**Track tool calls:**
- Increment `self.tool_call_count` when tools executed
- Update `self.tracked_files` saat read/write/edit operations

#### File: `src/joy/llm.py` - load_config()

**Add to return dict:**
```python
return {
    # ... existing ...
    "session_id": os.getenv("JOY_SESSION_ID"),  # Optional session to resume
    "session_dir": os.getenv("JOY_SESSION_DIR", "~/.joy/sessions"),
}
```

### Integration Checklist
- [ ] Import SessionManager, SessionSnapshot, SessionState
- [ ] Add session fields ke StreamWorker.__init__()
- [ ] Add _save_session_snapshot() method
- [ ] Call save setelah iteration
- [ ] Track modified files
- [ ] Handle session initialization
- [ ] Ensure backward compat (session optional)

---

## Phase 2: CLI Commands (20 min)

### Objectives
- Add session management commands
- Resume previous sessions
- List and manage sessions
- Show session information

### Changes Required

#### File: `src/joy/cli.py`

**Add argument parser group:**
```python
parser.add_argument(
    "session_cmd",
    nargs="?",
    choices=["resume", "list-sessions", "delete-session", "session-info"],
    help="Session management commands"
)

parser.add_argument(
    "session_id",
    nargs="?",
    help="Session ID for resume/delete/info commands"
)

parser.add_argument(
    "--no-session",
    action="store_true",
    help="Disable session persistence for this run"
)

parser.add_argument(
    "--session-dir",
    type=str,
    default="~/.joy/sessions",
    help="Custom session storage directory"
)
```

**Add command handlers:**

```python
def handle_resume_session(session_id: str, session_manager: SessionManager):
    """Resume a previous session."""
    snapshot = session_manager.load_snapshot(session_id)
    if not snapshot:
        print(f"❌ Session not found: {session_id}")
        return
    
    print(f"📋 Resuming session: {session_id}")
    print(f"   Created: {snapshot.created_at}")
    print(f"   Updated: {snapshot.updated_at}")
    print(f"   Turns: {snapshot.turn_count}")
    print(f"   Goal: {snapshot.user_goal or '(not set)'}")
    print(f"   Model: {snapshot.model}")
    
    # Continue with loaded conversation
    return snapshot

def handle_list_sessions(session_manager: SessionManager):
    """List all saved sessions."""
    sessions = session_manager.list_sessions()
    if not sessions:
        print("📭 No sessions found")
        return
    
    print("📋 Saved Sessions:")
    print("-" * 80)
    for s in sessions:
        print(f"  {s['session_id']:<20} | Updated: {s['updated_at']}")
    print("-" * 80)

def handle_delete_session(session_id: str, session_manager: SessionManager):
    """Delete a session."""
    if session_manager.delete_session(session_id):
        print(f"✅ Session deleted: {session_id}")
    else:
        print(f"❌ Failed to delete session: {session_id}")

def handle_session_info(session_id: str, session_manager: SessionManager):
    """Show detailed session information."""
    snapshot = session_manager.load_snapshot(session_id)
    if not snapshot:
        print(f"❌ Session not found: {session_id}")
        return
    
    print(f"📋 Session Info: {session_id}")
    print(f"   State: {snapshot.state}")
    print(f"   Created: {snapshot.created_at}")
    print(f"   Updated: {snapshot.updated_at}")
    print(f"   Turns: {snapshot.turn_count}")
    print(f"   Tools Called: {snapshot.tool_call_count}")
    print(f"   Errors: {snapshot.error_count}")
    print(f"   Model: {snapshot.model}")
    print(f"   Context Window: {snapshot.context_window_size}")
    print(f"   Files Modified: {len(snapshot.modified_files)}")
    
    if snapshot.modified_files:
        print("   Files:")
        for path, change_type in snapshot.modified_files.items():
            print(f"     - {path} ({change_type})")
    
    if snapshot.task_status:
        print(f"   Current Task: {snapshot.task_status.get('current_task', 'N/A')}")
```

### CLI Integration Points

```python
def main():
    args = parser.parse_args()
    
    # Check for session commands
    if args.session_cmd:
        session_manager = SessionManager(args.session_dir)
        
        if args.session_cmd == "resume":
            snapshot = handle_resume_session(args.session_id, session_manager)
            if snapshot:
                # Initialize agent with loaded conversation
                pass
        elif args.session_cmd == "list-sessions":
            handle_list_sessions(session_manager)
            return
        elif args.session_cmd == "delete-session":
            handle_delete_session(args.session_id, session_manager)
            return
        elif args.session_cmd == "session-info":
            handle_session_info(args.session_id, session_manager)
            return
    
    # Normal agent flow
    # ... existing code ...
```

### CLI Commands
```bash
# Resume a session
joy resume joy_abc123def456

# List all sessions
joy list-sessions

# Show session details
joy session-info joy_abc123def456

# Delete a session
joy delete-session joy_abc123def456

# Disable session persistence
joy --no-session

# Custom session directory
joy --session-dir /tmp/joy-sessions
```

### Checklist
- [ ] Add argument parser for session commands
- [ ] Implement resume handler
- [ ] Implement list handler
- [ ] Implement delete handler
- [ ] Implement info handler
- [ ] Add --no-session flag
- [ ] Add --session-dir flag
- [ ] Update main() flow

---

## Phase 3: Monitoring (20 min)

### Objectives
- Track session metrics
- Display usage information
- Monitor storage

### New File: `src/joy/session_metrics.py`

```python
"""Session metrics collection and reporting."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from joy.session_manager import SessionManager

@dataclass
class SessionMetrics:
    """Metrics about session storage and usage."""
    total_sessions: int
    total_size_bytes: int
    active_sessions: int
    average_turns: float
    max_turns: int
    storage_used_percent: float

class SessionMetricsCollector:
    """Collect and report session metrics."""
    
    def __init__(self, session_manager: SessionManager):
        self.session_manager = session_manager
    
    def collect_metrics(self) -> Optional[SessionMetrics]:
        """Collect current session metrics."""
        try:
            sessions = self.session_manager.list_sessions()
            
            if not sessions:
                return SessionMetrics(0, 0, 0, 0.0, 0, 0.0)
            
            total_size = 0
            active_count = 0
            turns_list = []
            
            for session in sessions:
                # Get file size
                session_file = self.session_manager.session_dir / f"{session['session_id']}.json"
                if session_file.exists():
                    total_size += session_file.stat().st_size
                
                # Load to get details
                snapshot = self.session_manager.load_snapshot(session['session_id'])
                if snapshot:
                    if snapshot.state == "active":
                        active_count += 1
                    turns_list.append(snapshot.turn_count)
            
            avg_turns = sum(turns_list) / len(turns_list) if turns_list else 0
            max_turns = max(turns_list) if turns_list else 0
            
            # Calculate storage percentage (assuming 1GB quota)
            quota_bytes = 1024 * 1024 * 1024
            storage_percent = (total_size / quota_bytes) * 100
            
            return SessionMetrics(
                total_sessions=len(sessions),
                total_size_bytes=total_size,
                active_sessions=active_count,
                average_turns=avg_turns,
                max_turns=max_turns,
                storage_used_percent=storage_percent
            )
        except Exception as e:
            logger.error(f"Failed to collect metrics: {e}")
            return None
    
    def format_metrics(self, metrics: SessionMetrics) -> str:
        """Format metrics for display."""
        lines = [
            "📊 Session Metrics:",
            f"   Total Sessions: {metrics.total_sessions}",
            f"   Active: {metrics.active_sessions}",
            f"   Avg Turns: {metrics.average_turns:.1f}",
            f"   Max Turns: {metrics.max_turns}",
            f"   Storage: {metrics.total_size_bytes / 1024:.1f} KB ({metrics.storage_used_percent:.1f}%)",
        ]
        return "\n".join(lines)
```

### Integration dalam CLI

```python
from joy.session_metrics import SessionMetricsCollector

def display_session_metrics(session_manager: SessionManager):
    """Display session metrics in CLI."""
    collector = SessionMetricsCollector(session_manager)
    metrics = collector.collect_metrics()
    if metrics:
        print(collector.format_metrics(metrics))
```

### Checklist
- [ ] Create session_metrics.py
- [ ] Implement SessionMetrics dataclass
- [ ] Implement SessionMetricsCollector
- [ ] Add format_metrics()
- [ ] Integrate dengan CLI
- [ ] Display pada startup (if sessions exist)

---

## Testing Strategy

### Unit Tests (Existing: ✅ 25/25 passing)
- SessionSnapshot creation/serialization
- SessionManager save/load
- SessionRecovery repairs
- Checksum validation

### Integration Tests (NEW)

```python
# tests/test_session_integration.py

def test_agent_loop_saves_session():
    """Verify agent loop saves session state."""
    # Create StreamWorker with session enabled
    # Run for N iterations
    # Verify SessionSnapshot saved with correct data

def test_session_resume_loads_conversation():
    """Verify resumed session loads full conversation history."""
    # Save session after N turns
    # Resume session
    # Verify conversation history matches

def test_session_recovery_on_corruption():
    """Verify corrupted session is recovered."""
    # Save session
    # Corrupt snapshot file
    # Resume session
    # Verify auto-recovery

def test_cli_resume_command():
    """Verify CLI resume command works."""
    # Create and save session
    # Run "joy resume session_id"
    # Verify conversation continues

def test_cli_list_sessions():
    """Verify CLI lists sessions correctly."""
    # Create multiple sessions
    # Run "joy list-sessions"
    # Verify output shows all sessions

def test_no_session_flag_disables_persistence():
    """Verify --no-session disables persistence."""
    # Run with --no-session
    # Verify no snapshot saved
```

### Performance Tests
- Session save latency < 500ms
- Session load latency < 100ms
- No impact on agent loop performance

---

## Success Criteria

- ✅ Agent loop saves session state automatically
- ✅ Sessions can be resumed from CLI
- ✅ All previous context is preserved
- ✅ Backward compatible (no breaking changes)
- ✅ Session commands integrated in CLI
- ✅ Metrics displayed in CLI
- ✅ All tests pass (existing + new)
- ✅ Zero data loss on process termination
- ✅ Graceful fallback if session corrupted

---

## Timeline Estimate

| Phase | Duration | Total |
|-------|----------|-------|
| 1. Agent Loop Integration | 30 min | 30 min |
| 2. CLI Commands | 20 min | 50 min |
| 3. Monitoring | 20 min | 70 min |
| Testing & Validation | 20 min | 90 min |
| **TOTAL** | | **~1.5 hours** |

---

## Dependencies

- ✅ SessionManager (complete)
- ✅ SessionRecovery (complete)
- ✅ Token Counter (complete)
- ✅ Automatic Compaction (complete)
- ⏳ Agent Loop Integration (pending)
- ⏳ CLI Integration (pending)
- ⏳ Metrics Tracking (pending)

---

## Rollout Plan

1. **Phase 1** → Merge to feature branch, test manually
2. **Phase 2** → Add CLI commands, update help text
3. **Phase 3** → Add metrics collection
4. **Full Integration** → Merge to main, deploy to production

---

## Known Limitations & Future Work

### Current Limitations
- Session storage in local filesystem (~/.joy/sessions/)
- No encryption for sensitive data
- Single-machine only (no sync)
- Manual cleanup required

### Future Enhancements
- [ ] Cloud backup integration
- [ ] Encryption for sensitive context
- [ ] Multi-machine sync
- [ ] Automatic retention policy
- [ ] Session export/import
- [ ] Collaborative sessions
- [ ] Web UI for session management

---

**Ready to implement Phase 1: Agent Loop Integration** ✅
