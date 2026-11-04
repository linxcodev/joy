"""Terminal interface for the Joy coding assistant."""

import asyncio
import queue
import re
import sys
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Always, Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import ConditionalContainer, Dimension, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text

from joy.llm import INTERRUPTED_NOTE, SYSTEM_PROMPT, LLMError, StreamWorker, load_config
from joy.diff_commands import DiffCommandHandler
from joy.diff_config import DiffConfig
from joy.session_manager import (CompactionMetadata, SessionManager, SessionRecovery,
                                 SessionSnapshot, TaskStatus)
from dataclasses import dataclass, field
from typing import Optional
import argparse
import json
from datetime import datetime, UTC
from pathlib import Path

GRAY = "#393939"
BORDER = "#5c5c5c"
PROMPT = "\u203a "
SPINNER = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
POLL_INTERVAL = 0.03


@dataclass
class DelegationEvent:
    """Represents a delegation event from the LLM worker."""
    task_id: str
    """Unique task identifier."""
    
    agent_name: str
    """Name of the delegated sub-agent."""
    
    status: str
    """Task status: queued, running, completed, failed, timeout, cancelled, blocked."""
    
    start_time: Optional[float] = None
    """Unix timestamp when task started."""
    
    end_time: Optional[float] = None
    """Unix timestamp when task ended."""
    
    result: Optional[dict] = None
    """Task result/output as dict."""
    
    error: Optional[str] = None
    """Error message if task failed."""
    
    @classmethod
    def from_payload(cls, payload: dict) -> "DelegationEvent":
        """Parse event payload into DelegationEvent."""
        return cls(
            task_id=payload.get("task_id", ""),
            agent_name=payload.get("agent_name", "sub_agent"),
            status=payload.get("status", "unknown"),
            start_time=payload.get("start_time"),
            end_time=payload.get("end_time"),
            result=payload.get("result"),
            error=payload.get("error"),
        )


def sanitize_error(error_str: str) -> str:
    """Remove sensitive information from error messages.
    
    Removes patterns like API keys, Bearer tokens, passwords, etc.
    """
    if not error_str:
        return error_str
    
    # Patterns to redact
    patterns = [
        (r'sk-[A-Za-z0-9\-_]{20,}', '[API_KEY_REDACTED]'),  # OpenAI format (min 20 chars)
        (r'Bearer [A-Za-z0-9\-_.]+', '[BEARER_TOKEN_REDACTED]'),
        (r'api_key\s*=\s*[\'"][^\'"]+[\'"]', 'api_key=[REDACTED]'),
        (r'password\s*=\s*[\'"][^\'"]+[\'"]', 'password=[REDACTED]'),
        (r'token\s*=\s*[\'"][^\'"]+[\'"]', 'token=[REDACTED]'),
    ]
    
    result = error_str
    for pattern, replacement in patterns:
        result = re.sub(pattern, replacement, result)
    
    return result


