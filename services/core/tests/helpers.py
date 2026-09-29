"""Shared test flows that go through the real API (no shortcuts into the database)."""

from httpx import AsyncClient

from mes_common.messaging import InboxConsumer
from tests.conftest import auth
from tests.factories import TOPIC, envelope

_device_tokens: dict[str, str] = {}


async def plant_id(client: AsyncClient) -> str:
    return (await client.get("/plants", headers=auth("admin"))).json()[0]["id"]


async def dispatched_order(client: AsyncClient, consumer: InboxConsumer, payload: dict) -> dict:
    """ERP order -> schedule -> publish -> dispatch. Returns the work order detail."""
    await consumer.process(TOPIC, envelope(payload))
    pid = await plant_id(client)
    sid = (await client.post("/schedules", json={"plant_id": pid, "horizon_days": 14}, headers=auth("supervisor"))).json()["id"]
    r = await client.post(f"/schedules/{sid}/auto-sequence", json={"reset_unlocked": False}, headers=auth("supervisor"))
    assert r.status_code == 200, r.text
    r = await client.post(f"/schedules/{sid}/publish", json={"reason": "test plan"}, headers=auth("supervisor"))
    assert r.status_code == 200, r.text
    items = (await client.get("/work-orders", params={"search": payload["erp_order_number"]}, headers=auth("supervisor"))).json()["items"]
    wo = (await client.get(f"/work-orders/{items[0]['id']}", headers=auth("supervisor"))).json()
    assert wo["status"] == "SCHEDULED", wo["status"]
    r = await client.post(f"/work-orders/{wo['id']}/dispatch", json={"row_version": wo["row_version"]}, headers=auth("supervisor"))
    assert r.status_code == 200, r.text
    return r.json()


async def terminal(client: AsyncClient, code: str) -> dict:
    pid = await plant_id(client)
    return next(t for t in (await client.get("/terminals", params={"plant_id": pid}, headers=auth("admin"))).json() if t["code"] == code)


async def device_token(client: AsyncClient, code: str, *, fresh: bool = False) -> str:
    if fresh or code not in _device_tokens:
        t = await terminal(client, code)
        r = await client.post(f"/terminals/{t['id']}/enroll", headers=auth("admin"))
        assert r.status_code == 200, r.text
        _device_tokens[code] = r.json()["device_token"]
    return _device_tokens[code]


async def terminal_login(client: AsyncClient, code: str, pin: str) -> dict[str, str]:
    r = await client.post("/terminal/login", json={"pin": pin}, headers={"X-Terminal-Device": await device_token(client, code)})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
