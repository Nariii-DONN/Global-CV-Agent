# Global CV Agent — Zero-Cost Edition

A recruiter-facing candidate discovery engine focused on going as far as possible with **zero paid services**:

JD PDF -> local JD extraction -> large query expansion -> free/public web search -> robots-aware document fetching -> PDF/DOCX/HTML parsing -> deduplication -> local candidate database -> deterministic GOOD/BETTER/BEST ranking.

## Zero-rupee mode

The app does **not require any API key** to start. It uses a free public-web search path and a local deterministic parser/ranker.

For stronger AI extraction/ranking without paying for an API, you can optionally install **Ollama locally** and set `OLLAMA_MODEL` in `.env`. No cloud API bill is required, though a local model needs disk/RAM.

Paid search/AI APIs remain optional enhancements. They are never required by the core path.

## What it can and cannot mine

The free mode searches publicly indexed web results and retrieves documents/pages that are publicly accessible and permitted to be fetched. It checks `robots.txt` before fetching.

It does **not** bypass logins, CAPTCHAs, paywalls, anti-bot controls, private candidate databases, or site restrictions. For Indeed, Monster, ZipRecruiter, Glassdoor and LinkedIn, the app can discover publicly indexed material, but private/recruiter-only candidate databases should only be connected through the platform's official/licensed integration or an employer account workflow that permits it.

No system can honestly guarantee every CV on the internet. Search-engine coverage, indexing, source restrictions and public availability limit what can be discovered.

## Run on Windows

```powershell
cd global-cv-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python server.py
```

Open http://127.0.0.1:8787

## Optional local AI, still zero-cost

1. Install Ollama locally.
2. Download any instruct/chat model supported by your machine.
3. Set, for example:

```text
OLLAMA_MODEL=<your-local-model-name>
```

The application automatically tries local Ollama when no cloud OpenAI key is available.

## Search strategy

The JD is converted into multiple resume-oriented queries, including combinations of:

- exact job title
- must-have skills
- preferred skills
- experience
- location
- `resume`, `CV`, `curriculum vitae`
- `filetype:pdf`
- `filetype:docx`
- public-web/source-oriented variants for LinkedIn, Indeed, Monster, ZipRecruiter and Glassdoor

The result set is deduplicated before documents are fetched.

## Data model

- `jobs`
- `candidates`
- `job_candidates`
- `search_runs`

Candidate records retain source URL, source type, extracted profile data, original document path (when fetched), match score and match evidence.

## Host free on Render ($0, permanent phone link)

`render.yaml` defines a free Python web service. You get a permanent `https://global-cv-agent.onrender.com` link for your phone.

1. Push this repo to GitHub.
2. Go to **dashboard.render.com → New → Blueprint**, select the repo, confirm.
3. Fill the prompted secrets (`ADMIN_NAME`, `ADMIN_EMAIL`, `ADMIN_PHONE`, `ADMIN_PASSWORD_SHA256`, `ADMIN_SECRET_SHA256`; `SESSION_SECRET` auto-generates). Hash values with `python -c "import hashlib; print(hashlib.sha256('VALUE'.encode()).hexdigest())"`.
4. Deploy, open the URL, sign in.

Notes:

- Free instances sleep after ~15 min idle; first load takes ~30–60s to wake. Keep the dashboard open during long searches so the run isn't interrupted.
- Disks are ephemeral on free: redeploys/restarts wipe SQLite + uploads. Use Export CSV for backups.

## Host free via Cloudflare Tunnel ($0, no paid plan)

Your PC runs the app; Cloudflare exposes it on a public `https://` URL. No Docker needed.

1. Install once: `winget install --id Cloudflare.cloudflared`
2. Double-click `tunnel.bat` (or run it). It starts the server and prints your public URL.
3. Open the URL and sign in with your admin account. Keep the window open while using it.

Notes:

- The quick-tunnel URL is random and changes on restart. For a stable address, add your domain to Cloudflare and create a named tunnel (`cloudflared tunnel login`, `create`, `route dns`), then point it at `http://127.0.0.1:8787`.
- Your PC must stay on and connected while the tunnel runs.
- Pause the failing Workers Builds while on the free plan: dashboard → **Workers & Pages → global-cv-agent → Settings → Builds → pause/disable**, or every push shows a red build.
- Optional extra lock: Cloudflare **Zero Trust Access** (free up to 50 users) can require an additional login before anyone reaches the tunnel URL.

## Host on Cloudflare (Containers, requires Workers Paid)

The app runs as a container behind a tiny Worker (`worker/index.js`, routed as a single stateful instance). `wrangler deploy` builds the image, pushes it, and deploys both.

Prerequisites (one time):

1. Install [Docker Desktop](https://docs.docker.com/get-started/get-docker/) and make sure it is running (`docker info` must succeed).
2. Install JS deps: `npm install`
3. Log in: `npx wrangler login`
4. Set the admin secrets (values stay in Cloudflare, never in git):
   `npx wrangler secret put ADMIN_NAME`, `ADMIN_EMAIL`, `ADMIN_PHONE`, `ADMIN_PASSWORD_SHA256`, `ADMIN_SECRET_SHA256`, `SESSION_SECRET`
   (For the two `*_SHA256` values, hash with `python -c "import hashlib; print(hashlib.sha256('VALUE'.encode()).hexdigest())"`.)

Build + deploy command:

```powershell
npm run deploy
```

Validate without deploying: `npm run build`. Watch logs: `npm run tail`.

## Auto-deploy on git push

`.github/workflows/deploy.yml` deploys on every push to `main` (plus a manual Run button). Runners have Docker, so no local Docker needed. One-time setup:

1. Cloudflare dashboard → **My Profile → API Tokens → Create Token** (start from the **Edit Cloudflare Workers** template; if a deploy fails on permissions, add the Containers scope the error names) → copy the token.
2. GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - `CLOUDFLARE_API_TOKEN` = the token
   - `CLOUDFLARE_ACCOUNT_ID` = your account ID (Workers overview page URL)
3. Set the six app secrets once (they persist across deploys): `npx wrangler secret put ADMIN_NAME` (repeat for `ADMIN_EMAIL`, `ADMIN_PHONE`, `ADMIN_PASSWORD_SHA256`, `ADMIN_SECRET_SHA256`, `SESSION_SECRET`).
4. Push to `main` and watch the run under the repo's **Actions** tab.

Alternative with zero files/secrets: **Workers & Pages → your Worker → Settings → Builds**, connect this repo, set deploy command `npx wrangler deploy`.

Notes:

- First request cold-boots the container (up to ~60s); retry the login page once if it happens.
- SQLite + uploads live on the container disk: they survive sleeps but reset on redeploy. Use Export CSV for backups.
- Cost: the container sleeps after 30 min idle (`sleepAfter` in `worker/index.js`); usage billing applies while running.
