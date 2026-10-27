"""
Diff engine untuk Joy - mengelola deteksi dan tracking perubahan file.

Module ini bertanggung jawab atas:
1. Capturing state awal sebelum operasi
2. Comparing state awal vs akhir
3. Generating structured diff metadata
4. Handling edge cases (binary, large, missing files)
5. Tracking agent-initiated changes

NOT bertanggung jawab atas:
- Git integration (optional, separate module)
- UI rendering (tanggung jawab CLI)
- Actual file modification (tanggung jawab tools.py)
"""

import hashlib
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from enum import Enum
import difflib
import io
import threading


class FileChangeType(Enum):
    """Tipe perubahan pada file."""
    CREATED = "created"      # File baru dibuat
    MODIFIED = "modified"    # File existing dimodifikasi
    DELETED = "deleted"      # File dihapus
    UNCHANGED = "unchanged"  # File tidak berubah


class FileStatus(Enum):
    """Status file saat diproccess."""
    READABLE = "readable"           # File bisa dibaca (text)
    BINARY = "binary"               # File binary, tidak dibaca
    NOT_FOUND = "not_found"         # File tidak ada
    UNREADABLE = "unreadable"       # File exists tapi tidak bisa dibaca
    TOO_LARGE = "too_large"         # File terlalu besar


@dataclass
class FileDiffMetadata:
    """Metadata untuk perubahan satu file."""
    path: str
    change_type: FileChangeType
    file_status: FileStatus
    
    # Untuk file readable
    lines_added: int = 0           # Jumlah baris ditambah
    lines_removed: int = 0         # Jumlah baris dihapus
    lines_modified: int = 0        # Jumlah baris diubah
    total_lines_before: int = 0    # Total baris sebelum
    total_lines_after: int = 0     # Total baris sesudah
    
    # Untuk deteksi manual changes
    file_hash_before: Optional[str] = None  # SHA256 konten awal
    file_hash_after: Optional[str] = None   # SHA256 konten akhir
    file_mtime_before: Optional[float] = None
    file_mtime_after: Optional[float] = None
    
    # Unified diff content (untuk debugging/rendering)
    unified_diff: str = ""
    
    # Raw line-by-line data
    diff_lines: List[Dict] = field(default_factory=list)  # [{'type': 'add'|'del'|'ctx', 'line': str, 'number': int}]


@dataclass
class DiffSnapshot:
    """
    Snapshot state files pada suatu waktu.
    Berguna untuk membandingkan before/after agent operations.
    """
    timestamp: float
    workspace_root: str
    file_snapshots: Dict[str, Dict] = field(default_factory=dict)  # path -> {content, hash, mtime, status, ...}
    
    def __repr__(self) -> str:
        num_files = len(self.file_snapshots)
        return f"DiffSnapshot({num_files} files @ {self.timestamp})"


