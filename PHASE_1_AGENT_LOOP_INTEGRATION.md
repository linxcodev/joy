# Phase 1: Agent Loop Integration - Session Persistence & Compaction

## Status Sebelumnya ✅
- ✅ Token Counter: 283 lines (accurate token tracking)
- ✅ Context Compaction: 406 lines (LLM-based summarization)
- ✅ Context Preservation: 518 lines (10 info categories)
- ✅ Automatic Compaction: Integrated in llm.py (19 tests passing)
- ✅ Session Manager: 564 lines (atomic persistence)
- ✅ Total: 1,960 lines of new code
- ✅ All tests: 387/399 passing (97%)

---

## Objective: Phase 1 Integration

**Goal**: Seamlessly integrate SessionManager dengan agent loop untuk:
1. Auto-save session setelah setiap iteration
2. Resume sessions dengan preserved context
3. Track file changes & errors across sessions
4. Zero impact pada existing agent behavior

**Timeline**: 30 minutes
**Effort**: Low (mostly configuration)

---

## Current Agent Loop State (llm.py)

### StreamWorker Status
```python
# Session fields already added:
self._session_manager: Optional[SessionManager]           # Line 194
self._session_id: Optional[str]                           # Line 195
self._session_enabled: bool                               # Line 196
self._session_start_time: Optional[str]                   # Line 197
self._model: str                                          # Line 198
self._tracked_files: dict                                 # Line 199
self._tool_call_count: int                                # Line 200
```

### Configuration Already Loaded (llm.py lines 84-96)
```python
session_enabled = os.getenv("JOY_SESSION_ENABLED", "true")
session_dir = os.getenv("JOY_SESSION_DIR", os.path.expanduser("~/.joy/sessions"))
```

### What's Missing
- Session initialization in StreamWorker.__init__()
- Auto-save mechanism in agent loop
- File tracking integration
- Session resume on startup

---

## Implementation Tasks

### Task 1: Initialize SessionManager (5 min)

**File**: `src/joy/llm.py`
**Location**: After `load_config()` call, before creating StreamWorker

```python
def init_session_manager(config: dict) -> Optional[SessionManager]:
    """Initialize session manager if enabled."""
    if not config.get("session_enabled"):
        return None
    
    try:
        session_dir = config.get("session_dir", "~/.joy/sessions")
        manager = SessionManager(session_dir=session_dir)
        return manager
    except Exception as e:
        logger.error(f"Failed to initialize session manager: {e}")
        return None
```

**Usage in main()**:
```python
def main(messages: list[dict]) -> dict[str, str]:
    config = load_config()
    
    # Initialize managers
    session_manager = init_session_manager(config)
    
    # Create worker
    worker = StreamWorker(messages)
    
    # Pass session manager
    if session_manager:
        worker.set_session_manager(session_manager, config)
```

---

### Task 2: Add Session Methods to StreamWorker (10 min)

**File**: `src/joy/llm.py`

**Method 1: set_session_manager()**
```python
def set_session_manager(self, manager: SessionManager, config: dict):
    """Set session manager and initialize session."""
    self._session_manager = manager
    self._session_enabled = config.get("session_enabled", True)
    
    # Create new session
    self._session_id = manager.create_session(
        model=self.model,
        user_goal="",  # Will be extracted from first message
        system_prompt=SYSTEM_PROMPT,
        context_window_size=get_model_config(self.model).context_window_size
    )
    self._session_start_time = datetime.utcnow().isoformat()
    
    logger.info(f"Session created: {self._session_id}")
```

**Method 2: _update_tracked_files()**
```python
def _update_tracked_files(self, tool_name: str, args: dict):
    """Track file modifications."""
    path = None
    change_type = None
    
    if tool_name == "read_ide":
        path = args.get("path")
        change_type = "read"
    elif tool_name == "write_ide":
        path = args.get("path")
        change_type = "written"
    elif tool_name == "edit_ide":
        path = args.get("path")
        change_type = "edited"
    
    if path:
        self._tracked_files[path] = change_type
```

**Method 3: _extract_user_goal()**
```python
def _extract_user_goal(self) -> Optional[str]:
    """Extract user goal from first user message."""
    for msg in self.conversation:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content[:200]  # First 200 chars
    return None
```

**Method 4: _save_session_snapshot()**
```python
def _save_session_snapshot(self) -> bool:
    """Save current session state to persistent storage."""
    if not self._session_manager or not self._session_id:
        return False
    
    try:
        user_goal = self._extract_user_goal()
        
        snapshot = SessionSnapshot(
            session_id=self._session_id,
            created_at=self._session_start_time,
            updated_at=datetime.utcnow().isoformat(),
            state=SessionState.ACTIVE.value,
            conversation_history=self.conversation,
            system_prompt=SYSTEM_PROMPT,
            user_goal=user_goal,
            model=self._model,
            context_window_size=get_model_config(self._model).context_window_size,
            turn_count=self.iteration,
            tool_call_count=self._tool_call_count,
            error_count=getattr(self, '_error_count', 0),
            last_error=None,
            modified_files=self._tracked_files,
        )
        return self._session_manager.save_snapshot(snapshot)
    except Exception as e:
        logger.error(f"Failed to save session snapshot: {e}")
        return False
```

---

### Task 3: Modify Agent Loop (_loop method) (10 min)

