---
git_backed: true
remote: github
repo: TadMSTR/memsearch
access: readwrite
owning_agent: shared
branch_required: true
inherit: true
---

# memsearch plugin fork workspace

Ted's fork of zilliztech/memsearch (`upstream` remote), forge-specific patches
tracked on the `forge-main` branch (diverges from `main`, which tracks upstream).
This checkout is also the live, running plugin code — Claude Code loads it
directly, no separate deploy step.

**Canonical path is `~/repos/personal/memsearch`** (moved 2026-08-13, vikunja#377).
`~/.claude/plugins/marketplaces/memsearch-plugins` is a *symlink* to it, kept so
Claude Code's recorded `installLocation` keeps resolving. Earlier revisions of this
file said the marketplaces path was the real checkout — that is now backwards.

Branch off `forge-main` for any forge-specific patch, PR back into `forge-main`.
Do not push directly to `forge-main`. Do not push to `main` (upstream-tracking).

**This marker no longer grants the access.** Since the move, the path resolves under
`~/repos/personal`, a container root in `/etc/forge/workspace-policy.yml`, and
`agent-workspace-check` resolves it via Path 1 — where the policy is the sole
authority and no marker walk happens. The frontmatter above is retained for
description only and is not consulted; if it disagrees with `workspace-policy.yml`,
the policy wins. (vikunja#330, closed 2026-08-13.)
