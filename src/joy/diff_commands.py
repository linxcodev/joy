"""Command handler untuk `/diff` dan related features."""

import re
from pathlib import Path
from typing import Optional, Dict, Tuple

from rich.console import Console
from rich.text import Text
from rich.panel import Panel

from joy.diff_engine import DiffEngine, FileDiffMetadata, FileChangeType
from joy.diff_ui import DiffUI
from joy.diff_config import DiffConfig


class DiffCommandHandler:
    """Handler untuk diff-related commands dan events."""
    
    def __init__(self, workspace_root: str, console: Console, config: DiffConfig = None):
        self.workspace_root = Path(workspace_root).resolve()
        self.console = console
        self.config = config or DiffConfig()
        self.engine = DiffEngine(str(self.workspace_root))
        self.ui = DiffUI(console)
        
        # Track last snapshots untuk auto diff
        self._last_snapshot = None
        self._pending_changes: Dict[str, FileDiffMetadata] = {}
    
    def is_diff_command(self, message: str) -> bool:
        """Check apakah message adalah diff command."""
        msg = message.strip()
        return msg == "/diff" or msg.startswith("/diff ")
    
    def parse_diff_command(self, message: str) -> Tuple[str, Optional[str]]:
        """
        Parse /diff command.
        
        Returns:
            (command_type, path_or_none)
            command_type: "full" (show all), "file" (show specific file), or "invalid"
        """
        msg = message.strip()
        
        if msg == "/diff":
            return "full", None
        
        match = re.match(r"^/diff\s+(.+)$", msg)
        if match:
            path = match.group(1).strip()
            return "file", path
        
        return "invalid", None
    
    def handle_diff_command(self, message: str) -> bool:
        """
        Handle /diff command execution.
        
        Returns:
            True if command was handled, False otherwise
        """
        if not self.is_diff_command(message):
            return False
        
        command_type, path = self.parse_diff_command(message)
        
        if command_type == "invalid":
            self.console.print(
                Panel(
                    Text("Invalid diff command. Use /diff or /diff <path>", style="red"),
                    border_style="red",
                )
            )
            return True
        
        if not self._pending_changes:
            self.console.print(
                Panel(
                    Text("No pending changes to display.", style="dim"),
                    border_style="#5c5c5c",
                )
            )
            return True
        
        if command_type == "file" and path:
            # Tampilkan diff untuk file tertentu
            self._display_single_file_diff(path)
        else:
            # Tampilkan semua diffs
            self._display_all_diffs()
        
        return True
    
    def _display_single_file_diff(self, file_path: str) -> None:
        """Display diff untuk single file."""
        # Normalize path
        rel_path = str(Path(file_path).as_posix())
        
        if rel_path not in self._pending_changes:
            self.console.print(
                Panel(
                    Text(f"File not found in pending changes: {file_path}", style="yellow"),
                    border_style="yellow",
                )
            )
            return
        
        metadata = self._pending_changes[rel_path]
        self.console.print()
        self.ui.display_diff_review({rel_path: metadata}, f"Changes: {file_path}")
        self.console.print()
    
    def _display_all_diffs(self) -> None:
        """Display semua pending diffs."""
        self.console.print()
        self.ui.display_diff_review(self._pending_changes, "Pending Changes")
        self.console.print()
    
    def capture_initial_state(self, file_paths: list[str] = None) -> None:
        """
        Capture initial state sebelum agent melakukan operasi.
        
        Args:
            file_paths: List file yang akan diwatch. Jika None, watch all.
        """
        self._last_snapshot = self.engine.capture_snapshot(file_paths)
    
    def detect_and_track_changes(self, file_paths: list[str] = None) -> Dict[str, FileDiffMetadata]:
        """
        Detect perubahan sejak last capture dan track them.
        
        Args:
            file_paths: List file yang di-check. Jika None, check all.
        
        Returns:
            Dict of changes detected
        """
        if self._last_snapshot is None:
            return {}
        
        current_snapshot = self.engine.capture_snapshot(file_paths)
        changes = self.engine.compare_snapshots(self._last_snapshot, current_snapshot)
        
        # Filter out unchanged files
        changed = {k: v for k, v in changes.items() 
                  if v.change_type != FileChangeType.UNCHANGED}
        
        # Update pending changes
        self._pending_changes.update(changed)
        
        return changed
    
    def should_auto_display_diff(self, changes: Dict[str, FileDiffMetadata]) -> bool:
        """
        Determine apakah diff harus ditampilkan otomatis.
        
        Returns:
            True if diff should be auto-displayed
        """
        if not self.config.auto_display:
            return False
        
        if not changes:
            return False
        
        # Check watch patterns
        if self.config.watch_patterns:
            has_matching = any(
                any(Path(path).match(pattern) for pattern in self.config.watch_patterns)
                for path in changes.keys()
            )
            if not has_matching:
                return False
        
        # Check ignore patterns
        if self.config.ignore_patterns:
            has_ignored = any(
                any(Path(path).match(pattern) for pattern in self.config.ignore_patterns)
                for path in changes.keys()
            )
            if has_ignored:
                return False
        
        return True
    
    def display_auto_diff(self, changes: Dict[str, FileDiffMetadata]) -> None:
        """
        Display diff otomatis setelah agent task selesai.
        
        Displays summary + (optionally) full diff based on config.
        """
        if not changes:
            return
        
        if self.config.show_summary:
            summary = self.engine.summary(changes)
            self.console.print()
            self.console.print(Panel(summary, title="Changes Summary", border_style="#5c5c5c"))
        
        if self.config.format == "full":
            self._display_all_diffs()
    
    def clear_pending(self) -> None:
        """Clear pending changes tracking."""
        self._pending_changes.clear()
        self._last_snapshot = None
    
    def format_summary_text(self, changes: Dict[str, FileDiffMetadata]) -> str:
        """Format summary as plain text (non-rich)."""
        if not changes:
            return "No changes detected."
        
        created = [d for d in changes.values() if d.change_type == FileChangeType.CREATED]
        modified = [d for d in changes.values() if d.change_type == FileChangeType.MODIFIED]
        deleted = [d for d in changes.values() if d.change_type == FileChangeType.DELETED]
        
        lines = []
        lines.append(f"📊 Summary: {len(created)} created, {len(modified)} modified, {len(deleted)} deleted")
        
        if created:
            lines.append(f"  ✨ Created: {', '.join(d.path for d in created[:3])}")
            if len(created) > 3:
                lines.append(f"       ... and {len(created)-3} more")
        
        if modified:
            lines.append(f"  ✏️  Modified: {', '.join(d.path for d in modified[:3])}")
            if len(modified) > 3:
                lines.append(f"       ... and {len(modified)-3} more")
        
        if deleted:
            lines.append(f"  🗑️  Deleted: {', '.join(d.path for d in deleted[:3])}")
            if len(deleted) > 3:
                lines.append(f"       ... and {len(deleted)-3} more")
        
        return "\n".join(lines)
