#!/usr/bin/env python3
"""Correlate the UserPromptSubmit injection log against session transcripts.

Answers, with numbers rather than intuition: how often does the memory
injection fire, where do the misses concentrate, and is any injected snippet
ever actually used?

    ./scripts/injection-analysis.py                     # text report
    ./scripts/injection-analysis.py --json              # machine-readable
    ./scripts/injection-analysis.py --log /path/to.jsonl

METHOD, AND WHAT IT CANNOT TELL YOU
-----------------------------------
"Was the injection used" is not directly observable. Two proxies are reported,
and both are weak in a stated direction:

  * A later memory-tool call is a WEAK NEGATIVE. If the session went and pulled
    context anyway, the pushed context did not substitute for the pull. It does
    not prove the injection was useless — the pull may have been for something
    else entirely.

  * A textual reference to the injected source is the POSITIVE signal, and it
    is a proxy, not proof. A model can be influenced without quoting, and can
    quote without being influenced.

Reference matching is on the source path, its basename, and its heading. It is
deliberately NOT done on the snippet text: the log stores no memory content, so
there is nothing to match against. That keeps the log small and keeps corpus
text out of a file that already carries prompt-derived keywords — but it means
a paraphrase of an injected snippet is invisible here, and the positive signal
is therefore a floor, not a ceiling.

Raw counts are printed alongside every rate so the numbers can be argued with.

WHAT `score` IS, AND IS NOT
---------------------------
It is NOT a similarity. `store.py` runs a Milvus hybrid search over a dense and a
BM25 retriever, fused with `RRFRanker(k=60)`, and normalises by the theoretical
maximum `len(reqs)/(k+1)` = 2/61. The logged `score` is therefore a function of
RANK POSITION in the two result lists, not of how close the match is:

    0.5000  rank 1 in exactly one retriever      (1/61) / (2/61)
    0.4919  rank 2 in exactly one retriever      (1/62) / (2/61)
    0.9841  rank 1 in one + rank 3 in the other
    1.0000  rank 1 in both

The hook additionally passes `--reranker-model ""`, so the cross-encoder in
`reranker.py` — the only component that would produce a real relevance score —
never runs.

Consequence: a minimum-score floor CANNOT work as a relevance filter. The best
available match always occupies rank 1 and therefore always scores ~0.5,
however weak it is in absolute terms. A floor at 0.5 keeps everything; a floor
above it keeps only results both retrievers agree on, which measures retriever
agreement, not quality. The build plan named a score floor as the cheap fix for
a "marginal" outcome; that option rests on a premise about this field that is
not true. Report the distribution as a rank-agreement histogram, and do not read
it as relevance.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

DEFAULT_LOG = Path.home() / ".memsearch" / "injection-log.jsonl"

# Transcripts live under the Claude Code projects root. `transcript_path` reaches
# this script from the hook's stdin, via the log, unvalidated — the hook only
# ever JSON-escapes it, never treats it as a path. Containing the open() here
# removes a silent dependency on "the log's trust level never gets weaker than
# the hook's". It is not closing a live hole today (writing an attacker-chosen
# value already requires code execution as the log's owner); it stops one from
# opening later if another writer or a narrower-trust consumer appears.
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

# Deliberate retrievals — the "pull" path the injection was meant to replace.
MEMORY_TOOLS = {
    "mcp__scoped-mcp__memsearch-mcp_search_memory",
    "mcp__scoped-mcp__qmd_get",
    "mcp__scoped-mcp__qmd_query",
    "mcp__scoped-mcp__qmd_multi_get",
    "mcp__scoped-mcp__memory-metadata-mcp_list_notes",
    "mcp__scoped-mcp__memory-metadata-mcp_get_note_metadata",
}
MEMORY_SKILLS = {"archival-search", "memsearch:memory-recall", "shared-memory-update"}

OUTCOMES = [
    "injected",
    "prompt_too_short",
    "no_keywords",
    "no_memsearch_cmd",
    "search_empty",
    "search_failed",
]


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_log(path: Path) -> tuple[list[dict], int]:
    """Read the live log plus one rotated generation, oldest first."""
    records: list[dict] = []
    malformed = 0
    for candidate in (Path(f"{path}.1"), path):
        if not candidate.exists():
            continue
        with candidate.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    malformed += 1
    return records, malformed


def agent_of(record: dict) -> str:
    """Derive the agent from the record's cwd. Falls back to the raw path."""
    cwd = record.get("cwd") or ""
    marker = "/.claude/projects/"
    if marker in cwd:
        return cwd.split(marker, 1)[1].split("/", 1)[0] or "unknown"
    return Path(cwd).name or "unknown"


