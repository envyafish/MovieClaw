import httpx
import pytest

from movieclaw_media.trakt import TraktAuthError, TraktClient, TraktPending


def _client(handler) -> TraktClient:
    return TraktClient("cid", "secret", transport=httpx.MockTransport(handler))


async def test_public_get_sends_client_headers_and_not_bearer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{"title": "Pop"}], headers={"X-Pagination-Page-Count": "4"})

    body, headers = await _client(handler).get_json("/movies/popular", params={"page": "1", "limit": "20"})
    assert body == [{"title": "Pop"}]
    assert headers["x-pagination-page-count"] == "4"
    assert seen[0].headers["trakt-api-key"] == "cid"
    assert seen[0].headers["trakt-api-version"] == "2"
    assert "authorization" not in seen[0].headers


async def test_user_get_sends_bearer_and_401_is_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer access"
        return httpx.Response(401, json={"error": "invalid"})

    with pytest.raises(TraktAuthError):
        await _client(handler).get_json("/calendars/my/movies/2026-10-16/30", token="access")


async def test_device_flow_pending_and_success() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device/code"):
            return httpx.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "user_code": "ABCD1234",
                    "verification_url": "https://trakt.tv/activate",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 86400,
            },
        )

    client = _client(handler)
    granted = await client.device_code()
    assert granted.user_code == "ABCD1234"
    assert granted.device_code == "device-secret"
    with pytest.raises(TraktPending):
        await client.device_token(granted.device_code)
    bundle = await client.device_token(granted.device_code)
    assert bundle.access_token == "access"
    assert bundle.refresh_token == "refresh"
