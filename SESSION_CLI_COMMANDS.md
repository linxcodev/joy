# Session CLI Commands - Joy Agent

Dokumentasi lengkap untuk session management commands di Joy.

## Overview

Joy sekarang menyimpan session state secara otomatis dan memungkinkan Anda melanjutkan pekerjaan kapan saja.

**Session Storage**: `~/.joy/sessions/`

---

## Commands

### 1. Resume Session
Resume pekerjaan dari session yang sudah ada.

```bash
# Resume most recent session
joy resume

# Resume specific session
joy resume joy_abc123def456

# Resume with different model
joy resume joy_abc123def456 --model gpt-4-turbo
```

**Output:**
```
Resumed Session: joy_abc123def456
  Model: gpt-4
  Turns: 15
  Goal: Implement context compaction feature
  📦 Compacted (1x)

Continuing from saved session state...
```

---

### 2. List Sessions
Tampilkan semua saved sessions.

```bash
# List most recent 10 sessions (default)
joy list-sessions

# Show all sessions
joy list-sessions --all

# Show 20 most recent
joy list-sessions --limit 20
```

**Output:**
```
Saved Sessions (3/3)

  joy_abc123def456
    Updated: 2024-01-15T10:30:00
    Turns: 15 | State: active 📦 compacted
    Task: Implement context preservation

  joy_xyz789uvw012
    Updated: 2024-01-14T15:45:00
    Turns: 8 | State: completed
    Task: Test token counting

  joy_old123old456
    Updated: 2024-01-10T12:00:00
    Turns: 3 | State: active
    Task: (no current task)
```

---

### 3. Session Status
Tampilkan detailed information tentang session.

```bash
# Show status of current/most recent session
joy session-status

# Show status of specific session
joy session-status joy_abc123def456
```

**Output:**
```
Session Status: joy_abc123def456

State:        active
Model:        gpt-4
Created:      2024-01-15T10:00:00
Updated:      2024-01-15T10:30:00

Progress:
  Turns:              15
  Iterations:         15
  Tool Calls:         12

Goal & Task:
  User Goal:          Implement context preservation
  Current Task:       Design Context Preservation Strategy
  Next Steps:         Write extraction logic, Add tests, Integration

Compaction:
  Is Compacted:       True
  Compaction Count:   1
  Last Compaction:    2024-01-15T10:28:00
  Tokens Saved:       25000

Modified Files:
  src/joy/context_preservation.py: written
  tests/test_context_preservation.py: edited
  docs/design.md: edited
  ... and 2 more
```

---

### 4. Delete Session
Hapus session yang sudah ada.

```bash
# Delete with confirmation
joy delete-session joy_abc123def456

# Delete without confirmation
joy delete-session joy_abc123def456 --force
```

**Output (with confirmation):**
```
Delete session: joy_abc123def456?
  Model:     gpt-4
  Turns:     15
  Goal:      Implement context preservation

Type 'yes' to confirm deletion: yes
✓ Deleted session: joy_abc123def456
```

**Output (--force):**
```
✓ Deleted session: joy_abc123def456
```

---

### 5. Export Session
Ekspor session ke JSON file untuk backup atau sharing.

```bash
# Export to default location (session_id.json)
joy export-session joy_abc123def456

# Export to custom path
joy export-session joy_abc123def456 --output backup.json
```

**Output:**
```
✓ Exported session to: joy_abc123def456.json
  Size: 245.3 KB
```

**Exported JSON struktur:**
```json
{
  "session_id": "joy_abc123def456",
  "created_at": "2024-01-15T10:00:00",
  "updated_at": "2024-01-15T10:30:00",
  "state": "active",
  "model": "gpt-4",
  "turn_count": 15,
  "user_goal": "Implement context preservation",
  "conversation_history": [
    {
      "role": "user",
      "content": "..."
    },
    {
      "role": "assistant",
      "content": "..."
    }
  ],
  "task_status": {
    "current_task": "Design extraction logic",
    "next_steps": ["Write tests", "Integrate with compaction"]
  },
  "modified_files": {
    "src/joy/context_preservation.py": "written",
    "tests/test_context_preservation.py": "edited"
  },
  "compaction_metadata": {
    "compaction_count": 1,
    "is_compacted": true,
    "total_tokens_saved": 25000
  }
}
```

---

### 6. Recover Session
Recover corrupted atau damaged session.

```bash
# Recover with non-strict mode (lenient repair)
joy recover-session joy_abc123def456

# Recover with strict mode (only valid repairs)
joy recover-session joy_abc123def456 --strict
```

**Output (success):**
```
Attempting to recover session: joy_abc123def456

✓ Successfully recovered session!
  State: active
  Turns: 15
  Model: gpt-4
```

**Output (failure):**
```
Attempting to recover session: joy_abc123def456

✗ Failed to recover session (strict mode: False)
  Try: joy recover-session joy_abc123def456
```

---

## Session State Diagram