def transcript_allowed(path: str, root: Path | None) -> bool:
    """True if `path` is inside `root` (or containment is disabled)."""
    if root is None:
        return True
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    # `resolve()` first, so a symlink or ../ traversal out of the root is caught.
    return resolved == root or root in resolved.parents


def iter_transcript(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def assistant_text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def tool_names(entry: dict) -> list[str]:
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    names = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name") or ""
        names.append(name)
        if name == "Skill":
            skill = (block.get("input") or {}).get("skill")
            if skill:
                names.append(f"Skill:{skill}")
    return names


def is_memory_call(name: str) -> bool:
    if name in MEMORY_TOOLS:
        return True
    return name.startswith("Skill:") and name.split(":", 1)[1] in MEMORY_SKILLS


def needles(result: dict) -> list[str]:
    """Strings whose appearance in later output counts as a reference."""
    out = []
    source = result.get("source") or ""
    if source:
        out.append(source)
        base = os.path.basename(source)
        # A bare basename is only distinctive if it is not a generic name that
        # every agent's memory dir contains.
        if base and base not in {"README.md", "index.md", "MEMORY.md", "notes.md"}:
            out.append(base)
    heading = (result.get("heading") or "").strip()
    if len(heading) >= 8:
        out.append(heading)
    return out


def correlate(records: list[dict], transcript_root: Path | None = TRANSCRIPT_ROOT) -> dict:
    """For each injected record, inspect its session's transcript afterwards."""
    by_session: dict[str, list[dict]] = defaultdict(list)
    for rec in records:
        if rec.get("outcome") == "injected":
            by_session[rec.get("session_id") or ""].append(rec)

    stats = {
        "analysed": 0,
        "transcript_missing": 0,
        # Counted separately, never folded into transcript_missing: a rejected
        # path is a measurement this script declined to take, not one the data
        # never had. Collapsing them would let containment quietly shrink the
        # denominator and flatter every rate computed from it.
        "transcript_rejected": 0,
        "rejected_paths": [],
        "followed_by_memory_tool": 0,
        "referenced": 0,
        "referenced_before_any_memory_tool": 0,
        "per_agent": defaultdict(lambda: Counter()),
    }

    for session_id, injections in by_session.items():
        injections.sort(key=lambda r: r.get("ts") or "")
        transcript = injections[0].get("transcript_path") or ""
        if transcript and not transcript_allowed(transcript, transcript_root):
            stats["transcript_rejected"] += len(injections)
            if transcript not in stats["rejected_paths"]:
                stats["rejected_paths"].append(transcript)
            continue
        entries = [e for e in iter_transcript(transcript) if parse_ts(e.get("timestamp"))]
        if not entries:
            stats["transcript_missing"] += len(injections)
            continue
        entries.sort(key=lambda e: parse_ts(e["timestamp"]))

        for idx, rec in enumerate(injections):
            start = parse_ts(rec.get("ts"))
            if start is None:
                stats["transcript_missing"] += 1
                continue
            # Window ends at the next injection in the same session, so a later
            # injection's usage is never credited to an earlier one.
            end = parse_ts(injections[idx + 1].get("ts")) if idx + 1 < len(injections) else None

            window = [
                e
                for e in entries
                if parse_ts(e["timestamp"]) > start and (end is None or parse_ts(e["timestamp"]) <= end)
            ]

            stats["analysed"] += 1
            agent = agent_of(rec)
            stats["per_agent"][agent]["analysed"] += 1

            wanted = [n for r in rec.get("results") or [] for n in needles(r)]

            saw_memory_tool = False
            memory_tool_seen_at: int | None = None
            referenced_at: int | None = None

            for pos, entry in enumerate(window):
                for name in tool_names(entry):
                    if is_memory_call(name):
                        saw_memory_tool = True
                        if memory_tool_seen_at is None:
                            memory_tool_seen_at = pos
                if referenced_at is None and entry.get("type") == "assistant":
                    text = assistant_text(entry)
                    if text and any(n in text for n in wanted):
                        referenced_at = pos

            if saw_memory_tool:
                stats["followed_by_memory_tool"] += 1
                stats["per_agent"][agent]["followed_by_memory_tool"] += 1
            if referenced_at is not None:
                stats["referenced"] += 1
                stats["per_agent"][agent]["referenced"] += 1
                # The stronger signal: the reference cannot have come from a
                # pull, because no pull had happened yet.
                if memory_tool_seen_at is None or referenced_at < memory_tool_seen_at:
                    stats["referenced_before_any_memory_tool"] += 1
                    stats["per_agent"][agent]["referenced_before_any_memory_tool"] += 1

    stats["per_agent"] = {k: dict(v) for k, v in stats["per_agent"].items()}
    return stats


def summarise(records: list[dict], malformed: int) -> dict:
    outcomes = Counter(r.get("outcome") or "unknown" for r in records)
    injected = [r for r in records if r.get("outcome") == "injected"]

    scores = [
        r["score"]
        for rec in injected
        for r in rec.get("results") or []
        if isinstance(r.get("score"), (int, float))
    ]
    tiers = Counter(
        (r.get("tier") or "unknown")
        for rec in injected
        for r in rec.get("results") or []
    )
    # How often the top-2 cap is spent on two chunks of the SAME file. Observed
    # live on the very first deployment probe, so it is not hypothetical: if it
    # is common, the cap delivers one document's worth of context, not two.
    multi = [rec for rec in injected if len(rec.get("results") or []) >= 2]
    same_source = sum(
        1 for rec in multi if len({(r.get("source") or "") for r in rec["results"]}) == 1
    )
    per_agent_outcomes: dict[str, Counter] = defaultdict(Counter)
    for rec in records:
        per_agent_outcomes[agent_of(rec)][rec.get("outcome") or "unknown"] += 1

    timestamps = sorted(t for t in (parse_ts(r.get("ts")) for r in records) if t)

    summary = {
        "prompts_seen": len(records),
        "malformed_lines": malformed,
        "first_record": timestamps[0].isoformat() if timestamps else None,
        "last_record": timestamps[-1].isoformat() if timestamps else None,
        "span_days": round((timestamps[-1] - timestamps[0]).total_seconds() / 86400, 1)
        if len(timestamps) > 1
        else 0.0,
        "outcomes": {k: outcomes.get(k, 0) for k in OUTCOMES},
        "outcomes_other": {k: v for k, v in outcomes.items() if k not in OUTCOMES},
        "fire_rate": round(len(injected) / len(records), 4) if records else 0.0,
        "total_injected_bytes": sum(int(r.get("injected_bytes") or 0) for r in injected),
        "search_rc_breakdown": dict(
            Counter(r.get("search_rc") for r in records if r.get("outcome") == "search_failed")
        ),
        "tiers": dict(tiers),
        "two_result_injections": len(multi),
        "both_results_same_source": same_source,
        "scores": {
            "n": len(scores),
            "min": round(min(scores), 4) if scores else None,
            "median": round(statistics.median(scores), 4) if scores else None,
            "mean": round(statistics.fmean(scores), 4) if scores else None,
            "max": round(max(scores), 4) if scores else None,
        },
        "per_agent_outcomes": {k: dict(v) for k, v in per_agent_outcomes.items()},
    }
    return summary


def pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:5.1f}%" if whole else "    —"


