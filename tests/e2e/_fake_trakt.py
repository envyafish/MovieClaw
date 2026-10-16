"""给浏览器测试用的 Trakt。设备码第一次轮询仍在等待，第二次发令牌。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_MOVIE = {"title": "Dune", "year": 2021, "ids": {"trakt": 1, "tmdb": 99}}


class _Handler(BaseHTTPRequestHandler):
    token_calls = 0

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        path = self.path.split("?", 1)[0]
        if path == "/oauth/device/code":
            self._send(
                200,
                {
                    "device_code": "device-code",
                    "user_code": "ABCD1234",
                    "verification_url": "https://trakt.tv/activate",
                    "expires_in": 600,
                    "interval": 1,
                },
            )
            return
        if path == "/oauth/device/token":
            _Handler.token_calls += 1
            if _Handler.token_calls == 1:
                self._send(400, {"error": "authorization_pending"})
                return
            self._send(
                200,
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 86400,
                },
            )
            return
        if path == "/oauth/token":
            self._send(
                200,
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 86400,
                },
            )
            return
        self._send(404, {})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/users/settings":
            self._send(200, {"user": {"username": "ada"}})
            return
        if path == "/movies/trending":
            self._send(200, [{"watchers": 3, "movie": _MOVIE}])
            return
        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[:3] == ["calendars", "my", "movies"] and parts[4] == "30":
            self._send(200, [{"released": parts[3], "movie": _MOVIE}])
            return
        self._send(200, [])

    def _send(self, status: int, payload: object) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve_fake_trakt() -> tuple[ThreadingHTTPServer, str]:
    _Handler.token_calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    return server, f"http://127.0.0.1:{server.server_address[1]}"