def build_session(state: dict | None = None) -> PromptSession:
    """Gray input area, with the Thinking indicator or streaming reply above it."""
    if state is None:
        state = {
            "waiting": False,
            "response": "",
            "delegations": {},      # {task_id: DelegationEvent}
            "active_tasks": set(),  # Set[str] of running task IDs
        }
    else:
        # Ensure new keys exist if state is provided
        state.setdefault("delegations", {})
        state.setdefault("active_tasks", set())
    
    waiting = Condition(lambda: bool(state.get("waiting")))
    bindings = KeyBindings()

    @bindings.add("enter")
    def submit(event: KeyPressEvent) -> None:
        # A pending reply owns the turn: Enter only submits after it finishes.
        if not state.get("waiting"):
            event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("c-c", filter=waiting)
    def cancel(event: KeyPressEvent) -> None:
        state["outcome"] = "cancelled"
        event.app.exit(result=event.current_buffer.text)

    def preview() -> list[tuple[str, str]]:
        response = state.get("response", "")
        if response:
            return [("class:response", response + "\n")]
        frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
        return [("class:thinking", f"{frame} Thinking\u2026\n")]

    def preview_cursor() -> Point:
        # Keep the end of the reply in view as it grows.
        lines = state.get("response", "").split("\n")
        return Point(x=len(lines[-1]), y=len(lines) - 1)

    def preview_height() -> Dimension | None:
        # Thinking plus a blank line; a reply sizes itself (wrapping included),
        # and HSplit shrinks it first so the input never leaves the screen.
        return None if state.get("response") else Dimension.exact(2)

    def line_prefix(lineno: int, wrap_count: int) -> list[tuple[str, str]]:
        return [("", " ")] if state.get("response") else []

    def tool_status() -> list[tuple[str, str]]:
        name, status = state.get("tool") or ("", "")
        style = "class:tool-failed" if status == "failed" else "class:tool"
        return [(style, f" \u2699 {name} \u00b7 {status}")]

    def delegation_status() -> list[tuple[str, str]]:
        """Display active delegations in format: ⧘ Analyzer [running] · Reviewer [queued]"""
        delegations = state.get("delegations", {})
        if not delegations:
            return []
        
        items = []
        for task_id, task_info in delegations.items():
            if isinstance(task_info, dict):
                name = task_info.get("agent_name", f"Task{task_id[:4]}")
                status = task_info.get("status", "unknown")
            else:  # DelegationEvent object
                name = task_info.agent_name
                status = task_info.status
            
            style = "class:delegation-failed" if status in ["failed", "timeout"] else "class:delegation"
            items.append((style, f"{name} [{status}]"))
        
        if items:
            # Combine items with separator
            result = [("class:delegation", " \u2317 ")]  # ⧘ symbol
            result.append(items[0])
            for item in items[1:]:
                result.append(("", " \u00b7 "))  # · separator
                result.append(item)
            return result
        return []

    session: PromptSession = PromptSession(
        erase_when_done=True,
        multiline=True,
        placeholder="Ask Joy to do something\u2026",
        prompt_continuation="  ",
        key_bindings=bindings,
        color_depth=ColorDepth.TRUE_COLOR,
        style=Style.from_dict({
            "frame.border": f"bg:{GRAY} {BORDER}",
            "placeholder": f"bg:{GRAY} #808080",
            "thinking": "#808080",
            "tool": "#808080",
            "tool-failed": "#d75f5f",
            "delegation": "#87ceeb",  # Sky blue for delegations
            "delegation-failed": "#d75f5f",  # Red for failed delegations
        }),
    )
    window = session.layout.current_window
    window.height = Dimension.exact(1)
    window.dont_extend_height = Always()
    window.style = f"bg:{GRAY} #ffffff"
    session.layout.container = HSplit([
        ConditionalContainer(
            Window(
                FormattedTextControl(preview, get_cursor_position=preview_cursor),
                height=preview_height,
                dont_extend_height=True,
                wrap_lines=True,
                get_line_prefix=line_prefix,
            ),
            filter=waiting,
        ),
        ConditionalContainer(
            Window(FormattedTextControl(tool_status), height=1, dont_extend_height=True),
            filter=waiting & Condition(lambda: bool(state.get("tool"))),
        ),
        ConditionalContainer(
            Window(FormattedTextControl(delegation_status), height=1, dont_extend_height=True),
            filter=waiting & Condition(lambda: bool(state.get("delegations"))),
        ),
        Frame(session.layout.container),
    ])
    return session


def print_reply(console: Console, text: str) -> None:
    if text.strip():
        console.print(Padding(Markdown(text), (0, 0, 0, 1)))


