# Change Management Feature - Comprehensive Implementation Guide

## Overview

Joy CLI sekarang memiliki **complete change management system** yang memungkinkan pengguna untuk:
- ✅ Review dan manage perubahan file setelah agent menjalankan task
- ✅ Accept atau reject perubahan dengan konfirmasi safety
- ✅ Deteksi konflik ketika terjadi manual edits
- ✅ Restore file dari snapshot jika diperlukan
- ✅ Batch operations dengan error handling

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLI Main Loop (cli.py)                   │
│  - Handles user input & /diff command interception              │
│  - Initializes diff handler & snapshot manager                  │
│  - Captures initial state before agent work                     │
│  - Detects changes after agent finishes                         │
│  - Displays auto-diff & clears pending                          │
└─────────────────────────────────────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
          ▼                   ▼                   ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │ DiffEngine   │  │DiffCommands  │  │DiffChangeMan-│
    │              │  │Handler       │  │ager          │
    │ - Snapshot   │  │              │  │              │
    │ - Compare    │  │ /diff <path> │  │ - Review     │
    │ - Metadata   │  │              │  │ - Accept/Reject
    └──────────────┘  └──────────────┘  └──────────────┘
          │                                      │
          ▼                                      ▼
    ┌──────────────────────────────────────────────────────┐
    │    DiffSnapshotManager (Snapshot & Restore Logic)    │
    │                                                       │
    │  - create_snapshot(task_id, files)                  │
    │  - detect_manual_changes(task_id, current_files)    │
    │  - can_restore(task_id, file_path)                  │
    │  - restore_file(task_id, file_path, backup=True)    │
    │  - mark_accepted(task_id, file_path)                │
    │  - get_change_summary(task_id)                       │
    │  - cleanup_snapshot(task_id)                         │
    └──────────────────────────────────────────────────────┘
          │
          ▼
    ┌──────────────────────────────────────────────────────┐
    │         DiffReviewCommandHandler                      │
    │  /accept <path>  /reject <path>  /status /conflicts │
    │  /accept-all     /reject-all      /undo             │
    └──────────────────────────────────────────────────────┘
```

## Modules & Components

### 1. **cli.py** (Main Entry Point)
**Tanggung Jawab:**
- Initialize `DiffCommandHandler` & `DiffSnapshotManager`
- Intercept & handle `/diff` commands (non-model commands)
- Capture initial file state sebelum agent bekerja
- Detect changes setelah agent selesai
- Auto-display diffs berdasarkan config
- Clear pending state untuk next turn

**Key Methods:**
```python
# Intercept /diff command (line 205-207)
if diff_handler.is_diff_command(message):
    if diff_handler.handle_diff_command(message):
        continue  # Don't send to model

# Capture initial state (line 214)
diff_handler.capture_initial_state()

# Detect & auto-display changes (line 227-232)
if finished and state["used_tools"]:
    changes = diff_handler.detect_and_track_changes()
    if changes and diff_handler.should_auto_display_diff(changes):
        diff_handler.display_auto_diff(changes)

# Clear pending (line 252-253)
if finished:
    diff_handler.clear_pending()
```

### 2. **diff_engine.py** (File Change Detection)
**Tanggung Jawab:**
- Capture snapshot state di suatu waktu
- Compare dua snapshots untuk mendeteksi perubahan
- Generate structured metadata untuk setiap file
- Handle edge cases (binary, large, unreadable files)

**Key Methods:**
```python
# Capture current state
snapshot = engine.capture_snapshot(file_paths=None)

# Compare before/after
changes = engine.compare_snapshots(snapshot_before, snapshot_after)
# Returns: Dict[path] -> FileDiffMetadata

# Get summary for display
summary = engine.summary(changes)
```

**Data Structures:**
```python
@dataclass
class FileDiffMetadata:
    path: str
    change_type: FileChangeType  # CREATED, MODIFIED, DELETED, UNCHANGED
    file_status: FileStatus      # READABLE, BINARY, NOT_FOUND, etc.
    lines_added: int
    lines_removed: int
    file_hash_before: Optional[str]  # SHA256
    file_hash_after: Optional[str]
    unified_diff: str
    diff_lines: List[Dict]
```

### 3. **diff_snapshot_manager.py** (Snapshot & Restore)
**Tanggung Jawab:**
- Simpan file content sebelum agent bekerja
- Detect perubahan manual yang terjadi setelah snapshot
- Restore file ke state snapshot (undo changes)
- Handle konflik saat restore
- Safely handle file creation/deletion

**Key Methods:**
```python
# Create snapshot sebelum agent bekerja
snapshot_manager.create_snapshot(task_id, {
    "src/main.py": "original content",
    "src/utils.py": None,  # Binary/unreadable
})

