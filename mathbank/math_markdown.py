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
_TABLE_ENVIRONMENT_RE = re.compile(
    r"\\begin\{(tabular\*?|tabularx|longtable|tblr|longtblr|talltblr)\}"
    r"[\s\S]*?\\end\{\1\}"
)
_MATH_ENVIRONMENT_RE = re.compile(
    r"\\begin\{(cases|aligned|alignedat|gathered|matrix|pmatrix|bmatrix|"
    r"Bmatrix|vmatrix|Vmatrix|smallmatrix|array|equation\*?|gather\*?|"
    r"multline\*?|split)\}[\s\S]*?\\end\{\1\}"
)
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
    "paren",
    "renewcommand",
    "textbf",
    "toprule",
}


def _replace_with_placeholders(
    value: str,
    pattern: re.Pattern[str],
    placeholders: list[tuple[str, str]],
    transform: Callable[[str], str] | None = None,
) -> str:
    def save(match: re.Match[str]) -> str:
        marker = f"\ue000{len(placeholders)}\ue001"
        original = match.group(0)
        placeholders.append((marker, transform(original) if transform else original))
        return marker

    return pattern.sub(save, value)


def _protect_non_candidates(value: str) -> tuple[str, Callable[[str], str]]:
    placeholders: list[tuple[str, str]] = []
    protected = value
    protected = _replace_with_placeholders(
        protected,
        re.compile(r"```[\s\S]*?```"),
        placeholders,
    )
    protected = _replace_with_placeholders(
        protected,
        re.compile(r"`[^`\n]*`"),
        placeholders,
    )
    protected = _replace_with_placeholders(
        protected,
        re.compile(r"!\[[^\]\n]*\]\([^\n)]*\)"),
        placeholders,
    )
    protected = _replace_with_placeholders(
        protected,
        re.compile(r"\[\[MBM_[A-Za-z0-9_:-]+\]\]"),
        placeholders,
    )
    protected = _replace_with_placeholders(
        protected,
        re.compile(r"\[ILLUSTRATION_BOX:\s*[^\]\n]*\]", re.IGNORECASE),
        placeholders,
    )
    protected = _replace_with_placeholders(
        protected,
        re.compile(r"\$\$[\s\S]*?\$\$"),
        placeholders,
    )
    protected = _replace_with_placeholders(
        protected,
        re.compile(r"\\\[[\s\S]*?\\\]"),
        placeholders,
    )
    protected = _replace_with_placeholders(
        protected,
        re.compile(r"\\\([\s\S]*?\\\)"),
        placeholders,
    )
    protected = _replace_with_placeholders(
        protected,
        re.compile(r"\$[\s\S]*?\$"),
        placeholders,
    )
    protected = _replace_with_placeholders(
        protected,
        _TABLE_ENVIRONMENT_RE,
        placeholders,
    )
    protected = _replace_with_placeholders(
        protected,
        _MATH_ENVIRONMENT_RE,
        placeholders,
        lambda environment: f"${environment.strip()}$",
    )
    patterns = (
        re.compile(r"\\(?:begin|end)\{[^}\n]+\}"),
        re.compile(r"\\item\b"),
        re.compile(r"\\fillin\b"),
        re.compile(r"\\paren\b"),
        re.compile(r"\\textbf\{[^{}\n]*\}"),
        re.compile(r"\\includegraphics(?:\s*\[[^\]\n]*\])?\s*\{[^}\n]+\}"),
        re.compile(r"</?[A-Za-z][^>\n]*>"),
    )
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


def normalize_question_math_markdown(value: str) -> str:
    """Wrap high-confidence naked LaTeX/math runs in inline delimiters.

    Existing math blocks, Markdown images, content-lock references, table
    environments and structural LaTeX commands are preserved byte-for-byte.
    Naked math environments are wrapped as a whole. Explicit typography such
    as ``\\mathbf`` or ``\\boldsymbol`` is never inferred from prose.
    """

    if not isinstance(value, str) or not value:
        return value or ""
    protected, restore = _protect_non_candidates(value)
    repaired_lines = [
        _MATH_RUN_RE.sub(_wrap_math_run, line)
        for line in protected.splitlines(keepends=True)
    ]
    return restore("".join(repaired_lines))
