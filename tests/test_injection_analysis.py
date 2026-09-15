"""Tests for scripts/injection-analysis.py (vikunja#853).

The script is a standalone CLI, not a package module, so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "injection-analysis.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("injection_analysis", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ia = _load_module()


def _injected(transcript: str, session: str = "s") -> dict:
    return {
        "ts": "2026-09-15T10:00:00Z",
        "session_id": session,
        "transcript_path": transcript,
        "cwd": "/home/ted/.claude/projects/developer",
        "prompt_len": 40,
        "keywords": ["alpha"],
        "results": [{"source": "/m/a.md", "score": 0.5, "tier": "working", "heading": "", "snippet_chars": 9}],
        "results_total": 1,
        "injected_bytes": 100,
        "search_rc": 0,
        "outcome": "injected",
    }


def _write_transcript(path: Path, text: str = "hello") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-09-15T10:01:00Z",
                "message": {"content": [{"type": "text", "text": text}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("proj/session.jsonl", True),
        ("../outside.jsonl", False),
        ("proj/../../outside.jsonl", False),
    ],
)
def test_transcript_containment(tmp_path: Path, relative: str, expected: bool) -> None:
    """Containment resolves before comparing, so `..` cannot escape the root."""
    root = tmp_path / "projects"
    root.mkdir()
    assert ia.transcript_allowed(str(root / relative), root) is expected


def test_transcript_containment_disabled_allows_anything(tmp_path: Path) -> None:
    assert ia.transcript_allowed(str(tmp_path / "anywhere.jsonl"), None) is True


def test_rejected_transcripts_are_counted_separately_from_missing(tmp_path: Path) -> None:
    """A rejected path is a measurement the script declined to take, not one the
    data never had. Folding the two together would let containment quietly
    shrink the denominator and flatter every rate derived from it."""
    root = tmp_path / "projects"
    inside = root / "proj" / "ok.jsonl"
    _write_transcript(inside)
    outside = tmp_path / "elsewhere" / "bait.jsonl"
    _write_transcript(outside)

    records = [
        _injected(str(inside), "a"),
        _injected(str(outside), "b"),
        _injected(str(root / "proj" / "gone.jsonl"), "c"),  # inside root, absent
    ]

    contained = ia.correlate(records, root)
    assert contained["analysed"] == 1
    assert contained["transcript_rejected"] == 1
    assert contained["rejected_paths"] == [str(outside)]
    # The absent-but-allowed one is 'missing', NOT 'rejected' — the two counters
    # must not absorb each other.
    assert contained["transcript_missing"] == 1

    # With containment off the bait becomes analysable, which proves the
    # rejection above was doing the work and not something incidental.
    allowed = ia.correlate(records, None)
    assert allowed["analysed"] == 2
    assert allowed["transcript_rejected"] == 0


def test_correlation_detects_a_reference_and_ignores_a_non_match(tmp_path: Path) -> None:
    """Guards the positive signal itself: a green containment test would be
    worthless if correlate() had stopped detecting references at all."""
    root = tmp_path / "projects"
    hit = root / "p" / "hit.jsonl"
    _write_transcript(hit, "as recorded in ssrf-guard-notes.md the answer is X")
    miss = root / "p" / "miss.jsonl"
    _write_transcript(miss, "an answer that cites nothing at all")

    def rec(path: Path, session: str) -> dict:
        r = _injected(str(path), session)
        r["results"] = [
            {
                "source": "/home/ted/.claude/memory/shared/ssrf-guard-notes.md",
                "score": 0.8,
                "tier": "working",
                "heading": "",
                "snippet_chars": 40,
            }
        ]
        return r

    assert ia.correlate([rec(hit, "h")], root)["referenced"] == 1
    assert ia.correlate([rec(miss, "m")], root)["referenced"] == 0
