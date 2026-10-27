"""Joy's coding tools: read, write, edit and bash, registered with aisuite's Tools.

File paths are resolved against the launch directory (ROOT) and may not escape
it, including through symlinks. bash is NOT confined by these checks: it runs
with the user's full permissions in ROOT.
"""

import difflib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from contextlib import suppress
from pathlib import Path

from aisuite.utils.tools import Tools

ROOT = Path.cwd().resolve()
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_CHARS = 20_000
MAX_READ_LINES = 2000
BOM = b"\xef\xbb\xbf"

_write_lock = threading.Lock()  # serializes file mutations
_local = threading.local()      # per-thread cancel event for bash


class ToolError(Exception):
    """A tool failure reported back to the model."""


# --- workspace helpers -------------------------------------------------------

def _resolve(path: str) -> Path:
    if not path or "\0" in path:
        raise ToolError("Path must be a non-empty string.")
    root = ROOT.resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()  # follows symlinks, so escaping links are caught
    if resolved != root and root not in resolved.parents:
        raise ToolError(f"Path is outside the workspace ({root}): {path}")
    return resolved


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT.resolve())) or "."
    except ValueError:
        return str(path)


def _read_bytes(path: Path) -> bytes:
    if not path.exists():
        raise ToolError(f"File not found: {_display(path)}")
    if not path.is_file():
        raise ToolError(f"Not a regular file: {_display(path)}")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ToolError(f"File is too large ({size} bytes; limit is {MAX_FILE_BYTES}).")
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(f"File is too large (limit is {MAX_FILE_BYTES} bytes).")
    return data


def _decode(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"File is not valid UTF-8: {_display(path)}") from None


