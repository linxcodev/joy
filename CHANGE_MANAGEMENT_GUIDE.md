# Joy Diff Review - Change Management Feature Guide

## Overview

The Change Management feature extends Joy's Diff Review system with full lifecycle management for code changes made by the agent:

- ✅ **Accept** changes with snapshot-backed safety
- ❌ **Reject** changes with automatic restoration to original state
- 🔍 **Detect conflicts** when manual edits overlap with agent changes
- 📊 **Track status** per file (pending, accepted, rejected, conflict)
- 🔄 **Batch operations** for accepting/rejecting multiple files
- 🛡️ **Backup creation** before restoration
- 📈 **Comprehensive reporting** of all actions

## Architecture

### Components

```
diff_snapshot_manager.py    → Handles file snapshots and restoration
    ├─ SnapshotFileRecord   → Single file in snapshot
    ├─ DiffSnapshot        → Collection of snapshots (deprecated in v2)
    └─ DiffSnapshotManager → Main snapshot manager

diff_change_manager.py      → Manages review state and operations
    ├─ ReviewStatus        → Enum: pending, accepted, rejected, conflict
    ├─ FileReviewState     → Per-file review state
    └─ DiffChangeManager   → Coordinates changes

diff_review_commands.py     → CLI command handlers
    ├─ DiffReviewCommandHandler → Parse /accept, /reject, etc.
    └─ ReviewCommandHelper      → Help display utilities
```

### Data Flow

```
User Types /accept or /reject
        ↓
DiffReviewCommandHandler parses command
        ↓
DiffChangeManager updates state
        ↓
DiffSnapshotManager performs actual file operations
        ↓
FileReviewState reflects new status
        ↓
Display confirmation/error to user
```

## Core Concepts

### 1. Snapshots

A snapshot captures the state of all tracked files **before** the agent begins working:

```python
# Snapshot structure
{
    "file.py": SnapshotFileRecord(
        path="file.py",
        exists=True,
        content="original code",
        hash="abc123...",      # SHA256
        mtime=1699564200.0,    # Modification time
        size=1024,
        file_status="readable"
    )
}
```

**Key properties:**
- **Immutable** after creation
- **Comprehensive** - includes content, hash, metadata
- **Safe** - used only for comparison and restoration
- **In-memory** by default, optionally persisted to disk

### 2. Review Status

Each file has one of four review statuses:

| Status | Meaning | Action |
|--------|---------|--------|
| `PENDING` | Awaiting user decision | User must accept or reject |
| `ACCEPTED` | User approved changes | File remains as-is |
| `REJECTED` | User declined changes | File restored to snapshot |
| `CONFLICT` | Manual changes detected | User must review manually |

### 3. Conflict Detection

Conflicts occur when:

- **Manual edits** to a file happen after snapshot is taken
- **Agent changes** overlap with those manual edits
- **Restore attempt** when file has changed since snapshot

Example:
```
[Initial]
file.py: "original code"

[Snapshot taken]
snapshot["file.py"]: "original code"

[Agent modifies]
file.py: "original code\n# agent added this"

[User manually edits]
file.py: "original code\n# user added this"

[User tries to reject]
→ CONFLICT: File differs from snapshot, cannot restore safely
```

## Usage

### Basic Workflow

```bash
$ joy

› Edit my config.py and refactor main()
[Agent works...]
[Auto-display shows changes]

# Review the changes (optional prompt)
› /status
Review Summary
  Total: 2 files
  Pending: 2
  Accepted: 0
  Rejected: 0

# Accept specific file
› /accept src/main.py
✓ Accepted: src/main.py

# Reject other file (will restore)
› /reject config.py
Reject changes to config.py? (y/n) y
✗ Rejected: config.py

# Accept all remaining
› /accept-all
Accept all 1 pending changes? (y/n) y
✓ Accepted 1 files
```

### Commands Reference

#### `/accept <path>`
Accept changes to a specific file.

```bash
› /accept src/main.py
✓ Accepted: src/main.py
```

#### `/accept-all`
Accept all pending changes.

```bash
› /accept-all
Accept all 3 pending changes? (y/n) y
✓ Accepted 3 files
```

#### `/reject <path>`
Reject changes and restore file to original state.

```bash
› /reject config.py
Reject changes to config.py? File will be restored. (y/n) y
✗ Rejected: config.py
```

**Important:** Creates automatic backup at `config.py.backup`

#### `/reject-all`
Reject all pending changes (DESTRUCTIVE).

```bash
› /reject-all
Reject ALL 3 pending changes? This will restore all files. (y/n) y
✗ Rejected 3 files
```

#### `/status`
Show detailed review summary.

```bash
› /status
Review Summary
  Total: 3
  Pending: 1
  Accepted: 1
  Rejected: 1
  Conflicts: 0

[File list with status indicators]
```

#### `/conflicts`
Show files with detected conflicts.

```bash
› /conflicts
⚠ 1 file(s) with conflicts detected

  service.py - Manual changes detected after snapshot
```

#### `/undo`
Undo the last accept/reject action (if supported).

```bash
› /undo
↶ Undone last action
```

### Advanced Scenarios

#### Handling Conflicts

When a conflict is detected, the file is marked as `CONFLICT` and must be reviewed manually:

