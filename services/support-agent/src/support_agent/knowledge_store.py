"""Searchable knowledge for the agents: machine guides and the MES user guide (built in, from knowledge/machines and
knowledge/guide) and documents admins upload (OEM manuals as PDF, fault-code lists as CSV, notes as text/markdown).

Documents are cut into chunks (a markdown section, or about 1500 characters of a PDF page) and searched with
PostgreSQL full-text search; results name the document, section and page so answers can cite them.
"""

import csv
import hashlib
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from pypdf import PdfReader
from sqlalchemy import delete, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mes_common.ids import new_id
from support_agent.db import knowledge_chunk, knowledge_doc

log = logging.getLogger(__name__)
KINDS = ("MACHINE_GUIDE", "MANUAL", "ALARM_LIST", "USER_GUIDE", "NOTE")
CHUNK = 1500
MAX_BYTES = 20 * 1024 * 1024
SEARCH_DOC = "to_tsvector('english', coalesce(heading, '') || ' ' || text)"


@dataclass
class Chunk:
    text: str
    heading: str | None = None
    page: int | None = None


def _front_matter(raw: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    if raw.startswith("---"):
        head, _, body = raw[3:].partition("\n---")
        for line in head.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
        return meta, body.lstrip("\n")
    return meta, raw


def _windows(text_: str, size: int = CHUNK) -> list[str]:
    text_ = text_.strip()
    if len(text_) <= size:
        return [text_] if text_ else []
    out, start = [], 0
    while start < len(text_):
        end = min(len(text_), start + size)
        cut = text_.rfind("\n", start + size // 2, end) if end < len(text_) else end
        end = cut if cut > start else end
        out.append(text_[start:end].strip())
        start = end
    return [o for o in out if o]


def chunk_markdown(body: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading, buf = None, []
    for line in body.splitlines():
        if re.match(r"^#{1,3} ", line):
            if "".join(buf).strip():
                chunks += [Chunk(w, heading) for w in _windows("\n".join(buf))]
            heading, buf = line.lstrip("#").strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        chunks += [Chunk(w, heading) for w in _windows("\n".join(buf))]
    return chunks


def chunk_pdf(data: bytes) -> tuple[list[Chunk], int]:
    reader = PdfReader(io.BytesIO(data))
    chunks: list[Chunk] = []
    for i, page in enumerate(reader.pages, start=1):
        chunks += [Chunk(w, None, i) for w in _windows(page.extract_text() or "")]
    return chunks, len(reader.pages)


def chunk_csv(raw: str) -> list[Chunk]:
    """Fault-code lists: one chunk per row (code: description, cause, remedy), so a code search finds its row."""
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return []
    header, out = rows[0], []
    for r in rows[1:]:
        if any(c.strip() for c in r):
            out.append(Chunk("; ".join(f"{h}: {v}" for h, v in zip(header, r, strict=False) if v.strip()), r[0].strip() or None))
    return out


def parse(filename: str, data: bytes) -> tuple[list[Chunk], int | None]:
    name = filename.lower()
    if name.endswith(".pdf"):
        return chunk_pdf(data)
    raw = data.decode("utf-8-sig", errors="replace")
    if name.endswith(".csv"):
        return chunk_csv(raw), None
    if name.endswith((".md", ".txt", ".markdown")):
        return chunk_markdown(_front_matter(raw)[1]), None
    raise ValueError("upload a PDF, a CSV (fault codes) or a text / markdown file")


class KnowledgeStore:
    def __init__(self, sf: async_sessionmaker[AsyncSession], root: Path) -> None:
        self._sf, self.root = sf, root

    async def _replace(self, s: AsyncSession, doc: dict[str, Any], chunks: list[Chunk]) -> UUID:
        doc_id = doc.get("id") or new_id()
        await s.execute(knowledge_doc.insert().values(id=doc_id, chars=sum(len(c.text) for c in chunks), **{
            k: v for k, v in doc.items() if k != "id"}))
        if chunks:
            await s.execute(knowledge_chunk.insert(), [
                {"doc_id": doc_id, "seq": i, "page": c.page, "heading": c.heading, "text": c.text}
                for i, c in enumerate(chunks)])
        return doc_id

    async def sync_builtin(self) -> int:
        """Load knowledge/machines/*.md and knowledge/guide/*.md; changed files replace their earlier version."""
        files = sorted([*self.root.glob("machines/*.md"), *self.root.glob("guide/*.md")])
        changed = 0
        async with self._sf() as s, s.begin():
            existing = {r.source: r for r in await s.execute(select(knowledge_doc).where(knowledge_doc.c.builtin))}
            seen = set()
            for f in files:
                raw = f.read_text(encoding="utf-8")
                source = f"knowledge/{f.parent.name}/{f.name}"
                seen.add(source)
                digest = hashlib.sha256(raw.encode()).hexdigest()
                old = existing.get(source)
                if old and old.checksum == digest:
                    continue
                if old:
                    await s.execute(delete(knowledge_doc).where(knowledge_doc.c.id == old.id))
                meta, body = _front_matter(raw)
                kind = meta.get("kind") or ("MACHINE_GUIDE" if f.parent.name == "machines" else "USER_GUIDE")
                await self._replace(s, {"title": meta.get("title", f.stem), "kind": kind,
                                        "equipment_type": meta.get("equipment_type"), "screen": meta.get("screen"),
                                        "source": source, "builtin": True, "checksum": digest,
                                        "uploaded_by": "built in"}, chunk_markdown(body))
                changed += 1
            for source, old in existing.items():
                if source not in seen:
                    await s.execute(delete(knowledge_doc).where(knowledge_doc.c.id == old.id))
        if changed:
            log.info("knowledge: %d built-in documents loaded", changed)
        return changed

    async def upload(self, *, filename: str, data: bytes, title: str, kind: str, equipment_type: str | None,
                     by: str) -> dict[str, Any]:
        if len(data) > MAX_BYTES:
            raise ValueError("file too large (max 20 MB)")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        chunks, pages = parse(filename, data)
        if not chunks:
            raise ValueError("no text found in the file (a scanned PDF needs OCR first)")
        async with self._sf() as s, s.begin():
            doc_id = await self._replace(s, {"title": title.strip()[:200], "kind": kind,
                                             "equipment_type": (equipment_type or "").strip().upper() or None,
                                             "source": filename[:200], "builtin": False,
                                             "checksum": hashlib.sha256(data).hexdigest(), "pages": pages,
                                             "uploaded_by": by}, chunks)
        return {"id": str(doc_id), "chunks": len(chunks), "pages": pages}

    async def documents(self) -> list[dict[str, Any]]:
        async with self._sf() as s:
            rows = await s.execute(select(knowledge_doc, func.count(knowledge_chunk.c.id).label("chunks"))
                                   .outerjoin(knowledge_chunk, knowledge_chunk.c.doc_id == knowledge_doc.c.id)
                                   .group_by(knowledge_doc.c.id).order_by(knowledge_doc.c.kind, knowledge_doc.c.title))
            return [dict(r._mapping) for r in rows]

    async def remove(self, doc_id: UUID) -> bool:
        async with self._sf() as s, s.begin():
            res = await s.execute(delete(knowledge_doc).where(knowledge_doc.c.id == doc_id, ~knowledge_doc.c.builtin))
        return res.rowcount > 0

    async def search(self, query: str, *, kind: str | None = None, equipment_type: str | None = None,
                     limit: int = 6) -> list[dict[str, Any]]:
        tsq = func.websearch_to_tsquery("english", query)
        doc = literal_column(SEARCH_DOC)
        rank = func.ts_rank_cd(doc, tsq, 1)  # 1: divided by 1 + log(length), so long chunks don't win by size
        q = (select(knowledge_doc.c.title, knowledge_doc.c.kind, knowledge_doc.c.equipment_type, knowledge_doc.c.screen,
                    knowledge_doc.c.source, knowledge_chunk.c.doc_id, knowledge_chunk.c.page, knowledge_chunk.c.heading,
                    knowledge_chunk.c.text, rank.label("rank"))
             .join(knowledge_doc, knowledge_doc.c.id == knowledge_chunk.c.doc_id))
        if kind:
            q = q.where(knowledge_doc.c.kind == kind)
        if equipment_type:
            q = q.where((knowledge_doc.c.equipment_type == equipment_type.upper()) | knowledge_doc.c.equipment_type.is_(None))
        async with self._sf() as s:
            rows = (await s.execute(q.where(doc.op("@@")(tsq)).order_by(rank.desc()).limit(limit))).all()
            if not rows:  # words the dictionary doesn't know (fault codes like E-2031): plain substring match
                words = [w for w in re.findall(r"[\w-]{3,}", query)][:4]
                if words:
                    cond = [knowledge_chunk.c.text.ilike(f"%{w}%") for w in words]
                    rows = (await s.execute(q.where(or_(*cond)).limit(limit))).all()
        return [{"doc_id": str(r.doc_id), "document": r.title, "kind": r.kind, "equipment_type": r.equipment_type,
                 "screen": r.screen, "section": r.heading, "page": r.page, "text": r.text[:1500]} for r in rows]

    async def read(self, doc_id: UUID, page: int | None = None) -> dict[str, Any]:
        async with self._sf() as s:
            d = (await s.execute(select(knowledge_doc).where(knowledge_doc.c.id == doc_id))).first()
            if d is None:
                raise ValueError("no such document")
            q = select(knowledge_chunk).where(knowledge_chunk.c.doc_id == doc_id).order_by(knowledge_chunk.c.seq)
            if page is not None:
                q = q.where(knowledge_chunk.c.page == page)
            chunks = (await s.execute(q.limit(12))).all()
        return {"document": d.title, "kind": d.kind, "equipment_type": d.equipment_type, "screen": d.screen,
                "pages": d.pages, "sections": [{"section": c.heading, "page": c.page, "text": c.text} for c in chunks]}

