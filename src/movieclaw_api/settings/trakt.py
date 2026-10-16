"""Trakt 应用凭证。成员连接在同一模块的后续任务里补上。"""

from __future__ import annotations

from movieclaw_api.settings.base import SettingSchema, register_setting

TRAKT_APP_NAMESPACE = "integration.trakt"


@register_setting(
    namespace=TRAKT_APP_NAMESPACE,
    title="Trakt",
    secret_fields=["client_secret"],
)
class TraktAppSetting(SettingSchema):
    client_id: str = ""
    client_secret: str = ""