def stream_turn(session: PromptSession, state: dict, messages: list[dict], draft: str) -> str:
    """Stream one reply above the editable input. Returns the user's next draft.

    Sets state["outcome"] to "done", "error" or "cancelled", and
    state["used_tools"] when any tool ran.
    """
    worker = StreamWorker(messages)
    state.update(waiting=True, response="", outcome=None, error=None, tool=None,
                 used_tools=False, delegations={}, active_tasks=set(), notices=[],
                 compacted_history=None, compactions=0, tokens_saved=0)

    async def consume() -> None:
        while True:
            try:
                kind, payload = worker.events.get_nowait()
            except queue.Empty:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            if kind == "text":
                state["response"] += payload
                session.app.invalidate()
                continue
            if kind == "notice":
                state["notices"].append(payload)
                session.app.invalidate()
                continue
            if kind == "tool":
                state["tool"] = payload
                session.app.invalidate()
                continue
            if kind == "delegation":
                event = DelegationEvent.from_payload(payload)
                state["delegations"][event.task_id] = event
                if event.status in ("queued", "running"):
                    state["active_tasks"].add(event.task_id)
                else:
                    state["active_tasks"].discard(event.task_id)
                session.app.invalidate()
                continue
            state["outcome"] = kind
            state["error"] = payload
            state["waiting"] = False
            session.app.invalidate()
            session.app.exit(result=session.default_buffer.text)
            return

    try:
        return session.prompt(
            PROMPT,
            default=draft,
            refresh_interval=0.1,
            pre_run=lambda: session.app.create_background_task(consume()),
        )
    finally:
        state["waiting"] = False
        state["tool"] = None
        state["compacted_history"] = worker.history
        state["compactions"] = worker.compaction_count
        state["tokens_saved"] = worker.tokens_saved
        if state["outcome"] != "done":
            worker.cancel()  # stops further tools; late events die with this turn's queue
        state["used_tools"] = worker.used_tools


def setup_session_commands(parser: argparse.ArgumentParser) -> None:
    """Add session management subcommands to argument parser."""
    subparsers = parser.add_subparsers(dest='session_command', help='Session management commands')
    
    # resume subcommand
    resume_parser = subparsers.add_parser('resume', help='Resume a saved session')
    resume_parser.add_argument('session_id', nargs='?', help='Session ID to resume (optional, uses most recent if not specified)')
    resume_parser.add_argument('--model', help='Override model for resumed session')
    
    # list-sessions subcommand
    list_parser = subparsers.add_parser('list-sessions', help='List all saved sessions')
    list_parser.add_argument('--limit', type=int, default=10, help='Maximum sessions to display')
    list_parser.add_argument('--all', action='store_true', help='Show all sessions (ignore limit)')
    
    # delete-session subcommand
    delete_parser = subparsers.add_parser('delete-session', help='Delete a saved session')
    delete_parser.add_argument('session_id', help='Session ID to delete')
    delete_parser.add_argument('--force', action='store_true', help='Skip confirmation')
    
    # status subcommand
    status_parser = subparsers.add_parser('session-status', help='Show detailed session status')
    status_parser.add_argument('session_id', nargs='?', help='Session ID to inspect (optional, uses current if not specified)')
    
    # export-session subcommand
    export_parser = subparsers.add_parser('export-session', help='Export session to JSON file')
    export_parser.add_argument('session_id', help='Session ID to export')
    export_parser.add_argument('--output', '-o', help='Output file path (default: session_id.json)')
    
    # recovery subcommand
    recovery_parser = subparsers.add_parser('recover-session', help='Recover corrupted session')
    recovery_parser.add_argument('session_id', help='Session ID to recover')
    recovery_parser.add_argument('--strict', action='store_true', help='Use strict validation')


