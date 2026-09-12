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
