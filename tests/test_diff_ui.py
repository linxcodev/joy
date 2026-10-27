"""
Tests untuk diff_ui.py - Modern terminal UI untuk diff review.
"""

import pytest
from pathlib import Path
from rich.console import Console
from io import StringIO

from src.joy.diff_engine import (
    DiffEngine, FileChangeType, FileStatus, FileDiffMetadata
)
from src.joy.diff_ui import DiffUI, DiffTable


@pytest.fixture
def console():
    """Create console yang writes ke string."""
    return Console(file=StringIO(), width=120, legacy_windows=False)


@pytest.fixture
def diff_ui(console):
    """Create DiffUI instance dengan console."""
    return DiffUI(console=console)


@pytest.fixture
def sample_diffs():
    """Create sample diffs untuk testing."""
    return {
        "src/hello.py": FileDiffMetadata(
            path="src/hello.py",
            change_type=FileChangeType.CREATED,
            file_status=FileStatus.READABLE,
            lines_added=15,
            lines_removed=0,
            total_lines_before=0,
            total_lines_after=15,
            unified_diff="""--- /dev/null
+++ b/src/hello.py
@@ -0,0 +1,3 @@
+def hello():
+    print("Hello, World!")
+    return True
""",
            diff_lines=[
                {"type": "add", "line": "def hello():"},
                {"type": "add", "line": '    print("Hello, World!")'},
                {"type": "add", "line": "    return True"},
            ],
        ),
        "src/config.py": FileDiffMetadata(
            path="src/config.py",
            change_type=FileChangeType.MODIFIED,
            file_status=FileStatus.READABLE,
            lines_added=3,
            lines_removed=2,
            total_lines_before=20,
            total_lines_after=21,
            unified_diff="""--- a/src/config.py
+++ b/src/config.py
@@ -5,7 +5,8 @@
 DEBUG = True
-DATABASE_URL = "sqlite:///app.db"
+DATABASE_URL = "postgresql://localhost/joy"
+TIMEOUT = 30
 LOG_LEVEL = "INFO"
-WORKERS = 4
+WORKERS = 8
""",
            diff_lines=[
                {"type": "del", "line": 'DATABASE_URL = "sqlite:///app.db"'},
                {"type": "add", "line": 'DATABASE_URL = "postgresql://localhost/joy"'},
                {"type": "add", "line": "TIMEOUT = 30"},
                {"type": "del", "line": "WORKERS = 4"},
                {"type": "add", "line": "WORKERS = 8"},
            ],
        ),
        "src/old_module.py": FileDiffMetadata(
            path="src/old_module.py",
            change_type=FileChangeType.DELETED,
            file_status=FileStatus.READABLE,
            lines_added=0,
            lines_removed=25,
            total_lines_before=25,
            total_lines_after=0,
            unified_diff="""--- a/src/old_module.py
+++ /dev/null
@@ -1,5 +0,0 @@
-class LegacyHandler:
-    def process(self):
-        pass
-    def cleanup(self):
-        pass
""",
            diff_lines=[
                {"type": "del", "line": "class LegacyHandler:"},
                {"type": "del", "line": "    def process(self):"},
                {"type": "del", "line": "        pass"},
            ],
        ),
    }


class TestDiffUIBasic:
    """Test basic DiffUI functionality."""

    def test_init(self, diff_ui):
        """Test DiffUI initialization."""
        assert diff_ui.console is not None
        assert diff_ui.state.selected_file_index == 0
        assert diff_ui.state.scroll_offset == 0

    def test_get_symbol_unicode(self, diff_ui):
        """Test symbol generation dengan unicode support."""
        symbol = diff_ui._get_symbol(FileChangeType.CREATED)
        assert symbol in ["✨", "[+]"]  # Should be unicode atau ascii fallback

    def test_infer_language_python(self, diff_ui):
        """Test language inference untuk python files."""
        lang = diff_ui._infer_language("src/main.py")
        assert lang == "python"

    def test_infer_language_javascript(self, diff_ui):
        """Test language inference untuk javascript files."""
        lang = diff_ui._infer_language("src/index.js")
        assert lang == "javascript"

    def test_infer_language_unknown(self, diff_ui):
        """Test language inference untuk unknown files."""
        lang = diff_ui._infer_language("src/unknown.xyz")
        assert lang == "text"


