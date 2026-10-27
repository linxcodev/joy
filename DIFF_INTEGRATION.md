# Diff Review Feature Integration Guide

## Overview

Fitur **Diff Review** telah diintegrasikan ke dalam alur percakapan utama Joy CLI. Fitur ini memungkinkan pengguna untuk:

1. **Melihat perubahan file secara otomatis** setelah agent menyelesaikan tugas edit
2. **Melihat perubahan file secara manual** dengan command `/diff`
3. **Melihat perubahan file tertentu** dengan command `/diff <path>`
4. **Mengonfigurasi perilaku auto-diff** melalui environment variables
5. **Mengelola diff display** tanpa mengganggu alur percakapan

## Arsitektur

### Komponen Utama

```
src/joy/
├── cli.py                 # Main CLI (entry point, integrated di main())
├── diff_commands.py       # Command handler (/diff, /diff <path>)
├── diff_config.py         # Configuration management
├── diff_engine.py         # Diff detection & comparison engine
└── diff_ui.py             # Terminal UI rendering (Rich)
```

### Flow Diagram

```
User Input
    ↓
[is_diff_command?] → YES → [handle_diff_command] → Display diff, return to prompt
    ↓ NO
Send to Agent (stream_turn)
    ↓
[Agent uses tools]
    ↓
[capture_initial_state] (before agent runs)
    ↓
[Agent finishes]
    ↓
[detect_and_track_changes] (compare before/after)
    ↓
[should_auto_display_diff?] → YES → [display_auto_diff]
    ↓ NO
[clear_pending] (cleanup)
    ↓
Back to prompt
```

## Command Syntax

### `/diff` - Show All Changes

Menampilkan semua perubahan file yang telah ditrack:

```bash
› /diff
```

**Output:**
- Header dengan ringkasan (files created/modified/deleted, lines added/removed)
- File list dengan stats (untuk navigasi)
- Unified diff view untuk file yang dipilih
- Syntax highlighting berdasarkan file extension

### `/diff <path>` - Show Specific File Changes

Menampilkan perubahan untuk file tertentu:

```bash
› /diff src/main.py
› /diff config/settings.json
› /diff path/to/nested/file.ts
```

**Behavior:**
- Jika file ada dalam pending changes: tampilkan diffnya
- Jika file tidak ada: tampilkan pesan error
- Support relative paths

## Configuration

### Environment Variables

Konfigurasi via environment variables:

```bash
# Enable/disable auto display (default: true)
export JOY_DIFF_AUTO=true

# Show summary sebelum full diff (default: true)
export JOY_DIFF_SUMMARY=true

# Max baris dalam diff view (default: 100, 0 = unlimited)
export JOY_DIFF_MAX_LINES=100

# Max files dalam file list (default: 20, 0 = unlimited)
export JOY_DIFF_MAX_FILES=20

# Output format: "full" atau "summary" (default: full)
export JOY_DIFF_FORMAT=full

# Enable syntax highlighting dalam diff (default: true)
export JOY_DIFF_SYNTAX=true

# Watch only these patterns (comma-separated, empty = all)
# Contoh: "*.py,src/**/*.ts"
export JOY_DIFF_WATCH=""

# Ignore these patterns (comma-separated)
# Contoh: "__pycache__,node_modules,*.pyc"
export JOY_DIFF_IGNORE=""
```

### DiffConfig Class

```python
from joy.diff_config import DiffConfig

# Load dari environment
config = DiffConfig.from_env()

# Manual override
config.auto_display = False
config.show_summary = True
config.watch_patterns = ["*.py", "src/**/*.ts"]
config.ignore_patterns = ["__pycache__"]
```

## CLI Integration Points

### 1. Command Interception in `main()`

```python
# cli.py::main()
if diff_handler.is_diff_command(message):
    if diff_handler.handle_diff_command(message):
        continue  # Back to prompt, don't send to model
```

**Key Point:** Command dideteksi dan dihandle sebelum dikirim ke model.

### 2. State Capture Before Agent Runs

```python
# Sebelum stream_turn()
diff_handler.capture_initial_state()

# Agent runs dan modify files
draft = stream_turn(session, state, messages, draft)
```

**Key Point:** Snapshot awal diambil sebelum agent melakukan operasi.