class DiffEngine:
    """
    Engine untuk tracking dan comparing file changes dalam Joy workspace.
    
    Thread-safe untuk concurrent operations.
    """
    
    # Limits
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
    MAX_DIFF_LINES = 50000             # Max baris dalam diff output
    
    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).resolve()
        self._lock = threading.Lock()
        self._snapshots: Dict[str, DiffSnapshot] = {}
    
    def _get_file_hash(self, content: str) -> str:
        """SHA256 hash dari konten file."""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def _is_binary(self, content: bytes) -> bool:
        """Check apakah bytes merupakan binary file."""
        if b'\x00' in content[:8192]:  # Check first 8KB
            return True
        return False
    
    def _decode_file_safely(self, path: Path) -> Tuple[Optional[str], FileStatus]:
        """
        Coba decode file ke text dengan aman.
        
        Returns:
            (content, status) - content bisa None jika file tidak readable
        """
        try:
            if not path.exists():
                return None, FileStatus.NOT_FOUND
            
            if not path.is_file():
                return None, FileStatus.NOT_FOUND
            
            size = path.stat().st_size
            if size > self.MAX_FILE_SIZE:
                return None, FileStatus.TOO_LARGE
            
            # Try read as binary first untuk detect binary
            raw_bytes = path.read_bytes()
            if self._is_binary(raw_bytes):
                return None, FileStatus.BINARY
            
            # Try decode UTF-8
            try:
                content = raw_bytes.decode('utf-8')
                return content, FileStatus.READABLE
            except UnicodeDecodeError:
                # Try alternative encoding
                try:
                    content = raw_bytes.decode('latin-1')
                    return content, FileStatus.READABLE
                except:
                    return None, FileStatus.UNREADABLE
                    
        except (PermissionError, OSError):
            return None, FileStatus.UNREADABLE
    
    def _validate_path(self, rel_path: str) -> Path:
        """Validate bahwa path dalam workspace, return absolute path."""
        abs_path = (self.workspace_root / rel_path).resolve()
        
        # Security: ensure path tidak escape workspace
        if not str(abs_path).startswith(str(self.workspace_root)):
            raise ValueError(f"Path escape workspace: {rel_path}")
        
        return abs_path
    
    def _snapshot_file(self, path: Path) -> Dict:
        """Ambil snapshot single file."""
        rel_path = str(path.relative_to(self.workspace_root))
        content, status = self._decode_file_safely(path)
        
        snapshot = {
            'path': rel_path,
            'status': status,
            'mtime': path.stat().st_mtime if path.exists() else None,
            'content': content,
            'hash': self._get_file_hash(content) if content else None,
            'size': path.stat().st_size if path.exists() else 0,
        }
        return snapshot
    
    def capture_snapshot(self, rel_paths: Optional[List[str]] = None) -> DiffSnapshot:
        """
        Capture state dari file di workspace saat ini.
        
        Args:
            rel_paths: List relative paths untuk di-snapshot. 
                      Jika None, snapshot workspace root.
        
        Returns:
            DiffSnapshot object berisi state saat capture
        """
        import time
        
        with self._lock:
            snapshot = DiffSnapshot(
                timestamp=time.time(),
                workspace_root=str(self.workspace_root),
            )
            
            if rel_paths is None:
                # Snapshot semua text files di workspace
                for path in self.workspace_root.rglob('*'):
                    if path.is_file() and not self._should_ignore(path):
                        snapshot.file_snapshots[str(path.relative_to(self.workspace_root))] = \
                            self._snapshot_file(path)
            else:
                # Snapshot specified paths saja
                for rel_path in rel_paths:
                    abs_path = self._validate_path(rel_path)
                    if abs_path.exists():
                        snapshot.file_snapshots[rel_path] = self._snapshot_file(abs_path)
                    else:
                        # File doesn't exist (akan ditandai deleted)
                        snapshot.file_snapshots[rel_path] = {
                            'path': rel_path,
                            'status': FileStatus.NOT_FOUND,
                            'content': None,
                            'hash': None,
                            'mtime': None,
                            'size': 0,
                        }
            
            return snapshot
    
    def _should_ignore(self, path: Path) -> bool:
        """Check apakah file should be ignored dari snapshot."""
        # Ignore common patterns
        ignore_patterns = {
            '.git', '.venv', '__pycache__', '.pytest_cache',
            '.egg-info', 'dist', 'build', '.tox',
            '.mypy_cache', '.DS_Store', 'node_modules',
        }
        
        for part in path.parts:
            if part in ignore_patterns or part.startswith('.'):
                return True
        
        return False
    
    def compare_snapshots(
        self, 
        before: DiffSnapshot, 
        after: DiffSnapshot
    ) -> Dict[str, FileDiffMetadata]:
        """
        Compare dua snapshots dan generate diff metadata.
        
        Returns:
            Dict[relative_path] -> FileDiffMetadata
        """
        result: Dict[str, FileDiffMetadata] = {}
        
        all_paths: Set[str] = set(before.file_snapshots.keys()) | set(after.file_snapshots.keys())
        
        for rel_path in sorted(all_paths):
            before_snap = before.file_snapshots.get(rel_path)
            after_snap = after.file_snapshots.get(rel_path)
            
            metadata = self._diff_file_snapshots(rel_path, before_snap, after_snap)
            result[rel_path] = metadata
        
        return result
    
    def _diff_file_snapshots(
        self,
        rel_path: str,
        before: Optional[Dict],
        after: Optional[Dict]
    ) -> FileDiffMetadata:
        """Generate diff metadata untuk satu file."""
        
        # Determine change type
        before_exists = before is not None and before['status'] == FileStatus.READABLE
        after_exists = after is not None and after['status'] == FileStatus.READABLE
        
        if not before_exists and after_exists:
            change_type = FileChangeType.CREATED
        elif before_exists and not after_exists:
            change_type = FileChangeType.DELETED
        elif not before_exists and not after_exists:
            change_type = FileChangeType.UNCHANGED
        else:
            # Both exist, check if content differs
            before_hash = before.get('hash') if before else None
            after_hash = after.get('hash') if after else None
            
            if before_hash == after_hash:
                change_type = FileChangeType.UNCHANGED
            else:
                change_type = FileChangeType.MODIFIED
        
        # Determine file status (use after status if available)
        file_status = after['status'] if after else before['status']
        
        metadata = FileDiffMetadata(
            path=rel_path,
            change_type=change_type,
            file_status=file_status,
            file_hash_before=before.get('hash') if before else None,
            file_hash_after=after.get('hash') if after else None,
            file_mtime_before=before.get('mtime') if before else None,
            file_mtime_after=after.get('mtime') if after else None,
        )
        
        # Generate unified diff untuk readable files
        if before_exists and after_exists:
            metadata.unified_diff, metadata.diff_lines = self._generate_unified_diff(
                rel_path,
                before['content'],
                after['content']
            )
            
            # Count lines
            metadata.total_lines_before = len(before['content'].splitlines()) if before['content'] else 0
            metadata.total_lines_after = len(after['content'].splitlines()) if after['content'] else 0
            
            # Count changes
            for line_data in metadata.diff_lines:
                if line_data['type'] == 'add':
                    metadata.lines_added += 1
                elif line_data['type'] == 'del':
                    metadata.lines_removed += 1
                elif line_data['type'] == 'mod':
                    metadata.lines_modified += 1
        
        elif change_type == FileChangeType.CREATED and after_exists:
            metadata.total_lines_after = len(after['content'].splitlines()) if after['content'] else 0
            metadata.lines_added = metadata.total_lines_after
        
        elif change_type == FileChangeType.DELETED and before_exists:
            metadata.total_lines_before = len(before['content'].splitlines()) if before['content'] else 0
            metadata.lines_removed = metadata.total_lines_before
        
        return metadata
    
    def _generate_unified_diff(self, path: str, before: str, after: str) -> Tuple[str, List[Dict]]:
        """
        Generate unified diff format dan extract line-by-line data.
        
        Returns:
            (unified_diff_string, line_data_list)
        """
        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        
        # Generate unified diff
        diff_gen = difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm=''
        )
        
        diff_lines_list = list(diff_gen)
        unified_diff_str = '\n'.join(diff_lines_list)
        
        # Truncate jika terlalu besar
        if len(diff_lines_list) > self.MAX_DIFF_LINES:
            diff_lines_list = diff_lines_list[:self.MAX_DIFF_LINES]
            unified_diff_str = '\n'.join(diff_lines_list) + f"\n... diff truncated ({len(diff_lines_list)} of {len(list(difflib.unified_diff(before_lines, after_lines)))} lines shown)"
        
        # Parse diff untuk extract line-by-line data
        line_data = []
        diff_line_num = 0
        for line in diff_lines_list[2:]:  # Skip header (---, +++)
            if line.startswith('@@'):
                continue
            
            if line.startswith('+') and not line.startswith('+++'):
                line_data.append({
                    'type': 'add',
                    'line': line[1:].rstrip('\n'),
                    'number': diff_line_num
                })
            elif line.startswith('-') and not line.startswith('---'):
                line_data.append({
                    'type': 'del',
                    'line': line[1:].rstrip('\n'),
                    'number': diff_line_num
                })
            else:  # Context line atau header
                if not line.startswith('@@'):
                    line_data.append({
                        'type': 'ctx',
                        'line': line[1:].rstrip('\n') if len(line) > 0 else '',
                        'number': diff_line_num
                    })
            
            diff_line_num += 1
        
        return unified_diff_str, line_data
    
    def summary(self, diffs: Dict[str, FileDiffMetadata]) -> str:
        """Generate human-readable summary dari diffs."""
        created = [d for d in diffs.values() if d.change_type == FileChangeType.CREATED]
        modified = [d for d in diffs.values() if d.change_type == FileChangeType.MODIFIED]
        deleted = [d for d in diffs.values() if d.change_type == FileChangeType.DELETED]
        
        total_added = sum(d.lines_added for d in modified) + sum(d.lines_added for d in created)
        total_removed = sum(d.lines_removed for d in modified) + sum(d.lines_removed for d in deleted)
        
        lines = [
            f"📊 Diff Summary:",
            f"  Files created: {len(created)}",
            f"  Files modified: {len(modified)}",
            f"  Files deleted: {len(deleted)}",
            f"  Lines added: {total_added}",
            f"  Lines removed: {total_removed}",
        ]
        
        if created:
            lines.append(f"\n✨ Created:")
            for d in created[:5]:
                lines.append(f"  + {d.path}")
            if len(created) > 5:
                lines.append(f"  ... and {len(created)-5} more")
        
        if modified:
            lines.append(f"\n✏️  Modified:")
            for d in modified[:5]:
                lines.append(f"  ~ {d.path} ({d.lines_added}+/{d.lines_removed}-)")
            if len(modified) > 5:
                lines.append(f"  ... and {len(modified)-5} more")
        
        if deleted:
            lines.append(f"\n🗑️  Deleted:")
            for d in deleted[:5]:
                lines.append(f"  - {d.path}")
            if len(deleted) > 5:
                lines.append(f"  ... and {len(deleted)-5} more")
        
        return '\n'.join(lines)
    
    def to_dict(self, diffs: Dict[str, FileDiffMetadata]) -> Dict:
        """Convert diff metadata ke dictionary untuk serialization."""
        return {
            path: {
                **asdict(metadata),
                'change_type': metadata.change_type.value,
                'file_status': metadata.file_status.value,
            }
            for path, metadata in diffs.items()
        }