class TestDiffUIRendering:
    """Test rendering functions."""

    def test_render_header_with_changes(self, diff_ui):
        """Test header rendering dengan changes."""
        header = diff_ui.render_header(
            total_files=3,
            lines_added=15,
            lines_removed=8,
            status="Review changes",
        )
        assert header is not None

    def test_render_header_no_changes(self, diff_ui):
        """Test header rendering tanpa changes."""
        header = diff_ui.render_header(
            total_files=0,
            lines_added=0,
            lines_removed=0,
        )
        assert header is not None

    def test_render_file_list(self, diff_ui, sample_diffs):
        """Test file list rendering."""
        table, total_files = diff_ui.render_file_list(sample_diffs, selected_index=0)
        assert table is not None
        assert total_files == 3

    def test_render_file_list_with_selection(self, diff_ui, sample_diffs):
        """Test file list rendering dengan selection."""
        table, _ = diff_ui.render_file_list(sample_diffs, selected_index=1)
        assert table is not None

    def test_render_file_diff(self, diff_ui, sample_diffs):
        """Test file diff rendering."""
        metadata = sample_diffs["src/hello.py"]
        diff_view = diff_ui.render_file_diff(metadata)
        assert diff_view is not None

    def test_render_file_diff_binary(self, diff_ui):
        """Test diff rendering untuk binary files."""
        metadata = FileDiffMetadata(
            path="image.png",
            change_type=FileChangeType.MODIFIED,
            file_status=FileStatus.BINARY,
            lines_added=0,
            lines_removed=0,
        )
        diff_view = diff_ui.render_file_diff(metadata)
        assert diff_view is not None

    def test_render_file_diff_too_large(self, diff_ui):
        """Test diff rendering untuk file yang terlalu besar."""
        metadata = FileDiffMetadata(
            path="large_file.py",
            change_type=FileChangeType.MODIFIED,
            file_status=FileStatus.READABLE,
            lines_added=1000,
            lines_removed=500,
            unified_diff="\n".join([f"Line {i}" for i in range(200)]),
            diff_lines=[],
        )
        diff_view = diff_ui.render_file_diff(metadata, max_lines=50)
        assert diff_view is not None


class TestDiffUIIntegration:
    """Integration tests untuk DiffUI."""

    def test_display_diff_review_no_changes(self, diff_ui):
        """Test displaying review dengan no changes."""
        diff_ui.display_diff_review({})
        output = diff_ui.console.file.getvalue()
        assert "No changes detected" in output

    def test_display_diff_review_with_changes(self, diff_ui, sample_diffs):
        """Test displaying review dengan changes."""
        diff_ui.display_diff_review(sample_diffs, title="Test Changes")
        output = diff_ui.console.file.getvalue()
        assert "Test Changes" in output
        assert "Files" in output or len(output) > 0  # Minimal output check


class TestDiffTable:
    """Test DiffTable helper."""

    def test_create_summary_table(self, sample_diffs):
        """Test summary table creation."""
        table = DiffTable.create_summary_table(sample_diffs)
        assert table is not None
        assert table.title == "File Changes Summary"

    def test_summary_table_with_empty_diffs(self):
        """Test summary table dengan empty diffs."""
        table = DiffTable.create_summary_table({})
        assert table is not None


class TestDiffUIScenarios:
    """Real-world scenarios testing."""

    def test_scenario_new_python_file(self, diff_ui):
        """Scenario: adding new Python file."""
        metadata = FileDiffMetadata(
            path="src/utils/helper.py",
            change_type=FileChangeType.CREATED,
            file_status=FileStatus.READABLE,
            lines_added=50,
            lines_removed=0,
            total_lines_before=0,
            total_lines_after=50,
            unified_diff="""--- /dev/null
+++ b/src/utils/helper.py
@@ -0,0 +1,5 @@
+\"\"\"Helper utilities.\"\"\"
+
+def process_data(items):
+    return [x * 2 for x in items]
+
""",
            diff_lines=[
                {"type": "add", "line": '"""Helper utilities."""'},
                {"type": "add", "line": ""},
                {"type": "add", "line": "def process_data(items):"},
                {"type": "add", "line": "    return [x * 2 for x in items]"},
            ],
        )

        diffs = {"src/utils/helper.py": metadata}
        diff_ui.display_diff_review(diffs, title="Add Helper Module")

    def test_scenario_modified_config(self, diff_ui):
        """Scenario: modifying configuration file."""
        metadata = FileDiffMetadata(
            path="config/settings.json",
            change_type=FileChangeType.MODIFIED,
            file_status=FileStatus.READABLE,
            lines_added=2,
            lines_removed=1,
            total_lines_before=15,
            total_lines_after=16,
            unified_diff="""--- a/config/settings.json
+++ b/config/settings.json
@@ -8,7 +8,8 @@
   "timeout": 30,
-  "workers": 4
+  "workers": 8,
+  "debug": false
 }
""",
            diff_lines=[
                {"type": "del", "line": '  "workers": 4'},
                {"type": "add", "line": '  "workers": 8,'},
                {"type": "add", "line": '  "debug": false'},
            ],
        )

        diffs = {"config/settings.json": metadata}
        diff_ui.display_diff_review(diffs, title="Update Settings")

    def test_scenario_multiple_file_changes(self, diff_ui, sample_diffs):
        """Scenario: multiple file changes di single review."""
        diff_ui.display_diff_review(sample_diffs, title="Feature: Refactor Core")