### 3. Change Detection After Agent Finishes

```python
# Setelah stream_turn() selesai
if finished and state["used_tools"]:
    changes = diff_handler.detect_and_track_changes()
    
    if changes and diff_handler.should_auto_display_diff(changes):
        diff_handler.display_auto_diff(changes)
```

**Key Point:** Deteksi perubahan hanya jika agent menggunakan tools.

### 4. Pending Changes Cleanup

```python
# Setelah menampilkan diff
if finished:
    diff_handler.clear_pending()
```

**Key Point:** State dibersihkan untuk turn berikutnya.

## Behavior Details

### Auto-Display Logic

Auto-display terjadi jika:

1. `config.auto_display == True` ✓
2. Ada perubahan file yang terdeteksi ✓
3. Perubahan cocok dengan `watch_patterns` (jika ada) ✓
4. Perubahan TIDAK cocok dengan `ignore_patterns` ✓
5. Agent menggunakan tools (`state["used_tools"] == True`) ✓

### No False Triggers

Diff **TIDAK** ditampilkan otomatis jika:

- Agent hanya membaca file (tidak modify)
- Agent menjalankan command tanpa mengubah file
- File yang diubah ada dalam `ignore_patterns`
- File yang diubah tidak cocok dengan `watch_patterns`
- Config `auto_display = False`

### Change Grouping

Perubahan yang dilakukan dalam satu turn agent dikelompokkan:

- Snapshot diambil saat turn dimulai
- Semua tool executions dalam turn itu ditrack
- Diff ditampilkan sekali setelah turn selesai
- Tidak ada multiple diff displays untuk satu turn

## Usage Examples

### Example 1: Enable Auto-Display dengan Summary

```bash
$ export JOY_DIFF_AUTO=true
$ export JOY_DIFF_SUMMARY=true
$ joy

# Agent edit file.py
# → Auto display: summary + full diff

› /exit
```

### Example 2: Disable Auto-Display, Use Manual `/diff`

```bash
$ export JOY_DIFF_AUTO=false
$ joy

# Agent edit file.py
# → No auto display

› /diff
# → Manual display: show all changes

› /diff src/main.py
# → Manual display: show specific file

›
```

### Example 3: Watch Only Python Files

```bash
$ export JOY_DIFF_WATCH="*.py"
$ export JOY_DIFF_IGNORE="__pycache__,*.pyc"
$ joy

# Agent edit app.py
# → Auto display (matches *.py)

# Agent edit config.json
# → No auto display (not *.py)

›
```

### Example 4: Limited Diff Lines

```bash
$ export JOY_DIFF_MAX_LINES=50
$ joy

# Large file edit
# → Only first 50 lines shown
# → Message: "... diff truncated, showing first 50 lines"

›
```

## Session Persistence

✅ Pengguna dapat kembali ke prompt utama:
- Setelah melihat diff otomatis
- Setelah menjalankan `/diff` command
- Riwayat percakapan tetap tersimpan
- State session tidak kehilangan data

```
Agent: *edits file*
[Auto diff display]
[Back to prompt]

› /diff
[Manual diff display]
[Back to prompt]

› Continue chatting with context preserved
```

## Testing

### Run Integration Tests

```bash
# Run all diff integration tests
pytest tests/test_diff_integration.py -v

# Run specific test class
pytest tests/test_diff_integration.py::TestDiffCommandBasics -v

# Run with coverage
pytest tests/test_diff_integration.py --cov=src/joy/diff_*
```

### Test Coverage

Tests mencakup:

- ✅ Command parsing (`/diff`, `/diff <path>`, invalid)
- ✅ Change detection (capture, compare, diff generation)
- ✅ Auto-display configuration
- ✅ Watch/ignore patterns filtering
- ✅ No false triggers (read-only operations)
- ✅ Config from environment variables
- ✅ Complete CLI workflows
- ✅ Session persistence
- ✅ Streaming & events not disrupted

## Implementation Details

### DiffCommandHandler

