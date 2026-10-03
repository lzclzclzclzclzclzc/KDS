import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { apply, TEAM_TOOLS } from "../app/dsh/team_tools.mjs";

const root = mkdtempSync(join(tmpdir(), "kds-team-tools-"));
const controlPath = join(root, "control.json");
process.env.KDS_CONTROL_FILE = controlPath;
const definitions = new Map();
const received = [];
let connectionClosed = false;
const server = createServer((request, response) => {
  let body = "";
  request.on("data", chunk => { body += chunk; });
  request.on("end", () => {
    assert.equal(request.headers.authorization, "Bearer private-activation-key");
    const command = JSON.parse(body);
    received.push(command);
    if (request.url === "/slow") {
      response.on("close", () => { connectionClosed = true; });
      return;
    }
    const status = request.url === "/reject" || request.url === "/untrusted-reject" ? 403 : request.url === "/server-error" ? 500 : 200;
    response.writeHead(status, { "Content-Type": "application/json" });
    const query = { tasks: [{ id: "child-1", parent_task_id: request.url === "/bad-yield" ? "foreign" : "parent-task", status: "queued" }],
      kds_control: { action: "wait_children", task_ids: ["child-1"], mode: "all" } };
    const denied = { error: "request rejected", kds_command_error: { kind: "rejected", accepted: false,
      status, tool: command.tool, request_id: command.args.request_id } };
    response.end(JSON.stringify(request.url === "/untrusted-reject" ? { error: "Error: KDS HTTP 403 rejected" }
      : status >= 400 ? denied : ["/yield", "/bad-yield"].includes(request.url) ? query : { task_id: "child-1", status: "queued" }));
  });
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
let state = { run_id: "activation-1", task_id: "parent-task", tools: [...TEAM_TOOLS], cancel: null,
  yield_file: join(root, "yield.json"),
  rejection_log: join(root, "rejections.jsonl"),
  command_url: `http://127.0.0.1:${server.address().port}/command`,
  credential: "private-activation-key", request_log: join(root, "requests.jsonl") };
const save = () => writeFileSync(controlPath, JSON.stringify(state));
save();
try {
  apply({ tools: { register(definition) {
    assert(!definitions.has(definition.name));
    definitions.set(definition.name, definition);
  } } });
  assert.deepEqual([...definitions.keys()], TEAM_TOOLS);
  assert(!JSON.stringify([...definitions.values()]).includes("private-activation-key"));
  const tool = definitions.get("kds_delegate_task");
  const args = { request_id: "stable-1", child_instance_id: "child", goal: "verify" };
  const execution = { callId: "actual-dsh-call", signal: new AbortController().signal };
  const result = await tool.execute(args, execution);
  assert.equal(result.task_id, "child-1");
  assert.deepEqual(received[0], { tool: "kds_delegate_task", args });
  assert.equal(tool.output.render(args, result)[0].text, JSON.stringify(result));
  await tool.execute(args, execution);
  assert.equal(readFileSync(state.request_log, "utf8").trim().split("\n").length, 1);
  await assert.rejects(() => tool.execute({ ...args, goal: "different" }, execution), /参数冲突/);
  assert.equal(received.length, 2);
  await assert.rejects(() => tool.execute({ ...args, instance_id: "spoof" }, execution), /身份/);
  await assert.rejects(() => tool.execute({ ...args, request_id: "" }, execution), /request_id/);
  state = { ...state, command_url: "http://example.com/command" };
  save();
  await assert.rejects(() => tool.execute({ ...args, request_id: "nonlocal" }, execution), /本机/);
  state = { ...state, command_url: `http://127.0.0.1:${server.address().port}/reject` };
  save();
  await assert.rejects(() => tool.execute({ ...args, request_id: "denied" }, execution), /HTTP 403/);
  const proof = JSON.parse(readFileSync(state.rejection_log, "utf8").trim());
  assert.equal(proof.call_id, execution.callId);
  assert.equal(proof.run_id, state.run_id);
  assert.equal(proof.args.request_id, "denied");
  assert.equal(proof.rejection.accepted, false);
  assert(!JSON.stringify(proof).includes(state.credential));
  for (const path of ["untrusted-reject", "server-error"]) {
    state = { ...state, command_url: `http://127.0.0.1:${server.address().port}/${path}` };
    save();
    await assert.rejects(() => tool.execute({ ...args, request_id: path }, execution), /HTTP/);
    assert.equal(readFileSync(state.rejection_log, "utf8").trim().split("\n").length, 1,
      "模型错误文本或5xx不能生成确定拒绝证据");
  }
  state = { ...state, command_url: `http://127.0.0.1:${server.address().port}/slow` };
  save();
  const controller = new AbortController();
  const pending = tool.execute({ ...args, request_id: "cancelled" }, { signal: controller.signal });
  setTimeout(() => controller.abort(), 100);
  await assert.rejects(() => pending, error => error.name === "AbortError");
  await new Promise(resolve => setTimeout(resolve, 100));
  assert(connectionClosed, "取消后底层本机连接必须关闭");
  state = { ...state, command_url: `http://127.0.0.1:${server.address().port}/yield` };
  save();
  const query = definitions.get("kds_get_task_results");
  const queryArgs = { request_id: "yield-once", task_ids: ["child-1", "child-1"] };
  const queryResult = await query.execute(queryArgs, execution);
  const signal = JSON.parse(readFileSync(state.yield_file, "utf8"));
  assert.equal(signal.run_id, state.run_id);
  assert.equal(signal.request_id, queryArgs.request_id);
  assert.deepEqual(signal.task_ids, ["child-1"]);
  assert.deepEqual(signal.receipt, queryResult);
  assert(!JSON.stringify(signal).includes(state.credential));
  writeFileSync(state.yield_file, "{}");
  state = { ...state, command_url: `http://127.0.0.1:${server.address().port}/bad-yield` };
  save();
  await assert.rejects(() => query.execute({ ...queryArgs, request_id: "reject-foreign-yield" }, execution), /控制回执无效/);
  assert.equal(readFileSync(state.yield_file, "utf8"), "{}");
  state = { ...state, tools: [] };
  save();
  const empty = [];
  apply({ tools: { register(definition) { empty.push(definition); } } });
  assert.equal(empty.length, 0);
  console.log("team tools: identity, stable request journal, loopback, denial and cancellation passed");
} finally {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  assert.equal(dirname(resolve(root)), resolve(tmpdir()));
  rmSync(root, { recursive: true, force: true });
}
