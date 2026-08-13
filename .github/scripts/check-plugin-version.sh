#!/usr/bin/env bash
# Enforce the forge plugin-version bump rule.
#
# Rule (vikunja#249): any change under plugins/claude-code/ MUST come with a bump to
# plugins/claude-code/.claude-plugin/plugin.json's "version".
#
# Why this is CI and not a doc: Claude Code keys its plugin cache on that version
# string. If plugin.json is unchanged, `/plugin update` is a no-op and the cache keeps
# serving the old code while the checkout has moved on. The rule was written down after
# the first occurrence and skipped twice anyway (vikunja#249, then again this cycle) —
# so it is enforced here instead.
#
# Also checks that .claude-plugin/marketplace.json's plugin entry carries the SAME
# version. Those two files are edited by hand and drifted apart is a silent trap:
# the marketplace listing would advertise a version the plugin itself does not claim.
#
# Runs in CI with BASE_SHA/HEAD_SHA from the pull_request event. Runnable locally:
#   BASE_SHA=forge-main HEAD_SHA=HEAD ./.github/scripts/check-plugin-version.sh

set -euo pipefail

PLUGIN_JSON="plugins/claude-code/.claude-plugin/plugin.json"
MARKETPLACE_JSON=".claude-plugin/marketplace.json"
WATCHED_PATH="plugins/claude-code/"

BASE_SHA="${BASE_SHA:?BASE_SHA must be set}"
HEAD_SHA="${HEAD_SHA:-HEAD}"

# Compare against the actual fork point, not the tip of the base branch — otherwise
# unrelated commits landing on the base while the PR is open show up as "changed".
base="$(git merge-base "$BASE_SHA" "$HEAD_SHA")"

# read_version <ref> — the plugin version at a ref, or empty if the file is absent there.
read_version() {
  local ref="$1"
  git show "$ref:$PLUGIN_JSON" 2>/dev/null | jq -r '.version // empty' || true
}

changed="$(git diff --name-only "$base" "$HEAD_SHA" -- "$WATCHED_PATH")"

if [ -z "$changed" ]; then
  echo "OK: no changes under $WATCHED_PATH — version bump not required."
  exit 0
fi

echo "Changed under $WATCHED_PATH:"
echo "$changed" | sed 's/^/  /'
echo

base_version="$(read_version "$base")"
head_version="$(read_version "$HEAD_SHA")"

if [ -z "$head_version" ]; then
  echo "FAIL: cannot read .version from $PLUGIN_JSON at $HEAD_SHA."
  exit 1
fi

if [ -n "$base_version" ] && [ "$base_version" = "$head_version" ]; then
  cat <<EOF
FAIL: $WATCHED_PATH changed but the plugin version did not.

  version at base ($base): $base_version
  version at head:         $head_version

Claude Code keys its plugin cache on this string. Leaving it unchanged means
'/plugin update' is a no-op and the cache keeps serving the previous code.

Bump "version" in $PLUGIN_JSON (and the matching entry in $MARKETPLACE_JSON).
EOF
  exit 1
fi

echo "OK: plugin version bumped ${base_version:-<absent>} -> $head_version"

# --- consistency: marketplace.json must advertise the same version -------------------
marketplace_version="$(
  git show "$HEAD_SHA:$MARKETPLACE_JSON" 2>/dev/null \
    | jq -r '.plugins[] | select(.name == "memsearch") | .version // empty' || true
)"

if [ -z "$marketplace_version" ]; then
  echo "FAIL: cannot read the memsearch plugin version from $MARKETPLACE_JSON at $HEAD_SHA."
  exit 1
fi

if [ "$marketplace_version" != "$head_version" ]; then
  cat <<EOF
FAIL: version mismatch between the two files that carry it.

  $PLUGIN_JSON:      $head_version
  $MARKETPLACE_JSON: $marketplace_version

These are maintained by hand and must agree, or the marketplace listing advertises
a version the plugin does not claim.
EOF
  exit 1
fi

echo "OK: $MARKETPLACE_JSON agrees ($marketplace_version)"
