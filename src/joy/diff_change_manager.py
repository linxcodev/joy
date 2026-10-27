"""
Change manager untuk Diff Review - mengelola accept/reject operations dan state management.

Fitur:
1. Review status per file (pending, accepted, rejected)
2. Accept/reject dengan konfirmasi
3. Deteksi konflik manual edits
4. Batch operations (accept/reject multiple files)
5. Undo operations
"""

from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass
from enum import Enum
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.padding import Padding

from joy.diff_engine import FileDiffMetadata, FileChangeType, FileStatus
from joy.diff_snapshot_manager import DiffSnapshotManager, ChangeManagementRecord


class ReviewStatus(Enum):
    """Status review untuk file perubahan."""
    PENDING = "pending"      # Awaiting user review
    ACCEPTED = "accepted"    # User approved changes
    REJECTED = "rejected"    # User rejected, restored to original
    CONFLICT = "conflict"    # Manual changes detected, needs review


@dataclass
class FileReviewState:
    """State review untuk single file."""
    file_path: str
    change_type: FileChangeType
    file_status: FileStatus
    review_status: ReviewStatus
    lines_added: int = 0
    lines_removed: int = 0
    conflict_reason: Optional[str] = None
    accepted_at: Optional[str] = None
    rejected_at: Optional[str] = None


