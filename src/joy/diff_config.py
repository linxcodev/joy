"""Configuration untuk Diff Review feature di Joy CLI."""

import os
from dataclasses import dataclass


@dataclass
class DiffConfig:
    """Configuration untuk diff behavior."""
    
    # Tampilkan diff otomatis setelah agent selesai edit
    auto_display: bool = True
    
    # Tampilkan ringkasan perubahan (summary) sebelum full diff
    show_summary: bool = True
    
    # Jumlah baris max dalam diff view (0 = unlimited)
    max_diff_lines: int = 100
    
    # Jumlah files max dalam file list (0 = unlimited)
    max_file_list: int = 20
    
    # Trigger diff hanya jika ada perubahan ke files tertentu (patterns)
    # Empty = semua file patterns
    watch_patterns: list[str] = None
    
    # Jangan trigger diff untuk files yang match pattern ini
    ignore_patterns: list[str] = None
    
    # Format output: "full" (dengan diff content) atau "summary" (ringkasan saja)
    format: str = "full"
    
    # Highlight syntax dalam diff
    syntax_highlight: bool = True
    
    @classmethod
    def from_env(cls) -> "DiffConfig":
        """Load config dari environment variables."""
        auto_display = os.getenv("JOY_DIFF_AUTO", "true").lower() in ("true", "1", "yes")
        show_summary = os.getenv("JOY_DIFF_SUMMARY", "true").lower() in ("true", "1", "yes")
        max_diff_lines = int(os.getenv("JOY_DIFF_MAX_LINES", "100"))
        max_file_list = int(os.getenv("JOY_DIFF_MAX_FILES", "20"))
        format_ = os.getenv("JOY_DIFF_FORMAT", "full")
        syntax_highlight = os.getenv("JOY_DIFF_SYNTAX", "true").lower() in ("true", "1", "yes")
        
        watch_patterns = None
        if watch_str := os.getenv("JOY_DIFF_WATCH"):
            watch_patterns = [p.strip() for p in watch_str.split(",")]
        
        ignore_patterns = None
        if ignore_str := os.getenv("JOY_DIFF_IGNORE"):
            ignore_patterns = [p.strip() for p in ignore_str.split(",")]
        
        return cls(
            auto_display=auto_display,
            show_summary=show_summary,
            max_diff_lines=max_diff_lines,
            max_file_list=max_file_list,
            watch_patterns=watch_patterns or [],
            ignore_patterns=ignore_patterns or [],
            format=format_,
            syntax_highlight=syntax_highlight,
        )
