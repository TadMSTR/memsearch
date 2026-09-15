from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _write_claude_transcript(path: Path, *, turn_uuid: str) -> None:
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "message": {"content": "start"}}),
                json.dumps({"type": "user", "uuid": turn_uuid, "message": {"content": "Summarize this session"}}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "I explained the hook behavior."}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_claude_hook_memsearch_disable_exits_before_writing_memory(tmp_path: Path) -> None:
    script = Path("plugins/claude-code/hooks/session-start.sh")
    env = {
        **os.environ,
        "MEMSEARCH_DISABLE": "1",
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "MEMSEARCH_DIR": str(tmp_path / ".memsearch"),
    }

    result = subprocess.run(
        ["bash", str(script)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    assert result.stdout.strip() == "{}"
    assert not (tmp_path / ".memsearch").exists()


def test_claude_session_start_does_not_create_journal(tmp_path: Path) -> None:
    script = Path("plugins/claude-code/hooks/session-start.sh")
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    memsearch_dir = tmp_path / ".memsearch"
    home.mkdir()
    fake_bin.mkdir()
    (home / ".memsearch").mkdir()
    (home / ".memsearch" / "config.toml").write_text("", encoding="utf-8")

    _write_executable(
        fake_bin / "memsearch",
        """#!/usr/bin/env bash
if [ "$1" = "config" ] && [ "$2" = "list" ]; then
  echo '{"embedding":{"provider":"onnx","model":"","api_key":""},"milvus":{"uri":"http://localhost:19530"}}'
  exit 0
fi
if [ "$1" = "--version" ]; then
  echo "memsearch, version 0.4.16"
  exit 0
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
echo '{"info":{"version":"0.4.16"}}'
""",
    )

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "MEMSEARCH_DIR": str(memsearch_dir),
        "MEMSEARCH_NO_WATCH": "1",
    }

    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    payload = json.loads(result.stdout)
    memory_dir = memsearch_dir / "memory"
    assert "systemMessage" in payload
    assert memory_dir.is_dir()
    assert list(memory_dir.glob("*.md")) == []


def test_claude_session_start_recent_memory_skips_empty_sessions(tmp_path: Path) -> None:
    script = Path("plugins/claude-code/hooks/session-start.sh")
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    memory = tmp_path / ".memsearch" / "memory"
    home.mkdir()
    fake_bin.mkdir()
    (home / ".memsearch").mkdir()
    (home / ".memsearch" / "config.toml").write_text("", encoding="utf-8")
    memory.mkdir(parents=True)
    (memory / "2026-01-01.md").write_text(
        """# 2026-01-01

## Session 09:00

## Session 09:01

### 09:01
- User discussed a useful migration note.

## Session 09:02
""",
        encoding="utf-8",
    )
    (memory / "zzz-scratch.md").write_text(
        """# Scratch

## Session 10:00

### 10:00
- Scratch content should not displace daily journals.
""",
        encoding="utf-8",
    )

    fake_memsearch = fake_bin / "memsearch"
    fake_memsearch.write_text(
        """#!/usr/bin/env bash
if [ "$1" = "config" ] && [ "$2" = "list" ]; then
  echo '{"embedding":{"provider":"onnx","model":"","api_key":""},"milvus":{"uri":"~/.memsearch/milvus.db"}}'
  exit 0
fi
if [ "$1" = "config" ] && [ "$2" = "get" ]; then
  case "$3" in
    embedding.provider) echo "onnx" ;;
    embedding.model) echo "" ;;
    milvus.uri) echo "~/.memsearch/milvus.db" ;;
    *) echo "" ;;
  esac
  exit 0
fi
if [ "$1" = "config" ] && [ "$2" = "set" ]; then
  exit 0
fi
if [ "$1" = "index" ]; then
  exit 0
fi
if [ "$1" = "--version" ]; then
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_memsearch.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "MEMSEARCH_DIR": str(tmp_path / ".memsearch"),
    }

    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]

    assert "User discussed a useful migration note." in context
    assert "Session 09:01" in context
    assert "Scratch content should not displace daily journals." not in context
    assert "Session 09:00" not in context
    assert "Session 09:02" not in context


def test_claude_session_start_uv_tool_upgrade_hint_preserves_extras(tmp_path: Path) -> None:
    script = Path("plugins/claude-code/hooks/session-start.sh")
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    uv_tool_bin = home / ".local" / "share" / "uv" / "tools" / "memsearch" / "bin"
    memsearch_dir = tmp_path / ".memsearch"
    home.mkdir()
    fake_bin.mkdir()
    uv_tool_bin.mkdir(parents=True)
    (home / ".memsearch").mkdir()
    (home / ".memsearch" / "config.toml").write_text("", encoding="utf-8")

    fake_memsearch = uv_tool_bin / "memsearch"
    fake_memsearch.write_text(
        """#!/usr/bin/env bash
if [ "$1" = "config" ] && [ "$2" = "list" ]; then
  echo '{"embedding":{"provider":"voyage","model":"voyage-3-lite","api_key":""},"milvus":{"uri":"http://localhost:19530"}}'
  exit 0
fi
if [ "$1" = "config" ] && [ "$2" = "get" ]; then
  case "$3" in
    embedding.provider) echo "voyage" ;;
    embedding.model) echo "voyage-3-lite" ;;
    milvus.uri) echo "http://localhost:19530" ;;
    *) echo "" ;;
  esac
  exit 0
