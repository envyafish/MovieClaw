"""每个用户的 Trakt 连接。本任务只留删除入口，设备码流程随后补上。"""

from __future__ import annotations


async def delete_trakt_connections() -> None:
    """应用凭证变化时清掉已有连接。连接仓储落地前这里是空操作。"""
    return None
