"""
LaTeX compilation (section 9).

Runs pdflatex and reports what happened. Two behaviours matter:

**A non-zero exit code is not automatically failure.** pdflatex exits non-zero
for recoverable problems (an overfull box, a missing font substitution) while
still producing a perfectly good PDF. What decides success is whether a PDF
exists and has pages — checking the exit code alone rejects working resumes.

**Errors are extracted, not dumped.** A pdflatex log is thousands of lines;
the three that say what went wrong are what a caller can act on.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "resume_templates"

#: pdflatex needs two passes for the page counter and any cross-references to
#: settle. A one-pass run reports the wrong page count on the boundary case
#: that matters most — the resume that is just over one page.
PASSES = 2
COMPILE_TIMEOUT_SECONDS = 120

_ERROR_RE = re.compile(r"^! (.+)$", re.MULTILINE)
_UNDEFINED_RE = re.compile(r"^! Undefined control sequence\.\s*\n(.*)$", re.MULTILINE)
_MISSING_FILE_RE = re.compile(r"^! LaTeX Error: File `([^']+)' not found", re.MULTILINE)


@dataclass(slots=True)
class CompileResult:
    ok: bool
    pdf_path: Path | None = None
    log: str = ""
    errors: list[str] = field(default_factory=list)
    page_count: int | None = None

    @property
    def summary(self) -> str:
        if self.ok:
            pages = f", {self.page_count} page(s)" if self.page_count else ""
            return f"Compiled successfully{pages}"
        return "Compilation failed: " + ("; ".join(self.errors[:3]) or "unknown error")


def latex_available(command: str = "pdflatex") -> bool:
    """Whether a LaTeX toolchain is installed."""
    return shutil.which(command) is not None


def extract_errors(log: str) -> list[str]:
    """Pull the actionable lines out of a pdflatex log."""
    errors: list[str] = []
    for match in _MISSING_FILE_RE.finditer(log):
        errors.append(f"Missing file: {match.group(1)}")
    for match in _UNDEFINED_RE.finditer(log):
        context = match.group(1).strip()
        errors.append(f"Undefined control sequence: {context[:120]}")
    for match in _ERROR_RE.finditer(log):
        message = match.group(1).strip()
        if message and not any(message in existing for existing in errors):
            errors.append(message)
    # Preserve order while dropping duplicates.
    return list(dict.fromkeys(errors))[:10]


def count_pages(pdf_path: Path) -> int | None:
    """
    Page count straight from the PDF.

    Counts `/Type /Page` objects rather than shelling out to a PDF library:
    this is the one fact the validator needs and it avoids a dependency that
    would otherwise only be used here. Returns None if the file is unreadable.
    """
    try:
        data = pdf_path.read_bytes()
    except OSError:
        return None
    # /Type/Page but not /Type/Pages — the negative lookahead is what keeps
    # the page-tree node from being counted as a page.
    return len(re.findall(rb"/Type\s*/Page(?![sA-Za-z])", data)) or None


def compile_tex(
    tex: str,
    output_dir: Path,
    *,
    stem: str = "resume",
    command: str = "pdflatex",
) -> CompileResult:
    """
    Compile LaTeX source to a PDF in `output_dir`.

    Compilation happens in a temp directory so the ~6 auxiliary files pdflatex
    produces never land beside the deliverable; only the .tex and .pdf are
    copied out.
    """
    if not latex_available(command):
        return CompileResult(
            ok=False,
            errors=[
                f"{command} not found on PATH. Install a LaTeX distribution "
                "(TeX Live, MiKTeX) to generate PDF resumes."
            ],
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    tex_path = output_dir / f"{stem}.tex"
    tex_path.write_text(tex, encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="resume-build-") as tmp:
        build_dir = Path(tmp)
        (build_dir / f"{stem}.tex").write_text(tex, encoding="utf-8")

        # The style file lives with the templates, not next to the source.
        for style in (TEMPLATE_ROOT / "styles").glob("*.sty"):
            shutil.copy(style, build_dir / style.name)

        log = ""
        for pass_no in range(PASSES):
            try:
                proc = subprocess.run(
                    [
                        command,
                        "-interaction=nonstopmode",
                        "-halt-on-error",
                        f"-output-directory={build_dir}",
                        str(build_dir / f"{stem}.tex"),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=COMPILE_TIMEOUT_SECONDS,
                    cwd=build_dir,
                )
            except subprocess.TimeoutExpired:
                return CompileResult(
                    ok=False,
                    log=log,
                    errors=[f"{command} timed out after {COMPILE_TIMEOUT_SECONDS}s"],
                )
            log = proc.stdout or ""
            built = build_dir / f"{stem}.pdf"
            if not built.exists() and pass_no == 0:
                # First pass produced nothing — a second will not help.
                break

        built = build_dir / f"{stem}.pdf"
        if not built.exists():
            return CompileResult(ok=False, log=log, errors=extract_errors(log))

        pdf_path = output_dir / f"{stem}.pdf"
        shutil.copy(built, pdf_path)

    pages = count_pages(pdf_path)
    if not pages:
        # A PDF with no countable pages is a failed build that happened to
        # leave a file behind.
        return CompileResult(
            ok=False,
            pdf_path=pdf_path,
            log=log,
            errors=extract_errors(log) or ["PDF was produced but contains no pages"],
        )

    # Deliberately not gated on the exit code: pdflatex returns non-zero for
    # recoverable issues while still producing a good PDF.
    return CompileResult(ok=True, pdf_path=pdf_path, log=log, page_count=pages)
