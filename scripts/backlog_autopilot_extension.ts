import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

export default function (pi: ExtensionAPI) {
  pi.registerCommand("autopilot", {
    description: "Show Sentry issue worker status, pause, or resume daily work",
    handler: async (args, ctx) => {
      const action = args.trim() || "status";
      if (!["status", "pause", "resume"].includes(action)) {
        ctx.ui.notify("Use /autopilot status, /autopilot pause, or /autopilot resume", "warning");
        return;
      }
      try {
        const path = join(homedir(), ".config/backlog-autopilot/config.json");
        const config = JSON.parse(await readFile(path, "utf8"));
        const result = await pi.exec(config.python || "python3", [
          join(config.state_dir, "current/backlog_autopilot.py"), "--config", path, action,
        ], { timeout: 10000 });
        ctx.ui.notify(result.stdout || result.stderr, result.code === 0 ? "info" : "error");
      } catch (error) {
        ctx.ui.notify(`Autopilot: ${String(error)}`, "error");
      }
    },
  });
}
