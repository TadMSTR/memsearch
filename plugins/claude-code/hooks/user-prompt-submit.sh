#!/usr/bin/env bash
# UserPromptSubmit hook: extract keywords from prompt, search forge memory,
# inject top-2 results as systemMessage.
#
# Replaces the old hint-only approach to address the ~56% tool-skip rate
# when agents are asked to decide whether to retrieve context themselves.
#
# PROVENANCE OF THAT 56% (checked 2026-09-15, vikunja#853): it was never
# measured on forge. It is cited from a third party — "Empirical measurement
# shows agents skip retrieval tool calls ~56% of the time even when memory is
# relevant (Vercel, 2025)" — in the originating build plan
# memsearch-prompt-injection-2026-06, and carried into this comment by f0979a8.
# There is no forge baseline behind it and nothing here can be compared against
# it. The telemetry below exists to produce forge's own number instead.
#
# Forge-specific: searches the global memsearch_chunks Milvus collection.
# Falls back silently (returns {}) on any failure or empty result.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

PROMPT=$(_json_val "$INPUT" "prompt" "")

# ---------------------------------------------------------------------------
# Injection effectiveness telemetry (vikunja#853)
# ---------------------------------------------------------------------------
# One JSONL record per hook invocation, written on EVERY exit path — misses
# included. The miss records are the control: a success-only log cannot
# distinguish "fires often, rarely used" from "rarely fires", and without that
# distinction a low usage rate means nothing.
#
# Every write is best-effort. common.sh runs under `set -euo pipefail`, so
# nothing here may return non-zero — a telemetry defect must never cost the
# user their prompt, and `echo '{}'; exit 0` stays reachable on all paths.
#
# The record carries prompt-derived keywords. Extraction is a stopword filter
# plus a 4-character minimum, which a pasted credential can survive — hence
# mode 0600. That is a floor, not a guarantee that the log is clean.
INJECTION_LOG="${MEMSEARCH_INJECTION_LOG:-$HOME/.memsearch/injection-log.jsonl}"
if [ "${MEMSEARCH_INJECTION_LOG_DISABLE:-}" = "1" ]; then
  INJECTION_LOG=""
fi
# Bound: single-generation size rotation. At the cap the live file is renamed
# to <log>.1, replacing any previous .1 — so on-disk use is capped at ~2x.
INJECTION_LOG_MAX_BYTES="${MEMSEARCH_INJECTION_LOG_MAX_BYTES:-5242880}"

INJ_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '')"
INJ_SESSION="$(_json_val "$INPUT" "session_id" "")"
INJ_TRANSCRIPT="$(_json_val "$INPUT" "transcript_path" "")"
INJ_CWD="$(_json_val "$INPUT" "cwd" "")"
INJ_PROMPT_LEN="${#PROMPT}"

# Escape for a JSON string literal. Pure bash, no subprocess. Covers what can
# actually appear in the logged fields (a session uuid and filesystem paths).
_inj_esc() {
  local s="${1:-}"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\t'/ }"
  s="${s//$'\n'/ }"
  s="${s//$'\r'/ }"
  printf '%s' "$s"
}

# Render $KEYWORDS (space-separated, unset on the earliest exits) as the body
# of a JSON array. Tokens are [A-Za-z0-9_-] by construction from the extractor
# below; the strip here is defence in depth, not the primary guarantee.
_inj_keywords_json() {
  local out="" k
  local -a toks=()
  # `read -a` splits without globbing. A bare `for k in ${KEYWORDS}` would
  # pathname-expand any token containing a glob character. The extractor's
  # regex cannot currently emit one, so this is defence in depth rather than a
  # live fix — but it removes the dependence on that regex staying as it is.
  read -r -a toks <<< "${KEYWORDS:-}" || true
  for k in ${toks[@]+"${toks[@]}"}; do
    k="${k//[^a-zA-Z0-9_-]/}"
    if [ -n "$k" ]; then
      if [ -n "$out" ]; then out="${out},"; fi
      out="${out}\"${k}\""
    fi
  done
  printf '%s' "$out"
}