fi
if [ "$1" = "--version" ]; then
  echo "memsearch, version 0.4.12"
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_memsearch.chmod(0o755)
    (fake_bin / "memsearch").symlink_to(fake_memsearch)

    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
echo '{"info":{"version":"0.4.13"}}'
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "MEMSEARCH_DIR": str(memsearch_dir),
        "MEMSEARCH_NO_WATCH": "1",
        "VOYAGE_API_KEY": "test-key",
    }

    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    status = json.loads(result.stdout)["systemMessage"]
    assert "UPDATE: v0.4.13 available" in status
    assert "uv tool upgrade memsearch" in status
    assert "uv tool install -U 'memsearch[onnx]'" not in status


def test_claude_session_start_reads_resolved_config_once(tmp_path: Path) -> None:
    script = Path("plugins/claude-code/hooks/session-start.sh")
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    memsearch_dir = tmp_path / ".memsearch"
    call_log = tmp_path / "memsearch-calls.txt"
    home.mkdir()
    fake_bin.mkdir()
    memsearch_dir.mkdir()
    (home / ".memsearch").mkdir()
    (home / ".memsearch" / "config.toml").write_text("", encoding="utf-8")

    fake_memsearch = fake_bin / "memsearch"
    fake_memsearch.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MEMSEARCH_CALL_LOG"
if [ "$1" = "config" ] && [ "$2" = "list" ]; then
  echo '{"embedding":{"provider":"voyage","model":"voyage-3-lite","api_key":"configured-key"},"milvus":{"uri":"http://localhost:19530"}}'
  exit 0
fi
if [ "$1" = "--version" ]; then
  echo "memsearch, version 0.4.15"
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_memsearch.chmod(0o755)

    fake_curl = fake_bin / "curl"
    fake_curl.write_text("""#!/usr/bin/env bash\necho '{"info":{"version":"0.4.15"}}'\n""", encoding="utf-8")
    fake_curl.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "MEMSEARCH_DIR": str(memsearch_dir),
        "MEMSEARCH_NO_WATCH": "1",
        "MEMSEARCH_CALL_LOG": str(call_log),
        "VOYAGE_API_KEY": "",
    }

    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    status = json.loads(result.stdout)["systemMessage"]
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert "embedding: voyage/voyage-3-lite" in status
    assert "ERROR: VOYAGE_API_KEY not set" not in status
    assert calls.count("config list --resolved --json-output") == 1
    assert not any(call.startswith("config get ") for call in calls)
    assert not any(call.startswith("skills status ") for call in calls)