```
┌─────────────────────────────────────────┐
│         Start New Session               │
│                                         │
│  joy                                    │
└────────────────┬────────────────────────┘
                 │
                 ▼
        ┌─────────────────┐
        │   Interactive   │
        │   Mode (REPL)   │
        └────────┬────────┘
                 │
        ┌────────┴────────┐
        ▼                 ▼
   Each Turn        Auto-Save
   Completed        Session
   ────────────────────►
        │
        ├─────────────────────────┐
        │                         │
    User Input              Context Grows
        │                         │
        ▼                         ▼
   /exit?              Approaching
        │              Limit?
        │                │
        │                ├─ Yes ──► Compact ──┐
        │                │                    │
        │                └─ No ──────────────┘
        │                         │
        │                         ▼
        │                   Save Snapshot
        │                   (Auto-save)
        │                         │
        ▼                         ▼
   Exit         ┌─────────────────┐
        │       │  Next Session   │
        │       │  Resume Old     │
        │       └────────┬────────┘
        │                │
        └────────┬───────┘
                 │
                 ▼
        ┌──────────────────┐
        │  Session Storage │
        │  (~/.joy/sessions) │
        └──────────────────┘
```

---

## Workflow Examples

### Example 1: Long-Running Session dengan Interruption

```bash
# Start session
$ joy
Joy — coding agent

Session: joy_abc123def456

> Implement feature X

# ... agent works ...
# ... compaction happens at 75% ...
# ... user hits Ctrl-D to exit ...

$ # Later in another terminal/machine

$ joy resume
[dim]Resuming most recent session: joy_abc123def456[/dim]

Resumed Session: joy_abc123def456
  Model: gpt-4
  Turns: 25
  Goal: Implement feature X
  📦 Compacted (2x)

Continuing from saved session state...

> Continue implementing feature Y

# ... session continues ...
```

---

### Example 2: Session Recovery from Corruption

```bash
# Session data corrupted (hardware issue, file deletion)
$ joy resume joy_abc123def456
[red]Failed to load session: joy_abc123def456[/red]

$ # Attempt recovery
$ joy recover-session joy_abc123def456
Attempting to recover session: joy_abc123def456

✓ Successfully recovered session!
  State: active
  Turns: 15
  Model: gpt-4

# Data recovered, can continue
```

---

### Example 3: Backup & Archive Sessions

```bash
# Export all sessions for backup
$ for session in $(joy list-sessions --all | grep -o 'joy_[^ ]*'); do
    joy export-session "$session" --output "backup/$session.json"
  done

Exported joy_abc123def456.json (245 KB)
Exported joy_xyz789uvw012.json (128 KB)
Exported joy_old123old456.json (512 KB)

# Can later re-import or analyze
```

---

## Advanced: Session Metadata

### Session ID Format
```
joy_<16-char-random-id>
# Example: joy_a7c3f9e2b4d1k6m9
```

### Session File Location
```bash
# Session data file
~/.joy/sessions/joy_abc123def456.json

# Session index (for fast listing)
~/.joy/sessions/index.json

# Temp file during save (atomic operations)
~/.joy/sessions/joy_abc123def456.tmp
```

### Session States
- `active`: Pekerjaan sedang berlangsung
- `paused`: Session disimpan, siap dilanjutkan
- `completed`: Pekerjaan selesai
- `failed`: Error yang tidak bisa diperbaiki
- `corrupted`: Data corrupted, perlu recovery

---

## Configuration

### Environment Variables
```bash
# Disable auto-save
export JOY_SESSION_AUTO_SAVE=false

# Custom session storage directory
export JOY_SESSIONS_DIR=/custom/path

# Auto-cleanup old sessions (days)
export JOY_SESSION_RETENTION_DAYS=30

# Max storage size (MB)
export JOY_SESSION_MAX_STORAGE_MB=1000
```

---

## Troubleshooting

### Q: Bagaimana jika session file terhapus?
**A:** Gunakan `joy recover-session` untuk repair atau buat session baru.

### Q: Bisakah saya share session dengan orang lain?
**A:** Ya, gunakan `joy export-session` dan bagikan JSON file. Orang lain bisa import ulang.

### Q: Storage semakin membesar, bagaimana?
**A:** Gunakan `joy delete-session [id]` untuk hapus old sessions. Auto-cleanup juga bisa dikonfigurasi.

### Q: Session tidak tersimpan, kenapa?
**A:** Pastikan:
1. `~/.joy/` directory writable
2. Disk space cukup
3. Tidak ada file permission issues

### Q: Ingin reset session?
**A:** Gunakan `joy delete-session [id]` dan buat baru.

---

## Integration dengan Agent Loop

Session auto-save terjadi setelah setiap turn yang berhasil:

```python
# Dalam cli.py - main() loop
if finished and session_manager:
    snapshot = SessionSnapshot(...)
    session_manager.save_snapshot(snapshot)  # Auto-save
```

**Kapan terjadi:**
- ✅ Setelah agent response selesai
- ✅ Setelah compaction (jika diperlukan)
- ✅ Setelah tool execution
- ❌ Tidak saat interrupted (incomplete turn)

---

**Latest Update**: Session Persistence & Recovery dengan CLI Commands - Production Ready 🚀
