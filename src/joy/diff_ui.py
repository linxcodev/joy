"""
Modern terminal UI untuk diff review menggunakan Rich dan Prompt Toolkit.

Fitur:
- Header dengan summary perubahan
- File list dengan icons dan statistics
- Unified diff view dengan syntax highlighting
- Navigation dan scrolling yang smooth
- Responsive terhadap ukuran terminal
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from enum import Enum
import difflib
import shutil

from rich.console import Console, RenderableType
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.panel import Panel
from rich.padding import Padding
from rich.segment import Segment
from rich.console import Group
from rich.align import Align

from joy.diff_engine import FileDiffMetadata, FileChangeType, FileStatus, DiffEngine


class DiffColorScheme(Enum):
    """Color scheme untuk diff display."""
    LIGHT = "light"
    DARK = "dark"


@dataclass
class DiffViewState:
    """State untuk diff viewer."""
    selected_file_index: int = 0
    scroll_offset: int = 0
    show_full_diff: bool = False


class DiffUI:
    """Modern diff viewer dengan Rich."""

    # Symbols untuk berbagai file status
    SYMBOLS = {
        FileChangeType.CREATED: "✨",  # New sparkle
        FileChangeType.MODIFIED: "📝",  # Modified pencil
        FileChangeType.DELETED: "🗑️",  # Deleted trash
        FileChangeType.UNCHANGED: "✓",  # Unchanged check
    }

    # Fallback symbols jika unicode tidak support
    SYMBOLS_ASCII = {
        FileChangeType.CREATED: "[+]",
        FileChangeType.MODIFIED: "[~]",
        FileChangeType.DELETED: "[-]",
        FileChangeType.UNCHANGED: "[=]",
    }

    # Colors untuk diff lines
    COLORS = {
        "added": "#2ecc71",      # Green
        "removed": "#e74c3c",    # Red
        "context": "#95a5a6",    # Gray
        "header": "#3498db",     # Blue
        "modified_file": "#f39c12", # Orange
        "created_file": "#2ecc71",  # Green
        "deleted_file": "#e74c3c",  # Red
    }

    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        # Check if terminal supports unicode by checking console encoding
        self._supports_unicode = hasattr(self.console, 'encoding') and \
                                  'utf' in (self.console.encoding or 'utf-8').lower()
        self.state = DiffViewState()

    def _get_symbol(self, change_type: FileChangeType) -> str:
        """Get symbol untuk file change type."""
        if self._supports_unicode:
            return self.SYMBOLS.get(change_type, "•")
        return self.SYMBOLS_ASCII.get(change_type, "-")

    def render_header(
        self,
        total_files: int,
        lines_added: int,
        lines_removed: int,
        status: str = "Review changes",
    ) -> RenderableType:
        """Render header dengan summary."""
        title_text = Text(status, style="bold cyan")

        # Summary stats
        stats_parts = []
        if total_files > 0:
            stats_parts.append(Text(f"{total_files} file{'s' if total_files != 1 else ''}", style="#95a5a6"))
        if lines_added > 0:
            stats_parts.append(Text(f"+{lines_added}", style=self.COLORS["added"]))
        if lines_removed > 0:
            stats_parts.append(Text(f"−{lines_removed}", style=self.COLORS["removed"]))

        stats_text = Text(" · ").join(stats_parts) if stats_parts else Text("No changes", style="#7f8c8d")

        # Combine title and stats
        header_content = Group(
            title_text,
            Padding(stats_text, (0, 0, 0, 1))
        )

        return Panel(
            header_content,
            style="bold",
            border_style="#5c5c5c",
            expand=False,
            padding=(0, 1),
        )

    def render_file_list(
        self,
        diffs: Dict[str, FileDiffMetadata],
        selected_index: int = 0,
        max_height: int = 10,
    ) -> Tuple[RenderableType, int]:
        """Render list of changed files dengan selection.
        
        Returns:
            (renderable, total_files)
        """
        files = sorted(diffs.items())
        total_files = len(files)

        if not files:
            return Text("No changes detected", style="dim"), 0

        table = Table(
            show_header=False,
            show_footer=False,
            padding=(0, 1),
            pad_edge=False,
            expand=False,
        )

        visible_start = max(0, selected_index - max_height // 2)
        visible_end = min(total_files, visible_start + max_height)

        for idx, (rel_path, metadata) in enumerate(files):
            # Skip lines outside visible range
            if idx < visible_start or idx >= visible_end:
                continue

            is_selected = idx == selected_index
            symbol = self._get_symbol(metadata.change_type)

            # File path (with color based on change type)
            if metadata.change_type == FileChangeType.CREATED:
                path_style = self.COLORS["created_file"]
            elif metadata.change_type == FileChangeType.DELETED:
                path_style = self.COLORS["deleted_file"]
            elif metadata.change_type == FileChangeType.MODIFIED:
                path_style = self.COLORS["modified_file"]
            else:
                path_style = "#95a5a6"

            path_text = Text(rel_path, style=path_style)

            # Stats
            stats = []
            if metadata.lines_added > 0:
                stats.append(Text(f"+{metadata.lines_added}", style=self.COLORS["added"]))
            if metadata.lines_removed > 0:
                stats.append(Text(f"−{metadata.lines_removed}", style=self.COLORS["removed"]))

            stats_text = Text(" ").join(stats) if stats else Text("", style="dim")

            # Status badge
            if metadata.file_status != FileStatus.READABLE:
                status_badge = Text(
                    f"[{metadata.file_status.value.upper()}]",
                    style="bold yellow" if metadata.file_status == FileStatus.BINARY else "bold red"
                )
            else:
                status_badge = Text()

            # Row content
            row_content = [symbol, path_text, stats_text, status_badge]
            row_text = Text()
            for item in row_content:
                if isinstance(item, Text):
                    row_text.append(item)
                else:
                    row_text.append(str(item))

            # Highlight selection
            if is_selected:
                row_text.stylize("reverse")

            table.add_row(row_text)

        return table, total_files

    def render_file_diff(
        self,
        metadata: FileDiffMetadata,
        max_lines: int = 100,
        language: Optional[str] = None,
    ) -> RenderableType:
        """Render unified diff untuk single file."""
        output = []

        # File header dengan stats
        header = Text()
        header.append(f"📄 {metadata.path}", style="bold cyan")
        header.append(f"  ", style="")
        header.append(f"+{metadata.lines_added} ", style=self.COLORS["added"] if metadata.lines_added > 0 else "")
        header.append(f"−{metadata.lines_removed}", style=self.COLORS["removed"] if metadata.lines_removed > 0 else "")

        output.append(header)

        # File status jika tidak readable
        if metadata.file_status != FileStatus.READABLE:
            status_msg = Text(
                f"  [{metadata.file_status.value.upper()}] File tidak dapat ditampilkan",
                style="bold yellow" if metadata.file_status == FileStatus.BINARY else "bold red"
            )
            output.append(status_msg)
            return Group(*output)

        # Unified diff content
        if metadata.unified_diff:
            diff_text = metadata.unified_diff
        else:
            # Generate unified diff dari diff_lines
            diff_text = self._generate_unified_diff(metadata.diff_lines)

        # Split menjadi lines dan render dengan syntax highlighting jika mungkin
        diff_lines_list = diff_text.split("\n")

        # Tentukan bahasa dari file extension
        if language is None:
            language = self._infer_language(metadata.path)

        # Limit banyaknya lines ditampilkan
        if len(diff_lines_list) > max_lines:
            diff_lines_list = diff_lines_list[:max_lines]
            diff_text = "\n".join(diff_lines_list)
            diff_text += f"\n\n[… truncated, showing first {max_lines} lines]"

        # Render diff dengan syntax highlighting jika language terdeteksi
        if language and language != "text":
            try:
                diff_rendered = Syntax(
                    diff_text,
                    language,
                    theme="monokai",
                    line_numbers=False,
                    word_wrap=True,
                    background_color="default",
                )
                output.append(diff_rendered)
            except Exception:
                # Fallback ke plain text jika syntax highlighting error
                output.append(self._render_diff_plain(diff_lines_list))
        else:
            output.append(self._render_diff_plain(diff_lines_list))

        return Group(*output)

    def _generate_unified_diff(self, diff_lines: List[Dict]) -> str:
        """Generate unified diff dari diff_lines metadata."""
        if not diff_lines:
            return ""

        result = []
        for item in diff_lines:
            line_type = item.get("type", "ctx")
            line_text = item.get("line", "")

            if line_type == "add":
                result.append(f"+{line_text}")
            elif line_type == "del":
                result.append(f"-{line_text}")
            else:
                result.append(f" {line_text}")

        return "\n".join(result)

    def _render_diff_plain(self, diff_lines: List[str]) -> RenderableType:
        """Render diff lines dengan color coding tanpa syntax highlighting."""
        output = []

        for line in diff_lines:
            if not line:
                output.append(Text())
                continue

            if line.startswith("+"):
                # Added line
                text = Text(line, style=self.COLORS["added"])
            elif line.startswith("-"):
                # Removed line
                text = Text(line, style=self.COLORS["removed"])
            elif line.startswith("@@"):
                # Hunk header
                text = Text(line, style=self.COLORS["header"])
            elif line.startswith("---") or line.startswith("+++"):
                # File markers
                text = Text(line, style="dim")
            else:
                # Context line
                text = Text(line, style=self.COLORS["context"])

            output.append(text)

        return Group(*output)

    def _infer_language(self, filepath: str) -> str:
        """Infer programming language dari file extension."""
        extension_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".jsx": "javascript",
            ".tsx": "typescript",
            ".java": "java",
            ".go": "go",
            ".rs": "rust",
            ".c": "c",
            ".cpp": "cpp",
            ".cs": "csharp",
            ".rb": "ruby",
            ".php": "php",
            ".html": "html",
            ".css": "css",
            ".json": "json",
            ".xml": "xml",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".sh": "bash",
            ".bash": "bash",
            ".md": "markdown",
            ".tex": "latex",
            ".sql": "sql",
        }

        for ext, lang in extension_map.items():
            if filepath.endswith(ext):
                return lang

        return "text"

    def display_diff_review(
        self,
        diffs: Dict[str, FileDiffMetadata],
        title: str = "Code Changes",
    ) -> None:
        """Display interactive diff review di console."""
        if not diffs:
            self.console.print(
                Panel(
                    Text("No changes detected", style="dim"),
                    title="Review",
                    border_style="#5c5c5c",
                )
            )
            return

        files = sorted(diffs.items())
        selected_idx = 0
        max_file_height = 8
        max_diff_height = 50

        # Calculate stats
        total_files = len(files)
        total_added = sum(m.lines_added for _, m in files)
        total_removed = sum(m.lines_removed for _, m in files)

        # Render and display
        self.console.print()
        self.console.print(self.render_header(total_files, total_added, total_removed, title))
        self.console.print()

        file_list, _ = self.render_file_list(diffs, selected_idx, max_file_height)
        self.console.print(Panel(file_list, title="Files", border_style="#5c5c5c", expand=False))
        self.console.print()

        # Display selected file diff
        if 0 <= selected_idx < len(files):
            _, metadata = files[selected_idx]
            diff_view = self.render_file_diff(metadata, max_diff_height)
            self.console.print(Panel(diff_view, title="Diff", border_style="#5c5c5c", expand=True))


class DiffTable:
    """Helper untuk render file changes sebagai table."""

    @staticmethod
    def create_summary_table(diffs: Dict[str, FileDiffMetadata]) -> Table:
        """Create summary table dari file changes."""
        table = Table(
            title="File Changes Summary",
            show_header=True,
            header_style="bold cyan",
            padding=(0, 1),
        )

        table.add_column("File", style="cyan")
        table.add_column("Type", style="magenta")
        table.add_column("Added", style="#2ecc71")
        table.add_column("Removed", style="#e74c3c")
        table.add_column("Status", style="#95a5a6")

        for filepath, metadata in sorted(diffs.items()):
            change_type = metadata.change_type.value.capitalize()
            added = str(metadata.lines_added) if metadata.lines_added > 0 else "—"
            removed = str(metadata.lines_removed) if metadata.lines_removed > 0 else "—"
            status = metadata.file_status.value if metadata.file_status != FileStatus.READABLE else "✓"

            table.add_row(filepath, change_type, added, removed, status)

        return table
