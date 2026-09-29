// KDS-owned SDK overlay. Uses the public Cordis services in dsh 0.1.5/0.1.7.
// No additional npm dependency: Node builtins and injected host services only.
import { readFileSync, writeFileSync } from "node:fs";

export const name = "kds-discussion";
export const inject = ["systemPrompt", "tools"];

export function apply(ctx) {
  const controlPath = process.env.KDS_CONTROL_FILE;
  const stopPath = process.env.KDS_STOP_FILE;
  if (!controlPath || !stopPath) throw new Error("缺少 KDS 回合控制文件");
  let current;
  let steps = 0;
  let calls = 0;
  let output = 0;
  const agents = new Set();

  function control() {
    const next = JSON.parse(readFileSync(controlPath, "utf8"));
    if (!current || next.run_id !== current.run_id) {
      steps = 0;
      calls = 0;
      output = 0;
    }
    current = next;
    return current;
  }

  function stop(reason, agent) {
    writeFileSync(stopPath, JSON.stringify({ run_id: current.run_id, reason }));
    agent.cancel({ kind: "hook", reason });
  }

  // Variable values are inserted literally (one expansion pass). This also
  // supports 0.1.5, before sections gained the interpolate:false option.
  ctx.systemPrompt.variable("kds_instructions", () => control().system);
  ctx.systemPrompt.section({
    name: "kds:discussion",
    order: 10300,
    text: "{{kds_instructions}}",
  });

  ctx.on("agent/created", ({ agent }) => {
    const allowed = new Set(control().tools);
    const visible = ctx.tools.schemas(agent).map((tool) => tool.name);
    const deny = visible.filter((tool) => !allowed.has(tool));
    if (deny.length) agent.ctx.tools.restrict({ deny });
    agents.add(agent);
  });
  ctx.on("agent/disposed", ({ agent }) => { agents.delete(agent); });

  ctx.on("agent/pre-step", async ({ agent }, next) => {
    const state = control();
    const reason = state.cancel || (steps >= state.max_steps ? "steps" : null);
    if (reason) {
      stop(reason, agent);
      return { kind: "reject" };
    }
    const decision = await next();
    if (decision.kind === "enter") steps += 1;
    return decision;
  });

  ctx.on("agent/request", async ({ agent }, next) => {
    const state = control();
    const remaining = state.output_budget - output;
    if (remaining <= 0) {
      stop(state.output_budget_reason, agent);
      throw new Error("KDS：本轮输出预算已用完");
    }
    const request = await next();
    return { ...request, maxTokens: Math.min(request.maxTokens ?? state.request_max_tokens,
      state.request_max_tokens, remaining) };
  });

  // A guard cannot force-allow a call denied by the host's sandbox/approval policy.
  ctx.tools.guard((execution) => {
    const state = control();
    if (!state.tools.includes(execution.name)) return "KDS：此角色未启用该工具";
    const reason = state.cancel || (calls >= state.max_tool_calls ? "tools" : null);
    if (reason) {
      if (execution.agent) stop(reason, execution.agent);
      return "KDS：本轮工具执行已停止";
    }
    calls += 1;
    return undefined;
  });

  ctx.on("session/event", (_session, event) => {
    if (["assistant/message", "compaction/summary"].includes(event.type)) {
      output += event.data.usage?.outputTokens ?? 0;
    }
    if (event.type === "assistant/attempt") {
      const usage = [...(event.data.stream ?? [])].reverse()
        .map((record) => record.chunk ?? record).find((chunk) => chunk.type === "usage");
      output += usage?.usage?.outputTokens ?? 0;
    }
  });

  // The SDK wire has no cancel method. This bridge cancels the actual agent,
  // allowing tool cleanup and usage settlement before Python's shutdown fallback.
  ctx.effect(() => {
    const timer = setInterval(() => {
      try {
        const state = control();
        if (state.cancel) for (const agent of agents) {
          if (agent.status === "running") stop(state.cancel, agent);
        }
      } catch {
        for (const agent of agents) if (agent.status === "running") {
          agent.cancel({ kind: "hook", reason: "KDS 控制文件不可用" });
        }
      }
    }, 100);
    return () => clearInterval(timer);
  });
}
