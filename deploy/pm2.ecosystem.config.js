// PM2 process definitions for the Sui Dev-Leads Ops Bot.
//
// Run both the always-on auto-tracker and the remote MCP HTTP endpoint under PM2:
//
//   pm2 start deploy/pm2.ecosystem.config.js
//   pm2 save
//
// Paths are derived from this file's location, so there is nothing machine
// specific to edit. Both processes read their config (tokens, sheet id, MCP
// token) from the repo-root .env, which the app loads on startup. No secrets
// live in this file.
//
// Prerequisite: a virtualenv at <repo>/.venv with the package installed
//   python -m venv .venv && .venv/bin/pip install -e .

const path = require("path");
const repo = path.resolve(__dirname, "..");
const python = path.join(repo, ".venv", "bin", "python");

const common = {
  cwd: repo,
  interpreter: "none", // `python` is the executable itself, do not prepend node
  env: { PYTHONUNBUFFERED: "1" }, // flush logs promptly to the PM2 log files
  time: true,
  autorestart: true,
  max_restarts: 20,
};

module.exports = {
  apps: [
    {
      name: "sui-ops-bot",
      script: python,
      args: "-m sui_ops_bot.slackbot",
      ...common,
    },
    {
      name: "sui-ops-mcp-http",
      script: python,
      args: "-m sui_ops_bot.mcpserver --http",
      ...common,
    },
  ],
};
