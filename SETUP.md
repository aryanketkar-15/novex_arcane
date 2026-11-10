# ARCANE — Contributor Setup Guide

ARCANE watches a GitHub repo, and when CI fails it finds the breaking commit, writes a fix, tests it in a Docker sandbox and opens a pull request. This guide gets the whole stack running on a fresh machine (Windows, macOS or Linux).

## 0. What you are running

| Part | Tech | Port | Folder |
|---|---|---|---|
| Backend API + agents | Python 3.10+ / FastAPI / LangGraph | 8000 | `api/`, `agents/` |
| Dashboard | React + Vite (Node 20+) | 5173 | `new_frontend/` |
| Sandbox | Docker image `arcane-sandbox` | – | `Dockerfile` |
| Patch memory | ChromaDB (local files) | – | `chroma_data/` |

The dashboard talks to the backend through a Vite proxy (`/api` → `localhost:8000`), so there is no CORS setup to do.

## 1. Install the prerequisites

| Tool | Version | Check with |
|---|---|---|
| Python | 3.10 – 3.12 | `python --version` |
| Node.js | 20 or newer (22 recommended) | `node --version` |
| Docker Desktop | any recent | `docker --version` |
| Git | any | `git --version` |

Ollama is optional (local LLM fallback): https://ollama.com

## 2. Docker (the sandbox)

ARCANE tests every patch inside a throwaway container, so Docker must be **running** whenever you run a repair.

1. Install Docker Desktop and start it. Wait until it says "Engine running".
2. Verify: `docker run --rm hello-world`
3. Build the sandbox image **once**, from the project root:
   ```bash
   docker build -t arcane-sandbox .
   ```
   The image name must be exactly `arcane-sandbox`; the agents look it up by name.
4. Verify: `docker images arcane-sandbox`

Troubleshooting
- `Cannot connect to the Docker daemon` → Docker Desktop is not running.
- `--storage-opt is supported only for overlay over xfs…` (Linux) → remove `--storage-opt size=2G` in `agents/git_bisect_agent.py`, or use Docker Desktop.
- Stale containers after a crash: `docker ps -a --filter "name=arcane-"` then `docker rm -f <names>` (or run `scripts/demo_reset.sh`).

## 3. Backend

From the project root:

```bash
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# macOS / Linux:       source .venv/bin/activate
pip install -r requirements.txt
```

The first install downloads PyTorch via `sentence-transformers` (a few hundred MB) — be patient.

### 3a. Configure `.env`

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Open `.env` and fill in:

| Variable | How to get it |
|---|---|
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | https://aistudio.google.com/apikey (same key in both) |
| `GITHUB_WEBHOOK_SECRET` | Any random string you make up |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | Your own GitHub OAuth App, see 3b |
| `CHROMADB_PATH` | Leave as `./chroma_data` |
| `ARCANE_DRY_RUN` | `1` while learning (simulated PRs), `0` for real PRs |

**Never commit or zip your real `.env`.** Each contributor creates their own.

### 3b. Create a GitHub OAuth App (for "Sign in with GitHub")

GitHub → Settings → Developer settings → OAuth Apps → **New OAuth App**

- Homepage URL: `http://localhost:5173`
- Authorization callback URL: `http://localhost:5173/api/auth/github/callback`
- Leave device flow off. If the form shows "Expire user access tokens", untick it.

Click **Generate a new client secret** and copy both values into `.env`. If you leave `GITHUB_CLIENT_ID`/`SECRET` empty, the dashboard runs in single-user "local" mode with no login, and uses `GITHUB_PAT` instead.

### 3c. Seed the patch memory (optional but recommended for demos)

```bash
python scripts/pre_populate_memory.py
```

The first run downloads a ~90 MB embedding model from Hugging Face, so you need internet once.

### 3d. Start it

```bash
python -m uvicorn api.main:app --port 8000
```

Check http://localhost:8000/health → `{"status":"ok"}`, and http://localhost:8000/docs for the API.

On Windows, set `PYTHONUTF8=1` first (`$env:PYTHONUTF8=1` in PowerShell) or the log output can crash on emoji.

## 4. Frontend (dashboard)

In a second terminal:

```bash
cd new_frontend
npm install
PORT=5173 npm run dev          # PowerShell:  $env:PORT=5173; npm run dev
```

