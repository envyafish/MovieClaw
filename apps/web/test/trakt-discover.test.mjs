import assert from "node:assert/strict";
import test from "node:test";

import { discoverCardFields, showCollectionNotice } from "../lib/discover-card.ts";

test("日历备注留下，空海报保持空字符串", () => {
  const fields = discoverCardFields({ note: "10月18日 上映", navigable: true, poster_url: "" });
  assert.equal(fields.note, "10月18日 上映");
  assert.equal(fields.navigable, true);
  assert.equal(fields.posterUrl, "");
});

test("未声明 navigable 时默认可点", () => {
  assert.equal(discoverCardFields({ poster_url: "https://img/x.jpg" }).navigable, true);
});

test("带说明的空行要显示，普通空行收起", () => {
  assert.equal(
    showCollectionNotice({ code: "trakt_not_connected", message: "连接 Trakt 后查看" }, 0),
    true,
  );
  assert.equal(showCollectionNotice(null, 0), false);
});
