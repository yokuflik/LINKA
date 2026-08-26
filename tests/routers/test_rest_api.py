import uuid

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

import main as main_module
from services import auth_service, chat_service, message_service

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
    # ASGITransport runs the app in-process, in the *same* event loop as the
    # calling test (no background thread) - unlike Starlette's TestClient,
    # which spins up its own thread+loop and would collide with the shared
    # Redis/DB singletons the same way the WebSocket tests had to avoid.
    transport = ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client: httpx.AsyncClient, redis_db, phone_number: str) -> tuple[dict, str, str]:
    """Drives the real OTP flow over HTTP, reading the code straight from Redis
    (there's no SMS provider wired up - see auth_service._deliver_otp).
    Returns (user, access_token, refresh_token)."""
    resp = await client.post("/auth/otp/request", json={"phone_number": phone_number})
    assert resp.status_code == 204

    code = await redis_db.get(auth_service._otp_key(phone_number))
    assert code is not None

    resp = await client.post("/auth/otp/verify", json={"phone_number": phone_number, "code": code})
    assert resp.status_code == 200
    body = resp.json()
    return body["user"], body["access_token"], body["refresh_token"]


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# /auth
# ---------------------------------------------------------------------------

async def test_otp_login_flow_returns_valid_tokens(client, db_session: AsyncSession, redis_db):
    user, access_token, _ = await _login(client, redis_db, "+972500100001")

    assert user["phone_number"] == "+972500100001"
    # The token the flow just issued must actually work against a protected route
    resp = await client.get("/users/me", headers=_auth_header(access_token))
    assert resp.status_code == 200
    assert resp.json()["id"] == user["id"]


async def test_otp_verify_wrong_code_returns_400(client, db_session: AsyncSession, redis_db):
    phone = "+972500100002"
    await client.post("/auth/otp/request", json={"phone_number": phone})

    resp = await client.post("/auth/otp/verify", json={"phone_number": phone, "code": "000000"})
    assert resp.status_code == 400


async def test_otp_request_is_rate_limited(client, redis_db, monkeypatch):
    monkeypatch.setattr(auth_service, "OTP_REQUEST_RATE_LIMIT_MAX", 2)
    phone = "+972500100003"

    for _ in range(2):
        resp = await client.post("/auth/otp/request", json={"phone_number": phone})
        assert resp.status_code == 204

    resp = await client.post("/auth/otp/request", json={"phone_number": phone})
    assert resp.status_code == 429


async def test_refresh_returns_a_new_working_access_token(client, db_session: AsyncSession, redis_db):
    _, _, refresh_token = await _login(client, redis_db, "+972500100004")

    resp = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    new_access_token = resp.json()["access_token"]

    resp = await client.get("/users/me", headers=_auth_header(new_access_token))
    assert resp.status_code == 200


