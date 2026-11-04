"""Context usage monitoring and display for Joy CLI.

This module provides:
- ContextMonitor: Display and track context usage metrics
- Formatted output using Rich for terminal display
- Integration points for compaction status and results
"""

import logging
from dataclasses import dataclass
from typing import Optional
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.live import Live
from rich.progress import Progress, BarColumn, PercentCompleteColumn, TextColumn

logger = logging.getLogger(__name__)


@dataclass
class ContextMetrics:
    """Current context usage metrics."""
    input_tokens: int
    context_window_size: int
    max_output_tokens: int
    usage_percent: float  # 0-100
    warning_threshold_percent: float
    is_warning: bool
    is_critical: bool
    estimated: bool  # True if using fallback token counting


@dataclass
class CompactionEvent:
    """Event triggered during context compaction."""
    event_type: str  # 'start', 'progress', 'complete', 'error'
    original_tokens: int
    new_tokens: int
    summary: Optional[str] = None
    error: Optional[str] = None
    reduction_percent: float = 0.0


class ContextMonitor:
    """Monitor and display context usage in the CLI."""

    def __init__(self, console: Optional[Console] = None):
        """Initialize context monitor.
        
        Args:
            console: Rich Console instance for output (creates new if None)
        """
        self.console = console or Console()
        self.current_metrics: Optional[ContextMetrics] = None
        self._compaction_in_progress = False
        self._live_display: Optional[Live] = None

    def update_metrics(self, metrics: ContextMetrics) -> None:
        """Update current context metrics.
        
        Args:
            metrics: ContextMetrics with current usage
        """
        self.current_metrics = metrics

    def display_context_usage(self) -> None:
        """Display current context usage in a compact format."""
        if not self.current_metrics:
            return

        metrics = self.current_metrics
        
        # Calculate available tokens
        available = metrics.context_window_size - metrics.max_output_tokens
        remaining = available - metrics.input_tokens
        
        # Build progress bar with color coding
        if metrics.usage_percent >= 90:
            usage_color = "red"
            warning_symbol = "🔴"
        elif metrics.usage_percent >= 75:
            usage_color = "yellow"
            warning_symbol = "🟡"
        else:
            usage_color = "green"
            warning_symbol = "🟢"

        # Display main usage line
        usage_line = (
            f"{warning_symbol} Context Usage: "
            f"[bold {usage_color}]{metrics.usage_percent:.1f}%[/] "
            f"({metrics.input_tokens:,} / {available:,} tokens)"
        )
        
        self.console.print(usage_line)
        
        # Display estimation note if applicable
        if metrics.estimated:
            self.console.print(
                "[dim italic]ⓘ Token count estimated (install tiktoken for accuracy)[/]"
            )

    def display_compaction_threshold(self, threshold_percent: float) -> None:
        """Display compaction threshold information.
        
        Args:
            threshold_percent: Threshold percentage (0-100)
        """
        threshold_line = f"[dim]Compaction threshold: {threshold_percent:.0f}%[/]"
        self.console.print(threshold_line)

    def display_compaction_starting(self) -> None:
        """Display message when compaction is starting."""
        self._compaction_in_progress = True
        self.console.print("\n[blue]⚡ Context threshold reached[/]")
        self.console.print("[cyan]Compacting conversation...[/]")

    def display_compaction_progress(self, progress: float, status: str = "") -> None:
        """Display compaction progress (0.0 to 1.0).
        
        Args:
            progress: Progress as decimal (0.0 to 1.0)
            status: Optional status message
        """
        if status:
            self.console.print(f"[cyan]{status}[/]")

    def display_compaction_complete(self, event: CompactionEvent) -> None:
        """Display compaction completion with results.
        
        Args:
            event: CompactionEvent with results
        """
        self._compaction_in_progress = False
        
        # Success indicator
        self.console.print("[green]✓ Context compacted[/]")
        
        # Compaction results table
        table = Table(title="Compaction Summary", show_header=False, box=None)
        table.add_column("Metric", style="dim")
        table.add_column("Value")
        
        # Add metrics
        reduction = event.original_tokens - event.new_tokens
        reduction_pct = (reduction / event.original_tokens * 100) if event.original_tokens > 0 else 0
        
        table.add_row("Original", f"{event.original_tokens:,} tokens")
        table.add_row("Compacted", f"{event.new_tokens:,} tokens")
        table.add_row("Reduction", f"{reduction:,} tokens ({reduction_pct:.1f}%)")
        
        self.console.print(table)
        
        # Display summary if available
        if event.summary and len(event.summary) > 200:
            # Show first 200 chars of summary in compact form
            summary_preview = event.summary[:200].replace("\n", " ") + "..."
            self.console.print(f"[dim italic]Summary: {summary_preview}[/]")

    def display_compaction_error(self, error: str) -> None:
        """Display compaction error gracefully.
        
        Args:
            error: Error message
        """
        self._compaction_in_progress = False
        self.console.print(f"[yellow]⚠ Compaction issue: {error}[/]")
        self.console.print("[dim]Continuing with current conversation...[/]")

    def display_context_after_compaction(self, metrics: ContextMetrics) -> None:
        """Display context usage after compaction.
        
        Args:
            metrics: ContextMetrics with post-compaction usage
        """
        self.console.print("\n[cyan]Updated Context Usage:[/]")
        self.update_metrics(metrics)
        self.display_context_usage()

    def create_context_status_panel(self) -> Optional[Panel]:
        """Create a Panel showing current context status for persistent display.
        
        Returns:
            Rich Panel or None if no metrics available
        """
        if not self.current_metrics:
            return None

        metrics = self.current_metrics
        available = metrics.context_window_size - metrics.max_output_tokens
        remaining = available - metrics.input_tokens
        
        # Color based on usage
        if metrics.usage_percent >= 90:
            color = "red"
        elif metrics.usage_percent >= 75:
            color = "yellow"
        else:
            color = "green"
        
        content = Text()
        content.append(f"{metrics.usage_percent:.1f}% ", style=f"bold {color}")
        content.append(f"({metrics.input_tokens:,}/{available:,})")
        
        if remaining < 1000:
            content.append(f"\n⚠ Low tokens: {remaining:,} remaining", style="yellow")
        
        return Panel(content, title="Context", border_style=color, expand=False)

    def format_context_info(self) -> str:
        """Format context information as a compact string.
        
        Returns:
            Formatted context info string
        """
        if not self.current_metrics:
            return ""
        
        metrics = self.current_metrics
        available = metrics.context_window_size - metrics.max_output_tokens
        
        # Emoji indicator
        if metrics.usage_percent >= 90:
            emoji = "🔴"
        elif metrics.usage_percent >= 75:
            emoji = "🟡"
        else:
            emoji = "🟢"
        
        return (
            f"{emoji} {metrics.usage_percent:.0f}% "
            f"({metrics.input_tokens:,}/{available:,})"
        )

    def get_compaction_status(self) -> str:
        """Get current compaction status string.
        
        Returns:
            Status string: 'idle', 'compacting', or 'completed'
        """
        return "compacting" if self._compaction_in_progress else "idle"