**File**: `src/joy/llm.py` - StreamWorker._loop()

**After successful turn completion** (around line 350-360):

```python
# After processing all tools and results
# ... existing code ...

# Save session snapshot for recovery
if self.iteration % 5 == 0:  # Save every 5 turns
    self._save_session_snapshot()

# Continue to next iteration
if self.iteration >= MAX_REQUESTS:
    self.events.put(("done", None))
    return
```

**Error handling**:
```python
except Exception as e:
    # ... existing error handling ...
    
    # Track error in session
    if self._session_manager:
        self._error_count = getattr(self, '_error_count', 0) + 1
        self._last_error = str(e)
    
    # ... rest of error handling ...
```

---

### Task 4: File Tracking Integration (5 min)

**File**: `src/joy/llm.py` - run_tool() calls

After successful tool execution:

```python
# In _loop() after tool execution
tool_name = response.tool_calls[i].function.name
tool_args = json.loads(response.tool_calls[i].function.arguments or "{}")

# Track files
self._update_tracked_files(tool_name, tool_args)

# Track tool calls
self._tool_call_count += 1
```

---

## Testing Integration

### Test 1: Session Creation ✅ (Already exists)
```
✅ SessionManager.create_session()
✅ Returns valid session_id
```

### Test 2: Auto-Save Functionality (New)
```python
def test_agent_loop_saves_session():
    """Verify session is saved during agent loop."""
    manager = SessionManager()
    worker = StreamWorker([])
    worker.set_session_manager(manager, {"session_enabled": True})
    
    # Simulate turn
    worker.conversation = [{"role": "user", "content": "test"}]
    worker._save_session_snapshot()
    
    # Verify saved
    loaded = manager.load_snapshot(worker._session_id)
    assert loaded is not None
    assert loaded.turn_count == 0
```

### Test 3: File Tracking (New)
```python
def test_file_tracking():
    """Verify file modifications are tracked."""
    worker = StreamWorker([])
    
    worker._update_tracked_files("read_ide", {"path": "src/joy/llm.py"})
    worker._update_tracked_files("write_ide", {"path": "src/joy/new.py"})
    
    assert worker._tracked_files["src/joy/llm.py"] == "read"
    assert worker._tracked_files["src/joy/new.py"] == "written"
```

### Test 4: Session Resume (New)
```python
def test_resume_session():
    """Verify session can be resumed."""
    manager = SessionManager()
    
    # Create and save
    session_id = manager.create_session(
        model="gpt-4",
        user_goal="Test goal",
        system_prompt="Test",
        context_window_size=8000
    )
    
    snapshot = SessionSnapshot(
        session_id=session_id,
        created_at="...",
        updated_at="...",
        state="active",
        conversation_history=[{"role": "user", "content": "test"}],
        system_prompt="Test",
        turn_count=5,
    )
    manager.save_snapshot(snapshot)
    
    # Resume
    loaded = manager.load_snapshot(session_id)
    assert loaded.turn_count == 5
    assert len(loaded.conversation_history) == 1
```

---

## Configuration

### Environment Variables (Already supported)
```bash
# Enable/disable session persistence
export JOY_SESSION_ENABLED=true              # Default: true

# Custom session directory
export JOY_SESSION_DIR=~/.joy/sessions       # Default: ~/.joy/sessions

# Continue specific session
export JOY_SESSION_ID=joy_abc123def456       # Optional
```

### No Code Changes Required!

---

## Deployment Checklist

- [ ] Add session manager initialization
- [ ] Add StreamWorker.set_session_manager()
- [ ] Add _save_session_snapshot()
- [ ] Add _update_tracked_files()
- [ ] Add _extract_user_goal()
- [ ] Integrate auto-save in _loop()
- [ ] Track tool calls and errors
- [ ] Write integration tests
- [ ] Test session resume
- [ ] Verify no regressions
- [ ] Update CLI with session commands (Phase 2)

---

## Next Phase: Phase 2 CLI Commands

After Phase 1 completion:

```bash
# Resume session
joy resume joy_abc123def456

# List sessions
joy list-sessions

# Show session info
joy session-info joy_abc123def456

# Delete session
joy delete-session joy_abc123def456
```

---

## Success Criteria

✅ Sessions auto-save during agent loop
✅ Session can be resumed with full context
✅ File modifications are tracked
✅ Errors are recorded and retrievable
✅ Zero breaking changes to existing behavior
✅ All tests passing (387+/399)
✅ Configuration via environment variables

---

## Estimated Impact

```
Code Changes:
├─ llm.py: +80 lines (new methods + integration)
├─ Tests: +150 lines (integration tests)
└─ Total: +230 lines

Performance:
├─ Save overhead: ~50ms per 5 turns
├─ Load overhead: ~30ms on startup
└─ No impact on agent loop performance

Backward Compatibility:
├─ 100% backward compatible
├─ Session persistence optional
└─ All existing code paths unchanged
```

---

## Summary

Phase 1 Integration is **straightforward** and **low-risk**:

1. ✅ SessionManager already fully implemented
2. ✅ Session fields already added to StreamWorker
3. ✅ Configuration already loaded
4. ⏳ Just need to connect the pieces in agent loop
5. ⏳ Add tests for new functionality

**Estimated time**: 30 minutes
**Code risk**: Low
**Test coverage**: High

Ready to proceed! 🚀
