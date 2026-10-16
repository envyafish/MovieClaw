"""Trakt 应用凭证。连接接口在成员区另挂一个路由。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import Field

from movieclaw_api.schemas.base import BaseModel
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.trakt_connection import delete_trakt_connections
from movieclaw_api.settings import TraktAppSetting, get_setting_store

router = APIRouter(prefix="/integrations/trakt", tags=["integrations"])

_HIDDEN = {"x-cli-hidden": True}


class TraktAppView(BaseModel):
    client_id: str
    secret_set: bool


class TraktAppPayload(BaseModel):
    client_id: str = Field(min_length=1)
    client_secret: str = ""


def _view(setting: TraktAppSetting) -> TraktAppView:
    return TraktAppView(client_id=setting.client_id, secret_set=bool(setting.client_secret))


@router.get(
    "/app",
    response_model=ApiResponse[TraktAppView],
    summary="读取 Trakt 应用凭证",
    operation_id="integrations.trakt.app.show",
    openapi_extra=_HIDDEN,
)
async def show_trakt_app() -> ApiResponse[TraktAppView]:
    setting = await get_setting_store().get(TraktAppSetting)
    return ok(_view(setting))


@router.put(
    "/app",
    response_model=ApiResponse[TraktAppView],
    summary="保存 Trakt 应用凭证",
    operation_id="integrations.trakt.app.set",
    openapi_extra=_HIDDEN,
)
async def set_trakt_app(payload: TraktAppPayload) -> ApiResponse[TraktAppView]:
    store = get_setting_store()
    current = await store.get(TraktAppSetting)
    secret = payload.client_secret or current.client_secret
    changed = payload.client_id != current.client_id or secret != current.client_secret
    saved = TraktAppSetting(client_id=payload.client_id, client_secret=secret)
    await store.set(saved)
    if changed:
        await delete_trakt_connections()
    return ok(_view(saved))