class ContextDisplayManager:
    """Manage context display lifecycle in CLI."""

    def __init__(self, console: Optional[Console] = None):
        """Initialize display manager.
        
        Args:
            console: Rich Console instance
        """
        self.monitor = ContextMonitor(console)
        self._display_enabled = True

    def enable_display(self) -> None:
        """Enable context display."""
        self._display_enabled = True

    def disable_display(self) -> None:
        """Disable context display."""
        self._display_enabled = False

    def should_display(self) -> bool:
        """Check if display is enabled."""
        return self._display_enabled

    def handle_context_event(self, event_type: str, data: dict) -> None:
        """Handle context-related events from LLM worker.
        
        Args:
            event_type: Type of event: 'metrics_update', 'compaction_start', 
                       'compaction_complete', 'compaction_error'
            data: Event data dict
        """
        if not self._display_enabled:
            return

        try:
            if event_type == "metrics_update":
                metrics = ContextMetrics(
                    input_tokens=data.get("input_tokens", 0),
                    context_window_size=data.get("context_window_size", 8192),
                    max_output_tokens=data.get("max_output_tokens", 2048),
                    usage_percent=data.get("usage_percent", 0.0),
                    warning_threshold_percent=data.get("warning_threshold_percent", 80.0),
                    is_warning=data.get("is_warning", False),
                    is_critical=data.get("is_critical", False),
                    estimated=data.get("estimated", False),
                )
                self.monitor.update_metrics(metrics)

            elif event_type == "compaction_start":
                self.monitor.display_compaction_starting()
                if "threshold_percent" in data:
                    self.monitor.display_compaction_threshold(data["threshold_percent"])

            elif event_type == "compaction_complete":
                event = CompactionEvent(
                    event_type="complete",
                    original_tokens=data.get("original_tokens", 0),
                    new_tokens=data.get("new_tokens", 0),
                    summary=data.get("summary"),
                    reduction_percent=data.get("reduction_percent", 0.0),
                )
                self.monitor.display_compaction_complete(event)
                if "new_metrics" in data:
                    new_metrics = ContextMetrics(
                        input_tokens=data["new_metrics"].get("input_tokens", 0),
                        context_window_size=data["new_metrics"].get("context_window_size", 8192),
                        max_output_tokens=data["new_metrics"].get("max_output_tokens", 2048),
                        usage_percent=data["new_metrics"].get("usage_percent", 0.0),
                        warning_threshold_percent=80.0,
                        is_warning=False,
                        is_critical=False,
                        estimated=data["new_metrics"].get("estimated", False),
                    )
                    self.monitor.display_context_after_compaction(new_metrics)

            elif event_type == "compaction_error":
                self.monitor.display_compaction_error(data.get("error", "Unknown error"))

        except Exception as exc:
            logger.error(f"Error handling context event: {exc}", exc_info=True)
