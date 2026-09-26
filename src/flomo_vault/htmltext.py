"""Small, dependency-free HTML to readable text conversion."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser


class _TextParser(HTMLParser):
    _BLOCKS = {"p", "div", "section", "article", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCKS or tag == "li" or tag.startswith("h"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: object) -> str:
    if value is None:
        return ""
    parser = _TextParser()
    try:
        parser.feed(str(value))
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", str(value))
    text = html.unescape(text).replace("\r", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_markdown(value: object) -> str:
    """Return conservative Markdown while preserving the original HTML elsewhere."""
    return html_to_text(value)