def test_claude_session_start_falls_back_for_older_cli(tmp_path: Path) -> None:
    script = Path("plugins/claude-code/hooks/session-start.sh")
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    memsearch_dir = tmp_path / ".memsearch"
    call_log = tmp_path / "memsearch-calls.txt"
    home.mkdir()
    fake_bin.mkdir()
    memsearch_dir.mkdir()
    (home / ".memsearch").mkdir()
    (home / ".memsearch" / "config.toml").write_text("", encoding="utf-8")

    fake_memsearch = fake_bin / "memsearch"
    fake_memsearch.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MEMSEARCH_CALL_LOG"
if [ "$1" = "config" ] && [ "$2" = "list" ]; then
  exit 2
fi
if [ "$1" = "config" ] && [ "$2" = "get" ]; then
  case "$3" in
    embedding.provider) echo "voyage" ;;
    embedding.model) echo "voyage-3-lite" ;;
    embedding.api_key) echo "configured-key" ;;
    milvus.uri) echo "http://localhost:19530" ;;
  esac
  exit 0
fi
if [ "$1" = "--version" ]; then
  echo "memsearch, version 0.4.14"
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_memsearch.chmod(0o755)

    fake_curl = fake_bin / "curl"
    fake_curl.write_text("""#!/usr/bin/env bash\necho '{"info":{"version":"0.4.14"}}'\n""", encoding="utf-8")
    fake_curl.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "MEMSEARCH_DIR": str(memsearch_dir),
        "MEMSEARCH_NO_WATCH": "1",
        "MEMSEARCH_CALL_LOG": str(call_log),
        "VOYAGE_API_KEY": "",
    }

    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    status = json.loads(result.stdout)["systemMessage"]
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert "embedding: voyage/voyage-3-lite" in status
    assert "ERROR: VOYAGE_API_KEY not set" not in status
    assert calls.count("config list --resolved --json-output") == 1
    assert "config get embedding.provider" in calls
    assert "config get embedding.model" in calls
    assert "config get milvus.uri" in calls
    assert "config get embedding.api_key" in calls


def test_session_start_upgrade_hints_do_not_clobber_extras() -> None:
    for script in (
        Path("plugins/claude-code/hooks/session-start.sh"),
        Path("plugins/codex/hooks/session-start.sh"),
    ):
        source = script.read_text(encoding="utf-8")

        assert "uv tool install -U 'memsearch[onnx]'" not in source
        assert "pip install --upgrade 'memsearch[onnx]'" not in source
        assert "uv tool upgrade memsearch" in source
        assert "pip install --upgrade memsearch" in source


def test_session_start_recent_memory_selects_daily_journals() -> None:
    for script in (
        Path("plugins/claude-code/hooks/session-start.sh"),
        Path("plugins/codex/hooks/session-start.sh"),
    ):
        source = script.read_text(encoding="utf-8")

        assert "DAILY_JOURNAL_PATTERN" in source
        assert "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md" in source


