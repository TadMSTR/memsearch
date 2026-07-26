from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from memsearch import core as core_module


class _FakeStore:
    def __init__(self, chunks: list[dict[str, str]]) -> None:
        self._chunks = chunks

    def query(self, *, filter_expr: str = "") -> list[dict[str, str]]:
        return self._chunks


def _make_self(tmp_path: Path, chunks: list[dict[str, str]]) -> SimpleNamespace:
    async def _index_file(_path: Path) -> int:
        return 0

    return SimpleNamespace(_store=_FakeStore(chunks), _paths=[str(tmp_path)], index_file=_index_file)


def _read_summary(tmp_path: Path) -> str:
    files = list((tmp_path / "memory").glob("*.md"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_compact_retries_once_when_first_attempt_contaminated(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_compact_chunks(chunks, *, prompt_template=None, **kwargs):
        calls.append(prompt_template or "")
        if len(calls) == 1:
            return "### Phase 1 — copied heading"  # trips template_signature
        return "- Clean second-attempt summary of the chunks."

    monkeypatch.setattr(core_module, "compact_chunks", fake_compact_chunks)

    fake_self = _make_self(tmp_path, [{"content": "raw chunk about the build"}])
    summary = await core_module.MemSearch.compact(fake_self, source=None)

    assert summary == "- Clean second-attempt summary of the chunks."
    assert len(calls) == 2
    # retry prompt carries the anti-regurgitation reminder
    assert core_module.COMPACT_RETRY_REMINDER in calls[1]
    assert "Clean second-attempt summary" in _read_summary(tmp_path)


@pytest.mark.asyncio
async def test_compact_falls_back_when_retry_still_contaminated(tmp_path: Path, monkeypatch) -> None:
    async def fake_compact_chunks(chunks, *, prompt_template=None, **kwargs):
        return "Base directory for this skill: /home/ted/.claude/skills/x"  # always contaminated

    monkeypatch.setattr(core_module, "compact_chunks", fake_compact_chunks)

    source = "/proj/.memsearch/memory/2026-07-26.md"
    fake_self = _make_self(tmp_path, [{"content": "raw"}])
    summary = await core_module.MemSearch.compact(fake_self, source=source)

    assert "contamination guard" in summary
    assert "2026-07-26.md" in summary
    assert "Base directory for this skill" not in summary
    assert "Base directory for this skill" not in _read_summary(tmp_path)


@pytest.mark.asyncio
async def test_compact_preindexes_on_disk_source(tmp_path: Path, monkeypatch) -> None:
    # Reindex-race guard (#245): a scoped compact over an on-disk source must
    # index that source itself before querying, so it never depends on the watch
    # daemon's timing.
    indexed: list[Path] = []

    async def fake_compact_chunks(chunks, *, prompt_template=None, **kwargs):
        return "- Clean summary."

    monkeypatch.setattr(core_module, "compact_chunks", fake_compact_chunks)

    source_file = tmp_path / "2026-07-26.md"
    source_file.write_text("### 09:00\n- some note\n", encoding="utf-8")

    async def _index_file(path):
        indexed.append(Path(path))
        return 1

    fake_self = SimpleNamespace(
        _store=_FakeStore([{"content": "raw"}]),
        _paths=[str(tmp_path)],
        index_file=_index_file,
    )
    await core_module.MemSearch.compact(fake_self, source=str(source_file))

    assert indexed and indexed[0] == source_file


@pytest.mark.asyncio
async def test_compact_skips_preindex_for_nonfile_source(tmp_path: Path, monkeypatch) -> None:
    # A non-path filter source (or a missing file) must not attempt a pre-index.
    indexed: list[Path] = []

    async def fake_compact_chunks(chunks, *, prompt_template=None, **kwargs):
        return "- Clean summary."

    monkeypatch.setattr(core_module, "compact_chunks", fake_compact_chunks)

    async def _index_file(path):
        indexed.append(Path(path))
        return 1

    fake_self = SimpleNamespace(
        _store=_FakeStore([{"content": "raw"}]),
        _paths=[str(tmp_path)],
        index_file=_index_file,
    )
    await core_module.MemSearch.compact(fake_self, source="/no/such/file/here.md")

    # The missing source is never pre-indexed. (The compact *output* file is still
    # indexed at the end, so `indexed` is not empty — assert the source specifically.)
    assert Path("/no/such/file/here.md") not in indexed


@pytest.mark.asyncio
async def test_compact_clean_summary_no_retry(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_compact_chunks(chunks, *, prompt_template=None, **kwargs):
        calls.append(prompt_template or "")
        return "- A perfectly clean factual summary."

    monkeypatch.setattr(core_module, "compact_chunks", fake_compact_chunks)

    fake_self = _make_self(tmp_path, [{"content": "raw"}])
    summary = await core_module.MemSearch.compact(fake_self, source=None)

    assert summary == "- A perfectly clean factual summary."
    assert len(calls) == 1  # no retry