def handle_session_list(console: Console, args: argparse.Namespace) -> None:
    """Handle list-sessions command."""
    manager = SessionManager()
    sessions = manager.list_sessions()
    
    if not sessions:
        console.print("[yellow]No saved sessions found.[/yellow]")
        return
    
    limit = None if args.all else args.limit
    display_sessions = sessions[:limit] if limit else sessions
    
    console.print(f"\n[bold cyan]Saved Sessions ({len(display_sessions)}/{len(sessions)})[/bold cyan]\n")
    
    for session_info in display_sessions:
        session_id = session_info['session_id']
        updated_at = session_info['updated_at']
        
        # Try to get more details
        snapshot = manager.load_snapshot(session_id)
        if snapshot:
            turns = snapshot.turn_count
            state = snapshot.state.value if hasattr(snapshot.state, 'value') else snapshot.state
            compacted = "📦 compacted" if as_record(snapshot.compaction_metadata, CompactionMetadata).is_compacted else ""
            task = as_record(snapshot.task_status, TaskStatus).current_task or "(no current task)"
            console.print(f"  [cyan]{session_id}[/cyan]")
            console.print(f"    Updated: {updated_at}")
            console.print(f"    Turns: {turns} | State: {state} {compacted}")
            console.print(f"    Task: {task}")
        else:
            console.print(f"  [cyan]{session_id}[/cyan] - Updated: {updated_at}")
        console.print()


def as_record(data, cls):
    """A snapshot's stored dict (or dataclass) as `cls`, ignoring unknown keys."""
    if isinstance(data, cls):
        return data
    known = cls.__dataclass_fields__
    return cls(**{k: v for k, v in (data or {}).items() if k in known})


def handle_session_resume(console: Console, args: argparse.Namespace, session_manager: SessionManager) -> tuple[list[dict], str] | None:
    """Handle resume-session command. Returns (conversation, session_id) or None."""
    manager = SessionManager()
    
    # Determine which session to load
    if args.session_id:
        session_id = args.session_id
    else:
        # Use most recent session
        sessions = manager.list_sessions()
        if not sessions:
            console.print("[yellow]No saved sessions found. Starting new session.[/yellow]")
            return None
        session_id = sessions[0]['session_id']
        console.print(f"[dim]Resuming most recent session: {session_id}[/dim]")
    
    # Load snapshot
    snapshot = manager.load_snapshot(session_id)
    if not snapshot:
        console.print(f"[red]Failed to load session: {session_id}[/red]")
        return None
    
    # Validate/recover if needed
    recovery = SessionRecovery(manager)
    if not recovery.validate_snapshot(snapshot)[0]:
        console.print(f"[yellow]Session is corrupted. Attempting recovery...[/yellow]")
        snapshot = recovery.recover_session(session_id, strict=False)
        if not snapshot:
            console.print(f"[red]Failed to recover session: {session_id}[/red]")
            return None
        console.print(f"[green]Successfully recovered session.[/green]")
    
    console.print(f"\n[bold cyan]Resumed Session: {session_id}[/bold cyan]")
    console.print(f"  Model: {snapshot.model}")
    console.print(f"  Turns: {snapshot.turn_count}")
    console.print(f"  Goal: {snapshot.user_goal or '(unknown)'}")
    if as_record(snapshot.compaction_metadata, CompactionMetadata).is_compacted:
        console.print(f"  📦 Compacted ({as_record(snapshot.compaction_metadata, CompactionMetadata).compaction_count}x)")
    console.print()
    
    return (snapshot.conversation_history, session_id)


