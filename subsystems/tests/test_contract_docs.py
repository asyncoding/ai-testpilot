"""fs-docs 契约测试（KnowledgeSourcePort）。"""

from pathlib import Path

from testpilot_adapters.fs_docs import FileSystemDocsAdapter
from testpilot_core.enums import AuthorityLevel
from testpilot_core.models import EventCheckpoint

FIXTURES = Path(__file__).parent / "fixtures"


async def test_scan_and_incremental():
    adapter = FileSystemDocsAdapter([FIXTURES / "docs"])
    docs = await adapter.list_documents()
    assert len(docs) == 2
    by_name = {Path(d.metadata_["relative_path"]).name: d for d in docs}
    kb = by_name["kill-backup.md"]
    assert kb.urn.startswith("urn:testpilot:fs-docs:document:")
    assert kb.content_hash and kb.authority_level == AuthorityLevel.L2_EXPERIENCE
    # 增量事件以 mtime 水位驱动
    events = await adapter.list_changes(EventCheckpoint(source_system="fs-docs", last_cursor=None))
    second = await adapter.list_changes(
        EventCheckpoint(source_system="fs-docs", last_cursor="2099-01-01T00:00:00+00:00")
    )
    assert len(events) == 2
    assert second == []


async def test_read_document_by_urn_and_missing_root():
    adapter = FileSystemDocsAdapter([FIXTURES / "docs"])
    docs = await adapter.list_documents()
    again = await adapter.read_document(docs[0].urn)
    assert again.content_hash == docs[0].content_hash

    from testpilot_core.errors import AdapterUnavailableError
    import pytest

    with pytest.raises(AdapterUnavailableError):
        await FileSystemDocsAdapter([FIXTURES / "missing"]).list_documents()
