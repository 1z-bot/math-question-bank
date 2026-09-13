"""Conservative normalization for mixed Chinese text and LaTeX math.

The AI import paths occasionally return valid LaTeX commands without math
delimiters.  This module repairs only high-confidence math-shaped runs while
leaving ordinary prose and document-structure commands untouched.
"""

from __future__ import annotations

import re
from collections.abc import Callable


_MATH_RUN_RE = re.compile(r"[A-Za-z0-9\\{}_^+\-*/=<>|(),.:\[\]\t ]+")
_MATH_COMMAND_RE = re.compile(r"\\([A-Za-z]+)")
_NON_MATH_COMMANDS = {
    "begin",
    "bottomrule",
    "centering",
    "cline",
    "end",
    "hline",
    "item",
    "fillin",
    "includegraphics",
    "midrule",
    "multicolumn",
    "multirow",
    "renewcommand",
    "textbf",
    "toprule",
}


def _replace_with_placeholders(
    value: str,
    pattern: re.Pattern[str],
    placeholders: list[tuple[str, str]],
) -> str:
    def save(match: re.Match[str]) -> str:
        marker = f"\ue000{len(placeholders)}\ue001"
        placeholders.append((marker, match.group(0)))
        return marker

    return pattern.sub(save, value)


def _protect_non_candidates(value: str) -> tuple[str, Callable[[str], str]]:
    placeholders: list[tuple[str, str]] = []
    patterns = (
        re.compile(r"```[\s\S]*?```"),
        re.compile(r"`[^`\n]*`"),
        re.compile(r"!\[[^\]\n]*\]\([^\n)]*\)"),
        re.compile(r"\[\[MBM_[A-Za-z0-9_:-]+\]\]"),
        re.compile(r"\$\$[\s\S]*?\$\$"),
        re.compile(r"\\\[[\s\S]*?\\\]"),
        re.compile(r"\\\([\s\S]*?\\\)"),
        re.compile(r"\$[^$\n]+?\$"),
        re.compile(r"\\(?:begin|end)\{[^}\n]+\}"),
        re.compile(r"\\item\b"),
        re.compile(r"\\fillin\b"),
        re.compile(r"\\textbf\{[^{}\n]*\}"),
        re.compile(r"\\includegraphics(?:\s*\[[^\]\n]*\])?\s*\{[^}\n]+\}"),
        re.compile(r"</?[A-Za-z][^>\n]*>"),
    )
    protected = value
    for pattern in patterns:
        protected = _replace_with_placeholders(protected, pattern, placeholders)

    def restore(result: str) -> str:
        for marker, original in reversed(placeholders):
            result = result.replace(marker, original)
        return result

    return protected, restore


def _is_high_confidence_math(value: str) -> bool:
    commands = {
        command.lower() for command in _MATH_COMMAND_RE.findall(value)
    } - _NON_MATH_COMMANDS
    if commands:
        return True
    if re.search(r"[A-Za-z0-9})\]]\s*[_^](?:\s*\{|\s*[A-Za-z0-9\\])", value):
        return True
    if re.search(
        r"[A-Za-z0-9})\]]\s*(?:=|<|>)\s*(?:[A-Za-z0-9({\[\\+\-])",
        value,
    ):
        return True
    if re.search(r"\b[A-Za-z]\s*\([^)]*[A-Za-z0-9_+\-,\\][^)]*\)", value):
        return True
    if re.search(r"\(\s*[+\-]?(?:\d+(?:\.\d+)?|[A-Za-z])\s*,[^)]*\)", value):
        return True
    return False


def _wrap_math_run(match: re.Match[str]) -> str:
    raw = match.group(0)
    leading_length = len(raw) - len(raw.lstrip())
    trailing_length = len(raw) - len(raw.rstrip())
    leading = raw[:leading_length]
    trailing = raw[len(raw) - trailing_length :] if trailing_length else ""
    core_end = len(raw) - trailing_length if trailing_length else len(raw)
    core = raw[leading_length:core_end]
    if not core or not _is_high_confidence_math(core):
        return raw
    return f"{leading}${core}${trailing}"


def _normalize_vector_commands(value: str) -> str:
    if not re.search(r"向量|vector", value, re.IGNORECASE):
        return value
    return re.sub(
        r"\\mathbf\s*\{\s*([a-z])\s*\}",
        r"\\boldsymbol{\1}",
        value,
    )


def normalize_question_math_markdown(value: str) -> str:
    """Wrap high-confidence naked LaTeX/math runs in inline delimiters.

    Existing math blocks, Markdown images, content-lock references and
    structural LaTeX commands are preserved byte-for-byte.  Lowercase
    ``\\mathbf`` symbols are converted to ``\\boldsymbol`` only when the
    surrounding question explicitly describes vectors.
    """

    if not isinstance(value, str) or not value:
        return value or ""
    normalized = _normalize_vector_commands(value)
    protected, restore = _protect_non_candidates(normalized)
    repaired_lines = [
        _MATH_RUN_RE.sub(_wrap_math_run, line)
        for line in protected.splitlines(keepends=True)
    ]
    return restore("".join(repaired_lines))