def handle_session_status(console: Console, args: argparse.Namespace) -> None:
    """Handle session-status command."""
    manager = SessionManager()
    
    if args.session_id:
        session_id = args.session_id
    else:
        session_id = manager.get_current_session()
        if not session_id:
            console.print("[yellow]No session specified and no current session active.[/yellow]")
            return
    
    snapshot = manager.load_snapshot(session_id)
    if not snapshot:
        console.print(f"[red]Session not found: {session_id}[/red]")
        return
    
    console.print(f"\n[bold cyan]Session Status: {session_id}[/bold cyan]\n")
    console.print(f"State:        {snapshot.state.value if hasattr(snapshot.state, 'value') else snapshot.state}")
    console.print(f"Model:        {snapshot.model}")
    console.print(f"Created:      {snapshot.created_at}")
    console.print(f"Updated:      {snapshot.updated_at}")
    console.print()
    
    console.print("[bold]Progress:[/bold]")
    console.print(f"  Turns:              {snapshot.turn_count}")
    console.print(f"  Iterations:         {snapshot.iteration_count}")
    console.print(f"  Tool Calls:         {snapshot.tool_call_count}")
    console.print()
    
    console.print("[bold]Goal & Task:[/bold]")
    console.print(f"  User Goal:          {snapshot.user_goal or '(unknown)'}")
    console.print(f"  Current Task:       {as_record(snapshot.task_status, TaskStatus).current_task or '(none)'}")
    if as_record(snapshot.task_status, TaskStatus).next_steps:
        console.print(f"  Next Steps:         {', '.join(as_record(snapshot.task_status, TaskStatus).next_steps)}")
    console.print()
    
    console.print("[bold]Compaction:[/bold]")
    meta = as_record(snapshot.compaction_metadata, CompactionMetadata)
    console.print(f"  Is Compacted:       {meta.is_compacted}")
    console.print(f"  Compaction Count:   {meta.compaction_count}")
    if meta.last_compaction_time:
        console.print(f"  Last Compaction:    {meta.last_compaction_time}")
    console.print(f"  Tokens Saved:       {meta.total_tokens_saved}")
    console.print()
    
    if snapshot.modified_files:
        console.print("[bold]Modified Files:[/bold]")
        for path, change_type in list(snapshot.modified_files.items())[:5]:
            console.print(f"  {path}: {change_type}")
        if len(snapshot.modified_files) > 5:
            console.print(f"  ... and {len(snapshot.modified_files) - 5} more")
        console.print()
    
    if snapshot.unresolved_errors:
        console.print("[bold]Unresolved Errors:[/bold]")
        for error in snapshot.unresolved_errors[:3]:
            console.print(f"  ⚠️  {error}")
        if len(snapshot.unresolved_errors) > 3:
            console.print(f"  ... and {len(snapshot.unresolved_errors) - 3} more")
        console.print()


def handle_session_delete(console: Console, args: argparse.Namespace) -> None:
    """Handle delete-session command."""
    manager = SessionManager()
    session_id = args.session_id
    
    # Confirm unless --force
    if not args.force:
        snapshot = manager.load_snapshot(session_id)
        if snapshot:
            console.print(f"\n[bold]Delete session: {session_id}?[/bold]")
            console.print(f"  Model:     {snapshot.model}")
            console.print(f"  Turns:     {snapshot.turn_count}")
            console.print(f"  Goal:      {snapshot.user_goal or '(unknown)'}")
            console.print()
            
            try:
                response = input("[yellow]Type 'yes' to confirm deletion: [/yellow]").strip().lower()
                if response != 'yes':
                    console.print("[dim]Cancelled.[/dim]")
                    return
            except KeyboardInterrupt:
                console.print("\n[dim]Cancelled.[/dim]")
                return
    
    if manager.delete_session(session_id):
        console.print(f"[green]✓ Deleted session: {session_id}[/green]")
    else:
        console.print(f"[red]Failed to delete session: {session_id}[/red]")


def handle_session_export(console: Console, args: argparse.Namespace) -> None:
    """Handle export-session command."""
    manager = SessionManager()
    session_id = args.session_id
    
    snapshot = manager.load_snapshot(session_id)
    if not snapshot:
        console.print(f"[red]Session not found: {session_id}[/red]")
        return
    
    # Determine output path
    output_path = args.output or f"{session_id}.json"
    
    try:
        # Convert snapshot to dict
        snapshot_dict = snapshot.to_dict()
        
        # Write to file
        Path(output_path).write_text(json.dumps(snapshot_dict, indent=2))
        
        file_size = Path(output_path).stat().st_size
        console.print(f"[green]✓ Exported session to: {output_path}[/green]")
        console.print(f"  Size: {file_size / 1024:.1f} KB")
    except Exception as e:
        console.print(f"[red]Failed to export session: {e}[/red]")