# mkdir + rotate + create 0600. Shared by this file and the formatting block
# below, so rotation has exactly one implementation.
_inj_prepare() {
  if [ -z "$INJECTION_LOG" ]; then return 0; fi
  local dir="${INJECTION_LOG%/*}"
  if [ -n "$dir" ] && [ "$dir" != "$INJECTION_LOG" ]; then
    mkdir -p "$dir" 2>/dev/null || true
  fi
  local size=""
  # Redirections are applied left to right, so `< "$f" 2>/dev/null` reports a
  # failed open to the ORIGINAL stderr. Group it so stderr is already gone.
  if [ -f "$INJECTION_LOG" ]; then
    size="$( { wc -c < "$INJECTION_LOG"; } 2>/dev/null )" || size=""
  fi
  size="${size//[^0-9]/}"
  if [ -n "$size" ] && [ "$size" -ge "$INJECTION_LOG_MAX_BYTES" ]; then
    mv -f "$INJECTION_LOG" "${INJECTION_LOG}.1" 2>/dev/null || true
  fi
  if [ ! -e "$INJECTION_LOG" ]; then
    ( umask 077; : > "$INJECTION_LOG" ) 2>/dev/null || true
  fi
  return 0
}

# Append one record for a non-injecting outcome.
#   $1 = outcome  $2 = search exit status (optional, 0 when no search ran)
_inj_log() {
  if [ -z "$INJECTION_LOG" ]; then return 0; fi
  _inj_prepare
  # Grouped for the same reason as above: an unwritable log must be silent,
  # not a stderr leak on every prompt.
  { printf '{"ts":"%s","session_id":"%s","transcript_path":"%s","cwd":"%s","prompt_len":%s,"keywords":[%s],"results":[],"results_total":0,"injected_bytes":0,"search_rc":%s,"outcome":"%s"}\n' \
    "$(_inj_esc "$INJ_TS")" \
    "$(_inj_esc "$INJ_SESSION")" \
    "$(_inj_esc "$INJ_TRANSCRIPT")" \
    "$(_inj_esc "$INJ_CWD")" \
    "$INJ_PROMPT_LEN" \
    "$(_inj_keywords_json)" \
    "${2:-0}" \
    "${1:-unknown}" \
    >> "$INJECTION_LOG"; } 2>/dev/null || true
  return 0
}

# Skip short prompts (greetings, single words, confirmations)
if [ -z "$PROMPT" ] || [ "${#PROMPT}" -lt 20 ]; then
  _inj_log "prompt_too_short"
  echo '{}'
  exit 0
fi

# Need memsearch binary available
if [ -z "$MEMSEARCH_CMD" ]; then
  _inj_log "no_memsearch_cmd"
  echo '{}'
  exit 0
fi

# --- Keyword extraction (stopword filter) ---
# Pass prompt via env var to avoid quoting/injection issues.
KEYWORDS=$(MEMSEARCH_PROMPT="$PROMPT" python3 - <<'PYEOF'
import os, re

STOPWORDS = {
    "a","an","the","and","or","but","in","on","at","to","for","of","with",
    "is","are","was","were","be","been","being","have","has","had","do",
    "does","did","will","would","could","should","may","might","can","shall",
    "this","that","these","those","i","me","my","we","our","you","your",
    "it","its","they","them","their","what","which","who","when","where",
    "how","why","if","then","than","there","here","so","as","not","no",
    "from","up","about","out","by","just","get","use","make","also","like",
    "want","need","run","set","check","add","see","new","any","all","some",
    "yes","okay","please","thanks","thank","sure","good","great","look",
    "show","tell","find","know","think","help","work","works","working",
}

prompt = os.environ.get("MEMSEARCH_PROMPT", "")
tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9\-_]{2,}", prompt)
keywords = [t.lower() for t in tokens if t.lower() not in STOPWORDS and len(t) > 3]
seen = set()
unique = []
for k in keywords:
    if k not in seen:
        seen.add(k)
        unique.append(k)
print(" ".join(unique[:8]))
PYEOF
)

# No meaningful keywords in prompt — nothing useful to search
if [ -z "$KEYWORDS" ]; then
  _inj_log "no_keywords"
  echo '{}'
  exit 0
fi

# --- Search global forge memory ---
# Use memsearch_chunks (global collection, all tiers: session/working/docs).
# Project-scoped COLLECTION_NAME is intentionally not used here.
# --reranker-model "" disables cross-encoder (loads from disk each CLI call):
#   with reranker: ~4.5s wall time; without: ~0.7s. Vector+BM25 is sufficient here.
# timeout 10: leaves 5s buffer within the 15s hook deadline.
#
# The exit status is captured rather than discarded so the log can tell a
# failed search (including a 124 timeout) apart from a search that genuinely
# returned nothing. Those are different answers to "why did nothing inject".
SEARCH_RC=0
SEARCH_JSON=$(timeout 10 "$MEMSEARCH_CMD" search "$KEYWORDS" \
    -k 3 \
    -j \
    -c memsearch_chunks \
    --reranker-model "" \
    2>/dev/null) || SEARCH_RC=$?

