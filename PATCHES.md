# Carried patches — forge fork

Manifest for `shared-fork-upstream-sync`. Lives on `forge-main` only; it must never land
on `main`, which tracks upstream.

Each entry carries a **probe**: a command that exits 0 iff upstream now covers the patch,
so a later sync can classify it mechanically instead of from memory.

> **This manifest is INCOMPLETE.** It was started during
> `doc-cache-mcp-integrity-2026-08` to record the patch below, as that build's plan
> required. The ~9 earlier forge patches on `forge-main` (isMeta transcript guard, hook
> PATH fix, UserPromptSubmit injection, async spool stop hook, compact output-name/date
> keying, contamination guard, ThinkingBlock fix, …) are **not yet entered**. Backfilling
> them is `shared-fork-upstream-sync` Step 0 and is tracked separately — do not treat the
> absence of an entry here as "not a carried patch".

---

## llm-config-resolution

- **status:** fork-only
- **commits:** see `git log --grep="vikunja#37[12]" forge-main`
- **upstream-pr:** none — worth upstreaming, neither bug is forge-specific
- **files:** `src/memsearch/config.py`, `src/memsearch/cli.py`
- **why:** Two independent config-resolution bugs. (1) `_resolve_env_refs_in_dict`
  resolved `[llm].api_key` eagerly, so every command that loads config — `index`,
  `search`, `status` — failed when the referenced variable was unset, even though none of
  them make an LLM call; the deployed workaround injected a live API key into five
  callers including a cron and a network-facing MCP. (2) `_PARAM_MAP` mapped the
  `--llm-*` flags into the deprecated `[compact]` section while `compact` resolves
  `cfg.llm.X or cfg.compact.X`, so a populated `[llm]` silently discarded every flag —
  the command accepted the override and then contacted the *old* provider anyway.
  Without this patch, forge's `[llm].api_key = "env:MISTRAL_API_KEY"` breaks
  `memsearch index`, and `--llm-*` cannot be used to trial a provider without editing
  production config.
- **probe:** exits 0 iff upstream covers BOTH halves; non-zero means still needed.
  ```sh
  # half 1 — top-level llm.api_key exempt from eager env: resolution
  git grep -qE '\("llm",[[:space:]]*"api_key"\)' <upstream-tag> -- src/memsearch/config.py &&
  # half 2 — the --llm-* flags target [llm], not [compact]
  git grep -qE '"llm_api_key":[[:space:]]*"llm\.api_key"' <upstream-tag> -- src/memsearch/cli.py
  ```
  Both are structural greps against upstream at the target tag. Half 1 asserts the
  behaviour (an exemption keyed on the `llm.api_key` path) rather than our helper's name,
  so an upstream rewrite that keeps the property still reads as covered; if upstream
  implements the exemption by a different structure the probe under-reports and the patch
  must be re-classified by hand rather than assumed still needed.
- **last-verified:** fork HEAD `12d473d` on 2026-08-13 (not yet compared to an upstream tag)