async def test_refresh_with_a_garbage_token_returns_401(client, redis_db):
    resp = await client.post("/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert resp.status_code == 401


async def test_logout_then_refresh_fails(client, db_session: AsyncSession, redis_db):
    _, _, refresh_token = await _login(client, redis_db, "+972500100025")

    resp = await client.post("/auth/logout", json={"refresh_token": refresh_token})
    assert resp.status_code == 204

    resp = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /users
# ---------------------------------------------------------------------------

async def test_get_me_without_a_token_is_rejected(client, db_session: AsyncSession):
    resp = await client.get("/users/me")
    assert resp.status_code == 403  # HTTPBearer's own "no credentials at all" response


async def test_get_me_with_a_garbage_token_returns_401(client, db_session: AsyncSession):
    resp = await client.get("/users/me", headers=_auth_header("not-a-real-token"))
    assert resp.status_code == 401


async def test_patch_me_updates_only_the_given_fields(client, db_session: AsyncSession, redis_db):
    _, access_token, _ = await _login(client, redis_db, "+972500100005")

    resp = await client.patch("/users/me", json={"display_name": "New Name"}, headers=_auth_header(access_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "New Name"
    assert body["about_text"] is None


async def test_lookup_user_by_phone(client, db_session: AsyncSession, redis_db):
    target, _, _ = await _login(client, redis_db, "+972500100026")
    _, looker_token, _ = await _login(client, redis_db, "+972500100027")

    resp = await client.get("/users/by-phone", params={"phone_number": "+972500100026"}, headers=_auth_header(looker_token))
    assert resp.status_code == 200
    assert resp.json()["id"] == target["id"]


async def test_lookup_user_by_phone_not_found_returns_404(client, db_session: AsyncSession, redis_db):
    _, token, _ = await _login(client, redis_db, "+972500100028")

    resp = await client.get("/users/by-phone", params={"phone_number": "+972500000000"}, headers=_auth_header(token))
    assert resp.status_code == 404


async def test_lookup_user_by_phone_requires_auth(client, db_session: AsyncSession):
    resp = await client.get("/users/by-phone", params={"phone_number": "+972500100026"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /chats
# ---------------------------------------------------------------------------

async def test_create_and_list_private_chat(client, db_session: AsyncSession, redis_db):
    _, token_a, _ = await _login(client, redis_db, "+972500100006")
    user_b, _, _ = await _login(client, redis_db, "+972500100007")

    resp = await client.post("/chats/private", json={"other_user_id": user_b["id"]}, headers=_auth_header(token_a))
    assert resp.status_code == 200
    chat = resp.json()
    assert chat["is_group"] is False

    resp = await client.get("/chats", headers=_auth_header(token_a))
    assert resp.status_code == 200
    chats = resp.json()
    assert any(c["chat"]["id"] == chat["id"] for c in chats)


async def test_get_chat_members_returns_both_sides_of_a_private_chat(client, db_session: AsyncSession, redis_db):
    user_a, token_a, _ = await _login(client, redis_db, "+972500100031")
    user_b, _, _ = await _login(client, redis_db, "+972500100032")

    resp = await client.post("/chats/private", json={"other_user_id": user_b["id"]}, headers=_auth_header(token_a))
    chat_id = resp.json()["id"]

    resp = await client.get(f"/chats/{chat_id}/members", headers=_auth_header(token_a))
    assert resp.status_code == 200
    members = resp.json()
    phone_numbers = {m["user"]["phone_number"] for m in members}
    assert phone_numbers == {"+972500100031", "+972500100032"}


async def test_get_chat_members_requires_being_a_participant(client, db_session: AsyncSession, redis_db):
    _, owner_token, _ = await _login(client, redis_db, "+972500100033")
    _, outsider_token, _ = await _login(client, redis_db, "+972500100034")

    resp = await client.post("/chats/groups", json={"title": "Group"}, headers=_auth_header(owner_token))
    chat_id = resp.json()["id"]

    resp = await client.get(f"/chats/{chat_id}/members", headers=_auth_header(outsider_token))
    assert resp.status_code == 403


async def test_create_group_chat_makes_creator_the_owner(client, db_session: AsyncSession, redis_db):
    _, token, _ = await _login(client, redis_db, "+972500100008")

    resp = await client.post("/chats/groups", json={"title": "REST Group"}, headers=_auth_header(token))
    assert resp.status_code == 200
    chat = resp.json()

    resp = await client.get("/chats", headers=_auth_header(token))
    item = next(c for c in resp.json() if c["chat"]["id"] == chat["id"])
    assert item["role"] == chat_service.ROLE_OWNER


async def test_non_admin_cannot_update_group_details(client, db_session: AsyncSession, redis_db):
    _, owner_token, _ = await _login(client, redis_db, "+972500100009")
    member, member_token, _ = await _login(client, redis_db, "+972500100010")

    resp = await client.post(
        "/chats/groups", json={"title": "Group", "initial_member_ids": [member["id"]]}, headers=_auth_header(owner_token)
    )
    chat_id = resp.json()["id"]

    resp = await client.patch(f"/chats/{chat_id}", json={"title": "Hijacked"}, headers=_auth_header(member_token))
    assert resp.status_code == 403

    resp = await client.patch(f"/chats/{chat_id}", json={"title": "Renamed"}, headers=_auth_header(owner_token))
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed"


async def test_add_member_then_duplicate_add_returns_409(client, db_session: AsyncSession, redis_db):
    _, owner_token, _ = await _login(client, redis_db, "+972500100011")
    newcomer, _, _ = await _login(client, redis_db, "+972500100012")

    resp = await client.post("/chats/groups", json={"title": "Group"}, headers=_auth_header(owner_token))
    chat_id = resp.json()["id"]

    resp = await client.post(f"/chats/{chat_id}/members", json={"user_id": newcomer["id"]}, headers=_auth_header(owner_token))
    assert resp.status_code == 200
    assert resp.json()["role"] == chat_service.ROLE_MEMBER

    resp = await client.post(f"/chats/{chat_id}/members", json={"user_id": newcomer["id"]}, headers=_auth_header(owner_token))
    assert resp.status_code == 409


async def test_non_admin_cannot_add_a_member(client, db_session: AsyncSession, redis_db):
    _, owner_token, _ = await _login(client, redis_db, "+972500100013")
    member, member_token, _ = await _login(client, redis_db, "+972500100014")
    outsider, _, _ = await _login(client, redis_db, "+972500100015")

    resp = await client.post(
        "/chats/groups", json={"title": "Group", "initial_member_ids": [member["id"]]}, headers=_auth_header(owner_token)
    )
    chat_id = resp.json()["id"]

    resp = await client.post(f"/chats/{chat_id}/members", json={"user_id": outsider["id"]}, headers=_auth_header(member_token))
    assert resp.status_code == 403


async def test_remove_member(client, db_session: AsyncSession, redis_db):
    _, owner_token, _ = await _login(client, redis_db, "+972500100016")
    member, _, _ = await _login(client, redis_db, "+972500100017")

    resp = await client.post(
        "/chats/groups", json={"title": "Group", "initial_member_ids": [member["id"]]}, headers=_auth_header(owner_token)
    )
    chat_id = resp.json()["id"]

    resp = await client.delete(f"/chats/{chat_id}/members/{member['id']}", headers=_auth_header(owner_token))
    assert resp.status_code == 204

    resp = await client.delete(f"/chats/{chat_id}/members/{member['id']}", headers=_auth_header(owner_token))
    assert resp.status_code == 404


async def test_only_owner_can_change_a_role(client, db_session: AsyncSession, redis_db):
    _, owner_token, _ = await _login(client, redis_db, "+972500100018")
    member_a, member_a_token, _ = await _login(client, redis_db, "+972500100019")
    member_b, _, _ = await _login(client, redis_db, "+972500100020")

    resp = await client.post(
        "/chats/groups",
        json={"title": "Group", "initial_member_ids": [member_a["id"], member_b["id"]]},
        headers=_auth_header(owner_token),
    )
    chat_id = resp.json()["id"]

    # An admin (not owner) may not promote/demote
    resp = await client.patch(
        f"/chats/{chat_id}/members/{member_b['id']}", json={"role": chat_service.ROLE_ADMIN}, headers=_auth_header(member_a_token)
    )
    assert resp.status_code == 403

    resp = await client.patch(
        f"/chats/{chat_id}/members/{member_a['id']}", json={"role": chat_service.ROLE_ADMIN}, headers=_auth_header(owner_token)
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == chat_service.ROLE_ADMIN


# ---------------------------------------------------------------------------
# /chats/{chat_id}/messages
# ---------------------------------------------------------------------------

async def test_message_history_is_empty_for_a_new_chat(client, db_session: AsyncSession, redis_db):
    _, token, _ = await _login(client, redis_db, "+972500100021")

    resp = await client.post("/chats/groups", json={"title": "Group"}, headers=_auth_header(token))
    chat_id = resp.json()["id"]

    resp = await client.get(f"/chats/{chat_id}/messages", headers=_auth_header(token))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_message_history_returns_messages_sent_over_the_service_layer(client, db_session: AsyncSession, redis_db):
    # Sending happens over the WebSocket protocol, not REST (by design) - this
    # confirms the REST history endpoint still sees what message_service wrote.
    user, token, _ = await _login(client, redis_db, "+972500100022")
    resp = await client.post("/chats/groups", json={"title": "Group"}, headers=_auth_header(token))
    chat_id = resp.json()["id"]

    # ids on the wire are strings (see routers/schemas.py's IdStr) - back to
    # ints for this direct service-layer call, same as a real dispatch layer
    # (WebSocket or REST) would do when parsing a request.
    await message_service.send_message(
        db_session, sender_id=int(user["id"]), chat_id=int(chat_id), client_message_id=str(uuid.uuid4()), content="hello from the service layer"
    )

    resp = await client.get(f"/chats/{chat_id}/messages", headers=_auth_header(token))
    assert resp.status_code == 200
    messages = resp.json()
    assert len(messages) == 1
    assert messages[0]["content"] == "hello from the service layer"


async def test_non_participant_cannot_read_message_history(client, db_session: AsyncSession, redis_db):
    _, owner_token, _ = await _login(client, redis_db, "+972500100023")
    _, outsider_token, _ = await _login(client, redis_db, "+972500100024")

    resp = await client.post("/chats/groups", json={"title": "Private Group"}, headers=_auth_header(owner_token))
    chat_id = resp.json()["id"]

    resp = await client.get(f"/chats/{chat_id}/messages", headers=_auth_header(outsider_token))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------

async def test_healthz_reports_the_database_as_reachable(client, db_session: AsyncSession):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"database": True}


# ---------------------------------------------------------------------------
# Ids on the wire must be JSON strings, never JSON numbers
# ---------------------------------------------------------------------------
# Regression coverage for a real bug: a 64-bit Snowflake id sent as a JSON
# number gets silently corrupted the instant a browser's JSON.parse decodes
# it (IEEE-754 doubles only represent integers exactly up to 2^53-1) - this
# is exactly what broke private-chat creation the first time this was tested
# end to end. These assertions are the contract that must never regress.

async def test_user_ids_are_strings_on_the_wire(client, db_session: AsyncSession, redis_db):
    user, token, _ = await _login(client, redis_db, "+972500100029")
    assert isinstance(user["id"], str)

    resp = await client.get("/users/me", headers=_auth_header(token))
    assert isinstance(resp.json()["id"], str)


async def test_chat_and_message_ids_are_strings_on_the_wire(client, db_session: AsyncSession, redis_db):
    user, token, _ = await _login(client, redis_db, "+972500100030")

    resp = await client.post("/chats/groups", json={"title": "Group"}, headers=_auth_header(token))
    chat = resp.json()
    assert isinstance(chat["id"], str)
    assert chat["last_message_id"] is None  # nullable id field - must stay None, not "None" or 0

    await message_service.send_message(
        db_session, sender_id=int(user["id"]), chat_id=int(chat["id"]), client_message_id=str(uuid.uuid4()), content="hi"
    )

    resp = await client.get("/chats", headers=_auth_header(token))
    item = resp.json()[0]
    assert isinstance(item["chat"]["id"], str)
    assert isinstance(item["chat"]["last_message_id"], str)

    resp = await client.get(f"/chats/{chat['id']}/messages", headers=_auth_header(token))
    message = resp.json()[0]
    assert isinstance(message["id"], str)
    assert isinstance(message["chat_id"], str)
    assert isinstance(message["sender_id"], str)
