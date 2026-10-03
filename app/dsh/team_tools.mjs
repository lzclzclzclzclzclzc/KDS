// KDS team command tools for the pinned SDK 0.1.5rc1. Registry-ready public
// ToolDefinition schemas avoid importing a package from outside the SDK binary.
// Credentials stay in the control file and never enter model parameters/results.
import { closeSync, existsSync, fsyncSync, openSync, readFileSync, renameSync, unlinkSync, writeSync } from "node:fs";
import { randomUUID } from "node:crypto";

export const name = "kds-team-tools";
export const inject = ["tools"];

const str = { type: "string" };
const obj = { type: "object", additionalProperties: true };
const strings = { type: "array", items: str };
const definitions = [
  ["kds_delegate_task", "给自己的直接子实例持久化派发任务；立即返回 task_id，不等待子任务。", {
    child_instance_id: str, goal: str, input: obj, acceptance: str, budget: obj,
  }, ["child_instance_id", "goal"]],
  ["kds_spawn_subagent", "创建有权限上限的子 Agent 与首个任务。引用角色版本，或提供临时 role；不使用原生 subagent。", {
    role_id: str, role_version: { type: "integer" }, role: obj, task: obj,
    budget: obj, join_room: { oneOf: [{ type: "boolean" }, str] }, name: str, reason: str,
  }, ["task"]],
  ["kds_get_task_results", "一次性读取本任务拥有的子任务结果。查询回执是不可变快照：同次连接重试复用 request_id，后续激活先读正式child_results，更新查询用新ID。queued/running 必须交付 wait_children 结束并释放执行槽；禁止轮询。", {
    task_ids: strings,
  }, ["task_ids"]],
  ["kds_send_group_message", "在本实例所属房间和有效讨论范围内发消息；不完成任务。", {
    room_id: str, content: str, mentions: strings, discussion_id: str,
  }, ["room_id", "content"]],
  ["kds_request_discussion", "请求本房间有限讨论，立即返回 discussion_id。", {
    room_id: str, goal: str, participants: strings, max_rounds: { type: "integer" },
  }, ["room_id", "goal"]],
  ["kds_cancel_task", "请求取消自己拥有的子任务及其子树。", {
    task_id: str, reason: str,
  }, ["task_id", "reason"]],
  ["kds_retry_task", "为自己拥有的失败或取消子任务创建替代任务。", {
    task_id: str, reason: str,
  }, ["task_id", "reason"]],
];

export const TEAM_TOOLS = Object.freeze(definitions.map(([tool]) => tool));

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(
    Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  return value;
}

function endpoint(value) {
  const url = new URL(value);
  if (url.protocol !== "http:" || !["127.0.0.1", "[::1]"].includes(url.hostname)
      || url.username || url.password || url.hash) {
    throw new Error("KDS 编排命令通道必须使用明确的本机 HTTP 地址");
  }
  return url.href;
}

