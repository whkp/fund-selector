from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class KnowledgeChunk:
    """A source fragment passed to the model; it is never treated as fund fact."""

    chunk_id: str
    title: str
    content: str
    source: str


class KnowledgeBase(Protocol):
    def status(self) -> dict[str, object]: ...

    async def search(self, query: str, limit: int = 5) -> list[KnowledgeChunk]: ...


class LocalMarkdownKnowledgeBase:
    """Small replaceable adapter for local research notes.

    It deliberately uses a simple lexical retriever for the MVP. The protocol can
    later be implemented by pgvector, Milvus, or an external retrieval service.
    """

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root).expanduser() if root else None

    def status(self) -> dict[str, object]:
        configured = bool(self.root and self.root.exists())
        return {
            "provider": "local-markdown",
            "status": "ACTIVE" if configured else "NOT_CONFIGURED",
            "configured": configured,
            "source": str(self.root) if self.root else "",
            "retrievalVersion": "lexical-v1",
        }

    async def search(self, query: str, limit: int = 5) -> list[KnowledgeChunk]:
        if not self.root or not self.root.exists():
            return []
        tokens = {token.lower() for token in re.split(r"[^\w\u4e00-\u9fff]+", query) if len(token) >= 2}
        # Chinese text commonly arrives without spaces. Add short overlapping terms
        # so a note containing "长期定投" can match a longer user question.
        han_runs = re.findall(r"[\u4e00-\u9fff]{2,}", query)
        tokens.update(run[index:index + 2] for run in han_runs for index in range(len(run) - 1))
        ranked: list[tuple[int, Path, str]] = []
        for path in self.root.rglob("*.md"):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            score = sum(content.lower().count(token) for token in tokens)
            if score:
                ranked.append((score, path, content))
        ranked.sort(key=lambda item: (-item[0], str(item[1])))
        chunks: list[KnowledgeChunk] = []
        for _, path, content in ranked[:limit]:
            title = next((line[2:].strip() for line in content.splitlines() if line.startswith("#")), path.stem)
            chunks.append(KnowledgeChunk(
                chunk_id=f"local:{path.relative_to(self.root)}",
                title=title,
                content=content[:4000],
                source=str(path),
            ))
        return chunks


class DisabledKnowledgeBase:
    def status(self) -> dict[str, object]:
        return {"provider": "disabled", "status": "NOT_CONFIGURED", "configured": False, "source": ""}

    async def search(self, query: str, limit: int = 5) -> list[KnowledgeChunk]:
        return []


def build_knowledge_base() -> KnowledgeBase:
    from .config import config_value

    path = str(config_value("knowledge", "path", "", env_name="FUND_COMPASS_KNOWLEDGE_BASE_PATH")).strip()
    return LocalMarkdownKnowledgeBase(path) if path else DisabledKnowledgeBase()
