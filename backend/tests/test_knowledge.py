import asyncio

from app.knowledge import LocalMarkdownKnowledgeBase


def test_local_markdown_knowledge_base_retrieves_matching_notes(tmp_path):
    note = tmp_path / "定投经验.md"
    note.write_text("# 定投复核\n\n长期定投需要先确认风险预算与数据来源。", encoding="utf-8")
    base = LocalMarkdownKnowledgeBase(str(tmp_path))
    results = asyncio.run(base.search("长期定投的风险预算", limit=3))
    assert base.status()["status"] == "ACTIVE"
    assert results[0].title == "定投复核"
    assert results[0].chunk_id == "local:定投经验.md"
