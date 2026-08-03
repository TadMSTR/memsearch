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
This checkout under `~/.claude/plugins/marketplaces/` is also the live, running
plugin code — Claude Code loads it directly from here, no separate deploy step.

Branch off `forge-main` for any forge-specific patch, PR back into `forge-main`.
Do not push directly to `forge-main`. Do not push to `main` (upstream-tracking).

The parent `~/.claude/AGENT_WORKSPACE.md` marker is `git_backed: false, inherit:
false` and does not cover this subtree — same pattern as vikunja#188 (~/.memsearch/)
and #195 (~/.config/). Filed as vikunja#330.