# Check if file dapat di-restore (detect conflicts)
can_restore, conflict_reason = snapshot_manager.can_restore(task_id, "src/main.py")

# Restore file ke snapshot state
success, error = snapshot_manager.restore_file(
    task_id, "src/main.py", create_backup=True
)

# Deteksi manual changes
manual_changes = snapshot_manager.detect_manual_changes(task_id, {
    "src/main.py": "current content after manual edits"
})

# Mark as accepted
snapshot_manager.mark_accepted(task_id, "src/main.py")

# Get summary
summary = snapshot_manager.get_change_summary(task_id)
# Returns: {
#   'total': 10,
#   'accepted': 5,
#   'rejected': 2,
#   'conflicts': 1,
#   'records': [...]
# }
```

**Data Structures:**
```python
@dataclass
class SnapshotFileRecord:
    path: str
    exists: bool
    content: Optional[str]           # None if binary
    hash: Optional[str]              # SHA256
    mtime: Optional[float]
    size: int
    file_status: str                 # FileStatus enum value

@dataclass
class ChangeManagementRecord:
    file_path: str
    change_type: str                 # FileChangeType
    original_status: str             # FileStatus
    current_status: str
    snapshot_hash: Optional[str]
    current_hash: Optional[str]
    action: str                      # "pending", "accepted", "rejected", "conflict"
    conflict_info: Optional[str]
    reason: Optional[str]
    timestamp: Optional[str]
```

### 4. **diff_change_manager.py** (Accept/Reject Logic)
**Tanggung Jawab:**
- Track review status per file (PENDING, ACCEPTED, REJECTED, CONFLICT)
- Accept perubahan (mark as approved)
- Reject perubahan (restore ke snapshot)
- Detect manual edits setelah snapshot
- Batch operations (accept-all, reject-all)
- Undo operations

**Key Methods:**
```python
# Initialize review untuk task baru
change_manager.initialize_review(task_id, {
    "src/main.py": FileDiffMetadata(...),
})

# Accept single file
success, error = change_manager.accept_file(task_id, "src/main.py")

# Accept all pending
accepted_count, errors = change_manager.accept_all(task_id)

# Reject single file (restore)
success, error = change_manager.reject_file(task_id, "src/main.py")

# Reject all pending
rejected_count, errors = change_manager.reject_all(task_id)

# Detect conflicts (manual changes)
conflicts = change_manager.detect_conflicts(task_id, {
    "src/main.py": "current content"
})

# Get summary
summary = change_manager.get_summary(task_id)
# Returns: {
#   'total': 10,
#   'pending': 3,
#   'accepted': 5,
#   'rejected': 2,
#   'conflicts': 0
# }

# Display visual summary
change_manager.display_review_summary(task_id)
```

**Review Status Enum:**
```python
class ReviewStatus(Enum):
    PENDING = "pending"        # Awaiting user review
    ACCEPTED = "accepted"      # User approved
    REJECTED = "rejected"      # User rejected, restored
    CONFLICT = "conflict"      # Manual changes detected
```

### 5. **diff_review_commands.py** (User Commands)
**Tanggung Jawab:**
- Parse & execute review commands
- Provide user confirmations untuk destructive operations
- Display status & conflict information
- Format output dengan rich colors

**Supported Commands:**
```
/accept <path>       - Accept changes untuk file tertentu
/accept-all          - Accept semua pending changes
/reject <path>       - Reject changes (restore file)
/reject-all          - Reject semua pending changes (DESTRUCTIVE)
/status              - Show review status summary
/conflicts           - List files dengan konflik
/undo                - Undo last action
```

**Key Methods:**
```python
# Check if message adalah review command
if review_handler.is_review_command(message):
    handled, output = review_handler.handle_command(message)

# Parse command
command, argument = review_handler.parse_command("/accept src/main.py")
# Returns: ("/accept", "src/main.py")

# Set current task
review_handler.set_current_task("task_123")
```

### 6. **diff_commands.py** (Diff Display Commands)
**Tanggung Jawab:**
- Handle `/diff` command dari user
- Show diff untuk single file atau all files
- Auto-display after agent work
- Manage pending changes state

**Key Methods:**
```python
# Check if message adalah /diff command
if diff_handler.is_diff_command(message):
    diff_handler.handle_diff_command(message)

# Capture initial state before agent
diff_handler.capture_initial_state()

# Detect changes after agent finishes
changes = diff_handler.detect_and_track_changes()

# Check if should auto-display (based on config)
if diff_handler.should_auto_display_diff(changes):
    diff_handler.display_auto_diff(changes)

# Clear pending state untuk next turn
diff_handler.clear_pending()
```

## User Workflow Examples

### Example 1: Basic Accept/Reject Workflow
```bash
$ joy
› Edit main.py to use async/await