def test_session_start_warns_when_index_state_is_unhealthy(tmp_path: Path) -> None:
    for name, script in (
        ("claude", Path("plugins/claude-code/hooks/session-start.sh")),
        ("codex", Path("plugins/codex/hooks/session-start.sh")),
    ):
        project = tmp_path / name
        home = project / "home"
        fake_bin = project / "bin"
        memsearch_dir = project / ".memsearch"
        home.mkdir(parents=True)
        fake_bin.mkdir()
        memsearch_dir.mkdir()
        (memsearch_dir / ".index-state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "error",
                    "last_error": "RuntimeError: store unavailable",
                }
            ),
            encoding="utf-8",
        )

        fake_memsearch = fake_bin / "memsearch"
        fake_memsearch.write_text(
            """#!/usr/bin/env bash
if [ "$1" = "config" ] && [ "$2" = "list" ]; then
  echo '{"embedding":{"provider":"onnx","model":"","api_key":""},"milvus":{"uri":"http://localhost:19530"}}'
  exit 0
fi
if [ "$1" = "config" ] && [ "$2" = "get" ]; then
  case "$3" in
    embedding.provider) echo "onnx" ;;
    embedding.model) echo "" ;;
    milvus.uri) echo "http://localhost:19530" ;;
    *) echo "" ;;
  esac
  exit 0
fi
if [ "$1" = "--version" ]; then
  echo "memsearch, version 0.4.14"
  exit 0
fi
exit 0
""",
            encoding="utf-8",
        )
        fake_memsearch.chmod(0o755)

        fake_curl = fake_bin / "curl"
        fake_curl.write_text("""#!/usr/bin/env bash\necho '{"info":{"version":"0.4.14"}}'\n""", encoding="utf-8")
        fake_curl.chmod(0o755)

        env = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CLAUDE_PROJECT_DIR": str(project),
            "MEMSEARCH_PROJECT_DIR": str(project),
            "MEMSEARCH_DIR": str(memsearch_dir),
            "MEMSEARCH_NO_WATCH": "1",
        }

        result = subprocess.run(
            ["bash", str(script)],
            input=json.dumps({"cwd": str(project)}),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )

        status = json.loads(result.stdout)["systemMessage"]
        assert "WARNING: memory index may be stale" in status
        assert "memory-config skill" in status


def test_session_start_shows_skill_candidate_hint(tmp_path: Path) -> None:
    hint = "SKILLS: 2 candidate skill version(s) pending install - run the memory-to-skill skill to review and install."
    for name, script in (
        ("claude", Path("plugins/claude-code/hooks/session-start.sh")),
        ("codex", Path("plugins/codex/hooks/session-start.sh")),
    ):
        project = tmp_path / name
        home = project / "home"
        fake_bin = project / "bin"
        memsearch_dir = project / ".memsearch"
        home.mkdir(parents=True)
        fake_bin.mkdir()
        memsearch_dir.mkdir()
        (memsearch_dir / "skill-candidates").mkdir()

        fake_memsearch = fake_bin / "memsearch"
        fake_memsearch.write_text(
            f"""#!/usr/bin/env bash
if [ "$1" = "config" ] && [ "$2" = "list" ]; then
  echo '{{"embedding":{{"provider":"onnx","model":"","api_key":""}},"milvus":{{"uri":"http://localhost:19530"}}}}'
  exit 0
fi
if [ "$1" = "config" ] && [ "$2" = "get" ]; then
  case "$3" in
    embedding.provider) echo "onnx" ;;
    embedding.model) echo "" ;;
    milvus.uri) echo "http://localhost:19530" ;;
    *) echo "" ;;
  esac
  exit 0
fi
if [ "$1" = "skills" ] && [ "$2" = "status" ] && [ "$3" = "--hint" ]; then
  echo "{hint}"
  exit 0
fi
if [ "$1" = "--version" ]; then
  echo "memsearch, version 0.4.14"
  exit 0
fi
exit 0
""",
            encoding="utf-8",
        )
        fake_memsearch.chmod(0o755)

        fake_curl = fake_bin / "curl"
        fake_curl.write_text("""#!/usr/bin/env bash\necho '{"info":{"version":"0.4.14"}}'\n""", encoding="utf-8")
        fake_curl.chmod(0o755)

        env = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CLAUDE_PROJECT_DIR": str(project),
            "MEMSEARCH_PROJECT_DIR": str(project),
            "MEMSEARCH_DIR": str(memsearch_dir),
            "MEMSEARCH_NO_WATCH": "1",
        }

        result = subprocess.run(
            ["bash", str(script)],
            input=json.dumps({"cwd": str(project)}),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )

        status = json.loads(result.stdout)["systemMessage"]
        assert hint in status