if [ "$SEARCH_RC" -ne 0 ]; then
  _inj_log "search_failed" "$SEARCH_RC"
  echo '{}'
  exit 0
fi

# No results — silent fallback
if [ -z "$SEARCH_JSON" ] || [ "$SEARCH_JSON" = "[]" ]; then
  _inj_log "search_empty"
  echo '{}'
  exit 0
fi

# --- Format and inject top-2 results ---
# NOTE: memsearch CLI -j output uses "source"/"content" field names.
# The MCP server normalizes these to "path"/"snippet" — CLI does not.
# Tier is not in CLI output; computed here from source path prefix.
#
# The telemetry record for the injected path is emitted from inside this block
# rather than from a third python3 subprocess: the results are already parsed
# here, and injected_bytes is only exactly knowable once the body is built.
_inj_prepare
INJECTION=$(MEMSEARCH_JSON="$SEARCH_JSON" \
  MEMSEARCH_INJ_LOG="$INJECTION_LOG" \
  MEMSEARCH_INJ_TS="$INJ_TS" \
  MEMSEARCH_INJ_SESSION="$INJ_SESSION" \
  MEMSEARCH_INJ_TRANSCRIPT="$INJ_TRANSCRIPT" \
  MEMSEARCH_INJ_CWD="$INJ_CWD" \
  MEMSEARCH_INJ_PROMPT_LEN="$INJ_PROMPT_LEN" \
  MEMSEARCH_INJ_KEYWORDS="$KEYWORDS" \
  python3 - <<'PYEOF'
import os, json, sys

raw = os.environ.get("MEMSEARCH_JSON", "")
try:
    results = json.loads(raw)
except (json.JSONDecodeError, ValueError):
    sys.exit(0)

if not results:
    sys.exit(0)

home = os.path.expanduser("~")

def infer_tier(source):
    if ".memsearch" in source or source.startswith("/opt/agents/memory"):
        return "session"
    if source.startswith(f"{home}/.claude/memory/docs/"):
        return "docs"
    if source.startswith(f"{home}/.claude/memory/"):
        return "working"
    return ""


def log_injection(body, results, injected):
    """Best-effort telemetry. Never raises; never blocks the injection."""
    path = os.environ.get("MEMSEARCH_INJ_LOG", "")
    if not path:
        return
    try:
        record = {
            "ts": os.environ.get("MEMSEARCH_INJ_TS", ""),
            "session_id": os.environ.get("MEMSEARCH_INJ_SESSION", ""),
            "transcript_path": os.environ.get("MEMSEARCH_INJ_TRANSCRIPT", ""),
            "cwd": os.environ.get("MEMSEARCH_INJ_CWD", ""),
            "prompt_len": int(os.environ.get("MEMSEARCH_INJ_PROMPT_LEN") or 0),
            "keywords": (os.environ.get("MEMSEARCH_INJ_KEYWORDS") or "").split(),
            "results": injected,
            "results_total": len(results),
            "injected_bytes": len(body.encode("utf-8")),
            "search_rc": 0,
            "outcome": "injected",
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        # 0600 on create, independently of whoever prepared the file.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


parts = []
injected = []
for r in results[:2]:
    source  = r.get("source", "")          # CLI field name (not "path")
    snippet = (r.get("content") or "").strip()[:400]  # CLI field name (not "snippet")
    heading = r.get("heading") or ""
    tier    = infer_tier(source)
    label   = f"[{tier}]" if tier else ""
    display = f"{label} {source}".strip() if label else source
    if heading:
        display += f" § {heading}"
    parts.append(f"{display}\n{snippet}")
    injected.append({
        "source": source,
        "score": r.get("score"),
        "tier": tier,
        "heading": heading,
        "snippet_chars": len(snippet),
    })

body = "[memory] Relevant context auto-injected:\n\n" + "\n\n---\n\n".join(parts)
log_injection(body, results, injected)
print(json.dumps({"systemMessage": body}))
PYEOF
) || INJECTION=""

if [ -n "$INJECTION" ]; then
  echo "$INJECTION"
else
  # The formatter produced nothing (unparseable or empty payload). No record
  # was written inside the block, so account for it here.
  _inj_log "search_empty"
  echo '{}'
fi
