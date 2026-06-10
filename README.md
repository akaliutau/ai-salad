# LeetCode Playwright submitter prototype — Python

This is a Python version of the LeetCode Playwright prototype.

It:

1. Opens a LeetCode problem URL.
2. Scrapes the problem statement from `__NEXT_DATA__`, falling back to `<meta name="description">`.
3. Scrapes the current Monaco editor signature/stub.
4. Reads solution code from `sol.txt` or a custom `--solution` path.
5. Inserts the solution into the Monaco editor.
6. Clicks Submit unless `--dry-run` is set.
7. Polls LeetCode's submission check endpoint and saves Runtime/Memory/status metrics.

## Install

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Login once

```bash
python login.py lc-auth.json
```

A browser opens. Log in manually, then press Enter in the terminal. This writes cookies/local storage to `lc-auth.json`.

Keep this file private. It contains authenticated browser state.

## Submit

```bash
python leetcode_submitter.py 'https://leetcode.com/problems/two-sum/' \
  --solution sol.txt \
  --auth lc-auth.json \
  --lang python3
```

Outputs by default:

- `problem.txt` — scraped problem text
- `signature.txt` — scraped editor stub/signature
- `leetcode-result.json` — final JSON including status/runtime/memory

The terminal now prints a compact summary by default (`problem_id`, status, runtime, memory, score, pack id, run folder, MongoDB status). The full JSON is still saved to `--out`; pass `--verbose-result` if you want the old full stdout dump.

## Scrape/inject without submitting

```bash
python leetcode_submitter.py 'https://leetcode.com/problems/two-sum/' \
  --solution sol.txt \
  --auth lc-auth.json \
  --lang python3 \
  --dry-run
```

## Useful options

```text
--headless             Run browser headless
--slow-mo 250          Slow browser operations for debugging
--timeout-ms 180000    Increase result polling timeout
--out result.json      Change result JSON path
--problem-out p.txt    Change problem dump path
--signature-out s.txt  Change signature dump path
```

## Notes

- This does not bypass login, CAPTCHA, premium restrictions, rate limits, or other access controls.
- Keep `--headless` off while developing selectors.
- Make sure the LeetCode language in the UI matches `sol.txt`; `--lang` only helps pick a stub from page data, it does not reliably change the UI language dropdown.
- LeetCode's React/Monaco DOM can change. The script avoids brittle CSS classes where possible, but this remains a prototype.

## Optional Gemini solution-generation add-on

The Gemini code is intentionally isolated in `leetcode_llm_gemini.py`. The browser submitter still works with `sol.txt`; add `--llm` to generate a solution from the scraped problem and editor signature before injection/submission.

Local env setup, Vertex AI mode:

```bash
cp .env.example .env
# edit GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION as needed
```

API-key mode for local experiments:

```bash
export GOOGLE_GENAI_USE_VERTEXAI=False
export GEMINI_API_KEY='...'
```

Generate with Gemini, inject, and submit:

```bash
python leetcode_submitter.py 'https://leetcode.com/problems/two-sum/' \
  --auth lc-auth.json \
  --lang python3 \
  --llm
```

Generate with Gemini, inject, but do not submit:

```bash
python leetcode_submitter.py 'https://leetcode.com/problems/two-sum/' \
  --auth lc-auth.json \
  --lang python3 \
  --llm \
  --dry-run
```

The LLM run folder is auto-created under `runs/` by default:

```text
runs/<timestamp>_<problem-slug>/
  input/stable_problem_input.json
  input/problem.txt
  input/signature.txt
  prompts/01_generate_code_only.txt
  prompts/02_rationale_and_reasoning_summary.txt
  llm/01_raw_code_response.txt
  llm/01_solution_sanitized.py
  llm/01_solution_sanitized.txt
  llm/02_raw_rationale_response.json
  llm/02_rationale.json
  llm_debug.jsonl
  run_summary.json
  result/leetcode-result.json
```

The first Gemini call is prompted to return direct code only. The second call returns a concise rationale/debug summary, complexity, edge cases, confidence, and possible failure modes. It intentionally asks for a high-level reasoning summary rather than hidden chain-of-thought.

Useful flags:

```text
--llm-model gemini-2.5-flash
--llm-rationale-model gemini-2.5-flash
--llm-temperature 0.2
--run-root /tmp/leetcode-runs
--run-dir /tmp/leetcode-runs/manual-test
--llm-project your-gcp-project
--llm-location global
```

## Optional result packs, Overmind traces, and MongoDB

Install the optional packages when you want tracing and MongoDB persistence:

```bash
pip install overmind 'pymongo[srv]'
```

