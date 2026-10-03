# AI Dashboard Builder Grafana app

Grafana app plugin for the repository's Ollama / OpenAI-compatible + Grafana MCP agent.

It provides:

- a Grafana page named **Create dashboard with AI**;
- a right-hand chat workspace for data questions and dashboard creation;
- a global **AI chat** drawer for Editor/Admin users on non-dashboard Grafana pages;
- RTL Persian chat layout with automatic per-message direction, sanitized Markdown,
  readable tables, and isolated left-to-right code/PromQL blocks;
- a Grafana command-palette action with the same name;
- a dashboard panel-menu link back to the AI builder;
- live polling of host-side agent progress;
- server-side conversation history backed by `.state/ai-chat.sqlite3`;
- recovery and retry of jobs interrupted by an AI bridge restart;
- an admin-only **AI connection** form that persists provider, external API base URL,
  encrypted API key and exact model ID in Grafana;
- automatic local model routing (`qwen3:4b` for read-only questions, `qwen3:8b` for dashboard writes), or an explicitly selected local/external model.

See [provider setup](../docs/MODEL_PROVIDERS.md). External startup (`LLM_PROVIDER=openai`)
does not require Ollama. Saved keys use Grafana `secureJsonData`, are encrypted at
rest, and are injected into local bridge requests by Grafana's server-side proxy.
They are not returned to the browser. `.env` remains a fallback for CLI/server defaults.
Jobs retain provider/model/endpoint metadata without keys.

The plugin never talks directly to Ollama or MCP from the browser. Requests go through Grafana's plugin proxy to the local host-side `agent.api` service, which reuses the same grounded agent and MCP permissions as the CLI.

Dashboard writes are advertised to tool-capable models from the first turn, including
for Persian requests. The local bridge blocks the actual write until metric discovery
and live PromQL validation succeed, then requires a read-back and data verification.
Grafana authorizes access to the proxy; MCP calls use the project's managed Editor
service account, whose dashboard permissions are sufficient for create/update flows.

SQLite is the source of truth for chats. Browser localStorage is used only for drafts, the last selected session, and a one-time import of the older `mkhco-ai-dashboard-app.chat.v1` state.

## Grafana 12.1 integration

Grafana 12.1 does not expose a public component extension point for a global chat
sidebar. Because this app is preloaded, it mounts one document-level drawer and
tracks Grafana SPA navigation. The drawer is hidden on dashboard routes and on the
full AI app page. This avoids maintaining a custom Grafana fork.