[Agent edits main.py]

┌─ Changes Summary ─────────────────────────────────┐
│ 1 file changed: main.py (+15 lines, -8 lines)     │
└───────────────────────────────────────────────────┘

[Full diff displayed]

› /status
┌─ Review Summary ──────────────────────────────────┐
│ Total: 1 · Pending: 1 · Accepted: 0 · Rejected: 0 │
└───────────────────────────────────────────────────┘

File          Status          Changes
main.py       ⦿ Pending       +15 −8 lines

› /accept main.py
✓ Accepted: main.py

› /status
┌─ Review Summary ──────────────────────────────────┐
│ Total: 1 · Pending: 0 · Accepted: 1 · Rejected: 0 │
└───────────────────────────────────────────────────┘
```

### Example 2: Conflict Detection
```bash
› /reject main.py
Reject changes to main.py? File will be restored. [y/N]: y

⚠ Error: File has manual changes since snapshot (conflict)
  Cannot reject due to conflict

› /status
Status: CONFLICT - Manual changes detected after snapshot

# User must manually review the file or accept the conflict
```

### Example 3: Batch Operations
```bash
› /accept-all
Accept all 5 pending changes? [y/N]: y

✓ Accepted 5 files

# Or reject all
› /reject-all
Reject ALL 5 pending changes? This will restore all files to their original state. [y/N]: y

✗ Rejected 5 files
```

## Configuration

### Environment Variables

```bash
# Enable/disable auto display after agent work
export JOY_DIFF_AUTO=true

# Show summary before full diff
export JOY_DIFF_SUMMARY=true

# Max lines in diff view (0 = unlimited)
export JOY_DIFF_MAX_LINES=100

# Max files in file list
export JOY_DIFF_MAX_FILES=20

# Only trigger diff for files matching these patterns
export JOY_DIFF_WATCH="*.py,src/**/*.ts"

# Don't trigger diff for files matching these patterns
export JOY_DIFF_IGNORE="__pycache__,*.pyc,node_modules"

# Output format: "full" (dengan diff content) atau "summary"
export JOY_DIFF_FORMAT=full

# Highlight syntax dalam diff
export JOY_DIFF_SYNTAX=true
```

### Programmatic Configuration

```python
from joy.diff_config import DiffConfig

config = DiffConfig(
    auto_display=True,
    show_summary=True,
    max_diff_lines=100,
    max_file_list=20,
    watch_patterns=["*.py", "src/**/*.ts"],
    ignore_patterns=["__pycache__", "*.pyc"],
    format="full",
    syntax_highlight=True,
)

# Load from environment
config = DiffConfig.from_env()
```

## Safety Features

### 1. **Snapshot Before Agent Work**
```python
# cli.py line 214
diff_handler.capture_initial_state()
# Before agent runs, captures all file states
```

### 2. **Conflict Detection**
```python
# snapshot_manager.py
can_restore, conflict = snapshot_manager.can_restore(task_id, file_path)

if conflict:
    # File has manual changes since snapshot
    # Prevent silent overwrites
```

### 3. **Backup Before Restore**
```python
# snapshot_manager.py line 256
success, error = snapshot_manager.restore_file(
    task_id, file_path, create_backup=True
)
# Creates <filename>.backup before restoring
```

### 4. **Confirmation Before Destructive Ops**
```python
# diff_review_commands.py line 179-180
if not Confirm.ask(
    f"Reject changes to {file_path}? File will be restored.",
    console=self.console
):
    return True, "Cancelled"
```

### 5. **No Git Reset Used**
- Only direct file I/O (no `git checkout`, `git reset --hard`)
- Preserves actual Git history
- Safe even in git repositories

## Key Design Decisions

### 1. **In-Memory Snapshots**
- Fast access without disk I/O
- Optional persistence untuk recovery
- Can be cleared after task completes

### 2. **One Snapshot Per Task**
- Captures state before agent work
- Prevents false conflicts from manual edits before agent
- Clean state management per turn

### 3. **File-Level Accept/Reject**
- Not yet supporting hunks/lines (PRs welcome!)
- Safer for now - clear semantics
- Can be extended later with advanced UI

### 4. **Manual Change Detection via Hash**
- Uses SHA256 hash comparison
- Fast check without file content comparison
- Detects changes reliably

### 5. **Session Persistence**
- Conversation history maintained
- Snapshots cleared after each task
- No cross-task state pollution

## Testing & Validation

### Unit Tests (in tests/test_diff_integration.py)
```python
# Test snapshot creation & detection
test_snapshot_creation_and_detection()

# Test conflict detection
test_detect_manual_changes()

# Test file restoration
test_restore_file_without_conflict()

