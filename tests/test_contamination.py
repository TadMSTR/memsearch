from __future__ import annotations

from memsearch.contamination import (
    build_compact_fallback_note,
    detect_contamination,
)


def test_clean_summary_passes() -> None:
    summary = (
        "- User asked to fix the reindex race in memsearch.\n"
        "- developer traced the delete path and added the project dirs to watched paths.\n"
    )
    raw = "some unrelated chunk content about ollama and gpu memory"
    assert detect_contamination(summary, raw) is None


def test_placeholder_token_flagged() -> None:
    assert detect_contamination("- Completed <build-name> for <specific step>.", "raw") == "placeholder_token"


def test_residue_token_flagged() -> None:
    assert detect_contamination("- Wrote N docs and closed M tickets.", "raw") == "residue_token"


def test_date_template_residue_flagged() -> None:
    assert detect_contamination("- Filed on YYYY-MM-DD.", "raw") == "residue_token"


def test_template_signature_flagged() -> None:
    assert detect_contamination("### Phase 1 — Skill-invocation sentinel strip", "raw") == "template_signature"


def test_skill_sentinel_signature_flagged() -> None:
    summary = "Base directory for this skill: /home/ted/.claude/skills/git-config-tracking"
    assert detect_contamination(summary, "raw") == "template_signature"


def test_verbatim_overlap_flagged() -> None:
    body = (
        "Forge has several version-controlled directories tracked in Gitea today.\n"
        "Files in these paths require a pre-edit commit before any change.\n"
        "A post-edit commit records what changed and pushes it to the remote.\n"
    )
    # summary reproduces >= 3 consecutive non-trivial lines from raw verbatim
    assert detect_contamination(body, body) == "verbatim_overlap"


def test_empty_summary_flagged() -> None:
    assert detect_contamination("", "raw") == "empty"
    assert detect_contamination("   \n", "raw") == "empty"


def test_ordinary_prose_not_flagged_by_residue() -> None:
    # "option A", "plan B", "N/A", "500M" must NOT trip the residue regexes.
    summary = "- Chose option A over plan B; the 500M model is N/A for this host."
    assert detect_contamination(summary, "raw") is None


def test_fallback_note_names_source_without_leaking() -> None:
    note = build_compact_fallback_note("/x/.memsearch/memory/2026-07-26.md")
    assert "2026-07-26.md" in note
    assert "contamination guard" in note
    # the note itself must be clean
    assert detect_contamination(note, "raw") is None


def test_fallback_note_without_source() -> None:
    note = build_compact_fallback_note(None)
    assert "contamination guard" in note
    assert detect_contamination(note, "raw") is None
