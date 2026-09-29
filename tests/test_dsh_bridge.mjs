import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { apply } from "../app/dsh/bridge.mjs";

const root = mkdtempSync(join(tmpdir(), "kds-bridge-"));
process.env.KDS_CONTROL_FILE = join(root, "control.json");
process.env.KDS_STOP_FILE = join(root, "stop.json");
let state = { run_id: "one", system: "角色 {{literal}}", tools: ["read"],
  max_steps: 2, max_tool_calls: 1, output_budget: 10, request_max_tokens: 8,
  output_budget_reason: "limit", cancel: null };
const save = () => writeFileSync(process.env.KDS_CONTROL_FILE, JSON.stringify(state));
save();
const handlers = {};
let guard, section, variable, dispose, restriction;
const ctx = {
  on(event, handler) { handlers[event] = handler; },
  effect(start) { dispose = start(); },
  systemPrompt: { section(value) { section = value; }, variable(_name, value) { variable = value; } },
  tools: { schemas() { return [{ name: "read" }, { name: "subagent" }]; },
    guard(value) { guard = value; } },
};
const cancellations = [];
const agent = { status: "running", cancel(cause) { cancellations.push(cause); },
  ctx: { tools: { restrict(filter) { restriction = filter; } } } };
try {
  apply(ctx);
  handlers["agent/created"]({ agent });
  assert.deepEqual(restriction, { deny: ["subagent"] });
  assert.equal(section.text, "{{kds_instructions}}");
  assert.equal(variable(), "角色 {{literal}}");
  assert.equal(await handlers["agent/pre-step"]({ agent }, async () => ({ kind: "enter" })).then(x => x.kind), "enter");
  const request = await handlers["agent/request"]({ agent }, async () => ({ maxTokens: 100 }));
  assert.equal(request.maxTokens, 8);
  handlers["session/event"](null, { type: "assistant/message", data: { usage: { outputTokens: 7 } } });
  assert.equal((await handlers["agent/request"]({ agent }, async () => ({}))).maxTokens, 3);
  assert.equal(guard({ agent, name: "read" }), undefined);
  assert.match(guard({ agent, name: "subagent" }), /未启用/);
  assert.match(guard({ agent, name: "read" }), /停止/);
  assert.equal(JSON.parse(readFileSync(process.env.KDS_STOP_FILE)).reason, "tools");
  await handlers["agent/pre-step"]({ agent }, async () => ({ kind: "enter" }));
  assert.equal((await handlers["agent/pre-step"]({ agent }, () => { throw Error("must not call"); })).kind, "reject");
  state = { ...state, run_id: "two", cancel: "manual" };
  save();
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.equal(cancellations.at(-1).reason, "manual");
  state = { ...state, run_id: "three", cancel: null };
  save();
  assert.equal((await handlers["agent/request"]({ agent }, async () => ({}))).maxTokens, 8);
  console.log("dsh bridge: budgets, tool restriction, prompt and cancellation passed");
} finally {
  dispose?.();
  // This test owns the uniquely created temporary directory.
  assert.equal(dirname(resolve(root)), resolve(tmpdir()));
  rmSync(root, { recursive: true, force: true });
}
