"""AOR 的终端展示；不读取状态、不联网，也不处理业务输出。"""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import TextIO

WORDMARK = (
    " ▄▄▄    ▄▄▄   ▄▄▄▄ ",
    "█   █  █   █  █   █",
    "█▄▄▄█  █   █  █▄▄▄▀",
    "█   █  █   █  █  ▀▄",
    "▀   ▀   ▀▀▀   ▀   ▀",
)
TAGLINE = "找到值得自己做的创业项目"
COMMANDS = (("aor skills", "查看技能"), ("aor doctor", "检查环境与更新"), ("aor update", "升级版本"))


def _clean(value: object) -> str:
    # 清单文字不能插入终端控制序列、额外行或不可见方向控制符。
    return "".join(" " if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char for char in str(value))


def _cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
               for char in text)


def _columns(stream: TextIO) -> int:
    try:
        columns = int(os.environ.get("COLUMNS", "0"))
        if columns > 0:
            return columns
    except ValueError:
        pass
    try:
        return max(1, os.get_terminal_size(stream.fileno()).columns)
    except (AttributeError, OSError, ValueError):
        return 80


class _Terminal:
    def __init__(self, stream: TextIO):
        self.stream = stream
        self.encoding = getattr(stream, "encoding", None) or "utf-8"
        self.tty = bool(getattr(stream, "isatty", lambda: False)())
        self.width = _columns(stream)
        try:
            "█◇│找到".encode(self.encoding)
            unicode_supported = True
        except (UnicodeError, LookupError):
            unicode_supported = False
        self.pretty = self.tty and os.environ.get("TERM") != "dumb" and unicode_supported and self.width >= 24
        self.color = self.pretty and not os.environ.get("NO_COLOR")
        background = os.environ.get("COLORFGBG", "").split(";")[-1]
        self.light = True if background in {"7", "15"} else False if background in {"0", "8"} else None

    def paint(self, text: str, style: str) -> str:
        if not self.color or not style or not text:
            return text
        if style == "title":
            code = "1"
        elif style == "muted":
            # 使用终端默认前景色，避免浅色主题下暗灰色不可读。
            return text
        elif style == "error":
            code = "33"
        else:
            index = {"top": 0, "accent": 1, "bottom": 2}.get(style, 1)
            term = os.environ.get("TERM", "")
            if self.light is None:
                # 未知背景沿用终端自带色盘，避免浅色主题收到深色专用亮色。
                code = ("36", "36", "34")[index]
            elif os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"} or term.endswith("-direct"):
                palette = ((0, 113, 118), (0, 104, 128), (39, 95, 159)) if self.light else (
                    (156, 234, 229), (99, 207, 220), (110, 171, 231))
                code = "38;2;" + ";".join(str(channel) for channel in palette[index])
            elif "256color" in term:
                palette = (30, 31, 25) if self.light else (116, 80, 75)
                code = f"38;5;{palette[index]}"
            else:
                code = ("36", "36", "34")[index]
        return f"\033[{code}m{text}\033[0m"

    def _emit(self, parts: list[tuple[str, str]], prefix: str) -> None:
        text = self.paint(prefix, "accent") + "".join(self.paint(value, style) for value, style in parts)
        # 无法显示中文的流仍可获得命令和版本，不因展示导致入口失败。
        text = (text + "\n").encode(self.encoding, errors="replace").decode(self.encoding)
        self.stream.write(text)

    def line(self, *parts: tuple[object, str], prefix: str = "") -> None:
        if not self.tty:
            self._emit([(_clean(value), style) for value, style in parts], prefix)
            return
        if _cell_width(prefix) + 2 > self.width:
            prefix = ""
        occupied = _cell_width(prefix)
        chunks: list[tuple[str, str]] = []
        for raw, style in parts:
            segment = ""
            for char in _clean(raw):
                size = _cell_width(char)
                if occupied + size > self.width and occupied > _cell_width(prefix):
                    chunks.append((segment, style))
                    self._emit(chunks, prefix)
                    chunks, segment = [], ""
                    occupied = _cell_width(prefix)
                segment += char
                occupied += size
            chunks.append((segment, style))
        self._emit(chunks, prefix)


