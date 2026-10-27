"""
Snapshot manager untuk diff review - menangani penyimpanan, pemulihan, dan deteksi konflik file.

Bertanggung jawab atas:
1. Menyimpan snapshot full file content sebelum agent bekerja
2. Memulihkan file ke state sebelumnya (reject changes)
3. Mendeteksi manual changes yang terjadi setelah snapshot awal
4. Mendeteksi konflik saat restore (file berubah sejak snapshot)
5. Safely handling file baru (deletion) dan file yang dihapus (recreation)
6. Tidak menggunakan git reset/checkout - manipulasi file langsung
"""

import os
import json
from pathlib import Path
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime
import hashlib
import shutil

from joy.diff_engine import FileStatus, FileChangeType


@dataclass
class SnapshotFileRecord:
    """Record untuk satu file dalam snapshot."""
    path: str
    exists: bool
    content: Optional[str]
    hash: Optional[str]
    mtime: Optional[float]
    size: int
    file_status: str  # FileStatus enum value


@dataclass
class ChangeManagementRecord:
    """Record perubahan dan tindakan yang diambil."""
    file_path: str
    change_type: str  # FileChangeType enum value
    original_status: str  # FileStatus value
    current_status: str
    snapshot_hash: Optional[str]
    current_hash: Optional[str]
    action: str  # "pending", "accepted", "rejected", "conflict"
    conflict_info: Optional[str] = None
    reason: Optional[str] = None
    timestamp: Optional[str] = None


