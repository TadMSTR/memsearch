"""LLM config resolution — vikunja#372 (eager env: refs) and #371 (discarded --llm-* flags).

Both are the same shape: config resolution quietly does something other than what the
caller asked for, and the command still exits 0.

#372  [llm].api_key = "env:MISTRAL_API_KEY" was resolved when the config loaded, so every
      command that loads config needed that variable — including `memsearch index`, which
      makes no LLM call at all. Its failure then surfaced as a buried index error rather
      than a config error. The deployed workaround injected the key into all five callers,
      spreading a live credential to a cron and a network-facing MCP that never use it.

#371  --llm-provider / --llm-model / --llm-base-url / --llm-api-key were mapped into the
      deprecated [compact] section, while compact resolves `cfg.llm.X or cfg.compact.X`.
      With [llm] populated — as it is on forge — every flag was silently discarded. The
      failure was worse than "ignored": the command accepted the override and then raised
      an auth error from the OLD provider, pointing debugging at the wrong service.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import memsearch.cli as cli_module
from memsearch.config import resolve_config, save_config


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: dict) -> Path:
    cfg_file = tmp_path / "config.toml"
    save_config(data, cfg_file)
    monkeypatch.setattr("memsearch.config.GLOBAL_CONFIG_PATH", cfg_file)
    monkeypatch.setattr("memsearch.config.PROJECT_CONFIG_PATH", tmp_path / "nope.toml")
    return cfg_file


# --- #372: an unset LLM key must not break unrelated commands ------------------------


def test_llm_api_key_env_ref_survives_config_load(tmp_path, monkeypatch):
    """The reported bug: config must load with [llm].api_key unresolvable.

    This is forge's exact config — provider openai, Mistral base_url, key by env: ref —
    with the variable absent, which is the state every non-LLM caller runs in.
    """
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    _isolate(
        monkeypatch,
        tmp_path,
        {
            "llm": {
                "provider": "openai",
                "model": "mistral-small-latest",
                "base_url": "https://api.mistral.ai/v1",
                "api_key": "env:MISTRAL_API_KEY",
            }
        },
    )

    cfg = resolve_config()  # must not raise

    assert cfg.llm.api_key == "env:MISTRAL_API_KEY", "env ref was resolved eagerly"
    assert cfg.llm.base_url == "https://api.mistral.ai/v1"
    assert cfg.llm.provider == "openai"


def test_named_provider_env_refs_still_deferred(tmp_path, monkeypatch):
    """The pre-existing [llm.providers.*] exemption must be untouched."""
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    _isolate(
        monkeypatch,
        tmp_path,
        {
            "llm": {
                "providers": {
                    "mistral": {
                        "type": "openai-compatible",
                        "api_key": "env:MISTRAL_API_KEY",
                    }
                }
            }
        },
    )

    cfg = resolve_config()
    assert cfg.llm.providers["mistral"].api_key == "env:MISTRAL_API_KEY"


def test_non_llm_env_refs_still_resolve_eagerly(tmp_path, monkeypatch):
    """Only the LLM credentials are deferred. Everything else keeps failing fast.

    Deferring embedding.api_key or milvus.token would be a real regression: those ARE
    needed by index and search, so an unset variable should be reported at config load
    rather than as a connection error much later.
    """
    monkeypatch.setenv("TEST_EMBED_KEY", "embed-secret")
    monkeypatch.setenv("TEST_MILVUS_TOKEN", "milvus-secret")
    _isolate(
        monkeypatch,
        tmp_path,
        {
            "embedding": {"api_key": "env:TEST_EMBED_KEY"},
            "milvus": {"token": "env:TEST_MILVUS_TOKEN"},
        },
    )

    cfg = resolve_config()
    assert cfg.embedding.api_key == "embed-secret"
    assert cfg.milvus.token == "milvus-secret"


def test_missing_embedding_key_still_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("DEFINITELY_UNSET_EMBED_KEY", raising=False)
    _isolate(monkeypatch, tmp_path, {"embedding": {"api_key": "env:DEFINITELY_UNSET_EMBED_KEY"}})

    with pytest.raises(KeyError, match="DEFINITELY_UNSET_EMBED_KEY"):
        resolve_config()


def test_missing_llm_key_still_fails_loudly_at_use(monkeypatch):
    """Deferring must not mean swallowing.

    The whole risk of lazy resolution is that a genuinely missing key becomes invisible.
    It does not: compact resolves the ref before building a client, so an unset variable
    raises there with the same message it used to raise at load.
    """
    import asyncio

    from memsearch.compact import _compact_openai

    monkeypatch.delenv("DEFINITELY_UNSET_LLM_KEY", raising=False)

    with pytest.raises(KeyError, match="DEFINITELY_UNSET_LLM_KEY"):
        asyncio.run(_compact_openai("hi", "m", api_key="env:DEFINITELY_UNSET_LLM_KEY"))


# --- #371: --llm-* flags must beat a populated [llm] section -------------------------


def test_llm_flags_override_populated_llm_section(tmp_path, monkeypatch):
    """The reported bug, exactly. Every one of these used to be silently discarded."""
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    _isolate(
        monkeypatch,
        tmp_path,
        {
            "llm": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "base_url": "https://api.anthropic.com",
                "api_key": "env:MISTRAL_API_KEY",
            }
        },
    )

    overrides = cli_module._build_cli_overrides(
        llm_provider="openai",
        llm_model="mistral-small-latest",
        llm_base_url="https://api.mistral.ai/v1",
        llm_api_key="sk-explicit",
    )
    cfg = resolve_config(overrides)

    # compact reads these exact expressions (cli.py, "Resolve LLM settings").
    assert (cfg.llm.provider or cfg.compact.llm_provider) == "openai"
    assert (cfg.llm.model or cfg.compact.llm_model or None) == "mistral-small-latest"
    assert (cfg.llm.base_url or cfg.compact.base_url or None) == "https://api.mistral.ai/v1"
    assert (cfg.llm.api_key or cfg.compact.api_key or None) == "sk-explicit"


def test_llm_flags_land_in_the_llm_section():
    """Pin the mapping itself — the bug was one line of _PARAM_MAP."""
    overrides = cli_module._build_cli_overrides(
        llm_provider="openai",
        llm_model="m",
        llm_base_url="https://u",
        llm_api_key="k",
    )
    assert overrides["llm"] == {
        "provider": "openai",
        "model": "m",
        "base_url": "https://u",
        "api_key": "k",
    }
    assert "compact" not in overrides


def test_prompt_file_flag_stays_on_compact():
    """--prompt-file has no [llm] equivalent; cli.py still reads cfg.compact.prompt_file."""
    overrides = cli_module._build_cli_overrides(prompt_file="prompts/x.txt")
    assert overrides == {"compact": {"prompt_file": "prompts/x.txt"}}


def test_deprecated_compact_section_still_works_without_flags(tmp_path, monkeypatch):
    """Back-compat: a user on [compact] alone is unaffected by the remap."""
    _isolate(
        monkeypatch,
        tmp_path,
        {"compact": {"llm_provider": "gemini", "llm_model": "gemini-3-flash-preview"}},
    )

    cfg = resolve_config()
    assert (cfg.llm.provider or cfg.compact.llm_provider) == "gemini"
    assert (cfg.llm.model or cfg.compact.llm_model or None) == "gemini-3-flash-preview"


def test_llm_flag_env_ref_is_also_deferred(tmp_path, monkeypatch):
    """--llm-api-key env:VAR follows the same lazy path as the config-file form."""
    monkeypatch.delenv("SOME_UNSET_KEY", raising=False)
    _isolate(monkeypatch, tmp_path, {})

    cfg = resolve_config(cli_module._build_cli_overrides(llm_api_key="env:SOME_UNSET_KEY"))
    assert cfg.llm.api_key == "env:SOME_UNSET_KEY"
