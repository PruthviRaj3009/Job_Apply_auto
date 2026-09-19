"""
LaTeX escaping and small rendering helpers.

Escaping is not a detail here. Profile text is user-supplied and routinely
contains the characters LaTeX treats as syntax — "C#", "20% faster",
"R&D", "Node.js & Express", "salary_range". Any one of them unescaped is a
compile failure at best, and silently mangled output at worst.
"""

from __future__ import annotations

import re

#: Characters that must be escaped, longest-first so the backslash rule does
#: not re-escape the backslashes the other rules introduce.
_ESCAPES: tuple[tuple[str, str], ...] = (
    ("\\", r"\textbackslash{}"),
    ("&", r"\&"),
    ("%", r"\%"),
    ("$", r"\$"),
    ("#", r"\#"),
    ("_", r"\_"),
    ("{", r"\{"),
    ("}", r"\}"),
    ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"),
)

#: Unicode that appears constantly in pasted profile text and breaks a
#: pdflatex run under the default T1 encoding.
_UNICODE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("\u2018", "`"), ("\u2019", "'"),
    ("\u201c", "``"), ("\u201d", "''"),
    ("\u2013", "--"), ("\u2014", "---"),
    ("\u2022", r"\textbullet{}"),
    ("\u00a0", " "),
    ("\u2026", r"\ldots{}"),
    ("\u2192", r"$\rightarrow$"),
    ("\u00d7", r"$\times$"),
    ("\u2265", r"$\geq$"), ("\u2264", r"$\leq$"),
    ("\u20b9", r"\rupee{}"),
)


def escape(text: str | None) -> str:
    """
    Make arbitrary text safe to place in a LaTeX document.

    The backslash substitution runs first and its replacement contains braces,
    which the brace rules would then double-escape — so braces are handled by
    a placeholder swap rather than by ordering alone.
    """
    if not text:
        return ""

    out = str(text)
    for source, target in _UNICODE_REPLACEMENTS:
        out = out.replace(source, target)

    # Protect the backslash first using a sentinel no input can contain.
    sentinel = "\x00BACKSLASH\x00"
    out = out.replace("\\", sentinel)
    for source, target in _ESCAPES[1:]:
        out = out.replace(source, target)
    out = out.replace(sentinel, r"\textbackslash{}")

    return out


def escape_url(url: str | None) -> str:
    """
    Escape a URL for \\href.

    Only `#` and `%` genuinely need it inside href; escaping the rest would
    corrupt the address. Underscores are common in real repo URLs and must
    survive verbatim.
    """
    if not url:
        return ""
    return str(url).replace("%", r"\%").replace("#", r"\#")


def link(url: str | None, text: str | None = None) -> str:
    """
    A hyperlink whose address also survives text extraction.

    Most ATS parsers read the text layer and ignore link annotations, so a
    link rendered only as "GitHub" loses the address entirely — exactly the
    failure section 30 forbids.
    """
    if not url:
        return escape(text or "")
    shown = text or _display_url(url)
    return rf"\visiblelink{{{escape_url(url)}}}{{{escape(shown)}}}"


def _display_url(url: str) -> str:
    """Strip the scheme and trailing slash for display."""
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")


def itemize(items: list[str], *, escape_items: bool = True) -> str:
    """Render a bullet list, or nothing at all when there is nothing to list."""
    kept = [i for i in items if i and str(i).strip()]
    if not kept:
        return ""
    lines = "\n".join(
        rf"  \item {escape(i) if escape_items else i}" for i in kept
    )
    return "\\begin{itemize}\n" + lines + "\n\\end{itemize}"


def section(title: str, body: str) -> str:
    """
    A titled section, or empty string when the body is empty.

    Empty sections must not be emitted: a resume with a bare "PROJECTS"
    heading and nothing under it looks like a generation bug to a human
    reader and confuses heading-based ATS parsers.
    """
    if not body or not body.strip():
        return ""
    return f"\\section{{{escape(title)}}}\n{body}\n"


def format_date_range(start, end, is_current: bool = False) -> str:
    """
    "Jan 2023 -- Present" style. Honest about what it does not know: a missing
    start date yields an empty range rather than an invented one.
    """
    def fmt(value) -> str:
        if value is None:
            return ""
        return value.strftime("%b %Y")

    left = fmt(start)
    right = "Present" if is_current else fmt(end)
    if left and right:
        return f"{left} -- {right}"
    return left or right or ""