If you build a Cloud Run image from a Dockerfile that only installs `requirements.txt`, either add these two packages to `requirements.txt` or add this line to the Dockerfile after the main install step:

```dockerfile
RUN pip install --no-cache-dir -r requirements.additions.txt
```

Overmind tracing is optional and is initialized before Gemini calls when `OVERMIND_API_KEY` is present, or when `OVERMIND_ENABLED=true`. The SDK auto-instruments Google Gemini calls and this code adds a small custom span around each LLM operation with LeetCode tags.

```bash
export OVERMIND_API_KEY='ovr_...'
export OVERMIND_SERVICE_NAME='leetcode-solver'
export OVERMIND_ENVIRONMENT='production'
```

MongoDB persistence is optional and is enabled only when `MONGODB_URI` is set. The Cloud Run deploy script stores the URI in Secret Manager and exposes it to the job as the `MONGODB_URI` environment variable.

```bash
export MONGODB_URI='mongodb+srv://...'
export MONGODB_DB='leetcode_solver'
export MONGODB_COLLECTION='solution_packs'
```

Each run writes a solution pack with:

- `problem_id`: the suffix from the URL, for example `two-sum` from `/problems/two-sum/`
- `problem`: title, IDs, source, and statement text
- `solution`: language, code, path, and SHA-256
- `metrics`: status, runtime, memory, test counts, pass rate
- `score`: correctness-first score, then runtime and memory
- `trace`: Overmind metadata when tracing is enabled

Local connectivity check:

```bash
export MONGODB_URI='mongodb+srv://USER:PASSWORD@HOST/leetcode_solver?retryWrites=true&w=majority'
python result_pack_store.py --ping
```

Query all packs for one problem, highest score first:

```bash
python result_pack_store.py two-sum --limit 20
```

The structured run folder also gets `result/solution-pack.json` when `--llm` is used.

## MongoDB Atlas on GCP for real Cloud Run testing

Recommended test setup: use MongoDB Atlas deployed on GCP, then inject the connection string into the Cloud Run Job from Google Secret Manager. This avoids hard-coding database credentials in the job definition.

### Option A: create Atlas manually in the UI

1. Create or open a MongoDB Atlas project.
2. Create a free/shared cluster. Choose **Google Cloud** as the provider and choose a region close to your Cloud Run region.
3. Create a database user, for example `leetcode_solver`, with a generated password.
4. Configure Network Access.
   - Fast smoke test: allow access from anywhere (`0.0.0.0/0`) temporarily.
   - Safer setup: use a static Cloud Run egress IP through Serverless VPC Access + Cloud NAT, then allowlist only that NAT IP.
   - Production setup: use Atlas private networking/private endpoint where available for your cluster tier and region.
5. Copy the driver connection string and replace placeholders with the database user and password.
6. Use database name `leetcode_solver` in the URI path.


Example final URI shape:

```text
mongodb+srv://leetcode_solver:<password>@<cluster-host>/leetcode_solver?retryWrites=true&w=majority
```

Then place it in `.env` before running `deploy_leetcode_job.sh`:

```bash
MONGODB_URI='mongodb+srv://leetcode_solver:...@.../leetcode_solver?retryWrites=true&w=majority'
MONGODB_DB=leetcode_solver
MONGODB_COLLECTION=solution_packs
```

### Option B: create Atlas with the Atlas CLI

Install and authenticate the MongoDB Atlas CLI first:

```bash
sudo apt-get install gnupg curl
curl -fsSL https://pgp.mongodb.com/server-7.0.asc | \
   sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg \
   --dearmor
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt-get update
sudo apt-get install -y mongodb-atlas

atlas auth login
atlas --version
```

A minimal free-tier GCP cluster can be created with `atlas setup`. This creates/configures the cluster and database user in one flow:

```bash
export ATLAS_CLUSTER_NAME=leetcode-solver
export ATLAS_DB_USERNAME=leetcode_solver
export ATLAS_DB_PASSWORD='replace-with-strong-password'

atlas setup \
  --clusterName "$ATLAS_CLUSTER_NAME" \
  --provider GCP \
  --region CENTRAL_US \
  --tier M0 \
  --username "$ATLAS_DB_USERNAME" \
  --password "$ATLAS_DB_PASSWORD" \
  --skipSampleData \
  --connectWith skip \
  --force
```

Fetch the SRV connection string:

```bash
atlas clusters connectionStrings describe "$ATLAS_CLUSTER_NAME"
```

Replace `<username>` and `<password>`, append `/leetcode_solver?retryWrites=true&w=majority` if the URI does not already include a database name, then store it as `MONGODB_URI`.

