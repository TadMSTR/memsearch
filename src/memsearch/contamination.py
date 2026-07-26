"""Deterministic contamination detection for generated summaries.

A backstop against template/skill text being reproduced verbatim in a compact
or summarize output instead of being described. The primary defense lives in
``plugins/claude-code/hooks/parse-transcript.sh`` (isMeta + injected-prefix
guards), which stops skill/command bodies from ever reaching a memory file.
This filter catches the residual case where a contaminated *historical* raw
file is re-read by ``compact()`` and the model faithfully copies its structure.

The detection logic is intentionally kept in lock-step with the summarize-layer
implementation in ``host-forge-scripts/scripts/memsearch-summarize.py``. That
copy runs as a standalone PM2 service in a different repo; unifying the two into
a single shared import is tracked as a follow-up (would couple host-forge-scripts
to a pinned memsearch version). Keep the two regex sets in sync until then.
"""

from __future__ import annotations

import re

# Angle-bracket placeholder tokens like <name>, <specific step>, <build-name>.
# Template fill-ins, essentially never legitimate in a third-person summary.
_PLACEHOLDER_RE = re.compile(r"<[a-zA-Z][a-zA-Z0-9 _./-]{0,40}>")

# Known skill/template signatures that mean the model reproduced source structure.
_TEMPLATE_SIGNATURES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?m)^\s*#{1,6}\s+Phase\s+\d"),  # "### Phase 1 — ..."
    re.compile(r"(?m)^\s*#{1,6}\s+Configuration\s*$"),  # bare "## Configuration" heading
    re.compile(r"<specific step>"),
    re.compile(r"<Env vars"),
    re.compile(r"(?m)^\s*Base directory for this skill:"),
)

# Bare template residue the angle-bracket regex misses: unfilled count letters
# (N, M), XX-style numeric placeholders, and unfilled date templates. Anchored to
# placeholder-like contexts so ordinary prose is not flagged.
_RESIDUE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:[NM]|XX)\b(?=\s+[A-Za-z])"),
    re.compile(r"\b[NM]/[NM0-9]\b|\b\d+/[NM]\b"),
    re.compile(r"\bYYYY(?:-MM(?:-(?:DD|XX))?)?\b|\b\d{4}-\d{2}-XX\b"),
)

_MIN_OVERLAP_LINES = 3  # N consecutive identical non-trivial lines = verbatim copy
_MIN_OVERLAP_LEN = 20  # ignore short/generic lines when comparing

# Appended to the compact prompt on a retry after the first attempt tripped the
# guard. Must not contain literal "{" / "}" — the prompt is passed through
# str.format(chunks=...).
COMPACT_RETRY_REMINDER = (
    "\n\nIMPORTANT: a previous attempt COPIED template/skill text verbatim. Do NOT "
    "reproduce any Markdown headers, numbered/phase step lists, or bracketed placeholder "
    "tokens from the chunks. Use the ACTUAL numbers and dates from the source instead of "
    "count letters like N, M, or XX or YYYY-MM-DD templates; if a value is unknown, omit "
    "it. Output ONLY a factual third-person summary of what the chunks contain. If a skill "
    "or template appears in the chunks, state only that it was referenced and for what "
    "purpose — never reproduce its contents."
)


def _normalize_line(s: str) -> str:
    return s.strip().lstrip("-*# ").strip()


def detect_contamination(summary: str, raw: str) -> str | None:
    """Return a short reason string if *summary* looks like copied template/skill
    text, else ``None``. Purely deterministic — no model involved.

    Signals: (a) an unresolved ``<...>`` placeholder token; (b) bare template
    residue (N/M count placeholders, XX / YYYY-MM-DD date templates) the ``<...>``
    regex misses; (c) a known template/skill signature; (d) at least
    ``_MIN_OVERLAP_LINES`` consecutive non-trivial lines reproduced verbatim from
    the raw source.
    """
    if not summary or not summary.strip():
        return "empty"

    if _PLACEHOLDER_RE.search(summary):
        return "placeholder_token"

    for res in _RESIDUE_RES:
        if res.search(summary):
            return "residue_token"

    for sig in _TEMPLATE_SIGNATURES:
        if sig.search(summary):
            return "template_signature"

    raw_lines = [n for n in (_normalize_line(line) for line in raw.splitlines()) if len(n) >= _MIN_OVERLAP_LEN]
    sum_lines = [n for n in (_normalize_line(line) for line in summary.splitlines()) if len(n) >= _MIN_OVERLAP_LEN]
    if len(sum_lines) >= _MIN_OVERLAP_LINES and raw_lines:
        raw_blob = "\n".join(raw_lines)
        for i in range(len(sum_lines) - _MIN_OVERLAP_LINES + 1):
            window = "\n".join(sum_lines[i : i + _MIN_OVERLAP_LINES])
            if window in raw_blob:
                return "verbatim_overlap"

    return None


def build_compact_fallback_note(source: str | None) -> str:
    """Deterministic note used when the model keeps regurgitating template text.

    Emits no source content at all — only a suppression marker naming the source
    (a filename, not free text), so it can never re-leak the disallowed content.
    """
    src = f" ({source})" if source else ""
    return (
        f"- Memory compact suppressed for this source{src}: the summarizer reproduced "
        "template/skill text it could not describe without copying it (contamination guard)."
    )
