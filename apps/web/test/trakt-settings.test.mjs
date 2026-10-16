import assert from "node:assert/strict";
import test from "node:test";

import { connectionLabel } from "../lib/trakt-connection.ts";

test("连接卡片文案", () => {
  assert.equal(
    connectionLabel({ app_configured: false, status: "disconnected" }),
    "管理员尚未配置 Trakt",
  );
  assert.equal(
    connectionLabel({ app_configured: true, status: "connected", username: "ada" }),
    "ada",
  );
  assert.equal(
    connectionLabel({ app_configured: true, status: "connected", username: "" }),
    "已连接",
  );
  assert.equal(
    connectionLabel({ app_configured: true, status: "expired", username: "ada" }),
    "ada",
  );
  assert.equal(
    connectionLabel({
      app_configured: true,
      status: "disconnected",
      message: "验证码已过期，请重新获取",
    }),
    "验证码已过期，请重新获取",
  );
});
