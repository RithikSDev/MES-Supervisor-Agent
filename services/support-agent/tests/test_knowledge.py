"""Knowledge store: built-in machine guides and user guide, uploads (PDF manual, CSV fault codes), search, and the
agents' search_knowledge tool."""

import json

import httpx
import pytest
from conftest import PLANT, Container, FakeMes, Script, bearer
from sqlalchemy import text

pytestmark = pytest.mark.db


def tiny_pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF with text (enough for text extraction)."""
    content = "BT /F1 11 Tf 50 750 Td " + " ".join(f"({ln}) Tj 0 -16 Td" for ln in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


async def test_builtin_guides_uploads_and_search(env: tuple[Container, FakeMes], http: httpx.AsyncClient) -> None:
    c, _ = env
    async with c.session_factory() as s, s.begin():
        await s.execute(text("DELETE FROM agent.knowledge_doc"))
    assert await c.agent.docs.sync_builtin() >= 8
    assert await c.agent.docs.sync_builtin() == 0  # unchanged files are not reloaded

    hits = await c.agent.docs.search("spindle drive alarm 2031 fuse blown")
    assert hits and hits[0]["equipment_type"] == "CNC_LATHE" and "megger" in hits[0]["text"].lower()
    howto = await c.agent.docs.search("publish the schedule e-signature", kind="USER_GUIDE")
    assert "/work-orders" in [h["screen"] for h in howto[:3]]  # the orders & scheduling guide

    # an OEM manual (PDF) and a fault-code list (CSV) uploaded by an admin
    files = {"file": ("mill-manual.pdf", tiny_pdf(["Alarm 7788 coolant pump overload", "Check pump motor thermal relay"]),
                      "application/pdf")}
    data = {"title": "MX-500 maintenance manual", "kind": "MANUAL", "equipment_type": "cnc_mill"}
    assert (await http.post("/api/v1/agent/knowledge", files=files, data=data, headers=bearer("sup-token"))).status_code == 403
    r = await http.post("/api/v1/agent/knowledge", files=files, data=data, headers=bearer("admin-token"))
    assert r.status_code == 201 and r.json()["pages"] == 1
    csv = "code,text,cause,remedy\nE-4455,Door lock not confirmed,Door switch misaligned,Adjust switch gap 2 mm\n"
    r = await http.post("/api/v1/agent/knowledge", files={"file": ("codes.csv", csv.encode(), "text/csv")},
                        data={"title": "Fault codes MX-500", "kind": "ALARM_LIST", "equipment_type": "CNC_MILL"},
                        headers=bearer("admin-token"))
    assert r.status_code == 201
    bad = await http.post("/api/v1/agent/knowledge", files={"file": ("x.exe", b"MZ", "application/octet-stream")},
                          data={"title": "x"}, headers=bearer("admin-token"))
    assert bad.status_code == 422

    pdf_hit = await c.agent.docs.search("coolant pump overload", equipment_type="CNC_MILL")
    assert pdf_hit[0]["document"] == "MX-500 maintenance manual" and pdf_hit[0]["page"] == 1
    code_hit = await c.agent.docs.search("E-4455")
    assert "Adjust switch gap" in code_hit[0]["text"]

    docs = (await http.get("/api/v1/agent/knowledge", headers=bearer("sup-token"))).json()
    builtin = next(d for d in docs if d["builtin"])
    uploaded = next(d for d in docs if d["title"] == "Fault codes MX-500")
    assert (await http.delete(f"/api/v1/agent/knowledge/{builtin['id']}", headers=bearer("admin-token"))).status_code == 409
    assert (await http.delete(f"/api/v1/agent/knowledge/{uploaded['id']}", headers=bearer("admin-token"))).status_code == 204


async def test_assistant_searches_the_knowledge(env: tuple[Container, FakeMes], http: httpx.AsyncClient) -> None:
    c, _ = env
    await c.agent.docs.sync_builtin()
    chat = (await http.post("/api/v1/agent/engineer/chats", json={"plant_id": PLANT}, headers=bearer("sup-token"))).json()
    model = Script([[("tool", "search_knowledge", {"query": "clamp pressure low", "equipment_type": "CNC_MILL"})],
                    [("text", "Check the clamp cylinder seal and the hydraulic filter.")]])
    c.agent.model = model
    r = await http.post(f"/api/v1/agent/engineer/chats/{chat['id']}/messages", json={"text": "MILL-03 clamp pressure low"},
                        headers=bearer("sup-token"))
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert {"type": "status", "text": "Looking in the manuals and guides"} in events
    result = model.seen[1]["messages"][-1]["content"][0]["content"]
    assert "SP-SEAL-KIT-CYL" in result and "CNC milling" in result
    assert "search_knowledge" in model.seen[0]["tools"]
