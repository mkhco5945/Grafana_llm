# Windows setup without WSL

This project can run from PowerShell without installing an Ubuntu distribution.
Docker runs the Linux Grafana, Prometheus, demo, MCP and AI bridge containers.
Docker Desktop's **Docker VMM** supplies their small Linux VM. The `ai-bridge`
service restarts with the stack and does not depend on a hidden Windows process.

Requirements:

- Windows 10/11 with hardware virtualization enabled and at least 8 GB RAM.
- Docker Desktop 4.86 or later, using Docker VMM (Beta) with at least 4 GB RAM.
- Node.js 22 or later and npm. Python is included in the AI bridge image.

Install Docker Desktop in per-user mode. Start it, open **Settings > General >
Virtual Machine Manager**, select **Docker VMM**, and choose **Apply & restart**.
Do not select the WSL 2 engine. Docker VMM is separate from an Ubuntu/WSL distro.

Docker VMM needs the Windows **Windows Hypervisor Platform** feature. If Docker
reports that virtualization support is unavailable even though virtualization is
enabled in BIOS/UEFI, run this once in an elevated PowerShell and restart Windows:

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All
```

This enables the Windows hypervisor support used by Docker VMM; it does not
install WSL or an Ubuntu distribution.

Docker VMM also permits bind mounts only from explicitly shared folders. Open
**Docker Desktop > Settings > Resources > File Sharing**, add
`D:\codes\Grafana_llm`, then choose **Apply & restart**. If the repository is in
another location, add that location instead.

From PowerShell:

```powershell
cd D:\codes\Grafana_llm
docker info
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

For a fresh checkout, `start.ps1` creates `.env` from `.env.example`. It starts
Grafana first, creates a local Editor service account using the development
`admin`/`admin` login, generates the MCP caller token, and stores both secrets in
the git-ignored `.env`. It never prints them. Existing configured tokens are kept.

Set `LLM_PROVIDER=openai` in `.env` to avoid Ollama. You may leave `OPENAI_API_KEY`
and `OPENAI_MODEL` empty and enter them in the plugin's **AI connection** form, or
store server defaults in `.env`. See [MODEL_PROVIDERS.md](MODEL_PROVIDERS.md).

Open <http://localhost:3000/a/mkhco-ai-dashboard-app/>. Stop everything without
deleting data:

```powershell
powershell -ExecutionPolicy Bypass -File .\stop.ps1
```

Useful checks:

```powershell
docker compose ps
docker compose logs --tail=100 grafana mcp-grafana ai-bridge
Invoke-RestMethod http://127.0.0.1:8010/health
```

Normal data persists in Docker named volumes and `.state\ai-chat.sqlite3`.
Uninstalling Docker Desktop does not remove this repository, but Docker's own
uninstall/data-reset options can remove its images, containers and named volumes.
