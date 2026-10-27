"""Terminal interface for the Joy coding assistant."""

import asyncio
import queue
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

GRAY = "#393939"
BORDER = "#5c5c5c"
PROMPT = "\u203a "
SPINNER = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
POLL_INTERVAL = 0.03


def build_session(state: dict | None = None) -> PromptSession:
    """Gray input area, with the Thinking indicator or streaming reply above it."""
    state = {"waiting": False, "response": ""} if state is None else state
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
                 used_tools=False)

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
            if kind == "tool":
                state["tool"] = payload
                session.app.invalidate()
                continue
            state["outcome"] = kind
            state["error"] = payload
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
        if state["outcome"] != "done":
            worker.cancel()  # stops further tools; late events die with this turn's queue
        state["used_tools"] = worker.used_tools


def main(console: Console | None = None) -> None:
    if console is None:
        console = Console(color_system="truecolor" if sys.stdout.isatty() else None)
    state: dict = {"waiting": False, "response": ""}
    session = build_session(state)
    history: list[dict] = []
    
    # Initialize diff command handler
    diff_config = DiffConfig.from_env()
    diff_handler = DiffCommandHandler(".", console, diff_config)

    console.print("[bold cyan]Joy[/bold cyan] \u2014 coding agent\n")
    console.print("Type /exit to exit. Type /diff to see changes. Ctrl-C cancels input; Ctrl-D exits.\n")
    try:
        load_config()
    except LLMError as exc:
        console.print(Text(str(exc), style="yellow"))
        console.print()
    console.print("[dim]Enter sends \u00b7 Alt+Enter adds a new line[/dim]")

    draft = ""
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
        print_reply(console, reply)
        finished = state["outcome"] == "done" and reply.strip()
        
        # Detect changes setelah agent selesai
        if finished and state["used_tools"]:
            changes = diff_handler.detect_and_track_changes()
            
            # Auto display diff jika ada perubahan dan config enabled
            if changes and diff_handler.should_auto_display_diff(changes):
                diff_handler.display_auto_diff(changes)
        
        if finished:
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
        
        # Clear pending changes after displaying them
        if finished:
            diff_handler.clear_pending()


if __name__ == "__main__":
    main()