def handle_session_recovery(console: Console, args: argparse.Namespace) -> None:
    """Handle recover-session command."""
    manager = SessionManager()
    recovery = SessionRecovery(manager)
    session_id = args.session_id
    
    console.print(f"[bold]Attempting to recover session: {session_id}[/bold]\n")
    
    # Attempt recovery
    snapshot = recovery.recover_session(session_id, strict=args.strict)
    
    if snapshot:
        console.print(f"[green]✓ Successfully recovered session![/green]")
        console.print(f"  State: {snapshot.state.value if hasattr(snapshot.state, 'value') else snapshot.state}")
        console.print(f"  Turns: {snapshot.turn_count}")
        console.print(f"  Model: {snapshot.model}")
    else:
        console.print(f"[red]✗ Failed to recover session (strict mode: {args.strict})[/red]")
        console.print(f"  Try: joy recover-session {session_id}")


def handle_session_commands(console: Console, args: argparse.Namespace, session_manager: SessionManager | None = None) -> tuple[list[dict], str] | None:
    """Route session commands to appropriate handlers."""
    if not hasattr(args, 'session_command') or not args.session_command:
        return None
    
    command = args.session_command
    
    if command == 'resume':
        return handle_session_resume(console, args, session_manager or SessionManager())
    elif command == 'list-sessions':
        handle_session_list(console, args)
    elif command == 'delete-session':
        handle_session_delete(console, args)
    elif command == 'session-status':
        handle_session_status(console, args)
    elif command == 'export-session':
        handle_session_export(console, args)
    elif command == 'recover-session':
        handle_session_recovery(console, args)
    
    return None


