#!/usr/bin/env bash
# Stop hook (forge fork): parse the last transcript turn, append its raw content
# to the daily memory file, and drop a spool entry. Summarization AND indexing
# are handled out-of-band by the memsearch-summarize PM2 service (port 8494) —
# this hook stays minimal to avoid hangs and per-turn cost in the async hook env.
#
# Forge divergence from upstream stop.sh (re-authored on top of v0.4.14, was
# forge commit 293fe23 on the stale v0.4.5 base):
#   - No inline `claude -p` summarization. Upstream's summarizer hardening
#     (--tools "", --safe-mode, SUMMARIZE_MODEL, MEMSEARCH_DISABLE) is not
#     applicable here because this hook never spawns claude; the PM2 service
#     calls the Anthropic API directly in a controlled, traced environment.
#   - No inline `run_memsearch index` — the PM2 service indexes each spool
#     entry after it summarizes it.
#   - Writes a spool JSON the PM2 service consumes.
# The stdin-hang and recursion-guard fixes now live in common.sh upstream
# (timeout-guarded INPUT read + MEMSEARCH_DISABLE early-exit), so this hook
# inherits them.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

# Close stdin — async hooks may leave the pipe open, blocking child processes.
# (common.sh already timeout-guards its own INPUT read; this protects the
# parse-transcript.sh / python children spawned below.)
exec < /dev/null

# common.sh sets -euo pipefail; this hook tolerates non-zero exits and handles
# failures with explicit fallbacks.
set +euo pipefail

# Prevent infinite loop: if this Stop was triggered by a previous Stop hook, bail out
STOP_HOOK_ACTIVE=$(_json_val "$INPUT" "stop_hook_active" "false")
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  echo '{}'
  exit 0
fi

# Extract transcript path and session id from hook input
TRANSCRIPT_PATH=$(_json_val "$INPUT" "transcript_path" "")
SESSION_ID=$(_json_val "$INPUT" "session_id" "")

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  echo '{}'
  exit 0
fi

# Fall back to the transcript filename when the hook input omits session_id
if [ -z "$SESSION_ID" ]; then
  SESSION_ID=$(basename "$TRANSCRIPT_PATH" .jsonl)
fi

# Check if transcript is empty (< 3 lines = no real content)
LINE_COUNT=$(wc -l < "$TRANSCRIPT_PATH" 2>/dev/null || echo "0")
if [ "$LINE_COUNT" -lt 3 ]; then
  echo '{}'
  exit 0
fi

ensure_memory_dir

# Parse transcript — extract the last turn only (one user question + all responses)
PARSED=$("$SCRIPT_DIR/parse-transcript.sh" "$TRANSCRIPT_PATH" 2>/dev/null || true)

if [ -z "$PARSED" ] || [ "$PARSED" = "(empty transcript)" ] || [ "$PARSED" = "(no user message found)" ] || [ "$PARSED" = "(empty turn)" ]; then
  echo '{}'
  exit 0
fi

# Determine today's date and current time
TODAY=$(date +%Y-%m-%d)
NOW=$(date +%H:%M)
MEMORY_FILE="$MEMORY_DIR/$TODAY.md"

# Extract last user turn UUID for progressive disclosure anchors.
# List-content-aware and UTF-8 tolerant (adopted from upstream v0.4.14).
LAST_USER_TURN_UUID=$(python3 -c "
import json, sys
uuid = ''
with open(sys.argv[1], encoding='utf-8', errors='replace') as f:
    for line in f:
        try:
            obj = json.loads(line)
            if obj.get('type') != 'user' or obj.get('isMeta'):
                continue
            content = obj.get('message', {}).get('content')
            if isinstance(content, str) and content.strip():
                uuid = obj.get('uuid', '')
                continue
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text' and block.get('text', '').strip():
                        uuid = obj.get('uuid', '')
                        break
        except: pass
print(uuid)
" "$TRANSCRIPT_PATH" 2>/dev/null || true)

# Write raw parsed content to the memory file — summarization happens later
# via the memsearch-summarize PM2 service.
{
  echo "### $NOW"
  if [ -n "$SESSION_ID" ]; then
    echo "<!-- session:${SESSION_ID} turn:${LAST_USER_TURN_UUID} transcript:${TRANSCRIPT_PATH} -->"
  fi
  echo "$PARSED"
  echo ""
} >> "$MEMORY_FILE"

# Spool for the async summarization service (memsearch-summarize).
SPOOL_DIR="$MEMSEARCH_DIR/spool"
mkdir -p "$SPOOL_DIR"
_SPOOL_FILE="$SPOOL_DIR/${SESSION_ID:-unknown}-$(date +%s).json"
python3 -c "
import json, sys
spool = {
    'session_id': sys.argv[1],
    'transcript_path': sys.argv[2],
    'memory_file': sys.argv[3],
    'timestamp': sys.argv[4],
    'turn_uuid': sys.argv[5],
    'parsed_len': int(sys.argv[6])
}
with open(sys.argv[7], 'w', encoding='utf-8') as f:
    json.dump(spool, f)
" "$SESSION_ID" "$TRANSCRIPT_PATH" "$MEMORY_FILE" "$NOW" "$LAST_USER_TURN_UUID" "${#PARSED}" "$_SPOOL_FILE" 2>/dev/null || true

echo '{}'