The deploy script can also run the Atlas CLI setup for you if `ATLAS_SETUP=true` is set:

```bash
ATLAS_SETUP=true
ATLAS_CLUSTER_NAME=leetcode-solver
ATLAS_PROVIDER=GCP
ATLAS_REGION=CENTRAL_US
ATLAS_TIER=M0
ATLAS_DB_USERNAME=leetcode_solver
ATLAS_DB_PASSWORD='replace-with-strong-password'
# Optional if your Atlas CLI profile does not have a default project:
# ATLAS_PROJECT_ID=...
# Optional for a temporary public smoke test, depending on your Atlas policy:
# ATLAS_ACCESS_LIST_IP=0.0.0.0/0
```

## Cloud Run Job deploy with URL-only execution arg

Use `deploy_leetcode_job.sh` to deploy this project as a Cloud Run Job. The script loads `.env`, enables required Google APIs, creates Artifact Registry if missing, creates a runner service account, creates/updates Secret Manager secrets, builds the container image, and creates or updates the Cloud Run Job.

Prepare auth and config:

```bash
python login.py lc-auth.json
cp .env.example .env  # or create .env manually
```

Minimum `.env` for a real GCP test with MongoDB Atlas:

```text
PROJECT_ID=your-gcp-project-id
REGION=us-central1
JOB_NAME=leetcode-solver-job
LC_AUTH_JSON_FILE=lc-auth.json
LC_LANG=python3
LC_LLM=true
LC_HEADLESS=true
LC_DRY_RUN=false
GOOGLE_GENAI_USE_VERTEXAI=True
GOOGLE_CLOUD_LOCATION=global
LEETCODE_CODE_MODEL=gemini-2.5-flash
LEETCODE_RATIONALE_MODEL=gemini-2.5-flash

# MongoDB Atlas persistence:
MONGODB_URI=mongodb+srv://leetcode_solver:...@.../leetcode_solver?retryWrites=true&w=majority
MONGODB_DB=leetcode_solver
MONGODB_COLLECTION=solution_packs

# Optional tracing:
OVERMIND_API_KEY=ovr_...
OVERMIND_SERVICE_NAME=leetcode-solver
OVERMIND_ENVIRONMENT=production

# Optional artifact upload:
OUTPUT_GCS_URI=gs://leetcode-solver-runs
```

Deploy:

```bash
chmod +x deploy_leetcode_job.sh
./deploy_leetcode_job.sh
```

Local testing:

```bash
python leetcode_submitter.py 'https://leetcode.com/problems/two-sum/'   --auth lc-auth.json   --lang python3   --llm   --dry-run
```
With LLM calling:

```bash
python leetcode_submitter.py 'https://leetcode.com/problems/two-sum/'   --auth lc-auth.json   --lang python3   --llm
```

What the deploy script does with secrets:

- `LC_AUTH_JSON_FILE` is uploaded to Secret Manager and mounted as `/secrets/lc-auth.json`.
- `MONGODB_URI` or `MONGODB_URI_FILE` is uploaded to Secret Manager and injected as env var `MONGODB_URI`.
- `OVERMIND_API_KEY` or `OVERMIND_API_KEY_FILE` is uploaded to Secret Manager and injected as env var `OVERMIND_API_KEY`.
- The Cloud Run runner service account receives Secret Manager Secret Accessor on those secrets only.

Execute the job. The only custom execution-time parameter is the LeetCode problem URL:

```bash
gcloud run jobs execute leetcode-solver-job \
  --project your-gcp-project-id \
  --region us-central1 \
  --args 'https://leetcode.com/problems/two-sum/' \
  --wait
```

Read logs:

```bash
gcloud run jobs executions list --job leetcode-solver-job --region us-central1
```

After a successful run, query MongoDB from your local machine with the same `MONGODB_URI`:

```bash
export MONGODB_URI='mongodb+srv://leetcode_solver:...@.../leetcode_solver?retryWrites=true&w=majority'
python result_pack_store.py --ping
python result_pack_store.py two-sum --limit 20
```

How the URL-only job works:

- `Dockerfile` should use `cloud_run_job.py` as the container entrypoint.
- `cloud_run_job.py` reads fixed options from env vars such as `LC_LANG`, `LC_LLM`, `LC_AUTH_PATH`, `RUN_ROOT`, MongoDB, and Gemini/Vertex settings.
- Cloud Run execution-time `--args` therefore only needs the URL; it does not need to repeat `leetcode_submitter.py`, auth paths, model flags, or headless flags.
- If `OUTPUT_GCS_URI=gs://...` is set, the structured run folder is uploaded to GCS after execution.
