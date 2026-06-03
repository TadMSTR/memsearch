#!/usr/bin/env bash
# UserPromptSubmit hook: extract keywords from prompt, search forge memory,
# inject top-2 results as systemMessage.
#
# Replaces the old hint-only approach to address the ~56% tool-skip rate
# when agents are asked to decide whether to retrieve context themselves.
#
# Forge-specific: searches the global memsearch_chunks Milvus collection.
# Falls back silently (returns {}) on any failure or empty result.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

# Skip short prompts (greetings, single words, confirmations)
PROMPT=$(_json_val "$INPUT" "prompt" "")
if [ -z "$PROMPT" ] || [ "${#PROMPT}" -lt 20 ]; then
  echo '{}'
  exit 0
fi

# Need memsearch binary available
if [ -z "$MEMSEARCH_CMD" ]; then
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
  echo '{}'
  exit 0
fi

# --- Search global forge memory ---
# Use memsearch_chunks (global collection, all tiers: session/working/docs).
# Project-scoped COLLECTION_NAME is intentionally not used here.
# --reranker-model "" disables cross-encoder (loads from disk each CLI call):
#   with reranker: ~4.5s wall time; without: ~0.7s. Vector+BM25 is sufficient here.
# timeout 10: leaves 5s buffer within the 15s hook deadline.
SEARCH_JSON=$(timeout 10 "$MEMSEARCH_CMD" search "$KEYWORDS" \
    -k 3 \
    -j \
    -c memsearch_chunks \
    --reranker-model "" \
    2>/dev/null) || true

# No results or search failed — silent fallback
if [ -z "$SEARCH_JSON" ] || [ "$SEARCH_JSON" = "[]" ]; then
  echo '{}'
  exit 0
fi

# --- Format and inject top-2 results ---
# NOTE: memsearch CLI -j output uses "source"/"content" field names.
# The MCP server normalizes these to "path"/"snippet" — CLI does not.
# Tier is not in CLI output; computed here from source path prefix.
INJECTION=$(MEMSEARCH_JSON="$SEARCH_JSON" python3 - <<'PYEOF'
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

parts = []
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

body = "[memory] Relevant context auto-injected:\n\n" + "\n\n---\n\n".join(parts)
print(json.dumps({"systemMessage": body}))
PYEOF
)

if [ -n "$INJECTION" ]; then
  echo "$INJECTION"
else
  echo '{}'
fi