def main(console: Console | None = None, args: argparse.Namespace | None = None) -> None:
    if console is None:
        console = Console(color_system="truecolor" if sys.stdout.isatty() else None)
    
    # Handle session commands if provided
    if args and hasattr(args, 'session_command') and args.session_command:
        session_manager = SessionManager()
        result = handle_session_commands(console, args, session_manager)
        
        if args.session_command == 'resume' and result:
            history, session_id = result
            console.print("[dim]Continuing from saved session state...[/dim]\n")
        elif args.session_command != 'resume':
            return  # Other commands just display info and exit
        else:
            return  # resume failed, exit gracefully
    else:
        # Start new session
        history = []
        session_id = None
    
    state: dict = {"waiting": False, "response": ""}
    session = build_session(state)
    
    # Initialize diff command handler
    diff_config = DiffConfig.from_env()
    diff_handler = DiffCommandHandler(".", console, diff_config)
    
    # Initialize session manager for auto-save
    session_manager = SessionManager()
    if not session_id:
        # Create new session
        try:
            config = load_config()
            model = config.get("model", "gpt-4")
        except:
            model = "gpt-4"
        
        session_id = session_manager.create_session(
            model=model,
            user_goal=None,
            system_prompt=SYSTEM_PROMPT,
            context_window_size=128000
        )

    console.print("[bold cyan]Joy[/bold cyan] \u2014 coding agent\n")
    console.print("Type /exit to exit. Type /diff to see changes. Ctrl-C cancels input; Ctrl-D exits.\n")
    console.print(f"[dim]Session: {session_id}[/dim]\n")
    
    try:
        load_config()
    except LLMError as exc:
        console.print(Text(str(exc), style="yellow"))
        console.print()
    console.print("[dim]Enter sends \u00b7 Alt+Enter adds a new line[/dim]")

    draft = ""
    turn_count = len(history) // 2  # Each turn is user + assistant message
    compaction_stats = {"count": 0, "tokens_saved": 0}
    resumed = session_manager.load_snapshot(session_id) if history else None
    if resumed:  # keep counting from the resumed session
        meta = as_record(resumed.compaction_metadata, CompactionMetadata)
        compaction_stats = {"count": meta.compaction_count, "tokens_saved": meta.total_tokens_saved}
    
    while True:
        try:
            message = session.prompt(PROMPT, default=draft).strip()
        except KeyboardInterrupt:
            draft = ""
            continue
        except EOFError:
            break
        draft = ""
        if message == "/exit":
            break
        if not message:
            continue
        
        # Handle /diff command
        if diff_handler.is_diff_command(message):
            if diff_handler.handle_diff_command(message):
                continue

        console.print(Padding(Text(PROMPT + message.replace("\n", "\n  ")),
                              (1, 1), style=f"#ffffff on {GRAY}"))
        console.print()
        
        # Capture initial state sebelum agent bekerja
        diff_handler.capture_initial_state()

        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history,
                    {"role": "user", "content": message}]
        try:
            draft = stream_turn(session, state, messages, draft)
        except EOFError:
            break
        reply = state["response"]
        for notice in state.get("notices", []):
            console.print(Text(notice, style="dim"))
        print_reply(console, reply)
        finished = state["outcome"] == "done" and reply.strip()
        
        # Detect changes setelah agent selesai
        if finished and state["used_tools"]:
            changes = diff_handler.detect_and_track_changes()
            
            # Auto display diff jika ada perubahan dan config enabled
            if changes and diff_handler.should_auto_display_diff(changes):
                diff_handler.display_auto_diff(changes)
        
        if finished and state["compacted_history"]:
            # The worker compacted this turn: its history already holds the
            # summary, the tool work since, and the reply, so later turns start small.
            history = state["compacted_history"]
            compaction_stats["count"] += state["compactions"]
            compaction_stats["tokens_saved"] += state["tokens_saved"]
        elif finished:
            history += [{"role": "user", "content": message},
                        {"role": "assistant", "content": reply}]
        elif state["outcome"] == "done":
            console.print(Text("The model returned an empty response.", style="red"))
        elif state["outcome"] == "cancelled":
            console.print("[dim]Response cancelled.[/dim]")
        else:
            console.print(Text(state["error"], style="red"))
        if not finished and state["used_tools"]:
            # Tools may already have changed files: keep the request so a retry checks them.
            history += [{"role": "user", "content": message},
                        {"role": "assistant", "content": INTERRUPTED_NOTE}]
            console.print(Text(INTERRUPTED_NOTE, style="dim"))
        console.print()
        state["response"] = ""
        
        # Auto-save session state
        if finished and session_manager and session_id:
            turn_count += 1
            
            # Get current model
            try:
                config = load_config()
                model = config.get("model", "gpt-4")
            except:
                model = "gpt-4"
            
            # Extract goal from first user message
            user_goal = None
            if history:
                for msg in history:
                    if msg.get("role") == "user":
                        user_goal = msg.get("content", "")[:100]  # First 100 chars
                        break
            
            # Create snapshot
            snapshot = SessionSnapshot(
                session_id=session_id,
                created_at=datetime.now(UTC).isoformat(),
                updated_at=datetime.now(UTC).isoformat(),
                state="active",
                conversation_history=history,
                preserved_context=None,
                system_prompt=SYSTEM_PROMPT,
                user_goal=user_goal,
                task_status={
                    "current_task": None,
                    "subtasks": [],
                    "completed_tasks": [],
                    "failed_tasks": [],
                    "next_steps": [],
                },
                compaction_metadata={
                    "compaction_count": compaction_stats["count"],
                    "is_compacted": compaction_stats["count"] > 0,
                    "total_tokens_saved": float(compaction_stats["tokens_saved"]),
                },
                model=model,
                context_window_size=128000,
                turn_count=turn_count,
                tool_call_count=1 if state.get("used_tools") else 0,
                iteration_count=turn_count,
                last_tool_call=None,
                modified_files=diff_handler.tracked_files if hasattr(diff_handler, 'tracked_files') else {},
                last_error=None,
                error_count=0,
            )
            
            session_manager.save_snapshot(snapshot)
        
        # Clear pending changes after displaying them
        if finished:
            diff_handler.clear_pending()


if __name__ == "__main__":
    main()
