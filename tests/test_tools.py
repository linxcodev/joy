"""Tests for the coding tools, run against a temporary workspace."""

import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import joy.tools as tools
from joy.tools import ToolError


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "ws"
        self.root.mkdir()
        patcher = patch.object(tools, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def call(self, name, **args):
        return tools.run_tool(name, json.dumps(args))

    def put(self, name, data: bytes, mode=None):
        path = self.root / name
        path.write_bytes(data)
        if mode is not None:
            path.chmod(mode)
        return path

    # read / write ----------------------------------------------------------

    def test_read_numbers_lines_and_pages(self):
        self.put("a.txt", b"\xef\xbb\xbfone\r\ntwo\r\nthree\r\n")
        out, ok = self.call("read", path="a.txt")
        self.assertTrue(ok)
        self.assertEqual(out, "     1\tone\n     2\ttwo\n     3\tthree")
        out, _ = self.call("read", path="a.txt", offset=2, limit=1)
        self.assertIn("     2\ttwo", out)
        self.assertIn("use offset=3", out)
        self.assertFalse(self.call("read", path="a.txt", offset=9)[1])
        self.assertFalse(self.call("read", path="a.txt", limit=2001)[1])
        out, ok = self.call("read", path="a.txt", limit="many")
        self.assertFalse(ok)
        self.assertIn("limit", out)

    def test_write_creates_parents_and_reports(self):
        out, ok = self.call("write", path="deep/dir/f.py", content="x = 1\n")
        self.assertTrue(ok, out)
        self.assertEqual((self.root / "deep/dir/f.py").read_text(), "x = 1\n")
        self.assertIn("Created", out)
        self.assertIn("Overwrote", self.call("write", path="deep/dir/f.py", content="y")[0])

    def test_size_limits_and_truncation(self):
        self.put("big.txt", b"a" * (tools.MAX_FILE_BYTES + 1))
        out, ok = self.call("read", path="big.txt")
        self.assertFalse(ok)
        self.assertIn("too large", out)
        self.assertFalse(self.call("write", path="w.txt", content="a" * (tools.MAX_FILE_BYTES + 1))[1])
        self.assertFalse((self.root / "w.txt").exists())
        out = tools.truncate("x" * 25_000)
        self.assertTrue(out.startswith("x" * 20_000))
        self.assertIn("[output truncated: showing first 20000 of 25000 characters]", out)

    def test_paths_and_symlinks_cannot_escape(self):
        outside = self.root.parent / "secret.txt"
        outside.write_text("secret")
        (self.root / "link").symlink_to(outside)
        (self.root / "dirlink").symlink_to(self.root.parent)
        for path in ("../secret.txt", str(outside), "link", "dirlink/secret.txt", "/etc/passwd"):
            out, ok = self.call("read", path=path)
            self.assertFalse(ok, path)
            self.assertIn("outside the workspace", out)
        self.assertFalse(self.call("write", path="link", content="pwned")[1])
        self.assertFalse(self.call("edit", path="link", old_text="secret", new_text="x")[1])
        self.assertEqual(outside.read_text(), "secret")

    # edit ------------------------------------------------------------------

    def test_exact_edit_returns_diff_and_keeps_bom_crlf_mode(self):
        path = self.put("c.py", b"\xef\xbb\xbfa = 1\r\nb = 2\r\n", mode=0o751)
        out, ok = self.call("edit", path="c.py", old_text="b = 2", new_text="b = 3")
        self.assertTrue(ok, out)
        self.assertIn("-b = 2", out)
        self.assertIn("+b = 3", out)
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbfa = 1\r\nb = 3\r\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o751)
        self.assertEqual([p.name for p in self.root.iterdir()], ["c.py"])  # no temp files left

    def test_fuzzy_edit_normalizes_only_touched_lines(self):
        original = ("keep \u201cthis\u201d  \n"
                    "    msg = \u2018hi\u2019 \u2014 there  \n"
                    "\tother\u00a0line\n")
        path = self.put("f.py", original.encode())
        out, ok = self.call("edit", path="f.py", old_text="msg = 'hi' - there",
                            new_text="msg = 'bye'")
        self.assertTrue(ok, out)
        self.assertEqual(path.read_text(), "keep \u201cthis\u201d  \n"
                                           "    msg = 'bye'\n"
                                           "\tother\u00a0line\n")

    def test_fuzzy_multiline_edit_keeps_crlf(self):
        path = self.put("m.txt", "a\r\n  x \u2013 1   \r\n  y\u00a0= 2\r\nz\r\n".encode())
        out, ok = self.call("edit", path="m.txt", old_text="x - 1\n  y = 2\n", new_text="q\n")
        self.assertTrue(ok, out)
        self.assertEqual(path.read_bytes(), b"a\r\n  q\r\nz\r\n")

    def test_edit_rejections(self):
        self.put("r.txt", "dup\ndup\n\u201cq\u201d\n\"q\"\nsame\n".encode())
        cases = [("", "x", "empty"), ("  \n ", "x", "empty"), ("nope", "x", "not found"),
                 ("dup", "x", "2 places"), ('"q"', "x", "2 places"),
                 ("same", "same", "no change")]
        for old, new, message in cases:
            out, ok = self.call("edit", path="r.txt", old_text=old, new_text=new)
            self.assertFalse(ok, old)
            self.assertIn(message, out)
        self.assertFalse(self.call("edit", path="missing.txt", old_text="a", new_text="b")[1])

    def test_edit_detects_concurrent_change(self):
        path = self.put("race.txt", b"one\n")
        real = tools.apply_edit

        def change_then_apply(content, old, new):
            path.write_bytes(b"changed\n")
            return real(content, old, new)

        with patch.object(tools, "apply_edit", change_then_apply):
            out, ok = self.call("edit", path="race.txt", old_text="one", new_text="two")
        self.assertFalse(ok)
        self.assertIn("changed on disk", out)
        self.assertEqual(path.read_bytes(), b"changed\n")

    # bash ------------------------------------------------------------------

    def test_bash_output_exit_code_and_cwd(self):
        out, ok = self.call("bash", command="pwd; echo err >&2; exit 3")
        self.assertTrue(ok)
        self.assertIn("Exit code: 3", out)
        self.assertIn(str(self.root), out)
        self.assertIn("err", out)
        self.assertFalse(self.call("bash", command="true", timeout=0)[1])
        self.assertFalse(self.call("bash", command="true", timeout=301)[1])

    def test_bash_timeout_kills_process_group(self):
        marker = self.root / "survived"
        start = time.monotonic()
        out, ok = self.call("bash", command=f"(sleep 3; touch {marker}) & echo started; sleep 30",
                            timeout=1)
        self.assertFalse(ok)
        self.assertIn("timed out after 1 s", out)
        self.assertIn("started", out)
        self.assertLess(time.monotonic() - start, 5)
        time.sleep(3.5)
        self.assertFalse(marker.exists())  # the background child died with the group

    def test_bash_cancellation(self):
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        start = time.monotonic()
        out, ok = tools.run_tool("bash", json.dumps({"command": "sleep 30"}), cancel)
        self.assertFalse(ok)
        self.assertIn("cancelled", out)
        self.assertLess(time.monotonic() - start, 5)

    def test_schemas_for_responses(self):
        specs = {spec["name"]: spec for spec in tools.response_tools()}
        self.assertEqual(set(specs), {"read", "write", "edit", "bash"})
        for spec in specs.values():
            self.assertEqual(spec["type"], "function")
            self.assertIs(spec["strict"], False)
        self.assertEqual(specs["read"]["parameters"]["required"], ["path"])
        self.assertEqual(specs["bash"]["parameters"]["properties"]["timeout"]["default"], 30)
        out, ok = self.call("nope")
        self.assertFalse(ok)
        self.assertIn("not registered", out)
        self.assertFalse(tools.run_tool("read", "{bad json")[1])


if __name__ == "__main__":
    unittest.main()