class DiffSnapshotManager:
    """
    Mengelola snapshot file state dan restore/rollback operations.
    
    Snapshot disimpan secara in-memory dan optional ke disk untuk recovery.
    """
    
    def __init__(self, workspace_root: str, snapshot_dir: Optional[str] = None):
        self.workspace_root = Path(workspace_root).resolve()
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else None
        
        # In-memory snapshots: task_id -> {path -> SnapshotFileRecord}
        self._snapshots: Dict[str, Dict[str, SnapshotFileRecord]] = {}
        
        # Change management records: task_id -> [ChangeManagementRecord]
        self._change_records: Dict[str, List[ChangeManagementRecord]] = {}
        
        # Create snapshot directory jika diperlukan
        if self.snapshot_dir:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_file_hash(self, content: Optional[str]) -> Optional[str]:
        """SHA256 hash dari konten file."""
        if content is None:
            return None
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def create_snapshot(self, task_id: str, files: Dict[str, str]) -> bool:
        """
        Buat snapshot untuk task tertentu.
        
        Args:
            task_id: Identifier unik untuk task
            files: Dict[rel_path] -> file content (None jika tidak readable)
        
        Returns:
            True jika snapshot berhasil dibuat
        """
        snapshot = {}
        
        for rel_path, content in files.items():
            abs_path = self.workspace_root / rel_path
            
            try:
                exists = abs_path.exists()
                mtime = abs_path.stat().st_mtime if exists else None
                size = abs_path.stat().st_size if exists else 0
                
                # Determine file status
                if content is not None:
                    status = FileStatus.READABLE
                elif exists:
                    status = FileStatus.BINARY  # File exists but not readable
                else:
                    status = FileStatus.NOT_FOUND
                
                record = SnapshotFileRecord(
                    path=rel_path,
                    exists=exists,
                    content=content,
                    hash=self._get_file_hash(content),
                    mtime=mtime,
                    size=size,
                    file_status=status.value,
                )
                snapshot[rel_path] = record
                
            except (PermissionError, OSError) as e:
                # Log error tapi lanjut
                print(f"Warning: Could not snapshot {rel_path}: {e}")
                continue
        
        self._snapshots[task_id] = snapshot
        self._change_records[task_id] = []
        
        # Persist to disk if configured
        if self.snapshot_dir:
            self._persist_snapshot(task_id, snapshot)
        
        return bool(snapshot)
    
    def _persist_snapshot(self, task_id: str, snapshot: Dict[str, SnapshotFileRecord]) -> None:
        """Simpan snapshot ke disk untuk recovery."""
        try:
            snapshot_file = self.snapshot_dir / f"{task_id}.json"
            
            # Serialize snapshots
            data = {
                'task_id': task_id,
                'timestamp': datetime.now().isoformat(),
                'files': {
                    path: asdict(record)
                    for path, record in snapshot.items()
                }
            }
            
            snapshot_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"Warning: Could not persist snapshot {task_id}: {e}")
    
    def detect_manual_changes(self, task_id: str, current_files: Dict[str, str]) -> Dict[str, Tuple[str, str]]:
        """
        Deteksi perubahan manual yang terjadi setelah snapshot diambil.
        
        Args:
            task_id: Task identifier
            current_files: Dict[rel_path] -> current content
        
        Returns:
            Dict[path] -> (snapshot_hash, current_hash) untuk files yang berubah
        """
        if task_id not in self._snapshots:
            return {}
        
        snapshot = self._snapshots[task_id]
        manual_changes = {}
        
        for rel_path, snapshot_record in snapshot.items():
            current_content = current_files.get(rel_path)
            current_hash = self._get_file_hash(current_content)
            
            # Check apakah content berubah sejak snapshot
            if current_hash != snapshot_record.hash:
                manual_changes[rel_path] = (snapshot_record.hash, current_hash)
        
        return manual_changes
    
    def can_restore(self, task_id: str, file_path: str) -> Tuple[bool, Optional[str]]:
        """
        Check apakah file bisa di-restore tanpa konflik.
        
        Returns:
            (can_restore, conflict_reason) - reason is None jika bisa restore
        """
        if task_id not in self._snapshots:
            return False, "Snapshot not found"
        
        snapshot = self._snapshots[task_id]
        if file_path not in snapshot:
            return False, "File not in snapshot"
        
        snapshot_record = snapshot[file_path]
        abs_path = self.workspace_root / file_path
        
        # Check apakah file telah dimodifikasi secara manual sejak snapshot
        try:
            if abs_path.exists():
                current_content = abs_path.read_text(encoding='utf-8', errors='replace')
                current_hash = self._get_file_hash(current_content)
                
                if current_hash != snapshot_record.hash:
                    # File telah berubah - konflik
                    return False, f"File has manual changes since snapshot (conflict)"
        except Exception as e:
            return False, f"Could not read current file: {e}"
        
        return True, None
    
    def restore_file(self, task_id: str, file_path: str, create_backup: bool = True) -> Tuple[bool, Optional[str]]:
        """
        Restore file ke state snapshot (reject changes).
        
        Args:
            task_id: Task identifier
            file_path: Relative file path
            create_backup: Jika True, backup file current sebelum restore
        
        Returns:
            (success, error_message)
        """
        if task_id not in self._snapshots:
            return False, "Snapshot not found"
        
        snapshot = self._snapshots[task_id]
        if file_path not in snapshot:
            return False, "File not in snapshot"
        
        snapshot_record = snapshot[file_path]
        abs_path = self.workspace_root / file_path
        
        try:
            # Backup current file jika diminta
            if create_backup and abs_path.exists():
                backup_path = abs_path.parent / f"{abs_path.name}.backup"
                shutil.copy2(abs_path, backup_path)
            
            # Handle berbagai cases
            if not snapshot_record.exists:
                # File tidak ada di snapshot - hapus saja
                if abs_path.exists():
                    abs_path.unlink()
                
                # Record action
                self._record_change(
                    task_id, file_path,
                    FileChangeType.CREATED,
                    FileStatus.READABLE,
                    FileStatus.NOT_FOUND,
                    action="rejected"
                )
                return True, None
            
            else:
                # File ada di snapshot - restore dari snapshot
                if snapshot_record.content is not None:
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    abs_path.write_text(snapshot_record.content, encoding='utf-8')
                    
                    # Restore mtime jika tersedia
                    if snapshot_record.mtime:
                        os.utime(abs_path, (snapshot_record.mtime, snapshot_record.mtime))
                else:
                    # Snapshot record indicates file exists tapi tidak readable
                    # Best effort: jangan write
                    return False, f"Cannot restore binary/non-readable file: {file_path}"
                
                # Record action
                self._record_change(
                    task_id, file_path,
                    FileChangeType.MODIFIED,
                    FileStatus(snapshot_record.file_status),
                    FileStatus.READABLE,
                    action="rejected"
                )
                return True, None
        
        except Exception as e:
            return False, f"Restore failed: {e}"
    
    def mark_accepted(self, task_id: str, file_path: str) -> bool:
        """Tandai file sebagai accepted."""
        if task_id not in self._snapshots:
            return False
        
        self._record_change(
            task_id, file_path,
            FileChangeType.MODIFIED,
            FileStatus.READABLE,
            FileStatus.READABLE,
            action="accepted"
        )
        return True
    
    def _record_change(
        self,
        task_id: str,
        file_path: str,
        change_type: FileChangeType,
        original_status: FileStatus,
        current_status: FileStatus,
        action: str,
        conflict_info: Optional[str] = None,
    ) -> None:
        """Record change management action."""
        if task_id not in self._change_records:
            self._change_records[task_id] = []
        
        snapshot = self._snapshots.get(task_id, {})
        snapshot_record = snapshot.get(file_path)
        
        record = ChangeManagementRecord(
            file_path=file_path,
            change_type=change_type.value,
            original_status=original_status.value,
            current_status=current_status.value,
            snapshot_hash=snapshot_record.hash if snapshot_record else None,
            current_hash=None,  # Will be set later if needed
            action=action,
            conflict_info=conflict_info,
            timestamp=datetime.now().isoformat(),
        )
        
        self._change_records[task_id].append(record)
    
    def get_change_summary(self, task_id: str) -> Dict:
        """Get summary dari semua changes untuk task."""
        records = self._change_records.get(task_id, [])
        
        accepted = [r for r in records if r.action == "accepted"]
        rejected = [r for r in records if r.action == "rejected"]
        conflicts = [r for r in records if r.action == "conflict"]
        
        return {
            'total': len(records),
            'accepted': len(accepted),
            'rejected': len(rejected),
            'conflicts': len(conflicts),
            'records': records,
        }
    
    def cleanup_snapshot(self, task_id: str) -> None:
        """Clean up snapshot setelah task selesai."""
        self._snapshots.pop(task_id, None)
        self._change_records.pop(task_id, None)
        
        if self.snapshot_dir:
            snapshot_file = self.snapshot_dir / f"{task_id}.json"
            if snapshot_file.exists():
                snapshot_file.unlink()
