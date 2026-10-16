"""Trakt 设置、连接和发现页的浏览器端到端。

标 integration：要 pnpm 与 Playwright Chromium，CI 不跑。
本地：``pytest -m integration tests/e2e/test_trakt_discover_browser.py``。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fake_trakt import serve_fake_trakt

playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "apps" / "web"
ADMIN = {"username": "admin", "password": "e2e-passw0rd"}
MEMBER = {"username": "family", "password": "family-pass-1"}
_CHROMIUM_CANDIDATES = (os.environ.get("E2E_CHROMIUM"), "/opt/pw-browsers/chromium")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("pnpm") is None, reason="需要 pnpm 启动前端 dev server"),
    pytest.mark.skipif(
        not (WEB / "node_modules").is_dir(), reason="apps/web 未安装依赖（pnpm install）"
    ),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_http(url: str, timeout: float) -> None:
    import urllib.request

    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310
                if resp.status < 500:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(1)
    raise RuntimeError(f"服务未就绪：{url}（{last}）")


def _chromium_kwargs() -> dict:
    for cand in _CHROMIUM_CANDIDATES:
        if cand and Path(cand).exists():
            return {"executable_path": cand}
    return {}


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    root = tmp_path_factory.mktemp("trakt-e2e")
    api_port, web_port = _free_port(), _free_port()
    data = root / "data"
    data.mkdir()
    # 现有启动器无条件读取这份日志；Trakt 用例本身不查它。
    tmdb_log = root / "tmdb-requests.log"
    tmdb_log.touch()
    trakt, trakt_base = serve_fake_trakt()
    thread = threading.Thread(target=trakt.serve_forever, daemon=True)
    thread.start()
    env = {
        **os.environ,
        "APP_ENV": "local",
        "APP_RELOAD": "false",
        "APP_PORT": str(api_port),
        "DATABASE_URL": f"sqlite+aiosqlite:///{data / 'app.db'}",
        "METADATA_DIR": str(data / "metadata"),
        "LOG_DIR": str(data / "logs"),
        "SECRET_KEY_FILE": str(data / ".secret_key"),
        "TMDB_API_KEY": "",
        "E2E_TMDB_LOG": str(tmdb_log),
        "TRAKT_API_BASE_URL": trakt_base,
        "SCHEDULER_ENABLED": "false",
    }
    api_log = (root / "api.log").open("w")
    api = subprocess.Popen(  # noqa: S603
        [sys.executable, str(Path(__file__).with_name("_api_launcher.py"))],
        env=env,
        stdout=api_log,
        stderr=subprocess.STDOUT,
        cwd=str(REPO),
    )
    web_log = (root / "web.log").open("w")
    web = subprocess.Popen(  # noqa: S603
        ["pnpm", "dev", "-p", str(web_port)],
        env={
            **os.environ,
            "NEXT_DIST_DIR": ".next-e2e-trakt",
            "MOVIECLAW_API_PROXY_TARGET": f"http://127.0.0.1:{api_port}",
        },
        stdout=web_log,
        stderr=subprocess.STDOUT,
        cwd=str(WEB),
    )
    try:
        try:
            _wait_http(f"http://127.0.0.1:{api_port}/api/v1/auth/bootstrap", 90)
            _wait_http(f"http://127.0.0.1:{web_port}/login", 180)
        except Exception:
            api_log.flush()
            web_log.flush()
            api_text = (root / "api.log").read_text(encoding="utf-8", errors="replace")[-4000:]
            web_text = (root / "web.log").read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"API:\n{api_text}\nWEB:\n{web_text}") from None
        yield {"base": f"http://127.0.0.1:{web_port}", "logs": (root / "api.log", root / "web.log")}
    finally:
        for proc in (web, api):
            proc.terminate()
        for proc in (web, api):
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        trakt.shutdown()
        api_log.close()
        web_log.close()


def _login(page, base: str, account: dict[str, str], *, setup: bool) -> None:
    page.goto(f"{base}/setup")
    page.wait_for_url(lambda url: "/setup" in url or "/login" in url)
    # 登录页发现未初始化后会自己转到 /setup，等这一跳结束再填表。
    page.wait_for_timeout(1000)
    if setup and "/setup" in page.url:
        page.get_by_role("button", name="开始使用").click()
        page.get_by_label("管理员用户名").fill(account["username"])
        page.get_by_label("密码（至少 8 位）").fill(account["password"])
        page.get_by_label("确认密码").fill(account["password"])
        page.locator("button[type=submit]").click(force=True)
        page.wait_for_url(lambda url: "/setup" not in url)
    if "/login" not in page.url:
        page.goto(f"{base}/login")
        page.wait_for_url(lambda url: "/login" in url)
    if "/login" in page.url:
        page.get_by_role("button", name="登录").click()
        page.get_by_label("用户名").fill(account["username"])
        page.get_by_label("密码").fill(account["password"])
        page.locator("button[type=submit]").click(force=True)
        page.wait_for_url(lambda url: "/login" not in url)


def test_trakt_connect_and_discover(stack) -> None:
    base = stack["base"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, **_chromium_kwargs())
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(20_000)
        page.set_default_navigation_timeout(120_000)
        _login(page, base, ADMIN, setup=True)

        page.goto(f"{base}/discover/movie?source=trakt")
        page.get_by_text("管理员尚未配置 Trakt").wait_for()

        page.goto(f"{base}/settings/trakt")
        page.get_by_label("Client ID").fill("cid")
        page.get_by_label("Client Secret").fill("secret")
        page.get_by_role("button", name="保存").click()
        page.get_by_text("已设置", exact=True).wait_for()

        page.goto(f"{base}/settings/profile")
        page.get_by_role("button", name="连接 Trakt").click()
        page.get_by_text("ABCD1234").wait_for()
        page.get_by_text("ada", exact=True).wait_for(timeout=30_000)

        page.goto(f"{base}/discover/movie?source=trakt")
        trending = page.locator("section").filter(has_text="热门")
        trending.get_by_text("Dune").wait_for()
        calendar = page.locator("section").filter(has_text="上映日历")
        note = calendar.get_by_text("日 上映")
        note.wait_for()
        assert "10月" in (note.text_content() or "")
        assert "上映" in (note.text_content() or "")

        created = page.request.post(
            f"{base}/api/v1/members",
            data={"username": MEMBER["username"], "password": MEMBER["password"]},
        )
        assert created.ok, created.text()

        member = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        member_page = member.new_page()
        member_page.set_default_timeout(20_000)
        _login(member_page, base, MEMBER, setup=False)
        member_page.goto(f"{base}/discover/movie?source=trakt")
        member_page.locator("section").filter(has_text="热门").get_by_text("Dune").wait_for()
        link = member_page.locator("section").filter(has_text="上映日历").get_by_role(
            "link", name="连接 Trakt 后查看"
        )
        link.wait_for()
        assert link.get_attribute("href") == "/settings/profile"
        browser.close()
