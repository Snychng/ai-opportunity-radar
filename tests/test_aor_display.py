from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import aor_display


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
BLOCK_RE = re.compile(r"[\u2580-\u259f]")


class TerminalStream(io.StringIO):
    @property
    def encoding(self) -> str:
        return "utf-8"

    def isatty(self) -> bool:
        return True


class AsciiTerminalStream(TerminalStream):
    @property
    def encoding(self) -> str:
        return "ascii"


def visible_width(value: str) -> int:
    """按终端显示列计数，中文占两列，组合字符不额外占列。"""
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
               for char in ANSI_RE.sub("", value))


class AorDisplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = {"version": "3.2.2", "root": Path("/private/source/long/路径"),
                        "commit": "a" * 40, "kind": "source"}
        self.skills = [{"name": "ai-opportunity-radar", "path": "/private/source/long/路径/SKILL.md",
                        "description": "创业机会研究、证据分层与个人验证 cafe\u0301 🚀",
                        "status": "ok", "message": "技能入口可用"}]

    def render(self, command: str = "overview", *, columns: int | str = 80, tty: bool = True,
               term: str = "xterm-256color", no_color: str = "", stream: io.StringIO | None = None,
               skills: list[dict] | None = None, colorterm: str = "truecolor", colorfgbg: str = "") -> str:
        output = stream if stream is not None else TerminalStream() if tty else io.StringIO()
        environment = {"TERM": term, "COLORTERM": colorterm, "COLUMNS": str(columns),
                       "LINES": "24", "NO_COLOR": no_color, "COLORFGBG": colorfgbg}
        with patch.dict(os.environ, environment):
            getattr(aor_display, f"print_{command}")(self.context, self.skills if skills is None else skills,
                                                     stream=output)
        return output.getvalue()

    def assert_no_internal_paths_or_commit(self, output: str) -> None:
        self.assertNotIn(str(self.context["root"]), output)
        self.assertNotIn(self.context["commit"], output)
        self.assertNotIn(self.skills[0]["path"], output)

    def assert_navigation(self, output: str) -> None:
        plain = ANSI_RE.sub("", output)
        self.assertIn("AI Opportunity Radar", plain)
        self.assertIn("3.2.2", plain)
        self.assertIn("ai-opportunity-radar", plain)
        for command in ("aor skills", "aor doctor", "aor update"):
            self.assertIn(command, plain)

    def test_overview_redirected_output_is_compact_plain_text_with_navigation(self) -> None:
        output = self.render(tty=False)
        self.assert_navigation(output)
        self.assertNotIn("\x1b", output)
        self.assertIsNone(BLOCK_RE.search(output))
        self.assert_no_internal_paths_or_commit(output)

    def test_dumb_terminal_uses_plain_text_without_wordmark(self) -> None:
        for command in ("overview", "skills"):
            with self.subTest(command=command):
                output = self.render(command, term="dumb")
                self.assertNotIn("\x1b", output)
                self.assertIsNone(BLOCK_RE.search(output))
                self.assertIn("3.2.2", output)
                self.assertIn("ai-opportunity-radar", output)

    def test_utf8_terminal_has_five_rows_of_wordmark_and_navigation(self) -> None:
        output = self.render()
        plain = ANSI_RE.sub("", output)
        wordmark_rows = [line for line in plain.splitlines() if BLOCK_RE.search(line)]
        self.assertEqual(len(wordmark_rows), 5)
        self.assert_navigation(output)
        self.assert_no_internal_paths_or_commit(output)

    def test_narrow_terminal_stacks_a_five_by_nineteen_wordmark(self) -> None:
        for columns in (32, 40):
            with self.subTest(columns=columns):
                output = ANSI_RE.sub("", self.render(columns=columns))
                wordmark_rows = [line for line in output.splitlines() if BLOCK_RE.search(line)]
                self.assertEqual(len(wordmark_rows), 5)
                for line in wordmark_rows:
                    self.assertIsNone(re.search(r"[A-Za-z0-9]", line))
                    self.assertLessEqual(visible_width(line.strip()), 19)

    def test_wide_terminal_places_brand_beside_wordmark(self) -> None:
        output = ANSI_RE.sub("", self.render(columns=80))
        self.assertTrue(any(BLOCK_RE.search(line) and "AI Opportunity Radar" in line
                            for line in output.splitlines()))

    def test_no_color_retains_terminal_wordmark_and_content(self) -> None:
        for columns in (32, 80):
            with self.subTest(columns=columns):
                colored = self.render(columns=columns)
                uncolored = self.render(columns=columns, no_color="1")
                self.assertNotIn("\x1b", uncolored)
                self.assertEqual(ANSI_RE.sub("", colored), uncolored)
                self.assertEqual(sum(bool(BLOCK_RE.search(line)) for line in uncolored.splitlines()), 5)
                self.assert_navigation(uncolored)

    def test_color_sequences_match_terminal_capability_without_changing_content(self) -> None:
        plain = self.render(no_color="1")
        cases = (("xterm-256color", "truecolor", "\x1b[38;2;"),
                 ("xterm-direct", "", "\x1b[38;2;"),
                 ("xterm-256color", "", "\x1b[38;5;"),
                 ("xterm", "", "\x1b[36m"))
        for term, colorterm, expected in cases:
            with self.subTest(term=term, colorterm=colorterm):
                output = self.render(term=term, colorterm=colorterm, colorfgbg="15;0")
                self.assertIn(expected, output)
                self.assertEqual(ANSI_RE.sub("", output), plain)
                if expected != "\x1b[38;2;":
                    self.assertNotIn("\x1b[38;2;", output)
                if term == "xterm":
                    self.assertNotIn("\x1b[38;5;", output)

    def test_light_background_changes_palette_without_changing_content(self) -> None:
        for colorterm in ("truecolor", ""):
            with self.subTest(colorterm=colorterm):
                dark = self.render(colorterm=colorterm, colorfgbg="15;0")
                light = self.render(colorterm=colorterm, colorfgbg="0;15")
                self.assertNotEqual(dark, light)
                self.assertEqual(ANSI_RE.sub("", dark), ANSI_RE.sub("", light))

    def test_unknown_background_uses_terminal_palette_without_changing_content(self) -> None:
        plain = self.render(no_color="1")
        for background in ("", "unknown", "0;invalid", "0;3"):
            for colorterm in ("truecolor", ""):
                with self.subTest(background=background, colorterm=colorterm):
                    output = self.render(colorterm=colorterm, colorfgbg=background)
                    self.assertIn("\x1b[36m", output)
                    self.assertNotIn("\x1b[38;2;", output)
                    self.assertNotIn("\x1b[38;5;", output)
                    self.assertEqual(ANSI_RE.sub("", output), plain)

    def test_invalid_terminal_columns_fall_back_to_usable_layout(self) -> None:
        for columns in ("not-a-number", "", "0", "-1"):
            with self.subTest(columns=columns):
                output = self.render(columns=columns)
                self.assert_navigation(output)
                for line in output.splitlines():
                    self.assertLessEqual(visible_width(line), 80)

    def test_registry_control_characters_cannot_inject_terminal_sequences_or_extra_lines(self) -> None:
        for command in ("overview", "skills"):
            for status in ("ok", "error"):
                with self.subTest(command=command, status=status):
                    ordinary = {**self.skills[0], "name": "entry END", "version": "3.2.2 END",
                                "description": "说明 END", "message": "入口异常 END", "status": status}
                    unsafe = {**ordinary, "name": "entry\n\x1b[31mEND", "version": "3.2.2\r\x1b[0mEND",
                              "description": "说明\n\r\t\b\u202e\u2066\x1b[31mEND",
                              "message": "入口异常\n\r\t\b\u202e\u2066\x1b[31mEND"}
                    safe_output = self.render(command, skills=[ordinary], no_color="1")
                    output = self.render(command, skills=[unsafe], no_color="1")
                    self.assertEqual(len(output.splitlines()), len(safe_output.splitlines()))
                    for control in ("\x1b", "\r", "\t", "\b", "\u202e", "\u2066"):
                        self.assertNotIn(control, output)
                    self.assertIn("entry", output)
                    self.assertIn("3.2.2", output)

    def test_terminal_columns_bound_chinese_and_combining_text_without_losing_commands(self) -> None:
        for command in ("overview", "skills"):
            for columns in (32, 40, 80):
                with self.subTest(command=command, columns=columns):
                    output = self.render(command, columns=columns)
                    for line in output.splitlines():
                        self.assertLessEqual(visible_width(line), columns, repr(line))
                    self.assertIn("3.2.2", ANSI_RE.sub("", output))
                    self.assertIn("ai-opportunity-radar", ANSI_RE.sub("", output))
                    if command == "overview":
                        self.assert_navigation(output)

    def test_ascii_terminal_does_not_receive_unicode_wordmark(self) -> None:
        output = self.render(stream=AsciiTerminalStream())
        self.assertIsNone(BLOCK_RE.search(output))
        self.assert_navigation(output)

    def test_default_stream_follows_current_stdout_redirect(self) -> None:
        for command in ("overview", "skills"):
            with self.subTest(command=command):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    getattr(aor_display, f"print_{command}")(self.context, self.skills)
                self.assertIn("ai-opportunity-radar", output.getvalue())
                self.assertIn("3.2.2", output.getvalue())

    def test_explicit_stream_receives_output_without_writing_stdout_or_stderr(self) -> None:
        for command in ("overview", "skills"):
            with self.subTest(command=command):
                output, standard, errors = io.StringIO(), io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(standard), contextlib.redirect_stderr(errors):
                    self.render(command, stream=output)
                self.assertIn("ai-opportunity-radar", output.getvalue())
                self.assertEqual((standard.getvalue(), errors.getvalue()), ("", ""))

    def test_skills_plain_output_keeps_description_and_version_without_private_details(self) -> None:
        output = self.render("skills", tty=False)
        self.assertIn("ai-opportunity-radar", output)
        self.assertIn("3.2.2", output)
        self.assertIn(self.skills[0]["description"], output)
        self.assertNotIn("\x1b", output)
        self.assertIsNone(BLOCK_RE.search(output))
        self.assert_no_internal_paths_or_commit(output)

    def test_skills_uses_own_version_when_provided(self) -> None:
        skills = [{**self.skills[0], "version": "4.0.1"}]
        output = self.render("skills", tty=False, skills=skills)
        self.assertIn("4.0.1", output)

    def test_missing_skill_entry_remains_visible_and_actionable(self) -> None:
        missing = {**self.skills[0], "name": "missing-entry", "status": "error", "message": "技能入口不存在"}
        for command in ("overview", "skills"):
            with self.subTest(command=command):
                output = ANSI_RE.sub("", self.render(command, columns=32, skills=[missing]))
                self.assertIn("missing-entry", output)
                self.assertRegex(output.replace("missing-entry", ""), r"缺失|缺少|不存在|不可用|missing|error|失败")
                self.assertIn("aor doctor", output)

    def test_empty_skill_registry_still_shows_version_and_diagnostic_command(self) -> None:
        for command in ("overview", "skills"):
            with self.subTest(command=command):
                output = self.render(command, tty=False, skills=[])
                self.assertIn("3.2.2", output)
                self.assertIn("aor doctor", output)


if __name__ == "__main__":
    unittest.main()