def _atomic_write(path: Path, data: bytes, expected: bytes | None = None) -> None:
    """Write via a temp file + os.replace, keeping permission bits.

    With `expected`, refuse to save if the file changed since it was read.
    """
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(f"Content is too large ({len(data)} bytes; limit is {MAX_FILE_BYTES}).")
    if path.is_dir():
        raise ToolError(f"Path is a directory: {_display(path)}")
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
    else:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        if expected is not None and (not path.is_file() or path.read_bytes() != expected):
            raise ToolError("File changed on disk since it was read; read it again and retry.")
        os.replace(tmp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[output truncated: showing first {limit} of {len(text)} characters]"


# --- edit matching (adapted from Pi's edit-diff.ts) ---------------------------

_SINGLE_QUOTES = re.compile("[\u2018\u2019\u201a\u201b]")
_DOUBLE_QUOTES = re.compile("[\u201c\u201d\u201e\u201f]")
_DASHES = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
_SPACES = re.compile("[\u00a0\u2002-\u200a\u202f\u205f\u3000]")


def _normalize_line(line: str) -> str:
    """NFKC, strip trailing whitespace, and fold smart quotes, dashes and spaces."""
    line = unicodedata.normalize("NFKC", line).rstrip()
    line = _SINGLE_QUOTES.sub("'", line)
    line = _DOUBLE_QUOTES.sub('"', line)
    line = _DASHES.sub("-", line)
    return _SPACES.sub(" ", line)


def _normalize(text: str) -> str:
    return "\n".join(_normalize_line(line) for line in text.split("\n"))


def _find_all(haystack: str, needle: str) -> list[int]:
    """Start offsets of every (possibly overlapping) occurrence."""
    found, start = [], haystack.find(needle)
    while start != -1:
        found.append(start)
        start = haystack.find(needle, start + 1)
    return found


def _uses_crlf(text: str) -> bool:
    """Like Pi: the file's style is decided by its first line break."""
    first = text.find("\n")
    return first > 0 and text[first - 1] == "\r"


def apply_edit(content: str, old_text: str, new_text: str) -> str:
    """Replace exactly one occurrence of old_text in LF-normalized content."""
    if not old_text.strip():
        raise ToolError("old_text must not be empty or whitespace-only.")
    exact = _find_all(content, old_text)
    norm_content, norm_old = _normalize(content), _normalize(old_text)
    if not norm_old.strip():
        raise ToolError("old_text must not be empty or whitespace-only.")
    fuzzy = _find_all(norm_content, norm_old)
    if len(exact) > 1 or len(fuzzy) > 1:
        count = max(len(exact), len(fuzzy))
        raise ToolError(f"old_text matches {count} places (counting Unicode/whitespace "
                        "variants). Include more surrounding context to make it unique.")
    if exact:
        start = exact[0]
        result = content[:start] + new_text + content[start + len(old_text):]
    elif fuzzy:
        result = _fuzzy_replace(content, norm_content, fuzzy[0], len(norm_old), new_text)
    else:
        raise ToolError("old_text was not found. It must match the file text, including "
                        "newlines and indentation; read the file again.")
    if result == content:
        raise ToolError("The replacement makes no change.")
    return result


def _line_bounds(text: str, first: int, last: int) -> tuple[int, int]:
    """Offsets of the start of line `first` and the end of line `last` (before its newline)."""
    starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    end = starts[last + 1] - 1 if last + 1 < len(starts) else len(text)
    return starts[first], end


def _fuzzy_replace(content: str, norm: str, start: int, length: int, new_text: str) -> str:
    """Splice new_text over a normalized match; only the touched lines are normalized."""
    end = start + length
    first, last = norm.count("\n", 0, start), norm.count("\n", 0, end - 1)
    norm_start, norm_end = _line_bounds(norm, first, last)
    orig_start, orig_end = _line_bounds(content, first, last)
    if norm[end - 1] == "\n":  # the match swallowed the line break: keep the rest intact
        norm_end, orig_end = end, orig_end + 1
    prefix = norm[norm_start:start]
    # Keep the first touched line's original leading indentation.
    line = content[orig_start:]
    indent = line[: len(line) - len(line.lstrip(" \t\u00a0\u2002\u2003\u2009\u3000"))]
    norm_indent = _normalize_line(indent + "x")[:-1]
    if indent and prefix.startswith(norm_indent):
        prefix = indent + prefix[len(norm_indent):]
    return content[:orig_start] + prefix + new_text + norm[end:norm_end] + content[orig_end:]


# --- tools -------------------------------------------------------------------

def read(path: str, offset: int = 1, limit: int = 200) -> str:
    """Read a UTF-8 text file and return its lines with line numbers.

    Args:
        path: File path, relative to the workspace root.
        offset: 1-based line number to start from.
        limit: Maximum number of lines to return (1-2000).
    """
    if offset < 1:
        raise ToolError("offset must be at least 1.")
    if not 1 <= limit <= MAX_READ_LINES:
        raise ToolError(f"limit must be between 1 and {MAX_READ_LINES}.")
    target = _resolve(path)
    data = _read_bytes(target)
    text = _decode(data[len(BOM):] if data.startswith(BOM) else data, target)
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()  # trailing newline, not an extra line
    if not lines:
        return "(empty file)"
    if offset > len(lines):
        raise ToolError(f"offset {offset} is past the end of the file ({len(lines)} lines).")
    chunk = lines[offset - 1: offset - 1 + limit]
    body = "\n".join(f"{offset + i:>6}\t{line}" for i, line in enumerate(chunk))
    last = offset + len(chunk) - 1
    if last < len(lines):
        body += f"\n\n[lines {offset}-{last} of {len(lines)}; use offset={last + 1} to continue]"
    return body


def write(path: str, content: str) -> str:
    """Create or overwrite a UTF-8 text file, creating parent directories as needed.

    Args:
        path: File path, relative to the workspace root.
        content: The complete new file content.
    """
    target = _resolve(path)
    data = content.encode("utf-8")
    with _write_lock:
        existed = target.exists()
        _atomic_write(target, data)
    return f"{'Overwrote' if existed else 'Created'} {_display(target)} ({len(data)} bytes)."


def edit(path: str, old_text: str, new_text: str) -> str:
    """Replace one unique occurrence of old_text with new_text and return a unified diff.

    Read the file first. old_text must match exactly one place; include enough
    surrounding lines to make it unique.

    Args:
        path: File path, relative to the workspace root.
        old_text: Existing text to replace (must be unique in the file).
        new_text: Replacement text.
    """
    target = _resolve(path)
    with _write_lock:
        original = _read_bytes(target)
        has_bom = original.startswith(BOM)
        text = _decode(original[len(BOM):] if has_bom else original, target)
        crlf = _uses_crlf(text)
        content = text.replace("\r\n", "\n") if crlf else text
        old = old_text.replace("\r\n", "\n")
        new = new_text.replace("\r\n", "\n")
        updated = apply_edit(content, old, new)
        output = updated.replace("\n", "\r\n") if crlf else updated
        data = (BOM if has_bom else b"") + output.encode("utf-8")
        _atomic_write(target, data, expected=original)
    name = _display(target)
    diff = difflib.unified_diff(content.splitlines(keepends=True),
                                updated.splitlines(keepends=True),
                                f"a/{name}", f"b/{name}")
    return "".join(line if line.endswith("\n") else line + "\n" for line in diff).rstrip("\n")


def bash(command: str, timeout: int = 30) -> str:
    """Run a noninteractive Bash command in the workspace root.

    Returns combined stdout/stderr and the exit code. Runs with the user's
    permissions; it is not sandboxed.

    Args:
        command: The Bash command to run.
        timeout: Seconds before the command is killed (1-300).
    """
    if not command.strip():
        raise ToolError("command must not be empty.")
    if not 1 <= timeout <= 300:
        raise ToolError("timeout must be between 1 and 300 seconds.")
    cancel = getattr(_local, "cancel", None)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "PAGER": "cat", "GIT_PAGER": "cat"}
    process = subprocess.Popen(
        ["bash", "-c", command], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + timeout
    status = None
    try:
        while True:
            try:
                out, _ = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    status = "cancelled"
                elif time.monotonic() >= deadline:
                    status = f"timed out after {timeout} s"
                else:
                    continue
                _kill_group(process)
                out, _ = process.communicate()
                break
    finally:
        if process.poll() is None:  # unexpected exit from the loop: never leak the group
            _kill_group(process)
    text = out.decode("utf-8", errors="replace")
    if status:
        raise ToolError(f"Command {status}; process group terminated.\n\n{text}".rstrip())
    return f"Exit code: {process.returncode}\n\n{text}".rstrip()


def _kill_group(process: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 2), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


TOOLS = Tools([read, write, edit, bash])


def response_tools() -> list[dict]:
    """aisuite's schemas in Responses function-tool format (strict off keeps defaults)."""
    return [{"type": "function", "name": spec["function"]["name"],
             "description": spec["function"]["description"],
             "parameters": spec["function"]["parameters"], "strict": False}
            for spec in TOOLS.tools()]


def run_tool(name: str, arguments: str, cancel: threading.Event | None = None) -> tuple[str, bool]:
    """Validate and execute one call. Returns (output for the model, succeeded)."""
    _local.cancel = cancel
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ToolError("Arguments must be a JSON object.")
        result = TOOLS.execute({"function": {"name": name, "arguments": args}})[0]
        return truncate(str(result)), True
    except json.JSONDecodeError as exc:
        return f"Error: arguments are not valid JSON ({exc}).", False
    except ToolError as exc:
        return truncate(f"Error: {exc}"), False
    except Exception as exc:
        return truncate(f"Error: {type(exc).__name__}: {exc}"), False
    finally:
        _local.cancel = None
