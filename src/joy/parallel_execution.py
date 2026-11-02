"""Parallel sub-agent execution framework.

This module provides:
- Task abstraction with status tracking and result correlation
- TaskPool for managing concurrent sub-agent execution with limits
- FileAccessManager for preventing concurrent file edits
- Dependency resolution for task ordering
- Timeout, cancellation, and partial failure handling
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set
from collections import defaultdict


class TaskStatus(Enum):
    """Status of a delegated task."""
    QUEUED = "queued"          # Waiting to execute
    RUNNING = "running"        # Currently executing
    COMPLETED = "completed"    # Finished successfully
    FAILED = "failed"          # Execution failed
    TIMEOUT = "timeout"        # Exceeded time limit
    CANCELLED = "cancelled"    # Explicitly cancelled
    BLOCKED = "blocked"        # Waiting for dependency


@dataclass
class TaskConfig:
    """Configuration for a parallel task."""
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    task: str = ""
    name: Optional[str] = None  # Human-readable name for sub-agent
    system_instruction: Optional[str] = None
    allowed_tools: List[str] = field(default_factory=lambda: ["read", "write", "edit"])
    context: Optional[str] = None
    max_requests: int = 8
    max_tool_calls: int = 4
    timeout_seconds: Optional[float] = None
    depends_on: Set[str] = field(default_factory=set)  # Task IDs this depends on
    file_locks: Set[str] = field(default_factory=set)  # Files to exclusively lock


@dataclass
class TaskResult:
    """Result of task execution."""
    task_id: str
    status: TaskStatus
    output: str = ""
    error: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    total_requests: int = 0
    total_tool_calls: int = 0
    
    @property
    def execution_time(self) -> float:
        """Wall-clock execution time in seconds."""
        if self.start_time is None or self.end_time is None:
            return 0.0
        return self.end_time - self.start_time


class FileAccessManager:
    """Manage exclusive file access across concurrent tasks.
    
    Prevents multiple tasks from editing the same file simultaneously.
    Uses a per-file lock strategy (finer than global write lock).
    """
    
    def __init__(self):
        self._file_locks: Dict[str, threading.Lock] = {}
        self._owner: Dict[str, str] = {}  # file -> task_id
        self._lock = threading.Lock()
    
    def acquire_files(self, task_id: str, files: Set[str], timeout: float = 5.0) -> bool:
        """Acquire exclusive access to files. Returns True if successful."""
        deadline = time.time() + timeout
        acquired = set()
        
        try:
            while acquired < files:
                remaining = files - acquired
                with self._lock:
                    # Get or create locks for all files
                    for file in remaining:
                        if file not in self._file_locks:
                            self._file_locks[file] = threading.Lock()
                    
                    # Try to acquire in order (deterministic to prevent deadlock)
                    for file in sorted(remaining):
                        lock = self._file_locks[file]
                        if lock.acquire(blocking=False):
                            if self._owner.get(file) is None:
                                self._owner[file] = task_id
                                acquired.add(file)
                            else:
                                lock.release()
                        elif time.time() > deadline:
                            return False  # Timeout
                
                if acquired < files:
                    time.sleep(0.01)  # Backoff before retry
            
            return True
        except Exception:
            # Release any acquired locks on error
            self.release_files(task_id, acquired)
            return False
    
    def release_files(self, task_id: str, files: Set[str]) -> None:
        """Release previously acquired file locks."""
        with self._lock:
            for file in files:
                if self._owner.get(file) == task_id and file in self._file_locks:
                    self._owner.pop(file, None)
                    self._file_locks[file].release()
    
    def get_owner(self, file: str) -> Optional[str]:
        """Get task ID currently owning the file, or None."""
        with self._lock:
            return self._owner.get(file)


class TaskPool:
    """Manages concurrent execution of sub-agent tasks with limits.
    
    Features:
    - Bounded concurrency (max_concurrent)
    - Task queueing and dependency resolution
    - Timeout and cancellation support
    - File access management
    - Result tracking by task ID
    - Event callback for delegation status updates
    """
    
    def __init__(self, max_concurrent: int = 4, event_callback: Optional[Callable] = None):
        """Initialize task pool.
        
        Args:
            max_concurrent: Maximum number of running tasks (1-16, default 4)
            event_callback: Optional callback(event_type: str, payload: dict) for delegation events
        """
        if not 1 <= max_concurrent <= 16:
            raise ValueError("max_concurrent must be between 1 and 16")
        
        self.max_concurrent = max_concurrent
        self.event_callback = event_callback
        self._queue: List[TaskConfig] = []
        self._running: Dict[str, threading.Thread] = {}
        self._results: Dict[str, TaskResult] = {}
        self._status: Dict[str, TaskStatus] = {}
        self._cancelled: Set[str] = set()
        self._file_manager = FileAccessManager()
        self._lock = threading.Lock()
        self._task_done = threading.Condition(self._lock)
    
    def submit_task(self, config: TaskConfig) -> str:
        """Submit a task for execution. Returns task_id."""
        with self._lock:
            # Validate dependencies exist (if any)
            for dep_id in config.depends_on:
                if dep_id not in self._status and dep_id not in self._results:
                    raise ValueError(f"Dependency {dep_id} does not exist")
            
            self._queue.append(config)
            self._status[config.task_id] = TaskStatus.QUEUED
            self._task_done.notify()  # Wake scheduler
            return config.task_id
    
    def submit_tasks(self, configs: List[TaskConfig]) -> List[str]:
        """Submit multiple tasks. Returns list of task_ids."""
        return [self.submit_task(c) for c in configs]
    
    def cancel_task(self, task_id: str) -> bool:
        """Cancel a queued or running task. Returns True if cancelled."""
        with self._lock:
            if task_id in self._running:
                self._cancelled.add(task_id)
                self._status[task_id] = TaskStatus.CANCELLED
                return True
            elif any(c.task_id == task_id for c in self._queue):
                self._queue = [c for c in self._queue if c.task_id != task_id]
                self._status[task_id] = TaskStatus.CANCELLED
                # Store cancelled result
                self._results[task_id] = TaskResult(
                    task_id=task_id,
                    status=TaskStatus.CANCELLED,
                    output="",
                    error="Task cancelled before execution",
                    start_time=time.time(),
                    end_time=time.time(),
                    total_requests=0,
                    total_tool_calls=0,
                )
                return True
        return False
    
    def get_status(self, task_id: str) -> Optional[TaskStatus]:
        """Get current status of a task."""
        with self._lock:
            return self._status.get(task_id)
    
    def get_result(self, task_id: str) -> Optional[TaskResult]:
        """Get result if task completed. Returns None if still running."""
        with self._lock:
            return self._results.get(task_id)
    
    def get_all_results(self) -> Dict[str, TaskResult]:
        """Get all completed/failed task results."""
        with self._lock:
            return dict(self._results)
    
    def get_aggregated_result(self) -> "ParallelDelegationResult":
        """Get aggregated result from all completed tasks."""
        with self._lock:
            results = dict(self._results)
            
            if not results:
                return ParallelDelegationResult(task_results={})
            
            # Calculate aggregated metrics
            start_times = [r.start_time for r in results.values() if r.start_time]
            end_times = [r.end_time for r in results.values() if r.end_time]
            
            total_requests = sum(getattr(r, 'total_requests', 0) for r in results.values())
            total_tool_calls = sum(getattr(r, 'total_tool_calls', 0) for r in results.values())
            
            aggregated = ParallelDelegationResult(
                task_results=results,
                start_time=min(start_times) if start_times else None,
                end_time=max(end_times) if end_times else None,
                total_requests=total_requests,
                total_tool_calls=total_tool_calls,
            )
            if start_times:
                aggregated.start_time = min(start_times)
            if end_times:
                aggregated.end_time = max(end_times)
            
            return aggregated
    
    def wait_all(self, timeout: Optional[float] = None) -> bool:
        """Wait for all tasks to complete. Returns True if all done."""
        deadline = (time.time() + timeout) if timeout else None
        with self._lock:
            while any(self._status.get(tid) in (TaskStatus.QUEUED, TaskStatus.RUNNING)
                     for tid in self._status):
                remaining = deadline - time.time() if deadline else None
                if remaining is not None and remaining <= 0:
                    return False
                self._task_done.wait(timeout=remaining or 1.0)
            return True
    
    def execute(self, task_executor: Callable[[TaskConfig], TaskResult]) -> None:
        """Run the scheduler loop. Call this from a main thread.
        
        Args:
            task_executor: Function(TaskConfig) -> TaskResult that executes a task
        """
        while True:
            with self._lock:
                # Check for tasks to schedule
                if len(self._running) < self.max_concurrent and self._queue:
                    # Get next non-blocked task
                    next_config = None
                    for config in self._queue:
                        if self._can_run(config):
                            next_config = config
                            self._queue.remove(config)
                            break
                    
                    if next_config:
                        thread = threading.Thread(
                            target=self._run_task,
                            args=(next_config, task_executor),
                            daemon=False,
                            name=f"task-{next_config.task_id}"
                        )
                        self._running[next_config.task_id] = thread
                        self._status[next_config.task_id] = TaskStatus.RUNNING
                        thread.start()
                
                # Check if all done
                if not self._queue and not self._running:
                    return
                
                # Wait for a task to complete or be cancelled
                self._task_done.wait(timeout=0.5)
    
    def _emit_event(self, event_type: str, payload: dict) -> None:
        """Emit delegation event via callback if configured."""
        if self.event_callback:
            try:
                self.event_callback(event_type, payload)
            except Exception as e:
                # Don't crash if callback fails, just log internally
                pass
    
    def _can_run(self, config: TaskConfig) -> bool:
        """Check if task dependencies are satisfied."""
        for dep_id in config.depends_on:
            dep_result = self._results.get(dep_id)
            if dep_result is None or dep_result.status != TaskStatus.COMPLETED:
                return False
        return True
    
    def _run_task(self, config: TaskConfig, executor: Callable) -> None:
        """Execute a single task (runs in worker thread)."""
        start_time = time.time()
        result = None
        
        try:
            # Check if cancelled before starting
            with self._lock:
                if config.task_id in self._cancelled:
                    self._emit_event("delegation", {
                        "task_id": config.task_id,
                        "status": "cancelled",
                        "start_time": start_time,
                        "end_time": time.time(),
                    })
                    self._results[config.task_id] = TaskResult(
                        task_id=config.task_id,
                        status=TaskStatus.CANCELLED,
                        start_time=start_time,
                        end_time=time.time(),
                    )
                    self._running.pop(config.task_id, None)
                    self._task_done.notify_all()
                    return
            
            # Emit task started event
            self._emit_event("delegation", {
                "task_id": config.task_id,
                "status": "running",
                "start_time": start_time,
            })
            
            # Try to acquire file locks
            if config.file_locks:
                lock_timeout = (config.timeout_seconds or 30.0) / 2
                if not self._file_manager.acquire_files(config.task_id, config.file_locks, lock_timeout):
                    error_msg = f"Could not acquire locks for files: {config.file_locks}"
                    self._emit_event("delegation", {
                        "task_id": config.task_id,
                        "status": "failed",
                        "error": error_msg,
                        "start_time": start_time,
                        "end_time": time.time(),
                    })
                    with self._lock:
                        self._results[config.task_id] = TaskResult(
                            task_id=config.task_id,
                            status=TaskStatus.FAILED,
                            error=error_msg,
                            start_time=start_time,
                            end_time=time.time(),
                        )
                        self._running.pop(config.task_id, None)
                        self._task_done.notify_all()
                    return
            
            # Execute the task
            result = executor(config)
            
            # Ensure result has timing info
            if result.start_time is None:
                result.start_time = start_time
            if result.end_time is None:
                result.end_time = time.time()
            
            # Emit completion event
            self._emit_event("delegation", {
                "task_id": result.task_id,
                "status": result.status.value,
                "output": result.output[:500] if result.output else "",  # Truncate for event
                "error": result.error,
                "execution_time": result.execution_time,
                "start_time": result.start_time,
                "end_time": result.end_time,
            })
            
        except Exception as e:
            error_str = str(e)
            end_time = time.time()
            self._emit_event("delegation", {
                "task_id": config.task_id,
                "status": "failed",
                "error": f"Task execution error: {error_str}",
                "start_time": start_time,
                "end_time": end_time,
            })
            result = TaskResult(
                task_id=config.task_id,
                status=TaskStatus.FAILED,
                error=f"Task execution error: {error_str}",
                start_time=start_time,
                end_time=end_time,
            )
        
        finally:
            # Release file locks
            if config.file_locks:
                self._file_manager.release_files(config.task_id, config.file_locks)
            
            # Store result
            with self._lock:
                if result is not None:
                    self._results[config.task_id] = result
                self._running.pop(config.task_id, None)
                self._task_done.notify_all()


class ParallelDelegationResult:
    """Result from parallel task delegation."""
    
    def __init__(
        self,
        task_results: Optional[Dict[str, TaskResult]] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        total_requests: int = 0,
        total_tool_calls: int = 0,
    ):
        self.task_results: Dict[str, TaskResult] = task_results or {}
        self.start_time: Optional[float] = start_time
        self.end_time: Optional[float] = end_time
        self.total_requests: int = total_requests
        self.total_tool_calls: int = total_tool_calls
    
    @property
    def execution_time(self) -> float:
        """Total wall-clock time for all tasks."""
        if self.start_time is None or self.end_time is None:
            return 0.0
        return self.end_time - self.start_time
    
    @property
    def success_count(self) -> int:
        """Number of tasks that completed successfully."""
        return sum(1 for r in self.task_results.values()
                  if r.status == TaskStatus.COMPLETED)
    
    @property
    def failed_count(self) -> int:
        """Number of tasks that failed."""
        return sum(1 for r in self.task_results.values()
                  if r.status in (TaskStatus.FAILED, TaskStatus.TIMEOUT))
    
    @property
    def total_count(self) -> int:
        """Total number of tasks submitted."""
        return len(self.task_results)
    
    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "total_tasks": self.total_count,
            "succeeded": self.success_count,
            "failed": self.failed_count,
            "execution_time": self.execution_time,
            "total_requests": self.total_requests,
            "total_tool_calls": self.total_tool_calls,
            "tasks": {
                task_id: {
                    "status": result.status.value,
                    "output": result.output[:500],  # Truncate for summary
                    "error": result.error,
                    "execution_time": result.execution_time,
                }
                for task_id, result in self.task_results.items()
            }
        }
