"""Support runbooks: Markdown files with a small front matter block.

    ---
    id: blank-labels
    title: Label printer prints blank labels
    category: HARDWARE
    symptoms: labels come out empty or faint
    ---
    (body: operator steps, diagnosis, fix, when to escalate)

The index (id, title, category, symptoms) goes into the system prompt; the agent reads a full runbook with the
`read_runbook` tool when one fits.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Runbook:
    id: str
    title: str
    category: str
    symptoms: str
    body: str


def parse(text: str, fallback_id: str) -> Runbook:
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        head, _, body = text[3:].partition("\n---")
        for line in head.strip().splitlines():
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    return Runbook(meta.get("id", fallback_id), meta.get("title", fallback_id), meta.get("category", "OTHER"),
                   meta.get("symptoms", ""), body.strip())


class Knowledge:
    def __init__(self, runbooks_dir: Path) -> None:
        self.runbooks: dict[str, Runbook] = {}
        guide = runbooks_dir.parent / "architecture.md"
        self.architecture = guide.read_text(encoding="utf-8") if guide.is_file() else "(no system guide)"
        if runbooks_dir.is_dir():
            for f in sorted(runbooks_dir.glob("*.md")):
                rb = parse(f.read_text(encoding="utf-8"), f.stem)
                self.runbooks[rb.id] = rb

    def index(self) -> str:
        if not self.runbooks:
            return "(no runbooks available)"
        return "\n".join(f"- {r.id}: {r.title} [{r.category}]. Symptoms: {r.symptoms}" for r in self.runbooks.values())

    def get(self, runbook_id: str) -> Runbook | None:
        return self.runbooks.get(runbook_id)