class DiffChangeManager:
    """
    Manage perubahan files - accept/reject, conflict detection, batch operations.
    """
    
    def __init__(self, snapshot_manager: DiffSnapshotManager, console: Console):
        self.snapshot_manager = snapshot_manager
        self.console = console
        
        # State per task: task_id -> {file_path -> FileReviewState}
        self._review_states: Dict[str, Dict[str, FileReviewState]] = {}
        
        # Undo stack: task_id -> [(action, state_before)]
        self._undo_stack: Dict[str, List] = {}
    
    def initialize_review(self, task_id: str, diffs: Dict[str, FileDiffMetadata]) -> None:
        """Initialize review state untuk task baru."""
        self._review_states[task_id] = {}
        self._undo_stack[task_id] = []
        
        for file_path, diff_metadata in diffs.items():
            state = FileReviewState(
                file_path=file_path,
                change_type=diff_metadata.change_type,
                file_status=diff_metadata.file_status,
                review_status=ReviewStatus.PENDING,
                lines_added=diff_metadata.lines_added,
                lines_removed=diff_metadata.lines_removed,
            )
            self._review_states[task_id][file_path] = state
    
    def get_review_status(self, task_id: str, file_path: str) -> Optional[ReviewStatus]:
        """Get review status untuk file."""
        if task_id not in self._review_states:
            return None
        return self._review_states[task_id].get(file_path, {}).review_status
    
    def get_pending_files(self, task_id: str) -> List[FileReviewState]:
        """Get semua files yang masih pending review."""
        if task_id not in self._review_states:
            return []
        
        return [
            state for state in self._review_states[task_id].values()
            if state.review_status == ReviewStatus.PENDING
        ]
    
    def accept_file(self, task_id: str, file_path: str, require_confirmation: bool = True) -> Tuple[bool, Optional[str]]:
        """
        Accept perubahan untuk file tertentu.
        
        Returns:
            (success, error_message)
        """
        if task_id not in self._review_states:
            return False, "Task not found"
        
        if file_path not in self._review_states[task_id]:
            return False, "File not in review state"
        
        state = self._review_states[task_id][file_path]
        
        # Require confirmation untuk beberapa tipe perubahan
        if require_confirmation and state.change_type == FileChangeType.DELETED:
            self.console.print(
                Panel(
                    Text(f"Accept DELETION of {file_path}? Type 'confirm' to proceed.", style="yellow"),
                    border_style="yellow"
                )
            )
            # In practice, this would be handled by CLI prompt
        
        # Record undo
        self._undo_stack[task_id].append(('accept', state.review_status))
        
        # Update state
        state.review_status = ReviewStatus.ACCEPTED
        state.accepted_at = datetime.now().isoformat()
        
        # Mark di snapshot manager
        self.snapshot_manager.mark_accepted(task_id, file_path)
        
        return True, None
    
    def reject_file(self, task_id: str, file_path: str, require_confirmation: bool = True) -> Tuple[bool, Optional[str]]:
        """
        Reject perubahan untuk file (restore ke snapshot).
        
        Returns:
            (success, error_message)
        """
        if task_id not in self._review_states:
            return False, "Task not found"
        
        if file_path not in self._review_states[task_id]:
            return False, "File not in review state"
        
        state = self._review_states[task_id][file_path]
        
        # Check jika bisa restore
        can_restore, conflict_reason = self.snapshot_manager.can_restore(task_id, file_path)
        
        if not can_restore:
            state.review_status = ReviewStatus.CONFLICT
            state.conflict_reason = conflict_reason
            return False, f"Cannot reject due to conflict: {conflict_reason}"
        
        # Require confirmation
        if require_confirmation:
            self.console.print(
                Panel(
                    Text(f"Reject changes to {file_path}? This will restore to original.", style="yellow"),
                    border_style="yellow"
                )
            )
        
        # Perform restore
        success, error = self.snapshot_manager.restore_file(task_id, file_path, create_backup=True)
        
        if not success:
            state.review_status = ReviewStatus.CONFLICT
            state.conflict_reason = error
            return False, error
        
        # Record undo
        self._undo_stack[task_id].append(('reject', state.review_status))
        
        # Update state
        state.review_status = ReviewStatus.REJECTED
        state.rejected_at = datetime.now().isoformat()
        
        return True, None
    
    def detect_conflicts(self, task_id: str, current_files: Dict[str, str]) -> Dict[str, str]:
        """
        Detect manual changes yang terjadi setelah snapshot.
        
        Returns:
            Dict[file_path] -> conflict_reason
        """
        if task_id not in self._review_states:
            return {}
        
        conflicts = {}
        manual_changes = self.snapshot_manager.detect_manual_changes(task_id, current_files)
        
        for file_path in manual_changes:
            if file_path in self._review_states[task_id]:
                state = self._review_states[task_id][file_path]
                state.review_status = ReviewStatus.CONFLICT
                state.conflict_reason = "Manual changes detected after snapshot"
                conflicts[file_path] = state.conflict_reason
        
        return conflicts
    
    def accept_all(self, task_id: str) -> Tuple[int, List[str]]:
        """
        Accept semua pending changes.
        
        Returns:
            (accepted_count, errors)
        """
        if task_id not in self._review_states:
            return 0, ["Task not found"]
        
        errors = []
        accepted_count = 0
        
        for file_path, state in list(self._review_states[task_id].items()):
            if state.review_status == ReviewStatus.PENDING:
                success, error = self.accept_file(task_id, file_path, require_confirmation=False)
                if success:
                    accepted_count += 1
                else:
                    errors.append(error or f"Failed to accept {file_path}")
        
        return accepted_count, errors
    
    def reject_all(self, task_id: str) -> Tuple[int, List[str]]:
        """
        Reject semua pending changes.
        
        Returns:
            (rejected_count, errors)
        """
        if task_id not in self._review_states:
            return 0, ["Task not found"]
        
        errors = []
        rejected_count = 0
        
        for file_path, state in list(self._review_states[task_id].items()):
            if state.review_status == ReviewStatus.PENDING:
                success, error = self.reject_file(task_id, file_path, require_confirmation=False)
                if success:
                    rejected_count += 1
                else:
                    errors.append(error or f"Failed to reject {file_path}")
        
        return rejected_count, errors
    
    def undo_last_action(self, task_id: str) -> Tuple[bool, Optional[str]]:
        """Undo last accept/reject action."""
        if task_id not in self._undo_stack or not self._undo_stack[task_id]:
            return False, "Nothing to undo"
        
        action, previous_status = self._undo_stack[task_id].pop()
        # TODO: Implement undo logic
        return True, None
    
    def get_summary(self, task_id: str) -> Dict:
        """Get summary dari review state."""
        if task_id not in self._review_states:
            return {'total': 0, 'pending': 0, 'accepted': 0, 'rejected': 0, 'conflicts': 0}
        
        states = self._review_states[task_id].values()
        
        return {
            'total': len(states),
            'pending': len([s for s in states if s.review_status == ReviewStatus.PENDING]),
            'accepted': len([s for s in states if s.review_status == ReviewStatus.ACCEPTED]),
            'rejected': len([s for s in states if s.review_status == ReviewStatus.REJECTED]),
            'conflicts': len([s for s in states if s.review_status == ReviewStatus.CONFLICT]),
        }
    
    def display_review_summary(self, task_id: str, show_accepted: bool = True, show_rejected: bool = True) -> None:
        """Display summary dari review state dengan colors dan formatting."""
        if task_id not in self._review_states:
            self.console.print("[red]No review state found[/red]")
            return
        
        states = self._review_states[task_id].values()
        summary = self.get_summary(task_id)
        
        # Title
        title_text = Text("Review Summary", style="bold cyan")
        stats = [
            Text(f"Total: {summary['total']}", style="#95a5a6"),
            Text(f"Pending: {summary['pending']}", style="yellow"),
            Text(f"Accepted: {summary['accepted']}", style="green"),
            Text(f"Rejected: {summary['rejected']}", style="red"),
        ]
        if summary['conflicts'] > 0:
            stats.append(Text(f"Conflicts: {summary['conflicts']}", style="magenta"))
        
        stats_text = Text(" · ").join(stats)
        
        self.console.print(Panel(
            Text.assemble(title_text, "\n", Padding(stats_text, (0, 0, 0, 1))),
            border_style="#5c5c5c"
        ))
        
        # File list dengan status
        table = Table(show_header=True, header_style="bold")
        table.add_column("File", style="cyan")
        table.add_column("Status", style="white")
        table.add_column("Changes", style="white")
        
        for state in sorted(states, key=lambda s: s.file_path):
            # Status badge
            if state.review_status == ReviewStatus.ACCEPTED:
                status_badge = Text("✓ Accepted", style="green")
            elif state.review_status == ReviewStatus.REJECTED:
                status_badge = Text("✗ Rejected", style="red")
            elif state.review_status == ReviewStatus.CONFLICT:
                status_badge = Text("⚠ Conflict", style="magenta")
            else:
                status_badge = Text("⦿ Pending", style="yellow")
            
            # Changes summary
            if state.change_type == FileChangeType.CREATED:
                changes_text = Text(f"+{state.lines_added} lines", style="green")
            elif state.change_type == FileChangeType.DELETED:
                changes_text = Text(f"−{state.lines_removed} lines", style="red")
            elif state.change_type == FileChangeType.MODIFIED:
                changes = []
                if state.lines_added > 0:
                    changes.append(Text(f"+{state.lines_added}", style="green"))
                if state.lines_removed > 0:
                    changes.append(Text(f"−{state.lines_removed}", style="red"))
                changes_text = Text(" ").join(changes)
            else:
                changes_text = Text("—", style="dim")
            
            table.add_row(state.file_path, status_badge, changes_text)
        
        self.console.print(table)
    
    def cleanup(self, task_id: str) -> None:
        """Clean up review state setelah task selesai."""
        self._review_states.pop(task_id, None)
        self._undo_stack.pop(task_id, None)