```bash
› /reject service.py
Error: Cannot reject due to conflict:
  File has manual changes since snapshot (conflict)

# User must manually resolve
$ cat service.py.backup  # View rejected changes
# Edit service.py manually to merge desired changes
```

#### Batch Acceptance

Accept all changes with one command:

```bash
› /accept-all
Accept all 5 pending changes? (y/n) y
✓ Accepted 5 files
```

#### Partial Rejection

Reject only specific files, keep others:

```bash
› /reject bad_change.py
✗ Rejected: bad_change.py

› /accept-all
Accept all 4 pending changes? (y/n) y
✓ Accepted 4 files
```

## Safety Features

### 1. Automatic Backups

When rejecting changes, a backup is created:

```
file.py       → Restored to original
file.py.backup → Contains current version (rejected)
```

This allows recovery if restore wasn't desired:
```bash
$ mv file.py.backup file.py
```

### 2. Confirmation Prompts

Destructive operations require confirmation:

```bash
› /reject-all
Reject ALL 3 pending changes? This will restore all files. (y/n) n
Cancelled
```

### 3. Conflict Detection

Manual edits are detected automatically:

```bash
# File snapshot: "original code"
# Agent modified: "original code\n# agent change"
# User edited:    "original code\n# user change"

› /reject agent_file.py
Error: Cannot reject due to conflict:
  File has manual changes since snapshot (conflict)
```

### 4. No Git Integration

**Important:** Change management does NOT use git:
- ✅ No `git reset --hard`
- ✅ No `git checkout`
- ✅ No `.git` directory access required

This makes it safe to use in non-git projects and prevents accidental git state corruption.

### 5. File Safety

- Binary files are never modified
- Permission errors are handled gracefully
- Large files (>10MB) are skipped
- Encoding errors don't crash the system

## Implementation Details

### Snapshot Mechanism

```python
# Creating a snapshot
snapshot_manager = DiffSnapshotManager(workspace_root)

files = {
    "main.py": "def main(): pass",
    "config.py": "CONFIG = {}",
}

snapshot_manager.create_snapshot("task_001", files)
```

### Change Detection

```python
# Detecting changes after agent work
current_files = {
    "main.py": "def main(): print('modified')",
    "config.py": "CONFIG = {}",  # Unchanged
}

manual_changes = snapshot_manager.detect_manual_changes("task_001", current_files)
# Returns: {"main.py": ("hash_before", "hash_after")}
```

### Restoration

```python
# Restoring a file
success, error = snapshot_manager.restore_file("task_001", "main.py", create_backup=True)

if success:
    # File restored to snapshot state
    # Backup created at main.py.backup
else:
    # Error with details
    print(error)
```

## Configuration

No special configuration needed - Change Management works out of the box.

Optional environment variables for advanced usage:

```bash
# Backup directory for restored files (default: same directory)
JOY_BACKUP_DIR="/tmp/joy_backups"

# Auto-cleanup snapshots after N hours (default: keep forever)
JOY_SNAPSHOT_CLEANUP_HOURS=24
```

## Limitations

### Current Version (v1)

✅ **Supported:**
- Accept/reject at file level
- Conflict detection
- Backup creation
- Batch operations
- Summary reporting

❌ **Not yet supported:**
- Accept/reject at hunk level (partial changes)
- Undo operations
- Snapshot persistence to disk
- Multiple active snapshots per task
- Git-aware merging

### Partial Changes

For rejecting only part of an agent's changes, currently:

1. **Reject the entire file** to restore original
2. **Manually edit** to bring back desired changes
3. **Accept** when done

This is safe but manual. Hunk-level control may be added in future versions.

## Testing

Comprehensive test suite covers:

```
✓ Snapshot creation and metadata
✓ File restoration scenarios
✓ Manual change detection
✓ Conflict detection and handling
✓ Accept/reject operations
✓ Batch operations
✓ File creation/deletion
✓ Permission errors
✓ Command parsing
✓ Edge cases
```

Run tests:
```bash
pytest tests/test_diff_change_management.py -v
```

## Troubleshooting

### "File has manual changes since snapshot"

**Cause:** File was edited after snapshot, blocking restoration

**Solution:**
1. Check `file.py.backup` for what would be restored
2. Manually merge changes if needed
3. Or accept the conflict and continue

### "Reject failed: Permission denied"

**Cause:** No write permission to file or directory

**Solution:**
1. Check file/directory permissions
2. Run Joy with appropriate privileges
3. Or move file to writable location

### "Snapshot not found"

**Cause:** Task ID mismatch or cleanup already done

**Solution:**
1. Create new snapshot for the task
2. Don't reuse old task IDs
3. Check task ID spelling

## Future Enhancements

Planned features:

- [ ] Hunk-level accept/reject
- [ ] Git integration (optional)
- [ ] Snapshot persistence and recovery
- [ ] Interactive diff editor
- [ ] Change squashing/reordering
- [ ] Automatic conflict resolution suggestions
- [ ] Snapshot expiration policies

## Related Documentation

- [Diff Review Guide](DIFF_INTEGRATION.md) - Core diff viewing
- [CLI Guide](README.md) - Main usage documentation
- [Architecture](ARCHITECTURE.md) - Overall system design
