"""
Command handler untuk review operations (/accept, /reject, /confirm, etc).

Commands:
- /accept <path>      : Accept changes untuk file tertentu
- /accept-all         : Accept semua pending changes
- /reject <path>      : Reject changes untuk file tertentu (restore)
- /reject-all         : Reject semua pending changes
- /status             : Show review status summary
- /conflicts          : Show files dengan konflik
- /undo               : Undo last action
"""

import re
from pathlib import Path
from typing import Optional, Tuple

from rich.console import Console
from rich.text import Text
from rich.panel import Panel
from rich.prompt import Confirm

from joy.diff_change_manager import DiffChangeManager, ReviewStatus


class DiffReviewCommandHandler:
    """Handler untuk diff review commands."""
    
    def __init__(self, change_manager: DiffChangeManager, console: Console):
        self.change_manager = change_manager
        self.console = console
        self.current_task_id: Optional[str] = None
    
    def set_current_task(self, task_id: str) -> None:
        """Set current task untuk review operations."""
        self.current_task_id = task_id
    
    def is_review_command(self, message: str) -> bool:
        """Check apakah message adalah review command."""
        msg = message.strip()
        return any(msg.startswith(cmd) for cmd in [
            '/accept', '/reject', '/status', '/conflicts', '/undo', '/confirm'
        ])
    
    def parse_command(self, message: str) -> Tuple[str, Optional[str]]:
        """
        Parse review command.
        
        Returns:
            (command, argument)
        """
        msg = message.strip()
        
        for cmd in ['/accept', '/reject']:
            if msg == cmd + '-all':
                return cmd + '-all', None
            
            match = re.match(rf"^{cmd}\s+(.+)$", msg)
            if match:
                return cmd, match.group(1).strip()
            
            if msg == cmd:
                return cmd, None
        
        for cmd in ['/status', '/conflicts', '/undo', '/confirm']:
            if msg == cmd:
                return cmd, None
        
        return 'invalid', None
    
    def handle_command(self, message: str) -> Tuple[bool, Optional[str]]:
        """
        Handle review command.
        
        Returns:
            (handled, output_message)
        """
        if not self.is_review_command(message):
            return False, None
        
        if not self.current_task_id:
            return True, "No active review session"
        
        command, argument = self.parse_command(message)
        
        if command == 'invalid':
            return True, "Invalid review command"
        
        # Dispatch ke handler
        if command == '/accept':
            return self._handle_accept(argument)
        elif command == '/accept-all':
            return self._handle_accept_all()
        elif command == '/reject':
            return self._handle_reject(argument)
        elif command == '/reject-all':
            return self._handle_reject_all()
        elif command == '/status':
            return self._handle_status()
        elif command == '/conflicts':
            return self._handle_conflicts()
        elif command == '/undo':
            return self._handle_undo()
        elif command == '/confirm':
            return True, None  # Placeholder
        
        return False, None
    
    def _handle_accept(self, file_path: Optional[str]) -> Tuple[bool, Optional[str]]:
        """Handle /accept <path> command."""
        if not file_path:
            return True, "Usage: /accept <path>"
        
        success, error = self.change_manager.accept_file(
            self.current_task_id, file_path, require_confirmation=True
        )
        
        if success:
            self.console.print(
                Panel(
                    Text(f"✓ Accepted: {file_path}", style="green"),
                    border_style="green"
                )
            )
            return True, None
        else:
            self.console.print(
                Panel(
                    Text(f"✗ Error: {error}", style="red"),
                    border_style="red"
                )
            )
            return True, None
    
    def _handle_accept_all(self) -> Tuple[bool, Optional[str]]:
        """Handle /accept-all command."""
        summary = self.change_manager.get_summary(self.current_task_id)
        pending = summary['pending']
        
        if pending == 0:
            self.console.print(
                Panel(
                    Text("No pending changes to accept", style="dim"),
                    border_style="#5c5c5c"
                )
            )
            return True, None
        
        # Confirm
        if not Confirm.ask(f"Accept all {pending} pending changes?", console=self.console):
            return True, "Cancelled"
        
        accepted, errors = self.change_manager.accept_all(self.current_task_id)
        
        self.console.print(
            Panel(
                Text(f"✓ Accepted {accepted} files", style="green"),
                border_style="green"
            )
        )
        
        if errors:
            self.console.print(
                Panel(
                    Text("\n".join(errors), style="yellow"),
                    border_style="yellow",
                    title="Warnings"
                )
            )
        
        return True, None
    
    def _handle_reject(self, file_path: Optional[str]) -> Tuple[bool, Optional[str]]:
        """Handle /reject <path> command."""
        if not file_path:
            return True, "Usage: /reject <path>"
        
        # Confirm reject (potentially destructive)
        if not Confirm.ask(f"Reject changes to {file_path}? File will be restored.", console=self.console):
            return True, "Cancelled"
        
        success, error = self.change_manager.reject_file(
            self.current_task_id, file_path, require_confirmation=False
        )
        
        if success:
            self.console.print(
                Panel(
                    Text(f"✗ Rejected: {file_path}", style="red"),
                    border_style="red"
                )
            )
            return True, None
        else:
            self.console.print(
                Panel(
                    Text(f"Error: {error}", style="red"),
                    border_style="red"
                )
            )
            return True, None
    
    def _handle_reject_all(self) -> Tuple[bool, Optional[str]]:
        """Handle /reject-all command."""
        summary = self.change_manager.get_summary(self.current_task_id)
        pending = summary['pending']
        
        if pending == 0:
            self.console.print(
                Panel(
                    Text("No pending changes to reject", style="dim"),
                    border_style="#5c5c5c"
                )
            )
            return True, None
        
        # Confirm (DESTRUCTIVE)
        if not Confirm.ask(
            f"Reject ALL {pending} pending changes? This will restore all files to their original state.",
            console=self.console,
            default=False
        ):
            return True, "Cancelled"
        
        rejected, errors = self.change_manager.reject_all(self.current_task_id)
        
        self.console.print(
            Panel(
                Text(f"✗ Rejected {rejected} files", style="red"),
                border_style="red"
            )
        )
        
        if errors:
            self.console.print(
                Panel(
                    Text("\n".join(errors), style="yellow"),
                    border_style="yellow",
                    title="Warnings"
                )
            )
        
        return True, None
    
    def _handle_status(self) -> Tuple[bool, Optional[str]]:
        """Handle /status command."""
        self.console.print()
        self.change_manager.display_review_summary(self.current_task_id)
        self.console.print()
        return True, None
    
    def _handle_conflicts(self) -> Tuple[bool, Optional[str]]:
        """Handle /conflicts command."""
        # Get all files with conflict status
        summary = self.change_manager.get_summary(self.current_task_id)
        
        if summary['conflicts'] == 0:
            self.console.print(
                Panel(
                    Text("No conflicts detected", style="green"),
                    border_style="green"
                )
            )
            return True, None
        
        # Display conflicts
        self.console.print()
        self.console.print(
            Panel(
                Text(f"⚠ {summary['conflicts']} file(s) with conflicts detected", style="magenta"),
                border_style="magenta"
            )
        )
        self.console.print()
        
        # List conflict files
        # TODO: Get actual conflicts from change_manager
        
        return True, None
    
    def _handle_undo(self) -> Tuple[bool, Optional[str]]:
        """Handle /undo command."""
        success, error = self.change_manager.undo_last_action(self.current_task_id)
        
        if success:
            self.console.print(
                Panel(
                    Text("↶ Undone last action", style="cyan"),
                    border_style="cyan"
                )
            )
        else:
            self.console.print(
                Panel(
                    Text(error or "Could not undo", style="yellow"),
                    border_style="yellow"
                )
            )
        
        return True, None


class ReviewCommandHelper:
    """Helper untuk menampilkan review help/hints."""
    
    @staticmethod
    def display_help(console: Console) -> None:
        """Display review commands help."""
        help_text = """[bold]Review Commands:[/bold]
  [cyan]/accept <path>[/cyan]    Accept changes to a file
  [cyan]/accept-all[/cyan]       Accept all pending changes
  [cyan]/reject <path>[/cyan]    Reject changes (restore file)
  [cyan]/reject-all[/cyan]       Reject all pending changes
  [cyan]/status[/cyan]           Show review summary
  [cyan]/conflicts[/cyan]        Show files with conflicts
  [cyan]/undo[/cyan]             Undo last action
"""
        console.print(help_text)
    
    @staticmethod
    def display_quick_commands(console: Console) -> None:
        """Display quick command hints."""
        console.print(
            Panel(
                "[dim]Type [cyan]/accept-all[/cyan] to accept all changes, "
                "[cyan]/status[/cyan] to see summary, or [cyan]/help[/cyan] for more[/dim]",
                border_style="#5c5c5c"
            )
        )