def _version(context: dict, item: dict | None = None) -> str:
    return "v" + _clean((item or {}).get("version") or context.get("version") or "unknown").removeprefix("v")


def _skill_rows(terminal: _Terminal, context: dict, skills: list[dict], *, descriptions: bool) -> None:
    prefix = "  │  " if terminal.pretty else "  "
    for item in skills:
        name = _clean(item.get("name") or "未命名技能")
        version = _version(context, item)
        if terminal.tty and _cell_width(prefix + name + "  " + version) > terminal.width:
            terminal.line((name, "title"), prefix=prefix)
            terminal.line((version, "muted"), prefix=prefix)
        else:
            terminal.line((name, "title"), ("  " + version, "muted"), prefix=prefix)
        if item.get("status") == "error":
            terminal.line(("! " + _clean(item.get("message") or "技能入口异常；运行 aor doctor 查看详情"), "error"),
                          prefix=prefix)
        elif descriptions and item.get("description"):
            terminal.line((item["description"], "muted"), prefix=prefix)


def print_overview(context: dict, skills: list[dict], *, stream: TextIO | None = None) -> None:
    """显示品牌首页；重定向输出使用紧凑文字，完整安装信息由 JSON 和诊断提供。"""
    terminal = _Terminal(stream if stream is not None else sys.stdout)
    version = _version(context)
    if terminal.pretty:
        terminal.line()
        copy = ("AI Opportunity Radar", version, "", TAGLINE, "")
        styles = ("title", "muted", "", "muted", "")
        side_by_side = terminal.width >= 2 + 19 + 4 + max(_cell_width(text) for text in copy)
        for index, row in enumerate(WORDMARK):
            tone = ("top", "top", "accent", "accent", "bottom")[index]
            parts = [(row, tone)]
            if side_by_side and copy[index]:
                parts.append(("    " + copy[index], styles[index]))
            terminal.line(*parts, prefix="  ")
        if not side_by_side:
            terminal.line()
            terminal.line(("AI Opportunity Radar", "title"), prefix="  ")
            terminal.line((version, "muted"), prefix="  ")
            terminal.line((TAGLINE, "muted"), prefix="  ")
        terminal.line()
        terminal.line((f"技能 · {len(skills)}", "muted"), prefix="  ◇  ")
    else:
        terminal.line((f"AOR {version}  AI Opportunity Radar", "title"))
        terminal.line((f"技能：{len(skills)} 个", "muted"))
    _skill_rows(terminal, context, skills, descriptions=False)
    terminal.line(prefix="  │" if terminal.pretty else "")
    terminal.line(("常用命令", "muted"), prefix="  ◇  " if terminal.pretty else "")
    prefix = "  │  " if terminal.pretty else "  "
    for command, description in COMMANDS:
        if terminal.tty and _cell_width(prefix) + 15 + _cell_width(description) > terminal.width:
            terminal.line((command, "accent"), prefix=prefix)
            terminal.line((description, "muted"), prefix=prefix)
        else:
            terminal.line((command.ljust(15), "accent"), (description, ""), prefix=prefix)
    if terminal.pretty:
        terminal.line(prefix="  ╵")
        terminal.line()


def print_skills(context: dict, skills: list[dict], *, stream: TextIO | None = None) -> None:
    """列表突出名称、版本与说明；异常始终可见，完整路径仍可通过 JSON 获取。"""
    terminal = _Terminal(stream if stream is not None else sys.stdout)
    terminal.line((f"AOR 技能 · {len(skills)}  {_version(context)}", "title"))
    if terminal.pretty:
        terminal.line(prefix="  │")
    _skill_rows(terminal, context, skills, descriptions=True)
    if terminal.pretty:
        terminal.line(prefix="  ╵")
    terminal.line(("完整路径：aor skills --json", "muted"))
    terminal.line(("环境诊断：aor doctor", "muted"))
