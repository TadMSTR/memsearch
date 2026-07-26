from __future__ import annotations

import json
import subprocess
from pathlib import Path

SCRIPT = Path("plugins/claude-code/hooks/parse-transcript.sh")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _run_parse(path: Path) -> str:
    result = subprocess.run(
        ["bash", str(SCRIPT), str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_claude_parse_transcript_omits_successful_tool_output_content(tmp_path: Path) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "uuid": "u1", "message": {"content": "Check the current version"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "tail memory.md"}},
                        {"type": "text", "text": "Checking"},
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "stale fact from old journal: memsearch 0.4.4",
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Current version is 0.4.5."}]},
            },
        ],
    )

    output = _run_parse(transcript)

    assert "[User]: Check the current version" in output
    assert "[Claude Code calls tool]" not in output
    assert "[Tool output]" not in output
    assert "stale fact" not in output
    assert "0.4.4" not in output
    assert "Current version is 0.4.5." in output


def test_claude_parse_transcript_omits_tool_error_content(tmp_path: Path) -> None:
    transcript = tmp_path / "claude-error.jsonl"
    error_text = "prefix " + ("x" * 1200) + " final traceback marker"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "uuid": "u1", "message": {"content": "Debug the failure"}},
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": error_text,
                        }
                    ]
                },
            },
        ],
    )

    output = _run_parse(transcript)

    assert "[Tool error]" not in output
    assert "final traceback marker" not in output
    assert "prefix " not in output


def test_claude_parse_transcript_drops_ismeta_skill_body(tmp_path: Path) -> None:
    # Skill invocations render the whole SKILL.md into the transcript as an
    # isMeta:true user turn beginning "Base directory for this skill:". It must
    # never reach the memory file.
    transcript = tmp_path / "claude-skill.jsonl"
    skill_body = (
        "Base directory for this skill: /home/ted/.claude/skills/git-config-tracking\n\n"
        "# Git Config Tracking Skill\n\n"
        "## Tracked Paths\n- ~/docker/ -> host-forge/stacks\n"
        "### access: readonly\n- Refuse the edit entirely.\n"
    )
    _write_jsonl(
        transcript,
        [
            {"type": "user", "uuid": "u1", "message": {"content": "commit the pipeline fix"}},
            {
                "type": "user",
                "isMeta": True,
                "message": {"content": [{"type": "text", "text": skill_body}]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Committed on a feature branch."}]},
            },
        ],
    )

    output = _run_parse(transcript)

    assert "[User]: commit the pipeline fix" in output
    assert "Committed on a feature branch." in output
    assert "Base directory for this skill:" not in output
    assert "Git Config Tracking Skill" not in output
    assert "access: readonly" not in output


def test_claude_parse_transcript_drops_non_ismeta_command_wrapper(tmp_path: Path) -> None:
    # Slash-command invocation wrappers are plain (NON-isMeta) string turns
    # beginning "<command-name>". The isMeta guard does not catch these; the
    # injected-prefix guard must.
    transcript = tmp_path / "claude-command.jsonl"
    wrapper = (
        "<command-name>/login</command-name>\n"
        "            <command-message>login</command-message>\n"
        "            <command-args></command-args>"
    )
    _write_jsonl(
        transcript,
        [
            {"type": "user", "uuid": "u1", "message": {"content": "deploy the service"}},
            {"type": "user", "uuid": "u2", "message": {"content": wrapper}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Deploying now."}]},
            },
        ],
    )

    output = _run_parse(transcript)

    assert "[User]: deploy the service" in output
    assert "Deploying now." in output
    assert "<command-name>" not in output
    assert "/login" not in output


def test_claude_parse_transcript_skips_command_wrapper_as_last_turn(tmp_path: Path) -> None:
    # find_last_turn_start() must not anchor the turn on a command-wrapper turn;
    # otherwise the real preceding user question is dropped from the slice.
    transcript = tmp_path / "claude-command-last.jsonl"
    wrapper = "<command-name>/status</command-name>\n<command-message>status</command-message>"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "uuid": "u1", "message": {"content": "what is the deploy status?"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "All green."}]},
            },
            {"type": "user", "uuid": "u2", "message": {"content": wrapper}},
        ],
    )

    output = _run_parse(transcript)

    assert "[User]: what is the deploy status?" in output
    assert "All green." in output
    assert "<command-name>" not in output


def test_injected_prefix_drop_is_intentional_tradeoff(tmp_path: Path) -> None:
    # Documents an accepted trade-off (audit INFO, 2026-07-26): the injected-prefix
    # guard is exact-prefix-anchored, so a *real* user turn that literally begins with
    # one of the four markers is dropped rather than preserved. This favours dropping
    # over leaking skill/command bodies — the correct bias for the contamination fix.
    # This test pins that behaviour so the prefix list can't be silently widened
    # without a conscious update here.
    transcript = tmp_path / "claude-falsepos.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "uuid": "u0", "message": {"content": "explain the injection markers"}},
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "<command-name> is the tag Claude Code injects, FYI"},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Noted."}]},
            },
        ],
    )

    output = _run_parse(transcript)

    # The turn beginning with the marker is dropped (accepted trade-off); the real
    # preceding turn and the assistant reply are preserved.
    assert "[User]: explain the injection markers" in output
    assert "Noted." in output
    assert "is the tag Claude Code injects" not in output