```python
class DiffCommandHandler:
    """Main handler untuk diff operations."""
    
    def is_diff_command(message: str) -> bool
        """Check if message is /diff command."""
    
    def handle_diff_command(message: str) -> bool
        """Execute diff command, return success."""
    
    def capture_initial_state(file_paths: list = None) -> None
        """Snapshot workspace before agent runs."""
    
    def detect_and_track_changes(file_paths: list = None) -> dict
        """Detect changes since last capture."""
    
    def should_auto_display_diff(changes: dict) -> bool
        """Check if diff should be auto-displayed."""
    
    def display_auto_diff(changes: dict) -> None
        """Display diff automatically."""
    
    def clear_pending() -> None
        """Clear pending changes."""
```

### DiffEngine

```python
class DiffEngine:
    """Low-level diff detection & comparison."""
    
    def capture_snapshot(rel_paths: list = None) -> DiffSnapshot
        """Snapshot file states."""
    
    def compare_snapshots(before, after) -> dict[FileDiffMetadata]
        """Compare two snapshots."""
    
    def summary(diffs: dict) -> str
        """Generate human-readable summary."""
```

### DiffUI

```python
class DiffUI:
    """Terminal UI rendering dengan Rich."""
    
    def render_header(total_files, lines_added, lines_removed) -> Renderable
        """Render summary header."""
    
    def render_file_list(diffs, selected_index) -> Renderable
        """Render file list."""
    
    def render_file_diff(metadata, max_lines) -> Renderable
        """Render unified diff dengan syntax highlighting."""
    
    def display_diff_review(diffs, title) -> None
        """Display interactive diff review."""
```

## Edge Cases Handled

1. **Binary Files**: Ditandai `[BINARY]`, tidak ditampilkan diff
2. **Large Files** (>10MB): Ditandai `[TOO LARGE]`, tidak ditampilkan diff
3. **Unreadable Files**: Ditandai `[UNREADABLE]`, tidak ditampilkan diff
4. **Deleted Files**: Show yang file dihapus dengan jalur lengkap
5. **Created Files**: Show new file dengan full content
6. **Unicode/Encoding**: Support UTF-8 dan fallback ke Latin-1
7. **Long Diffs**: Truncate ke `max_diff_lines`, show message

## Backward Compatibility

✅ **Fully compatible** dengan existing Joy workflows:

- Jika environment variables tidak set → use defaults (auto display on)
- Existing commands (`/exit`, chat messages) tidak berubah
- Streaming output tetap smooth
- Tool events tetap berjalan normal
- Error handling tetap sama

## Performance Considerations

- ✅ Snapshot hanya files dalam workspace
- ✅ Ignore common patterns (`.git`, `__pycache__`, etc.)
- ✅ Thread-safe file access
- ✅ Diff generation di-limit 50000 lines
- ✅ Console rendering optimal dengan Rich

## Future Enhancements

Possible improvements untuk masa depan:

- [ ] Interactive diff viewer (navigate files, scroll diffs)
- [ ] Git integration (stage/unstage changes)
- [ ] Diff persistence (save to file)
- [ ] Diff filtering (by change type, by pattern)
- [ ] Undo changes (if agent made mistakes)
- [ ] Side-by-side diff view
- [ ] Statistics per file type

## Troubleshooting

### Diff not showing even though files changed?

1. Check `JOY_DIFF_AUTO` = `true`
2. Check `JOY_DIFF_WATCH` patterns (if set)
3. Check `JOY_DIFF_IGNORE` patterns (if set)
4. Verify agent used tools (`state["used_tools"]` in code)

### Manual `/diff` shows "No pending changes"?

- Changes may have been cleared after last auto-display
- Run agent task lagi untuk generate changes
- Or check specific file: `/diff <path>`

### Too much output?

Set `JOY_DIFF_MAX_LINES` to lower value:
```bash
export JOY_DIFF_MAX_LINES=30
```

### Performance issue?

- Set `JOY_DIFF_AUTO=false` to disable auto display
- Use manual `/diff` only when needed
- Set `JOY_DIFF_WATCH` untuk limit file patterns

## Summary

Fitur Diff Review telah diintegrasikan dengan sempurna ke Joy CLI dengan:

✅ Minimal changes ke existing code
✅ Full backward compatibility
✅ Configurable behavior
✅ No false triggers
✅ Proper session management
✅ Comprehensive error handling
✅ Full test coverage
✅ Performance optimized