# Upstream stop-hook tests deliberately NOT carried onto the fork (sync to
# upstream/main @ d5809d7, 2026-08-13). Recorded here so a later reader can tell
# "intentionally retired" from "lost in a merge":
#
#   test_claude_stop_hook_writes_summary_without_safe_mode_flag
#   test_claude_stop_hook_sends_large_native_prompt_on_stdin
#       Both assert upstream's INLINE `claude -p` summarization. The forge hook
#       has no inline summarizer at all — memsearch-summarize does it out of
#       band — so there is no code path for either to exercise.
#
#   test_claude_stop_hook_avoids_empty_array_expansion_under_nounset
#       Not dropped, RENAMED. It is the same body, extended, and now runs as
#       test_claude_stop_hook_is_spool_based_without_inline_summarizer below.
#
#   test_claude_stop_hook_failure_never_persists_transcript
#       ** This one is a KNOWN GAP, not an inapplicable test. ** It asserts
#       upstream 1c82054's property: when the summarizer fails, transcript
#       content is never written to memory. The forge hook writes the raw turn
#       unconditionally and relies on memsearch-summarize to replace it, so this
#       test FAILS on forge by design of the current architecture — which is
#       precisely what orphaned 155 raw transcript blocks during the 2026-08
#       Mistral outage. Closing it means changing the spool contract (the
#       service reads the raw block back out of the memory file), not this test.
#       See PATCHES.md :: async-spool-stop-hook, and vikunja#386.


def test_claude_stop_hook_writes_raw_turn_and_spools(tmp_path: Path) -> None:
    script = Path("plugins/claude-code/hooks/stop.sh")
    plugin_root = Path("plugins/claude-code").resolve()
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    memsearch_dir = tmp_path / ".memsearch"
    transcript = tmp_path / "session-123.jsonl"
    claude_args = tmp_path / "claude-args.txt"
    home.mkdir()
    fake_bin.mkdir()
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "message": {"content": "start"}}),
                json.dumps({"type": "user", "uuid": "turn-1", "message": {"content": "Summarize this session"}}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "I explained the macOS hook issue."}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    fake_memsearch = fake_bin / "memsearch"
    fake_memsearch.write_text(
        """#!/usr/bin/env bash
if [ "$1" = "config" ] && [ "$2" = "get" ]; then
  case "$3" in
    embedding.provider) echo "onnx" ;;
    plugins.claude-code.summarize.enabled) echo "true" ;;
    plugins.claude-code.summarize.model) echo "" ;;
    plugins.claude-code.summarize.provider) echo "" ;;
    prompts.summarize) echo "" ;;
    *) echo "" ;;
  esac
  exit 0
fi
if [ "$1" = "index" ]; then
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_memsearch.chmod(0o755)

    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        """#!/usr/bin/env bash
if [ "${1:-}" = "--help" ]; then
  echo "Usage: claude"
  exit 0
fi
printf '%s\n' "$@" > "$CLAUDE_ARGS_FILE"
echo "- User discussed a macOS stop hook regression."
""",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "MEMSEARCH_DIR": str(memsearch_dir),
        "CLAUDE_ARGS_FILE": str(claude_args),
    }
    result = subprocess.run(
        ["bash", str(script)],
        input=json.dumps({"transcript_path": str(transcript)}),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    # Forge divergence: stop.sh writes the RAW parsed turn to the daily memory
    # file and drops a spool entry for the out-of-band memsearch-summarize
    # service. It never shells out to `claude` for inline summarization (that
    # upstream path was removed — see the hook header).
    memory_files = list((memsearch_dir / "memory").glob("*.md"))
    assert result.stdout.strip() == "{}"
    assert len(memory_files) == 1
    memory_text = memory_files[0].read_text(encoding="utf-8")
    assert "[User]: Summarize this session" in memory_text
    assert "I explained the macOS hook issue." in memory_text

    # A spool entry is dropped for the async summarizer.
    spool_files = list((memsearch_dir / "spool").glob("*.json"))
    assert len(spool_files) == 1

    # `claude` is never invoked, so its args file is never written.
    assert not claude_args.exists()


def test_claude_stop_hook_groups_session_headings(tmp_path: Path) -> None:
    script = Path("plugins/claude-code/hooks/stop.sh")
    plugin_root = Path("plugins/claude-code").resolve()
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    memsearch_dir = tmp_path / ".memsearch"
    transcript_a = tmp_path / "session-a.jsonl"
    transcript_b = tmp_path / "session-b.jsonl"
    home.mkdir()
    fake_bin.mkdir()
    _write_claude_transcript(transcript_a, turn_uuid="turn-a")
    _write_claude_transcript(transcript_b, turn_uuid="turn-b")

    _write_executable(
        fake_bin / "memsearch",
        """#!/usr/bin/env bash
if [ "$1" = "config" ] && [ "$2" = "get" ]; then
  case "$3" in
    embedding.provider) echo "onnx" ;;
    plugins.claude-code.summarize.enabled) echo "true" ;;
    plugins.claude-code.summarize.model) echo "" ;;
    plugins.claude-code.summarize.provider) echo "" ;;
    prompts.summarize) echo "" ;;
    *) echo "" ;;
  esac
  exit 0