# Test backup creation
test_restore_creates_backup()

# Test accept/reject status tracking
test_accept_file_updates_review_status()

# Test batch operations
test_accept_all_with_mixed_status()

# Test conflict state
test_conflict_prevents_restore()

# Test no false positives
test_no_false_trigger_on_read_only()

# And 18+ more comprehensive tests...
```

### Running Tests
```bash
pytest tests/test_diff_integration.py -v
pytest tests/test_diff_integration.py::test_snapshot_creation_and_detection -v
```

## Integration Points

### With CLI Main Loop
```python
# cli.py main()
diff_config = DiffConfig.from_env()
diff_handler = DiffCommandHandler(".", console, diff_config)

# Before each agent turn
diff_handler.capture_initial_state()

# After agent finishes
if finished and state["used_tools"]:
    changes = diff_handler.detect_and_track_changes()
    if changes and diff_handler.should_auto_display_diff(changes):
        diff_handler.display_auto_diff(changes)

# Cleanup
if finished:
    diff_handler.clear_pending()
```

### With LLM Streaming
- Does **not** interrupt streaming
- Does **not** interfere with tool execution
- Only activates after agent turn is complete

### With Tool Events
- Tracks `state["used_tools"]` to detect work happened
- Only triggers auto-diff if tools were used
- No false positives on idle turns

## Extending the System

### Add Partial Accept/Reject (Hunks)
1. Extend `FileDiffMetadata` with hunk information
2. Add hunk-level ReviewStatus tracking
3. Implement hunk selection UI
4. Extend `restore_file()` to apply partial restoration

### Add Git Integration (Optional)
1. Track changes relative to git HEAD
2. Offer `git add` for accepted changes
3. Offer `git discard` for rejected changes
4. Show git diff instead of file comparison

### Add Change Notifications
1. Log all accept/reject actions
2. Generate audit trail
3. Notify external systems on changes
4. Webhooks for integrations

### Add Selective Restore (Lines)
1. Parse unified diff into hunks
2. Allow user to select hunks
3. Apply surgical restoration
4. Handle conflicts in hunk selection

## Troubleshooting

### "File has manual changes since snapshot"
- User edited file after snapshot was captured
- Options:
  - Accept the changes (keep both agent + manual)
  - View the conflict with `/diff <path>`
  - Manually edit to merge
  - Reject manually, then run agent again

### "Cannot restore binary/non-readable file"
- Snapshot didn't capture file content (binary/permission)
- Options:
  - Manually restore from backup
  - Accept the changes
  - Keep the agent-modified version

### "/diff shows nothing"
- No changes detected since snapshot
- Possible causes:
  - Agent didn't modify any files
  - All changes reverted during task
  - Watch/ignore patterns filtered them out
- Check: `/status` to see if there are pending files

## Performance Considerations

### Snapshot Creation
- O(n) where n = number of files to snapshot
- Reads file content once
- Computes SHA256 hash
- ~10-50ms for typical projects

### Change Detection
- O(n) to capture current state
- O(n) to compare (hash check)
- ~10-50ms for typical projects

### Memory Usage
- Snapshot stores content in-memory
- ~1MB per typical project
- Cleared after task completes
- Can be disabled with config

### Large Files
- Skipped if > configured size
- Hash still computed
- Marked as TOO_LARGE in metadata

## Security Considerations

### No Git Exposure
- Uses direct file I/O only
- No git credential exposure
- Safe in CI/CD environments

### Backup Safety
- Creates `.backup` files before restore
- Backups kept until explicitly cleaned
- Can be recovered if restore goes wrong

### Confirmation Prompts
- User confirms all destructive operations
- Particularly for /reject-all
- Prevents accidental data loss

## Future Enhancements

1. **Hunk-level Review** - Accept/reject individual code hunks
2. **Change Annotations** - Add comments to specific changes
3. **Change History** - View what was accepted/rejected in past turns
4. **Undo/Redo** - Full undo stack for operations
5. **Git Integration** - Optional git staging & commits
6. **Change Notifications** - Webhooks, logging, external integrations
7. **Merge Conflict Resolution** - UI for resolving conflicts
8. **Change Statistics** - Analytics on accept/reject patterns

## Summary

The Change Management Feature provides:
- ✅ **Safe** - Snapshots before agent work, conflict detection, backups
- ✅ **Simple** - File-level accept/reject operations
- ✅ **Integrated** - Works seamlessly with CLI main loop
- ✅ **Configurable** - Environment variables for behavior
- ✅ **Non-intrusive** - Doesn't interrupt agent work or streaming
- ✅ **Testable** - 26+ comprehensive test cases
- ✅ **Extensible** - Clean architecture for future features

Perfect for production use in Joy CI/CD and interactive scenarios! 🚀
