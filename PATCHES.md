# Carried patches — forge fork

Manifest for `shared-fork-upstream-sync`. Lives on `forge-main` only; it must never land
on `main`, which tracks upstream.

Each entry carries a **probe**: a command that exits 0 iff upstream now covers the patch,
so a later sync can classify it mechanically instead of from memory.

**Running the probes.** Every probe is a structural grep against an upstream ref and can
be evaluated before any branch exists. Set the ref once and paste the probe:

```sh
T=upstream/main          # or a tag: T=v0.4.17
```

Exit 0 means *upstream covers this — drop the patch*. Non-zero means *still needed*.

**Run every probe in both directions.** A probe that reports "not covered" against
upstream proves nothing until the same probe reports "covered" against `forge-main`,
which demonstrably *has* the patch. A probe that fails against both is broken, not a
result. The 2026-08-13 backfill ran exactly this check and it caught one:
`user-prompt-memory-injection`'s first probe matched neither the fork's spelling nor
upstream's, and would have silently reported "still needed" forever — including on an
upstream that had adopted the feature.

```sh
for T in upstream/main forge-main; do echo "-- $T"; <paste probe>; echo "exit $?"; done
```

---

## hook-memsearch-path-fallback

- **status:** fork-only
- **commits:** `0f485f0` (original), `64bdbbf` (rewritten as a fallback)
- **upstream-pr:** none — forge-specific install layout
- **files:** `plugins/claude-code/hooks/common.sh`
- **why:** memsearch is installed to a venv at `/opt/venvs/memsearch/bin`, which
  upstream's PATH loop only reaches via a `$HOME/.local/bin` symlink. A hook running
  under a minimal or non-standard `HOME` therefore detects no memsearch at all and
  silently skips session summarization. Written as a **fallback** (`if ! command -v`),
  not a prepend: `0f485f0`'s unconditional prepend also shadowed a memsearch supplied on
  PATH by the caller, which is how the hook tests inject their stubs — they set `HOME` to
  a tmpdir precisely to get a minimal env, so the venv won and the tests exercised the
  real forge install instead of their fixtures (vikunja#375). The fallback shape keeps
  upstream's own PATH loop byte-identical, so this patch has zero conflict surface.
- **probe:** exits 0 iff upstream resolves memsearch outside the standard PATH dirs.
  ```sh
  git grep -qE '/opt/venvs/memsearch' "$T" -- plugins/claude-code/hooks/
  ```
  Upstream has no concept of this install layout, so per the skill's stated exception the
  fork's own path is the only legitimate probe target until an upstream mechanism exists.
  If upstream ever adds a general "extra PATH candidates" hook, re-target this probe at
  that construct rather than the literal path.
- **last-verified:** `upstream/main` @ `d5809d7` (v0.4.17+3) on 2026-08-13 — **not covered**

## transcript-meta-turn-guard

- **status:** fork-only
- **commits:** `f779354`, `1d03e47` (injected-prefix half)
- **upstream-pr:** none — candidate, see Step 10 notes below
- **files:** `plugins/claude-code/hooks/parse-transcript.sh`
- **why:** Two leaks of harness-synthetic content into indexed memory. (1) Claude Code
  writes a skill load as a `user` entry whose content is the entire skill markdown body,
  flagged `isMeta: true`. Upstream's `format_turn()` never checks `isMeta`, so that body
  was rendered verbatim into the transcript and the summarizer faithfully "summarized"
  it, leaking template placeholders into memory files.
  `find_last_turn_start()` already applied the guard; `format_turn()` was missing it.
  (2) Some injected turns are **not** flagged `isMeta` — slash-command wrappers whose text
  begins with a `<command-…>` marker (17 turns in the corpus at the time) — so a prefix
  guard covers the shapes `isMeta` misses. Deliberate trade-off, pinned by a test: a real
  user turn literally beginning with one of those markers is dropped rather than
  preserved, favouring drop-over-leak.
- **probe:** exits 0 iff upstream's `format_turn` skips meta turns AND filters injected
  prefixes. Both halves must pass; either alone leaves a live leak path.
  ```sh
  git show "$T":plugins/claude-code/hooks/parse-transcript.sh \
    | awk '/^def format_turn/,0' | grep -qE 'isMeta' &&
  git grep -qE 'command-name|Base directory for this skill' "$T" -- plugins/claude-code/hooks/
  ```
  The `awk` range starts at `format_turn` and runs to EOF, so a rename of the *following*
  function cannot silently truncate the window. Asserts the behaviour (a meta check
  inside the rendering path) rather than the fork's helper name, so an upstream rewrite
  that keeps the property still reads as covered.
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-13 — **not covered**
  (`format_turn` still present and still unguarded; `isMeta` appears only in
  `find_last_turn_start`)

## async-spool-stop-hook

- **status:** fork-only
- **commits:** `d1e1697`
- **upstream-pr:** none — architecture is forge-specific
- **files:** `plugins/claude-code/hooks/stop.sh`
- **why:** Forge summarizes out-of-band. The hook writes the parsed turn to the daily
  memory file, drops a spool JSON, and exits; the `memsearch-summarize` PM2 service later
  calls the LLM and **replaces** that raw block with the summary. Upstream summarizes
  inline via `claude -p` inside the hook. Forge moved it out to avoid per-turn cost and
  hangs in the async hook environment, and to run the call in a traced, credentialed
  service rather than a hook.
- **⚠ known gap — read before dropping or re-applying:** the raw write is
  **unconditional**, and the replacement is what makes it transient. When the summarizer
  is unreachable the service returns `retry`/`error` and the raw block stays in the
  memory file permanently. That is the exact path that orphaned 155 raw transcript blocks
  into the corpus during the 2026-08 Mistral outage. Upstream's `1c82054` fixes the
  *analogous* defect in its own inline design by never writing raw at all — it writes a
  bounded diagnostic line instead. Forge has the equivalent bounded note
  (`build_fallback_note`) only on the **contamination** path, not the
  **summarizer-unavailable** path. See Step 10 notes.
- **probe:** exits 0 iff upstream has adopted an out-of-band spool.
  ```sh
  git grep -qE 'spool' "$T" -- plugins/claude-code/hooks/
  ```
  Companion check — upstream's own raw-transcript fallback, tracked separately because it
  is the property forge's gap above is measured against. Exits 0 iff upstream still
  writes raw on summarizer failure (i.e. `1c82054` reverted):
  ```sh
  git show "$T":plugins/claude-code/hooks/stop.sh | grep -qE 'SUMMARY="\$PARSED"'
  ```
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-13 — spool **not covered**
  (still fork-only); upstream raw fallback **removed** by `1c82054`

## user-prompt-memory-injection

- **status:** fork-only
- **commits:** `f0979a8`
- **upstream-pr:** none
- **files:** `plugins/claude-code/hooks/user-prompt-submit.sh`
- **why:** This is a **push vs pull** divergence, not a missing feature. Upstream's hook is
  deliberately pull-based: it emits a static `"[memsearch] Memory available"` notice and
  leaves the actual retrieval to the `memory-recall` skill, so the agent decides whether
  to look. The forge hook extracts keywords from the submitted prompt, searches the global
  `memsearch_chunks` collection (all tiers) with a 10s timeout, and injects the results as
  a `systemMessage`, because agents asked to decide for themselves frequently did not.
  Failure is silent by design — no keywords, no results, or a search error all emit `{}`.
- **probe:** exits 0 iff upstream's UserPromptSubmit hook actually invokes a search, i.e.
  has moved from pull to push.
  ```sh
  git show "$T":plugins/claude-code/hooks/user-prompt-submit.sh \
    | grep -qE '(MEMSEARCH_CMD|memsearch)"?[[:space:]]+search'
  ```
  Asserts the invocation, matching either the fork's `"$MEMSEARCH_CMD" search` or a bare
  `memsearch search`. **Do not** probe the output field name: the fork uses
  `systemMessage`, which upstream also emits for its static notice, so that field
  cannot distinguish push from pull.
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-13 — **not covered**
  (upstream still pull-based)

## contamination-guard

- **status:** fork-only
- **commits:** `1d03e47`
- **upstream-pr:** none — candidate, generalisable beyond forge
- **files:** `src/memsearch/contamination.py`, `src/memsearch/core.py`,
  `tests/test_contamination.py`, `tests/test_compact_backstop.py`
- **why:** Two guards on the compact path. (1) **Contamination backstop** — a
  contaminated historical raw block, re-read by `compact`, could be copied forward into a
  new artifact. `compact()` now detects regurgitated template/skill text, retries once
  with an anti-regurgitation reminder, then falls back to a deterministic note. The
  detector was lifted from the summarize layer so both layers share one definition of
  contamination. (2) **Reindex-race guard** (vikunja#245) — an on-disk `--source` is
  pre-indexed before querying, so `compact` is self-sufficient and never returns a
  spurious "No chunks matched source" while the watch daemon is mid-reindex.
- **probe:** exits 0 iff upstream detects regurgitated source text on the compact path.
  ```sh
  git grep -qiE 'contaminat|regurgitat' "$T" -- src/
  ```
  Deliberately broad across `src/` and matching either vocabulary: upstream would
  plausibly name this differently, and a probe pinned to the fork's module path would
  under-report a genuine upstream implementation.
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-13 — **not covered**

## compact-source-date-output

- **status:** fork-only
- **commits:** `81c8a71`
- **upstream-pr:** none — candidate
- **files:** `src/memsearch/cli.py`, `src/memsearch/core.py`
- **why:** `compact` keyed its output filename to the **run** date, so compacting several
  historical sources into one `--output-dir` had every run overwrite the last. Output is
  now keyed to the **source** file's date (falling back to today), and `--output-name`
  gives an explicit stem to break collisions when several sources share an output dir.
- **probe:** exits 0 iff upstream derives the compact output name from the source rather
  than the run date, or offers an explicit override.
  ```sh
  git grep -qE 'output.name|output_name' "$T" -- src/memsearch/cli.py src/memsearch/core.py
  ```
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-13 — **not covered**

## compact-token-usage-log

- **status:** fork-only
- **upstream-pr:** none — candidate
- **files:** `src/memsearch/compact.py`, `tests/test_compact.py`
- **why:** `memsearch compact` logged no token counts at all, so the compact half of
  the pipeline's spend could only be estimated from file sizes (bytes/4) while the
  summarize half had exact per-call figures. That gap is what made the 2026-08 cost
  projection guesswork on the model that mattered most. `_compact_openai` now appends
  one JSON line per call — `timestamp`, `model`, `input_tokens`, `output_tokens`,
  `event` — to `$MEMSEARCH_TOKEN_LOG`. Inert unless that variable is set, and wrapped
  so a telemetry failure can never fail a compact. Consumed by `memsearch-spend.sh`
  in `host-forge-scripts`.
- **probe:** exits 0 iff upstream emits per-call token accounting from the compact path.
  ```sh
  git grep -qE 'prompt_tokens|completion_tokens|usage' "$T" -- src/memsearch/compact.py
  ```
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-16 — **not covered**

## compact-thinking-block-tolerance

- **status:** pending-PR
- **commits:** `f53f19b`, merge `12d473d`
- **upstream-pr:** https://github.com/zilliztech/memsearch/pull/675 (opened 2026-08-13)
  — submitted from a clean `upstream/main` base as `upstream-pr/thinking-block`, carrying
  only the `compact.py` hunk plus two regression tests; the commit's other half
  (`AGENT_WORKSPACE.md`) is forge-only and was deliberately left behind. **At the next
  sync, check the `merged` boolean, not `state`** — a closed-unmerged PR often names the
  PR that absorbed it, which then needs its own probe run.
- **files:** `src/memsearch/compact.py`
- **why:** `_compact_anthropic` read `resp.content[0].text` unconditionally. When the
  model returns a `ThinkingBlock` first — which any extended-thinking-capable model may
  do — element 0 has no `.text` and compact crashes. The fix selects the first block that
  actually carries text instead of assuming position 0.
- **probe:** exits 0 iff upstream selects a text-bearing block rather than indexing
  position 0.
  ```sh
  ! git show "$T":src/memsearch/compact.py | grep -qE 'resp\.content\[0\]\.text'
  ```
  **Inverted probe** — it asserts the *absence* of the unsafe access, because the safe
  form has many possible spellings and the unsafe one has exactly the one. If upstream
  refactors this call out of `compact.py` entirely the probe passes vacuously; re-confirm
  by locating the new Anthropic response-parsing site before dropping the patch.
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-13 — **not covered**
  (`compact.py:129` is still `return resp.content[0].text`)

## llm-config-resolution

- **status:** fork-only
- **commits:** `eae37ae` (see also `git log --grep="vikunja#37[12]" forge-main`)
- **upstream-pr:** none yet — **both halves are upstreamable**, neither is forge-specific
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
  git grep -qE '\("llm",[[:space:]]*"api_key"\)' "$T" -- src/memsearch/config.py &&
  # half 2 — the --llm-* flags target [llm], not [compact]
  git grep -qE '"llm_api_key":[[:space:]]*"llm\.api_key"' "$T" -- src/memsearch/cli.py
  ```
  Both are structural greps against upstream at the target ref. Half 1 asserts the
  behaviour (an exemption keyed on the `llm.api_key` path) rather than our helper's name,
  so an upstream rewrite that keeps the property still reads as covered; if upstream
  implements the exemption by a different structure the probe under-reports and the patch
  must be re-classified by hand rather than assumed still needed.
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-13 — **not covered**.
  Checked hard against `2761915 feat: add configurable index exclusions`, the one upstream
  commit touching `config.py`: it adds `IndexingConfig`, two `_LIST_FIELDS`, two
  `_PROJECT_CONFIG_ALLOWED_PATHS` entries and a `_SECTION_CLASSES` row — all in regions
  disjoint from `_resolve_env_refs_in_dict`, and it introduces no `env:`-bearing field
  that would need the exemption. The lazy-resolution property is intact.

## plugin-version-ci-gate

- **status:** fork-only
- **commits:** `6a44b46`
- **upstream-pr:** none — enforces a fork-local release rule
- **files:** `.github/scripts/check-plugin-version.sh`,
  `.github/workflows/plugin-version.yml`
- **why:** Hook changes do not go live until the installed plugin cache is refreshed, and
  the cache only refreshes when the plugin version changes. A change under
  `plugins/claude-code/` that forgets the bump therefore ships nothing while appearing to
  merge cleanly — the deployment gap that let an isMeta guard sit undeployed for days
  while memory kept being contaminated (vikunja#249). CI now fails any PR touching that
  subtree without bumping both `plugin.json` and `.claude-plugin/marketplace.json`.
  Note the script diffs *commits*, so it reads "no changes" until the work is committed.
- **probe:** exits 0 iff upstream enforces a plugin version bump in CI.
  ```sh
  git grep -qiE 'plugin.version|check-plugin-version' "$T" -- .github/
  ```
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-13 — **not covered**
  (upstream `.github/` has no `scripts/` dir and no such workflow)

## forge-ci-branch-triggers

- **status:** fork-only
- **commits:** `1d03e47` (rider, vikunja#246)
- **upstream-pr:** none — never upstreamable by construction
- **files:** `.github/workflows/test.yml`, `.github/workflows/lint.yml`
- **why:** Both workflows triggered on `main` only, so fork PRs targeting `forge-main` got
  no CI at all. Adds `forge-main` to the `push` and `pull_request` branch lists.
- **probe:** exits 0 iff upstream triggers CI on the fork branch — which it never will;
  this entry is permanently fork-only and exists so the next sync does not have to
  re-derive that.
  ```sh
  git show "$T":.github/workflows/test.yml | grep -qE 'forge-main'
  ```
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-13 — **not covered**.
  Upstream's one change to `test.yml` (`9004e32`) appends a separate `opencode` job at the
  end of the file and does not touch the trigger block, so this patch merges clean.

## gitignore-secrets-coredumps

- **status:** fork-only
- **commits:** `687ac26`
- **upstream-pr:** none — candidate, the `.env` half is generally useful
- **files:** `.gitignore`
- **why:** The fork is a working checkout on a host where `.env` files and core dumps land
  in the tree. Neither was ignored, so both were one `git add -A` away from being
  committed to a public repo.
- **probe:** exits 0 iff upstream ignores env files and core dumps.
  ```sh
  git show "$T":.gitignore | grep -qE '(^|/)\.env' &&
  git show "$T":.gitignore | grep -qiE '^core($|\.|/)'
  ```
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-13 — **not covered**
  (upstream `.gitignore` has neither pattern)

## forge-version-scheme

- **status:** fork-only
- **commits:** version-file hunks within `d1e1697`, `1d03e47`, `6a44b46`, `64bdbbf`
- **upstream-pr:** none — never upstreamable by construction
- **files:** `plugins/claude-code/.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json`
- **why:** The fork layers a `-forge.N` suffix on the upstream plugin version so a forge
  build is distinguishable from the upstream release it is based on, and so the plugin
  cache can be forced to refresh without waiting for an upstream release. `N` counts
  forge builds against a given upstream version and **resets to 1 when the upstream base
  version changes**. `pyproject.toml` deliberately keeps the bare upstream version — the
  suffix is a plugin-distribution concern, not a Python package one.
- **probe:** permanently fork-only by construction; no upstream equivalent is possible.
  These four files collide with every upstream release bump, so the resolution rule is
  the deliverable, not the probe: **take upstream's version number, re-apply the
  `-forge.N` suffix to the two plugin files, leave `pyproject.toml` bare.** Settle it once
  per sync and apply it to all files in one pass.
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-13 — n/a by construction

## forge-fork-docs

- **status:** fork-only
- **commits:** `3122e4b`, `f53f19b` (AGENT_WORKSPACE half), `687ac26` (AGENT_WORKSPACE half)
- **upstream-pr:** none — never upstreamable by construction
- **files:** `README.md`, `AGENT_WORKSPACE.md`
- **why:** `README.md` carries a "Forge fork" section stating that the fork tracks
  upstream and listing the forge-specific patches, so a reader landing on the fork knows
  it is not upstream. `AGENT_WORKSPACE.md` is forge agent-platform metadata (access mode,
  branch requirement) and has no upstream meaning. Now redundant with the central
  workspace policy that covers `~/repos/personal`, but retained — flagged on vikunja#377.
- **probe:** permanently fork-only by construction. The `README.md` section collides with
  any upstream README edit; resolve by keeping both (upstream's body, forge's section
  appended) rather than choosing.
- **last-verified:** `upstream/main` @ `d5809d7` on 2026-08-13 — n/a by construction

---

## Retired

*(none yet — no carried patch has been absorbed upstream as of the 2026-08-13 sync)*

---

## Step 10 — upstream-PR candidates and known gaps

Recorded here so the next sync does not re-derive them.

| Item | Note |
|---|---|
| `compact-thinking-block-tolerance` | **Submitted — zilliztech/memsearch#675, 2026-08-13.** Pure upstream bug, trivially reproducible, no forge coupling. |
| `llm-config-resolution` | Both halves upstreamable; tracked as vikunja#371 / #372. |
| `contamination-guard` | Generalisable, but the largest diff of the three — offer after the two above land. |
| `compact.py` `api_key` ignored for anthropic/gemini; `compact_chunks` rejects `openai-compatible` while `summarize_text` accepts it | vikunja#376. Pre-existing upstream defect, found on the fork, not carried as a patch. |
| **`async-spool-stop-hook` raw-write gap** | **Not an upstream candidate — a forge defect. Tracked as vikunja#386.** The raw block persists whenever the summarizer is unreachable, which is what orphaned 155 transcript blocks during the 2026-08 Mistral outage. Upstream's `1c82054` shows the shape of the answer (bounded diagnostic line, never raw). Fixing it spans this repo and the `memsearch-summarize` service in `host-forge-scripts`, because the service reads the raw block back out of the memory file — so it is a spool-contract change, not a one-line hook edit. |