fi
if [ "$1" = "index" ]; then
  exit 0
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "claude",
        """#!/usr/bin/env bash
if [ "${1:-}" = "--help" ]; then
  echo "Usage: claude"
  exit 0
fi
echo "- Captured a session summary."
""",
    )
    _write_executable(
        fake_bin / "date",
        """#!/usr/bin/env bash
case "${1:-}" in
  +%Y-%m-%d) echo "2026-07-23" ;;
  +%H:%M) echo "12:34" ;;
  *) /bin/date "$@" ;;
esac
""",
    )

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "MEMSEARCH_DIR": str(memsearch_dir),
    }

    def run_stop(transcript: Path) -> None:
        result = subprocess.run(
            ["bash", str(script)],
            input=json.dumps({"transcript_path": str(transcript)}),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert result.stdout.strip() == "{}"

    memory_file = memsearch_dir / "memory" / "2026-07-23.md"

    run_stop(transcript_a)
    memory_text = memory_file.read_text(encoding="utf-8")
    assert memory_text.count("## Session 12:34") == 1
    assert memory_text.count("### 12:34") == 1
    assert memory_text.count("session:session-a ") == 1

    run_stop(transcript_a)
    memory_text = memory_file.read_text(encoding="utf-8")
    assert memory_text.count("## Session 12:34") == 1
    assert memory_text.count("### 12:34") == 2
    assert memory_text.count("session:session-a ") == 2

    run_stop(transcript_b)
    memory_text = memory_file.read_text(encoding="utf-8")
    sections = memory_text.split("## Session 12:34")
    assert len(sections) == 3
    assert sections[1].count("session:session-a ") == 2
    assert "session:session-b " not in sections[1]
    assert sections[2].count("session:session-b ") == 1


def test_claude_stop_hook_is_spool_based_without_inline_summarizer() -> None:
    # The forge stop.sh removed upstream's inline `claude -p` summarization (and
    # its CLAUDE_SAFE_MODE_ARGS array). It must not reference the unsafe empty-
    # array expansion, and must drive the async spool contract instead.
    script = Path("plugins/claude-code/hooks/stop.sh")
    source = script.read_text(encoding="utf-8")

    assert '"${CLAUDE_SAFE_MODE_ARGS[@]}"' not in source
    assert "CLAUDE_SAFE_MODE_ARG" not in source
    assert "parse-transcript.sh" in source
    assert "spool" in source


# ---------------------------------------------------------------------------
# UserPromptSubmit injection telemetry (vikunja#853)
# ---------------------------------------------------------------------------
# The hook injects memory into every qualifying prompt and, before this suite,
# recorded nothing about it. These tests pin the telemetry contract: a record
# on EVERY exit path (the misses are the control for "rarely fires" vs "fires
# often, rarely used"), owner-only mode, a bounded file, and — most
# importantly — that instrumenting the hook did not break the thing being
# measured. A telemetry patch that silently killed the injection would produce
# a very convincing "nobody uses it" result.

_UPS_HOOK = "plugins/claude-code/hooks/user-prompt-submit.sh"

_UPS_RESULTS = """[{"source":"/home/ted/.claude/memory/docs/alpha.md","content":"Alpha body.","heading":"H1","score":0.91},
 {"source":"/home/ted/.claude/projects/p/.memsearch/memory/s.md","content":"Session body.","heading":"","score":0.52},
 {"source":"/home/ted/.claude/memory/shared/w.md","content":"Working body.","heading":"H3","score":0.31}]"""


def _write_fake_memsearch(path: Path, *, mode: str = "hits") -> None:
    """Stub the memsearch CLI. `mode` selects the search behaviour under test."""
    if mode == "hits":
        body = f"cat <<'J'\n{_UPS_RESULTS}\nJ\n  exit 0"
    elif mode == "empty":
        body = 'echo "[]"\n  exit 0'
    elif mode == "fail":
        body = "exit 3"
    else:  # pragma: no cover - guards against a typo in a future test
        raise ValueError(mode)
    _write_executable(
        path,
        f"""#!/usr/bin/env bash
if [ "$1" = "search" ]; then
  {body}
fi
exit 0
""",
    )


def _run_prompt_hook(
    tmp_path: Path,
    prompt: str,
    *,
    mode: str = "hits",
    log: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    home.mkdir(exist_ok=True)
    fake_bin.mkdir(exist_ok=True)
    _write_fake_memsearch(fake_bin / "memsearch", mode=mode)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "MEMSEARCH_DIR": str(tmp_path / ".memsearch"),
    }
    if log is not None:
        env["MEMSEARCH_INJECTION_LOG"] = str(log)
    if extra_env:
        env.update(extra_env)

    payload = json.dumps(
        {
            "prompt": prompt,
            "session_id": "sess-0001",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/home/ted/project",
        }
    )
    return subprocess.run(
        ["bash", _UPS_HOOK],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )


def _log_records(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_user_prompt_submit_logs_an_injection(tmp_path: Path) -> None:
    log = tmp_path / "injection-log.jsonl"
    result = _run_prompt_hook(tmp_path, "milvus tuning telemetry injection", log=log)

    # The injection itself must still happen — this is the subject of the
    # measurement, and a broken injection would fake a "nobody uses it" result.
    payload = json.loads(result.stdout)
    assert payload["systemMessage"].startswith("[memory] Relevant context auto-injected:")
    assert "Alpha body." in payload["systemMessage"]

    (record,) = _log_records(log)
    assert record["outcome"] == "injected"
    assert record["session_id"] == "sess-0001"
    assert record["transcript_path"] == "/tmp/transcript.jsonl"
    assert record["cwd"] == "/home/ted/project"
    assert record["keywords"] == ["milvus", "tuning", "telemetry", "injection"]
    assert record["prompt_len"] == len("milvus tuning telemetry injection")
    # Top-2 injected, out of 3 returned — the cap is what is being measured.
    assert record["results_total"] == 3
    assert [r["source"] for r in record["results"]] == [
        "/home/ted/.claude/memory/docs/alpha.md",
        "/home/ted/.claude/projects/p/.memsearch/memory/s.md",
    ]
    assert [r["score"] for r in record["results"]] == [0.91, 0.52]
    # injected_bytes must be the real payload size, not a placeholder.
    assert record["injected_bytes"] == len(payload["systemMessage"].encode("utf-8"))
    assert record["injected_bytes"] > 0


def test_user_prompt_submit_logs_the_misses_not_just_the_injections(tmp_path: Path) -> None:
    """The control. A success-only log cannot tell 'rarely fires' from
    'fires often, rarely used', which is the whole question."""
    log = tmp_path / "injection-log.jsonl"

    _run_prompt_hook(tmp_path, "hi", log=log)
    _run_prompt_hook(tmp_path, "is it the one that we do so as no", log=log)
    _run_prompt_hook(tmp_path, "milvus tuning telemetry injection", mode="empty", log=log)
    _run_prompt_hook(tmp_path, "milvus tuning telemetry injection", mode="fail", log=log)

    records = _log_records(log)
    assert [r["outcome"] for r in records] == [
        "prompt_too_short",
        "no_keywords",
        "search_empty",
        "search_failed",
    ]
    assert all(r["results"] == [] and r["injected_bytes"] == 0 for r in records)
    assert records[0]["prompt_len"] == 2
    # A failed search must be distinguishable from one that returned nothing:
    # they are different answers to "why did nothing inject".
    assert records[2]["search_rc"] == 0
    assert records[3]["search_rc"] == 3


def test_user_prompt_submit_log_is_owner_only(tmp_path: Path) -> None:
    """The record carries prompt-derived keywords past only a stopword filter
    and a 4-char minimum, so a pasted credential can survive into it."""
    log = tmp_path / "injection-log.jsonl"
    _run_prompt_hook(tmp_path, "milvus tuning telemetry injection", log=log)
    assert log.stat().st_mode & 0o777 == 0o600


def test_user_prompt_submit_survives_an_unwritable_log(tmp_path: Path) -> None:
    """A telemetry failure must never cost the user their prompt."""
    log = tmp_path / "injection-log.jsonl"

    # Establish the writer is actually live first. Without this the test passes
    # against any hook that never writes a log at all, including the one this
    # replaced — "no record appeared" would be vacuously true.
    _run_prompt_hook(tmp_path, "milvus tuning telemetry injection", log=log)
    assert len(_log_records(log)) == 1

    log.chmod(0o000)
    try:
        result = _run_prompt_hook(tmp_path, "milvus tuning telemetry injection", log=log)
        payload = json.loads(result.stdout)
        assert payload["systemMessage"].startswith("[memory] Relevant context auto-injected:")
        # Silent, too: a failing append must not leak to stderr on every prompt.
        assert result.stderr == ""
    finally:
        log.chmod(0o600)
    assert len(_log_records(log)) == 1  # unchanged — the failed write was dropped


def test_user_prompt_submit_log_is_bounded(tmp_path: Path) -> None:
    """Single-generation size rotation — on-disk use is capped at ~2x the cap
    rather than growing unattended."""
    log = tmp_path / "injection-log.jsonl"
    log.write_text("x" * 4096, encoding="utf-8")
    for _ in range(3):
        _run_prompt_hook(
            tmp_path,
            "milvus tuning telemetry injection",
            log=log,
            extra_env={"MEMSEARCH_INJECTION_LOG_MAX_BYTES": "1024"},
        )

    assert (tmp_path / "injection-log.jsonl.1").exists()
    # The pre-seeded 4096 bytes of padding are gone: rotation actually moved
    # the file rather than appending forever.
    assert "x" * 4096 not in log.read_text(encoding="utf-8")
    assert len(_log_records(log)) == 1


def test_user_prompt_submit_telemetry_can_be_disabled(tmp_path: Path) -> None:
    log = tmp_path / "injection-log.jsonl"

    # Same guard as above: prove logging is on by default before asserting the
    # switch turns it off, or "no log" is true for the wrong reason.
    _run_prompt_hook(tmp_path, "milvus tuning telemetry injection", log=log)
    assert log.exists()
    log.unlink()

    result = _run_prompt_hook(
        tmp_path,
        "milvus tuning telemetry injection",
        log=log,
        extra_env={"MEMSEARCH_INJECTION_LOG_DISABLE": "1"},
    )
    assert "systemMessage" in json.loads(result.stdout)
    assert not log.exists()
