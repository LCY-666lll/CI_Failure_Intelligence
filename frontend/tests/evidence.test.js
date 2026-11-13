import test from "node:test";
import assert from "node:assert/strict";
import { evidenceLocation, historyStatus } from "../src/evidence.js";

test("当前引用定位当前失败，历史引用导航来源事件", () => {
  assert.deepEqual(evidenceLocation("failure:2:0", 19).to, {
    path: "/runs/19",
    hash: "#failure:2:0",
  });
  assert.deepEqual(evidenceLocation("history:13:0:1", 19).to, {
    path: "/runs/13",
    hash: "#failure:0:1",
  });
});
test("拒绝任意URL、负数、尾部文本和不安全的数字身份", () => {
  for (const value of [
    "https://evil.test",
    "javascript:alert(1)",
    "failure:-1:0",
    "history:0:0:0",
    "history:13:0:0/x",
    "failure:9007199254740992:0",
    null,
  ])
    assert.equal(evidenceLocation(value, 19), null);
});
test("空结果只在available时表示没有匹配，降级和未启用独立展示", () => {
  const state = (retrieval_status) =>
    historyStatus({ retrieval_status, parse_status: "parsed", history: [] });
  assert.match(state("available").message, /未找到匹配/);
  assert.equal(state("degraded").warning, true);
  assert.match(state("degraded").message, /暂时不可用/);
  assert.match(state("disabled").message, /未启用/);
  assert.match(state("not_ready").message, /等待解析/);
  assert.match(
    historyStatus({ retrieval_status: "not_ready", parse_status: "failed" })
      .message,
    /解析失败/,
  );
});