Open http://localhost:5173. Without `PORT=5173` it uses 8443 and the OAuth callback will not match.

## 5. One-command start (Windows)

After steps 2–4 are done once:

```powershell
scripts\start_dev.ps1
```

It opens the backend and the dashboard in two windows.

## 6. Try it

1. Open http://localhost:5173 and **Sign in with GitHub**.
2. **Settings → Repositories → Watch** one of *your own throwaway repos* (needs a Python repo with a pytest suite).
3. **Home → Run repair on a build**, or push a commit that breaks a test.
4. Watch **Repair runs**: the timeline fills in, then the diff and the confidence score appear, then **Open pull request**.

### Receiving real GitHub pushes (webhook)

GitHub cannot reach `localhost`, so expose port 8000 with a tunnel:

```bash
ssh -p 443 -R0:127.0.0.1:8000 qr@a.pinggy.io
```

Put the URL it prints in `PUBLIC_URL` in `.env`, restart the backend, then press **Watch** again — ARCANE creates the push webhook (`<url>/webhook`, JSON, your `GITHUB_WEBHOOK_SECRET`) for you. Doing it by hand: repo → Settings → Webhooks → Add webhook with the same values and only the **push** event. Pushes to branches other than `main`/`master` and to `arcane/*` are ignored on purpose.

## 7. Safety switches

- `ARCANE_DRY_RUN=1` → no GitHub PRs are created; a fake URL is shown instead.
- Real mode commits to a new branch `arcane/fix-<sha>` and opens a PR; it never pushes to your main branch.
- The OAuth scope is `repo admin:repo_hook`. Sign out from the sidebar; tokens are stored in `data/sessions.json` (local only — protect that file).

## 8. Tests

```bash
pytest tests/ -v
cd new_frontend && npx tsc --noEmit
```

Some tests need Docker and the `arcane-sandbox` image.

## 9. Common problems

| Symptom | Fix |
|---|---|
| Dashboard says "Backend offline" | Backend not running on port 8000 |
| Sign-in bounces back with `auth_error` | Callback URL in the OAuth App must be exactly `http://localhost:5173/api/auth/github/callback`, and the dashboard must be on port 5173 |
| Patch is empty / "ARCANE did not produce a patch" | The failing file could not be read — sign in so ARCANE has a GitHub token with access to the repo |
| `404 NOT_FOUND … model is no longer available` | Google retired the model; change `GEMINI_MODEL`/`GEMMA_MODEL` in `.env` |
| First repair takes minutes | Hugging Face model download; after it is cached everything is fast |
| `ModuleNotFoundError` | Activate the venv and re-run `pip install -r requirements.txt` |
| Stuck "working" run after restart | Runs interrupted by a restart are marked failed automatically; start a new one |

## 10. Packaging the project as a zip

Do **not** include `.env` (it holds your real keys), `node_modules/`, `.venv/`, `data/`, `chroma_data/`, `memory/` or `__pycache__`. In PowerShell, from the folder *above* the project:

```powershell
robocopy ARCANE_Nexus2.0-main arcane-share /E /XD node_modules .venv data chroma_data memory __pycache__ .git /XF .env
Compress-Archive -Path arcane-share\* -DestinationPath arcane-share.zip
```

(`robocopy` exit codes 0–7 mean success.) Contributors unzip it and follow this guide from step 1.

## Model keys and fallback

Sign in with GitHub and open **Settings → Models**. Add a provider, its exact
model ID, and an API key. Add more entries for backup keys or different
models. Each new repair tries the owner's entries in insertion order, then
server credentials. Model access and quota depend on the provider account.

For server-wide fallback, set the following on the backend:

- Google uses `GEMINI_API_KEY`, `GOOGLE_API_KEY`, and/or comma-separated
  `GEMINI_API_KEYS`. Set `GEMINI_MODEL` and `GEMMA_MODEL` to available model IDs.
- OpenAI uses `OPENAI_MODEL` and `OPENAI_API_KEY` or comma-separated `OPENAI_API_KEYS`.
- Groq uses `GROQ_MODEL` and `GROQ_API_KEY` or comma-separated `GROQ_API_KEYS`.

Personal credentials use the backend's session encryption key. Keep
`SECRET_KEY` stable and configure persistent database storage to retain keys
across Render instance replacements. Hosted repairs report a model error
when all available providers fail.
