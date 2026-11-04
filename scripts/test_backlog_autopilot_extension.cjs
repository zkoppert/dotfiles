const assert = require("node:assert/strict");
const { mkdtemp, mkdir, writeFile, rm } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const { pathToFileURL } = require("node:url");
const { test } = require("node:test");

test("autopilot commands use fixed arguments and show worker results", async () => {
  const home = await mkdtemp(join(tmpdir(), "autopilot-extension-"));
  const originalHome = process.env.HOME;
  process.env.HOME = home;
  try {
    const directory = join(home, ".config/backlog-autopilot");
    await mkdir(directory, { recursive: true });
    const configPath = join(directory, "config.json");
    await writeFile(configPath, JSON.stringify({ state_dir: "/private/worker", python: "/usr/bin/python3" }));
    const extension = await import(pathToFileURL(join(__dirname, "backlog_autopilot_extension.ts")));
    let command;
    const calls = [];
    const messages = [];
    extension.default({
      registerCommand(name, definition) {
        assert.equal(name, "autopilot");
        command = definition;
      },
      async exec(executable, args) {
        calls.push({ executable, args });
        return { code: 0, stdout: "Saved worker status", stderr: "" };
      },
    });
    const context = { ui: { notify: (...args) => messages.push(args) } };
    await command.handler("", context);
    await command.handler("pause", context);
    await command.handler("resume", context);
    assert.deepEqual(calls.map(call => call.args.at(-1)), ["status", "pause", "resume"]);
    assert.deepEqual(calls[0], {
      executable: "/usr/bin/python3",
      args: ["/private/worker/current/backlog_autopilot.py", "--config", configPath, "status"],
    });
    assert.deepEqual(messages[0], ["Saved worker status", "info"]);
    await command.handler("status; touch /tmp/unwanted", context);
    assert.equal(calls.length, 3);
    assert.equal(messages.at(-1)[1], "warning");
    await rm(configPath);
    await command.handler("status", context);
    assert.equal(messages.at(-1)[1], "error");
  } finally {
    if (originalHome === undefined) delete process.env.HOME;
    else process.env.HOME = originalHome;
    await rm(home, { recursive: true, force: true });
  }
});
