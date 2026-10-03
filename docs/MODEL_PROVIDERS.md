# Ollama and external model APIs

The CLI and Grafana plugin use the same agent with two transports:

- `ollama`: local native `/api/chat`, with the existing local model routing.
- `openai`: OpenAI-compatible `/chat/completions` with bearer API-key authentication.

Both retain MCP tool allowlists, live metric discovery, PromQL checks, dashboard write
gating and post-write verification. The external model does not need inbound access
to Grafana: the containerized Python bridge executes tools and sends their results back.
`update_dashboard` is present in the model's tool schema from the first inference
turn. The bridge still rejects the write until the referenced metrics are discovered
and every panel query has returned live data. Persian dashboard verbs such as
`بساز`, `ایجاد`, `ذخیره`, `ویرایش` and `اصلاح` are recognized as write intent.

## Run without Ollama

Use WSL/Linux for `start.sh`. Install Docker Compose, Node >=22, npm and curl.
Python runs inside the AI bridge container; install Python >=3.10 and uv on the
host only if you also want the optional CLI. Ollama and model downloads are
**not required** in external mode.

Keep the two Grafana/MCP tokens configured as described in the main README.
Set this in `.env` (copy `.env.example` for a fresh installation):

```dotenv
LLM_PROVIDER=openai
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=
OPENAI_MODEL=
OPENAI_TIMEOUT_SECONDS=180
```

Then run:

```bash
bash start.sh
```

Open <http://localhost:3000/a/mkhco-ai-dashboard-app/>. In **AI connection**:

1. Choose **OpenAI / OpenAI-compatible API**.
2. Enter the base URL including the provider's API prefix, usually `/v1`.
   Do not append `/chat/completions`; the bridge appends it.
3. Enter the provider's API key.
4. Enter the exact model ID available in your account. It must support Chat
   Completions and structured function/tool calling.
5. Choose **Save connection**. Only a Grafana organization administrator can
   change this shared connection.
6. Send a question or dashboard request.

The base URL defaults to OpenAI. For another compatible service, enter its own
HTTPS base URL, key and model ID. This connects to the OpenAI API, not the ChatGPT
website or a browser login. Responses-only models/endpoints, Azure-specific API-key
headers and proprietary protocols are not implemented by this transport.

For persistent server defaults and CLI use, fill `OPENAI_API_KEY` and
`OPENAI_MODEL` in `.env`. Restart via `bash start.sh` after changes. Compose recreates
the bridge with the new environment; active jobs become interrupted and can be retried.
Environment variables take precedence over `.env`.

```bash
uv run python -m agent.main "Inspect the metrics and build a service health dashboard"
```

For a fresh Grafana data volume, create the service-account token first: build the
plugin (`cd grafana-ai-plugin && npm ci && npm run build`), return to the project
root, and run `docker compose up -d --build grafana` using the nonempty MCP token
placeholders in `.env`. Create an Editor service account/token in Grafana, replace
the placeholders with the real Grafana token and a random MCP caller token, then
run `bash start.sh`.

## Local mode and switching

```dotenv
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen3:8b
AI_FAST_MODEL=qwen3:4b
AI_DASHBOARD_MODEL=qwen3:8b
```

Local startup checks the installed Ollama service and dashboard model. If the fast
model is absent it uses the dashboard model. In the UI, an empty local model field
keeps automatic routing; a supplied model ID overrides it. In external mode,
`OPENAI_MODEL` (or the UI model) is used for both questions and dashboards, ignoring
the local `AI_FAST_MODEL`/`AI_DASHBOARD_MODEL` defaults.

The UI can switch providers for subsequent messages. Selecting Ollama requires an
already-running Ollama service; switching the UI does not install or start it.
The saved Grafana configuration is organization-scoped and is used by authorized
Editor/Admin users, including the global chat drawer. It does not change CLI
settings. Each running job receives an immutable settings snapshot, so changing
the saved connection does not reroute it.

Editor/Admin users see an **AI chat** control on Grafana pages outside the app and
dashboard routes. It opens a right-side drawer backed by the same persistent chat
sessions. The drawer is hidden on the dedicated AI Dashboard Builder page, where
the full chat workspace is already visible, and on dashboard pages as requested.

Grafana checks the signed-in user's role before allowing access to the plugin proxy.
MCP operations then use the local managed Grafana service account, currently created
with the Editor organization role. This role can create, update and read dashboards;
the browser password/session is never forwarded to MCP. The agent exposes only its
documented MCP allowlist, even when the signed-in user is a Grafana administrator.

## Keys, history and retries

- Choosing **Save connection** writes provider, base URL and model to Grafana
  `jsonData`. Grafana encrypts the API key in `secureJsonData`; the key is never
  returned to browser code after saving. Grafana's server-side plugin proxy
  decrypts it and forwards it only to the local bridge for an authenticated request.
- A key typed but not saved remains only in page memory. API keys are never placed
  in localStorage, SQLite, job metadata or progress logs.
- A blank key uses the server's `OPENAI_API_KEY` only when the selected base URL
  exactly matches the configured `OPENAI_BASE_URL` (ignoring trailing slash).
  Changing the URL clears the UI key. Redirects are refused to avoid forwarding
  credentials to another destination.
- Jobs persist provider, base URL and model, without keys. Old databases migrate
  automatically without deleting conversations. **Retry** uses the current UI
  connection; the API also supports retrying with saved non-secret job settings.
- A request already running continues if you leave the page. After bridge restart,
  interrupted requests can use Retry with the saved Grafana connection.
- External mode sends prompts, recent conversation context and queried monitoring
  data to the chosen provider. The bridge port is bound only to host loopback and is
  not an independently authenticated public service.
- `.env` is git-ignored. Keep API keys out of screenshots and diagnostic reports.
  `start.sh` and `start.ps1` preserve existing Grafana plugin settings on restart.

## Compatibility and diagnostics

The transport follows [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling):
assistant function calls retain their IDs and JSON arguments, and tool results
return using the matching `tool_call_id`, including multiple calls per turn.
Ollama-specific `think`, `keep_alive` and `options` are not sent to remote APIs.
Temperature and token-limit parameters are omitted for cross-model compatibility;
provider defaults apply. Agent turn limits and request timeout remain enforced.

HTTP 401/403 means check credentials/permissions; 404 means check URL/model;
400 may indicate unsupported tools or model options; 429 indicates rate limit or
quota. Remote error bodies are not stored because they may contain sensitive data.
There is no automatic retry of remote HTTP failures; use the visible Retry action.

`start.sh` health checks verify the local stack, not paid inference. To verify your
actual API credentials, send a data question in the plugin, then a dashboard request.

```bash
uv run python -m unittest discover -s tests -v
bash scripts/test_ai_plugin.sh
```

Unit tests mock remote responses and exercise tool-call round trips, key isolation,
errors, model routing, and job persistence. The smoke test uses the configured
provider and may incur provider usage charges. A real external dashboard run needs
your API credentials and the running Grafana/MCP stack.