export function apply(ctx) {
  const path = process.env.KDS_CONTROL_FILE;
  if (!path) throw new Error("缺少 KDS 团队控制文件");
  const read = () => JSON.parse(readFileSync(path, "utf8"));
  // Register only explicitly permitted tools. An auxiliary profile with [] has
  // no business tools at all; bridge.mjs restricts all remaining host tools.
  const allowed = new Set(read().tools || []);
  for (const [tool, description, fields, required] of definitions) {
    if (!allowed.has(tool)) continue;
    ctx.tools.register({
      name: tool, description: description + " request_id 必须是稳定的非空操作 ID；恢复时复用已有 ID。",
      parameters: { type: "object", properties: { request_id: str, ...fields },
        required: ["request_id", ...required], additionalProperties: false },
      output: { schema: obj, render: (_args, value) => [{ type: "text", text: JSON.stringify(value) }] },
      async execute(args, execution) {
        const state = read();
        if (state.cancel || !state.tools.includes(tool)) throw new Error("KDS：本次激活已停止或工具未授权");
        if (!args || typeof args.request_id !== "string" || !args.request_id.trim()
            || args.request_id.length > 160) throw new Error("KDS：request_id 必须是 1–160 字符的稳定 ID");
        for (const key of ["conversation_id", "instance_id", "activation_id", "caller", "parent_id"]) {
          if (key in args) throw new Error("KDS：调用身份由命令通道绑定，不能提供调用者字段");
        }
        const url = endpoint(state.command_url);
        if (typeof state.credential !== "string" || !state.credential) throw new Error("缺少 KDS 激活凭证");
        const entry = { task_id: state.task_id, tool, args: canonical(args) };
        // Persist intent before transmission. The service owns durable receipts;
        // this log preserves the request ID even if the connection is interrupted.
        const prior = existsSync(state.request_log) ? readFileSync(state.request_log, "utf8")
          .split("\n").filter(Boolean).map(line => JSON.parse(line)) : [];
        const old = prior.find(value => value.task_id === entry.task_id && value.args.request_id === args.request_id);
        if (old && JSON.stringify(old) !== JSON.stringify(entry)) throw new Error("KDS：相同 request_id 的参数冲突");
        if (!old) {
          const file = openSync(state.request_log, "a", 0o600);
          try { writeSync(file, JSON.stringify(entry) + "\n"); fsyncSync(file); }
          finally { closeSync(file); }
        }
        execution.signal?.throwIfAborted();
        const response = await fetch(url, { method: "POST", redirect: "error",
          headers: { "Content-Type": "application/json", "Authorization": "Bearer " + state.credential },
          body: JSON.stringify({ tool, args }), signal: execution.signal });
        if (!response.ok) {
          // Only the authenticated local command handler can attest a complete
          // transactional rejection. Never infer safety from model error text,
          // 5xx, an interrupted response or a native tool failure.
          let failure;
          try { failure = await response.json(); } catch { /* no complete receipt */ }
          const rejection = failure?.kds_command_error;
          if (response.status >= 400 && response.status < 500 && typeof failure?.error === "string"
              && rejection?.kind === "rejected" && rejection.accepted === false
              && rejection.status === response.status && rejection.tool === tool
              && rejection.request_id === args.request_id && typeof execution.callId === "string"
              && execution.callId && state.rejection_log) {
            const file = openSync(state.rejection_log, "a", 0o600);
            try {
              writeSync(file, JSON.stringify({ ...entry, run_id: state.run_id,
                call_id: execution.callId, status: response.status, rejection }) + "\n");
              fsyncSync(file);
            } finally { closeSync(file); }
          }
          throw new Error("KDS：编排命令被拒绝（HTTP " + response.status + "）");
        }
        const value = await response.json();
        if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("KDS：编排回执格式无效");
        if (tool === "kds_get_task_results" && value.kds_control) {
          const directive = value.kds_control;
          const ids = [...new Set(args.task_ids)];
          const tasks = value.tasks;
          if (directive.action !== "wait_children" || directive.mode !== "all"
              || JSON.stringify(directive.task_ids) !== JSON.stringify(ids)
              || !ids.length || !Array.isArray(tasks) || tasks.length !== ids.length
              || tasks.some((task, index) => task.id !== ids[index] || task.parent_task_id !== state.task_id)
              || !tasks.some(task => !["succeeded", "failed", "cancelled"].includes(task.status))
              || !state.yield_file || !state.run_id) throw new Error("KDS：调度让位控制回执无效");
          const signal = { run_id: state.run_id, task_id: state.task_id, request_id: args.request_id,
            tool, task_ids: ids, mode: "all", receipt: value };
          // The accepted query receipt is already durable on the service. Keep
          // its explicit scheduling signal separate from cancellation/stop data.
          const temporary = state.yield_file + "." + randomUUID() + ".tmp";
          try {
            const file = openSync(temporary, "wx", 0o600);
            try { writeSync(file, JSON.stringify(signal)); fsyncSync(file); }
            finally { closeSync(file); }
            renameSync(temporary, state.yield_file);
          } finally { if (existsSync(temporary)) unlinkSync(temporary); }
        }
        return value;
      },
    });
  }
}
