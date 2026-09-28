# AI Dashboard Builder Grafana app

Local Grafana app plugin for the repository's Ollama + Grafana MCP agent.

It provides:

- a Grafana page named **Create dashboard with AI**;
- a right-hand chat workspace for data questions and dashboard creation;
- a Grafana command-palette action with the same name;
- a dashboard panel-menu link back to the AI builder;
- live polling of host-side agent progress;
- server-side conversation history backed by `.state/ai-chat.sqlite3`;
- recovery and retry of jobs interrupted by an AI bridge restart;
- automatic model routing (`qwen3:4b` for read-only questions, `qwen3:8b` for dashboard writes).

The plugin never talks directly to Ollama or MCP from the browser. Requests go through Grafana's plugin proxy to the local host-side `agent.api` service, which reuses the same grounded agent and MCP permissions as the CLI.

SQLite is the source of truth for chats. Browser localStorage is used only for drafts, the last selected session, and a one-time import of the older `mkhco-ai-dashboard-app.chat.v1` state.

## Grafana 12.1 limitation

Grafana 12.1 does not provide a public extension point inside the native **New dashboard** chooser. The supported integration is the app page plus the command palette. This avoids maintaining a custom Grafana fork.
