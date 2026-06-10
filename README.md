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

## Cloud Run packaging notes

The included `Dockerfile` installs Python dependencies plus Playwright Chromium dependencies. For Cloud Run Jobs, pass the same CLI args as the local command and provide auth state/secrets through Secret Manager, mounted files, or another controlled mechanism.

Example build:

```bash
gcloud builds submit --tag gcr.io/$GOOGLE_CLOUD_PROJECT/lc-playwright-solver-py
```

Example job creation sketch:

```bash
gcloud run jobs create lc-solver \
  --image gcr.io/$GOOGLE_CLOUD_PROJECT/lc-playwright-solver-py \
  --region us-central1 \
  --set-env-vars GOOGLE_GENAI_USE_VERTEXAI=True,GOOGLE_CLOUD_PROJECT=$GOOGLE_CLOUD_PROJECT,GOOGLE_CLOUD_LOCATION=global,RUN_ROOT=/tmp/leetcode-runs \
  --args="leetcode_submitter.py,https://leetcode.com/problems/two-sum/,--auth,/secrets/lc-auth.json,--lang,python3,--llm,--headless,--dry-run"
```

`/tmp` is writable in Cloud Run but ephemeral. Persist run folders to GCS in a later step if you need long-term artifacts.

## Cloud Run Job deploy with URL-only execution arg

Use `deploy_leetcode_job.sh` to deploy this project as a Cloud Run Job. It is adapted from the existing PoC deploy pattern: load `.env` safely, enable APIs, create Artifact Registry, create a runner service account, create/update Secret Manager secrets, build one image, and deploy a Cloud Run workload.

Prepare auth and config:

```bash
python login.py lc-auth.json
cp .env.example .env
# Edit PROJECT_ID, REGION, GOOGLE_CLOUD_LOCATION, models, bucket, etc.
```

Deploy:

```bash
chmod +x deploy_leetcode_job.sh
./deploy_leetcode_job.sh
```

Execute the job. The only custom execution-time parameter is the LeetCode problem URL:

```bash
gcloud run jobs execute leetcode-solver-job \
  --region us-central1 \
  --args 'https://leetcode.com/problems/two-sum/' \
  --wait
```

How that works:

- `Dockerfile` uses `cloud_run_job.py` as the container entrypoint.
- `cloud_run_job.py` reads fixed options from env vars such as `LC_LANG`, `LC_LLM`, `LC_AUTH_PATH`, `RUN_ROOT`, and Gemini/Vertex settings.
- Cloud Run execution-time `--args` therefore only needs the URL; it does not need to repeat `leetcode_submitter.py`, auth paths, model flags, or headless flags.
- If `OUTPUT_GCS_URI=gs://...` is set, the structured run folder is uploaded to GCS after execution.

Relevant `.env` values:

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
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global
LEETCODE_CODE_MODEL=gemini-2.5-flash
LEETCODE_RATIONALE_MODEL=gemini-2.5-flash
OUTPUT_GCS_URI=gs://leetcode-solver-runs
```
