"""Rich rendering of assistant replies. Injectable so tests need no TTY."""

from __future__ import annotations

from collections.abc import Callable
from typing import TextIO

from rich.console import Console
from rich.markdown import Markdown

ReplyRenderer = Callable[[str, TextIO], None]


def render_markdown_reply(text: str, out: TextIO) -> None:
    console = Console(
        file=out,
        force_terminal=True,
        color_system="standard",
        highlight=False,
        width=88,
        legacy_windows=False,
        soft_wrap=True,
    )
    console.print(Markdown(text), overflow="ignore", crop=False, soft_wrap=True)