def render(summary: dict, corr: dict) -> str:
    out: list[str] = []
    a = out.append
    total = summary["prompts_seen"]
    inj = summary["outcomes"]["injected"]

    a("memsearch injection effectiveness — vikunja#853")
    a("=" * 64)
    a(f"window          : {summary['first_record']} .. {summary['last_record']}")
    a(f"span            : {summary['span_days']} days")
    a(f"prompts seen    : {total}")
    if summary["malformed_lines"]:
        a(f"malformed lines : {summary['malformed_lines']}")
    if summary["span_days"] < 14:
        a("")
        a("  ** WARNING: under the 14-day bake period. The fleet's session")
        a("     volume is uneven; a short sample is not a rate. **")
    a("")

    a("Fire rate — how often the hook does anything")
    a("-" * 64)
    for name in OUTCOMES:
        count = summary["outcomes"][name]
        a(f"  {name:<20} {count:>7}  {pct(count, total)}")
    for name, count in sorted(summary["outcomes_other"].items()):
        a(f"  {name:<20} {count:>7}  {pct(count, total)}  (unrecognised)")
    if summary["search_rc_breakdown"]:
        a(f"  search_failed exit codes: {summary['search_rc_breakdown']}  (124 = timeout)")
    a("")

    a("Context cost")
    a("-" * 64)
    kb = summary["total_injected_bytes"] / 1024
    a(f"  total injected       {summary['total_injected_bytes']:>9} bytes ({kb:.1f} KiB)")
    if inj:
        a(f"  mean per injection   {summary['total_injected_bytes'] / inj:>9.0f} bytes")
    a("")

    a("Score distribution of injected results")
    a("-" * 64)
    s = summary["scores"]
    if s["n"]:
        a(f"  n={s['n']}  min={s['min']}  median={s['median']}  mean={s['mean']}  max={s['max']}")
        a("")
        a("  NOT a similarity. This is a normalised RRF rank-fusion score")
        a("  (RRFRanker k=60 over dense+BM25, /(2/61)), so it encodes rank")
        a("  position, not closeness:")
        a("     ~0.50 = rank 1 in one retriever   ~0.49 = rank 2 in one")
        a("     ~0.98 = rank 1 + rank 3           ~1.00 = rank 1 in both")
        a("  The best available match always sits at rank 1 and so always")
        a("  scores ~0.5 however weak it is. A minimum-score floor therefore")
        a("  filters on retriever agreement, not quality — it is NOT the cheap")
        a("  fix the build plan assumed. See the module docstring.")
    else:
        a("  no scored results")
    a("")

    a("Tier of injected results")
    a("-" * 64)
    tier_total = sum(summary["tiers"].values())
    for tier, count in sorted(summary["tiers"].items(), key=lambda kv: -kv[1]):
        a(f"  {tier or '(none)':<20} {count:>7}  {pct(count, tier_total)}")
    a("  Overwhelmingly 'session' would mean the hook mostly recycles recent chatter.")
    a("")
    a("Result diversity")
    a("-" * 64)
    a(f"  injections returning 2 results       {summary['two_result_injections']:>7}")
    a(
        f"  ...both from the SAME source file   {summary['both_results_same_source']:>7}"
        f"  {pct(summary['both_results_same_source'], summary['two_result_injections'])}"
    )
    a("  When both slots hold chunks of one document, the top-2 cap buys one")
    a("  document's worth of context rather than two.")
    a("")

    a("Was it used? (proxies — see module docstring)")
    a("-" * 64)
    an = corr["analysed"]
    a(f"  injections analysed              {an:>7}")
    a(f"  transcript unavailable           {corr['transcript_missing']:>7}")
    if corr.get("transcript_rejected"):
        a(f"  transcript path REJECTED         {corr['transcript_rejected']:>7}   (outside {TRANSCRIPT_ROOT})")
        for bad in corr["rejected_paths"][:5]:
            a(f"      {bad}")
        a("    These were not analysed. Re-run with --allow-any-transcript-path")
        a("    if the location is legitimate — do not read the rates above as")
        a("    if these injections simply had no transcript.")
    a(f"  followed by a memory-tool call   {corr['followed_by_memory_tool']:>7}  {pct(corr['followed_by_memory_tool'], an)}   (weak NEGATIVE)")
    a(f"  source referenced later          {corr['referenced']:>7}  {pct(corr['referenced'], an)}   (proxy POSITIVE)")
    a(f"    ...before any memory-tool call {corr['referenced_before_any_memory_tool']:>7}  {pct(corr['referenced_before_any_memory_tool'], an)}   (strongest available)")
    a("")

    a("Per agent")
    a("-" * 64)
    a(f"  {'agent':<22}{'prompts':>8}{'injected':>10}{'referenced':>12}{'mem-tool':>10}")
    for agent, counts in sorted(summary["per_agent_outcomes"].items()):
        ca = corr["per_agent"].get(agent, {})
        a(
            f"  {agent[:21]:<22}{sum(counts.values()):>8}{counts.get('injected', 0):>10}"
            f"{ca.get('referenced', 0):>12}{ca.get('followed_by_memory_tool', 0):>10}"
        )
    a("")
    a("This report does not recommend an outcome. Keep / tune / retire is the")
    a("operator's decision at the gate.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG, help=f"injection log (default: {DEFAULT_LOG})")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the text report")
    ap.add_argument(
        "--allow-any-transcript-path",
        action="store_true",
        help=f"do not require transcript_path to live under {TRANSCRIPT_ROOT}",
    )
    args = ap.parse_args()

    if not args.log.exists() and not Path(f"{args.log}.1").exists():
        print(f"No injection log at {args.log}", file=sys.stderr)
        return 1

    records, malformed = load_log(args.log)
    if not records:
        print(f"Injection log {args.log} is empty.", file=sys.stderr)
        print(
            "If the hook was merged but the plugin cache never refreshed, this is "
            "what an inert deploy looks like — check installed_plugins.json before "
            "concluding the hook never fires.",
            file=sys.stderr,
        )
        return 1

    summary = summarise(records, malformed)
    corr = correlate(records, None if args.allow_any_transcript_path else TRANSCRIPT_ROOT)

    if args.json:
        print(json.dumps({"summary": summary, "correlation": corr}, indent=2, ensure_ascii=False))
    else:
        print(render(summary, corr))
    return 0


if __name__ == "__main__":
    sys.exit(main())
