"""
Unit tests untuk diff_engine.py

Test coverage:
- File creation detection
- File modification detection
- File deletion detection
- Binary file handling
- Large file handling
- Unreadable file handling
- Hash-based change detection
- Unified diff generation
- Snapshot capture dan compare
- Thread safety
- Path validation
"""

import pytest
import tempfile
import os
import threading
import time
from pathlib import Path

from src.joy.diff_engine import (
    DiffEngine, FileChangeType, FileStatus, FileDiffMetadata, DiffSnapshot
)


@pytest.fixture
def temp_workspace():
    """Create temporary workspace untuk testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        yield workspace


@pytest.fixture
def diff_engine(temp_workspace):
    """Create DiffEngine instance dengan temporary workspace."""
    return DiffEngine(str(temp_workspace))


class TestFileStatusDetection:
    """Test deteksi berbagai file status."""
    
    def test_readable_text_file(self, temp_workspace, diff_engine):
        """Test deteksi file text yang readable."""
        test_file = temp_workspace / "test.py"
        test_file.write_text("print('hello')")
        
        content, status = diff_engine._decode_file_safely(test_file)
        assert status == FileStatus.READABLE
        assert content == "print('hello')"
    
    def test_binary_file(self, temp_workspace, diff_engine):
        """Test deteksi file binary."""
        test_file = temp_workspace / "test.bin"
        test_file.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR')
        
        content, status = diff_engine._decode_file_safely(test_file)
        assert status == FileStatus.BINARY
        assert content is None
    
    def test_nonexistent_file(self, temp_workspace, diff_engine):
        """Test deteksi file yang tidak ada."""
        test_file = temp_workspace / "nonexistent.txt"
        content, status = diff_engine._decode_file_safely(test_file)
        assert status == FileStatus.NOT_FOUND
        assert content is None
    
    def test_too_large_file(self, temp_workspace, diff_engine, monkeypatch):
        """Test deteksi file yang terlalu besar."""
        # Mock MAX_FILE_SIZE to small value
        monkeypatch.setattr(diff_engine, 'MAX_FILE_SIZE', 100)
        
        test_file = temp_workspace / "large.txt"
        test_file.write_text("x" * 1000)
        
        content, status = diff_engine._decode_file_safely(test_file)
        assert status == FileStatus.TOO_LARGE
        assert content is None
    
    def test_latin1_file(self, temp_workspace, diff_engine):
        """Test deteksi file dengan encoding latin-1."""
        test_file = temp_workspace / "latin1.txt"
        # Write some latin-1 specific chars
        test_file.write_bytes(b'Caf\xe9')  # Café in latin-1
        
        content, status = diff_engine._decode_file_safely(test_file)
        assert status == FileStatus.READABLE
        assert "Caf" in content  # Should successfully decode


class TestFileChangeDetection:
    """Test deteksi berbagai tipe perubahan file."""
    
    def test_file_creation(self, temp_workspace, diff_engine):
        """Test deteksi file creation."""
        # Snapshot before
        snap_before = diff_engine.capture_snapshot()
        
        # Create file
        new_file = temp_workspace / "new.py"
        new_file.write_text("print('new')")
        
        # Snapshot after
        snap_after = diff_engine.capture_snapshot()
        
        diffs = diff_engine.compare_snapshots(snap_before, snap_after)
        
        assert "new.py" in diffs
        assert diffs["new.py"].change_type == FileChangeType.CREATED
        assert diffs["new.py"].lines_added == 1
        assert diffs["new.py"].total_lines_after == 1
    
    def test_file_modification(self, temp_workspace, diff_engine):
        """Test deteksi file modification."""
        test_file = temp_workspace / "test.py"
        test_file.write_text("line1\nline2\nline3\n")
        
        snap_before = diff_engine.capture_snapshot(["test.py"])
        
        test_file.write_text("line1\nline2_modified\nline3\nline4\n")
        
        snap_after = diff_engine.capture_snapshot(["test.py"])
        
        diffs = diff_engine.compare_snapshots(snap_before, snap_after)
        
        assert "test.py" in diffs
        assert diffs["test.py"].change_type == FileChangeType.MODIFIED
        assert diffs["test.py"].lines_added == 2  # line2_modified + line4
        assert diffs["test.py"].lines_removed == 1  # line2 replaced
        assert diffs["test.py"].total_lines_before == 3
        assert diffs["test.py"].total_lines_after == 4
    
    def test_file_deletion(self, temp_workspace, diff_engine):
        """Test deteksi file deletion."""
        test_file = temp_workspace / "test.py"
        test_file.write_text("content\n")
        
        snap_before = diff_engine.capture_snapshot(["test.py"])
        
        test_file.unlink()
        
        snap_after = diff_engine.capture_snapshot(["test.py"])
        
        diffs = diff_engine.compare_snapshots(snap_before, snap_after)
        
        assert "test.py" in diffs
        assert diffs["test.py"].change_type == FileChangeType.DELETED
        assert diffs["test.py"].lines_removed == 1
    
    def test_file_unchanged(self, temp_workspace, diff_engine):
        """Test deteksi file yang tidak berubah."""
        test_file = temp_workspace / "test.py"
        test_file.write_text("unchanged content\n")
        
        snap_before = diff_engine.capture_snapshot(["test.py"])
        time.sleep(0.1)  # Ensure different timestamps
        snap_after = diff_engine.capture_snapshot(["test.py"])
        
        diffs = diff_engine.compare_snapshots(snap_before, snap_after)
        
        assert "test.py" in diffs
        assert diffs["test.py"].change_type == FileChangeType.UNCHANGED
        assert diffs["test.py"].lines_added == 0
        assert diffs["test.py"].lines_removed == 0


class TestMultipleFileChanges:
    """Test handling multiple file changes bersamaan."""
    
    def test_mixed_operations(self, temp_workspace, diff_engine):
        """Test mix of create, modify, delete operations."""
        # Setup initial files
        file1 = temp_workspace / "file1.py"
        file2 = temp_workspace / "file2.py"
        file3 = temp_workspace / "file3.py"
        
        file1.write_text("original1\n")
        file2.write_text("original2\n")
        file3.write_text("original3\n")
        
        snap_before = diff_engine.capture_snapshot()
        
        # Modify file1
        file1.write_text("original1\nmodified\n")
        
        # Delete file2
        file2.unlink()
        
        # Create file4
        file4 = temp_workspace / "file4.py"
        file4.write_text("new4\n")
        
        snap_after = diff_engine.capture_snapshot()
        
        diffs = diff_engine.compare_snapshots(snap_before, snap_after)
        
        # Check results
        assert diffs["file1.py"].change_type == FileChangeType.MODIFIED
        assert diffs["file2.py"].change_type == FileChangeType.DELETED
        assert diffs["file4.py"].change_type == FileChangeType.CREATED
        assert diffs["file3.py"].change_type == FileChangeType.UNCHANGED
        
        # Check summary
        summary = diff_engine.summary(diffs)
        assert "modified: 1" in summary.lower()
        assert "deleted: 1" in summary.lower()
        assert "created: 1" in summary.lower()


class TestUnifiedDiffGeneration:
    """Test unified diff generation accuracy."""
    
    def test_simple_modification(self, diff_engine):
        """Test simple line modification."""
        before = "line1\nline2\nline3\n"
        after = "line1\nline2_modified\nline3\n"
        
        diff_str, line_data = diff_engine._generate_unified_diff("test.py", before, after)
        
        assert "--- a/test.py" in diff_str
        assert "+++ b/test.py" in diff_str
        assert "line2_modified" in diff_str
        
        # Check line data parsing
        add_lines = [l for l in line_data if l['type'] == 'add']
        del_lines = [l for l in line_data if l['type'] == 'del']
        
        assert len(add_lines) > 0
        assert len(del_lines) > 0
    
    def test_multiline_addition(self, diff_engine):
        """Test adding multiple lines."""
        before = "line1\nline2\n"
        after = "line1\nline2\nnew1\nnew2\nnew3\n"
        
        diff_str, line_data = diff_engine._generate_unified_diff("test.py", before, after)
        
        add_lines = [l for l in line_data if l['type'] == 'add']
        assert len(add_lines) == 3
        assert any("new1" in l['line'] for l in add_lines)
    
    def test_multiline_deletion(self, diff_engine):
        """Test deleting multiple lines."""
        before = "line1\nold1\nold2\nold3\nline4\n"
        after = "line1\nline4\n"
        
        diff_str, line_data = diff_engine._generate_unified_diff("test.py", before, after)
        
        del_lines = [l for l in line_data if l['type'] == 'del']
        assert len(del_lines) == 3
        assert any("old1" in l['line'] for l in del_lines)


class TestHashBasedDetection:
    """Test hash-based change detection untuk manual vs agent changes."""
    
    def test_hash_unchanged_detection(self, temp_workspace, diff_engine):
        """Test bahwa hash yang sama berarti file unchanged."""
        test_file = temp_workspace / "test.py"
        original_content = "def foo():\n    return 42\n"
        test_file.write_text(original_content)
        
        snap1 = diff_engine.capture_snapshot(["test.py"])
        hash1 = snap1.file_snapshots["test.py"]['hash']
        
        # Modify and restore
        test_file.write_text("def bar():\n    return 0\n")
        test_file.write_text(original_content)
        
        snap2 = diff_engine.capture_snapshot(["test.py"])
        hash2 = snap2.file_snapshots["test.py"]['hash']
        
        assert hash1 == hash2
        
        diffs = diff_engine.compare_snapshots(snap1, snap2)
        assert diffs["test.py"].change_type == FileChangeType.UNCHANGED
    
    def test_hash_different_detection(self, temp_workspace, diff_engine):
        """Test bahwa hash berbeda berarti file modified."""
        test_file = temp_workspace / "test.py"
        test_file.write_text("original\n")
        
        snap1 = diff_engine.capture_snapshot(["test.py"])
        hash1 = snap1.file_snapshots["test.py"]['hash']
        
        test_file.write_text("modified\n")
        
        snap2 = diff_engine.capture_snapshot(["test.py"])
        hash2 = snap2.file_snapshots["test.py"]['hash']
        
        assert hash1 != hash2
        
        diffs = diff_engine.compare_snapshots(snap1, snap2)
        assert diffs["test.py"].change_type == FileChangeType.MODIFIED


class TestEdgeCases:
    """Test edge cases dan corner scenarios."""
    
    def test_empty_file_creation(self, temp_workspace, diff_engine):
        """Test creating empty file."""
        snap_before = diff_engine.capture_snapshot()
        
        empty_file = temp_workspace / "empty.py"
        empty_file.write_text("")
        
        snap_after = diff_engine.capture_snapshot()
        
        diffs = diff_engine.compare_snapshots(snap_before, snap_after)
        
        assert diffs["empty.py"].change_type == FileChangeType.CREATED
        assert diffs["empty.py"].lines_added == 0
    
    def test_file_with_no_final_newline(self, diff_engine):
        """Test file tanpa trailing newline."""
        before = "line1\nline2"
        after = "line1\nline2\nline3"
        
        diff_str, line_data = diff_engine._generate_unified_diff("test.py", before, after)
        
        add_lines = [l for l in line_data if l['type'] == 'add']
        assert len(add_lines) > 0
    
    def test_file_with_special_characters(self, temp_workspace, diff_engine):
        """Test file dengan special characters."""
        test_file = temp_workspace / "special.py"
        test_file.write_text("# -*- coding: utf-8 -*-\nprint('中文')\n")
        
        snap_before = diff_engine.capture_snapshot(["special.py"])
        
        test_file.write_text("# -*- coding: utf-8 -*-\nprint('中文')\nprint('日本語')\n")
        
        snap_after = diff_engine.capture_snapshot(["special.py"])
        
        diffs = diff_engine.compare_snapshots(snap_before, snap_after)
        
        assert diffs["special.py"].change_type == FileChangeType.MODIFIED
        assert diffs["special.py"].lines_added == 1
    
    def test_very_large_diff_truncation(self, diff_engine, monkeypatch):
        """Test bahwa very large diffs di-truncate dengan graceful."""
        monkeypatch.setattr(diff_engine, 'MAX_DIFF_LINES', 10)
        
        # Create large before/after
        before = "\n".join(f"line{i}" for i in range(1000))
        after = "\n".join(f"line{i}" for i in range(100, 1100))
        
        diff_str, line_data = diff_engine._generate_unified_diff("large.py", before, after)
        
        assert "truncated" in diff_str.lower()
        assert len(line_data) <= 100  # Should be truncated
    
    def test_path_traversal_protection(self, temp_workspace, diff_engine):
        """Test bahwa path traversal attacks dicegah."""
        with pytest.raises(ValueError):
            diff_engine._validate_path("../../etc/passwd")
        
        with pytest.raises(ValueError):
            diff_engine._validate_path("/etc/passwd")


class TestThreadSafety:
    """Test thread safety dari DiffEngine."""
    
    def test_concurrent_snapshots(self, temp_workspace, diff_engine):
        """Test concurrent snapshot captures."""
        test_file = temp_workspace / "test.py"
        test_file.write_text("initial\n")
        
        results = []
        errors = []
        
        def capture_and_record(file_index):
            try:
                # Modify file slightly
                test_file.write_text(f"version{file_index}\n")
                snap = diff_engine.capture_snapshot(["test.py"])
                results.append(snap)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=capture_and_record, args=(i,))
            for i in range(5)
        ]
        
        for t in threads:
            t.start()
        
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Errors occurred: {errors}"
        assert len(results) == 5


class TestSnapshotMetadata:
    """Test snapshot metadata accuracy."""
    
    def test_snapshot_includes_mtime(self, temp_workspace, diff_engine):
        """Test bahwa snapshot captures file mtime."""
        test_file = temp_workspace / "test.py"
        test_file.write_text("content")
        
        snap = diff_engine.capture_snapshot(["test.py"])
        
        assert snap.file_snapshots["test.py"]['mtime'] is not None
        assert snap.file_snapshots["test.py"]['mtime'] > 0
    
    def test_snapshot_includes_hash(self, temp_workspace, diff_engine):
        """Test bahwa snapshot captures file hash."""
        test_file = temp_workspace / "test.py"
        test_file.write_text("content")
        
        snap = diff_engine.capture_snapshot(["test.py"])
        
        file_snap = snap.file_snapshots["test.py"]
        assert file_snap['hash'] is not None
        assert len(file_snap['hash']) == 64  # SHA256 hex length
    
    def test_snapshot_timestamp(self, temp_workspace, diff_engine):
        """Test bahwa snapshot memiliki timestamp."""
        snap = diff_engine.capture_snapshot()
        
        assert snap.timestamp > 0
        assert snap.workspace_root == str(temp_workspace)


class TestSummaryGeneration:
    """Test diff summary generation."""
    
    def test_summary_format(self, temp_workspace, diff_engine):
        """Test bahwa summary memiliki format yang benar."""
        # Setup files
        file1 = temp_workspace / "file1.py"
        file1.write_text("original\n")
        
        snap_before = diff_engine.capture_snapshot()
        
        file1.write_text("modified\n")
        file2 = temp_workspace / "file2.py"
        file2.write_text("new\n")
        
        snap_after = diff_engine.capture_snapshot()
        
        diffs = diff_engine.compare_snapshots(snap_before, snap_after)
        summary = diff_engine.summary(diffs)
        
        assert "Diff Summary" in summary
        assert "modified" in summary.lower()
        assert "created" in summary.lower()


class TestEditorIntegration:
    """Test integration patterns for editor/CLI usage."""
    
    def test_snapshot_after_agent_operation(self, temp_workspace, diff_engine):
        """
        Test real-world scenario:
        1. Capture before
        2. Agent modifies file (via tools.edit)
        3. Capture after
        4. Compare
        """
        # Setup
        test_file = temp_workspace / "main.py"
        test_file.write_text("def hello():\n    print('old')\n")
        
        snap_before = diff_engine.capture_snapshot(["main.py"])
        
        # Simulate agent edit (in reality done by tools.edit)
        test_file.write_text("def hello():\n    print('new')\n\ndef world():\n    pass\n")
        
        snap_after = diff_engine.capture_snapshot(["main.py"])
        
        diffs = diff_engine.compare_snapshots(snap_before, snap_after)
        metadata = diffs["main.py"]
        
        assert metadata.change_type == FileChangeType.MODIFIED
        assert metadata.lines_added > 0
        assert metadata.unified_diff  # Should have diff content
        assert len(metadata.diff_lines) > 0  # Should have structured diff data
    
    def test_multiple_file_project(self, temp_workspace, diff_engine):
        """Test realistic multi-file project scenario."""
        # Setup project structure
        src_dir = temp_workspace / "src"
        src_dir.mkdir()
        
        (src_dir / "main.py").write_text("# Main module\nprint('start')\n")
        (src_dir / "utils.py").write_text("def helper():\n    pass\n")
        (temp_workspace / "README.md").write_text("# Project\n")
        
        snap_before = diff_engine.capture_snapshot()
        
        # Agent makes multiple changes
        (src_dir / "main.py").write_text("# Main module\nimport sys\nprint('start')\nprint('end')\n")
        (src_dir / "config.py").write_text("CONFIG = {}\n")  # New file
        (temp_workspace / "README.md").unlink()  # Delete file
        
        snap_after = diff_engine.capture_snapshot()
        
        diffs = diff_engine.compare_snapshots(snap_before, snap_after)
        
        # Verify changes
        assert len([d for d in diffs.values() if d.change_type == FileChangeType.MODIFIED]) >= 1
        assert len([d for d in diffs.values() if d.change_type == FileChangeType.CREATED]) >= 1
        assert len([d for d in diffs.values() if d.change_type == FileChangeType.DELETED]) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
