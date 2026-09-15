// Logbook observation extension. All events retain their native names.
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

export default function (pi: any) {
  const collector = join(dirname(fileURLToPath(import.meta.url)), "record_runtime_event.py");
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  let queue = Promise.resolve();
  const runID = `${process.pid}-${Date.now()}`;
  function send(name: string, ctx: any, extra: Record<string, unknown> = {}) {
    const payload = {
      session_id: ctx.sessionManager.getSessionId(),
      transcript_path: ctx.sessionManager.getSessionFile(),
      hook_event_name: name, run_id: runID,
      timestamp: new Date().toISOString(), ...extra,
    };
    // Serialize delivery so a slow collector cannot reorder end/start events.
    queue = queue.then(() => new Promise<void>((resolve) => {
      const child = spawn("python3", [collector, "--source", "pi"], {
        stdio: ["pipe", "ignore", "ignore"], timeout: 4000,
      });
      child.on("error", () => resolve());
      child.on("close", () => resolve());
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify(payload));
    }));
    return queue;
  }
  pi.on("session_start", async (_event: any, ctx: any) => {
    if (heartbeat) clearInterval(heartbeat);
    heartbeat = setInterval(() => { void send("heartbeat", ctx); }, 30000);
    heartbeat.unref();
    await send("session_start", ctx);
  });
  for (const name of ["before_agent_start", "agent_start", "agent_end", "agent_settled"]) {
    pi.on(name, (_event: any, ctx: any) => send(name, ctx));
  }
  for (const name of ["tool_execution_start", "tool_execution_end"]) {
    pi.on(name, (event: any, ctx: any) => send(name, ctx, {tool_name: event.toolName}));
  }
  pi.on("session_shutdown", (event: any, ctx: any) => {
    if (heartbeat) clearInterval(heartbeat);
    return send("session_shutdown", ctx, {reason: event.reason});
  });
}
