import pytest
from httpx import AsyncClient
from sqlalchemy import text

from mes_core.modules.identity.terminal_auth import hash_pin, verify_pin
from mes_core.shared.db.session import Database
from tests.conftest import auth
from tests.helpers import device_token, plant_id, terminal_login

pytestmark = pytest.mark.db


def test_pin_hash_roundtrip() -> None:
    h = hash_pin("1234")
    assert h.startswith("scrypt$") and verify_pin("1234", h) and not verify_pin("1235", h)
    assert hash_pin("1234") != h  # salted


async def test_enrolment_pin_login_and_session_lifecycle(client: AsyncClient) -> None:
    token = await device_token(client, "T-L1", fresh=True)
    r = await client.get("/terminal/device", headers={"X-Terminal-Device": token})
    assert r.status_code == 200 and r.json()["line_code"] == "L1"
    assert (await client.get("/terminal/device", headers={"X-Terminal-Device": "nope"})).status_code == 401

    r = await client.post("/terminal/login", json={"pin": "9876"}, headers={"X-Terminal-Device": token})
    assert r.status_code == 401 and r.json()["code"] == "wrong_pin"

    op1 = await terminal_login(client, "T-L1", "1111")
    me = (await client.get("/me", headers=op1)).json()
    assert me["username"] == "operator1"
    assert "execution:operate" in me["permissions"] and "schedule:publish" not in me["permissions"]
    assert len(me["plant_ids"]) == 1

    # a supervisor's PIN on a terminal still only gets operator rights
    sup = await terminal_login(client, "T-L1", "4444")
    assert "schedule:publish" not in (await client.get("/me", headers=sup)).json()["permissions"]
    # ...and replaced the previous session on that device
    assert (await client.get("/me", headers=op1)).status_code == 401

    assert (await client.post("/terminal/logout", headers=sup)).status_code == 204
    assert (await client.get("/me", headers=sup)).status_code == 401


async def test_revoked_enrolment_disables_device_and_sessions(client: AsyncClient) -> None:
    token = await device_token(client, "T-L1", fresh=True)
    session = await terminal_login(client, "T-L1", "2222")
    pid = await plant_id(client)
    tid = next(t["id"] for t in (await client.get("/terminals", params={"plant_id": pid}, headers=auth("admin"))).json() if t["code"] == "T-L1")
    assert (await client.post(f"/terminals/{tid}/revoke", headers=auth("admin"))).status_code == 204
    assert (await client.get("/terminal/device", headers={"X-Terminal-Device": token})).status_code == 401
    assert (await client.get("/me", headers=session)).status_code == 401
    await device_token(client, "T-L1", fresh=True)  # re-enrol for other tests


async def test_wrong_pins_lock_the_terminal(client: AsyncClient) -> None:
    pid = await plant_id(client)
    lines = (await client.get(f"/plants/{pid}/structure", headers=auth("admin"))).json()["lines"]
    r = await client.post("/terminals", json={"plant_id": pid, "line_id": lines[0]["id"], "code": "T-LOCK", "name": "Lock test"}, headers=auth("admin"))
    assert r.status_code == 201, r.text
    token = (await client.post(f"/terminals/{r.json()['id']}/enroll", headers=auth("admin"))).json()["device_token"]
    codes = []
    for _ in range(5):
        codes.append((await client.post("/terminal/login", json={"pin": "0000"}, headers={"X-Terminal-Device": token})).status_code)
    assert codes == [401, 401, 401, 401, 429]
    r = await client.post("/terminal/login", json={"pin": "1111"}, headers={"X-Terminal-Device": token})
    assert r.status_code == 429 and r.json()["details"]["retry_after"] > 0


async def test_operator_admin_and_pin_redaction(client: AsyncClient, db: Database) -> None:
    pid = await plant_id(client)
    r = await client.post("/operators", json={"plant_id": pid, "username": "op.tester", "display_name": "Tess Tester", "pin": "6543"}, headers=auth("supervisor"))
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    r = await client.put(f"/operators/{uid}/pin", json={"plant_id": pid, "pin": "1111"}, headers=auth("supervisor"))
    assert r.status_code == 409 and r.json()["code"] == "pin_in_use"
    assert (await client.put(f"/operators/{uid}/pin", json={"plant_id": pid, "pin": "12"}, headers=auth("supervisor"))).status_code == 422
    assert (await client.put(f"/operators/{uid}/pin", json={"plant_id": pid, "pin": "56789"}, headers=auth("supervisor"))).status_code == 204
    assert (await client.get("/operators", params={"plant_id": pid}, headers=auth("operator"))).status_code == 403
    ops = (await client.get("/operators", params={"plant_id": pid}, headers=auth("supervisor"))).json()
    assert next(o for o in ops if o["username"] == "op.tester")["has_pin"]

    async with db.read() as s:
        rows = (
            await s.execute(
                text("SELECT action, after->>'pin_hash', after->>'pin_lookup', changed_fields FROM audit.audit_log "
                     "WHERE table_name = 'operator_credential' AND after->>'user_id' = :u ORDER BY id"),
                {"u": uid},
            )
        ).all()
    assert rows and all(r[1] == "***" and r[2] == "***" for r in rows)
    assert "pin_hash" in rows[-1][3]  # the change itself is still recorded
