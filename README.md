
# 🥗 AI-Salad: Autonomous Dataset Factory

<p align="left">
  <img alt="Status" src="https://img.shields.io/badge/status-prototype-orange">
  <img alt="Agents" src="https://img.shields.io/badge/agents-browser--native-blue">
  <img alt="Data" src="https://img.shields.io/badge/data-validated--attempts-green">
</p>

**AI-Salad** is a browser-native agent system for solving coding problems, improving solutions through sequential submissions, 
extracting reusable solving heuristics, and automatically building HiFi fine-tuning datasets — without human labeling.

The project starts with LeetCode-style problems, but the bigger goal is broader: 
create a closed-loop environment where coding agents learn from real feedback, search toward optimal solutions, 
and turn every validated attempt into durable training data.

---

## The Idea

Most coding-agent systems stop after one generation:

```text
problem → model → solution → pass/fail
```

AI-Salad treats solving as an optimization process — a mixture of runs, coding environment feedback (essentially it could be any platform, 
even if it does not have API), traces, heuristics, and validated data:

```text
problem
  → generate candidate
  → submit in browser
  → observe real feedback
  → improve solution
  → resubmit
  → rank attempts
  → extract heuristic
  → store dataset row
```

Each submission becomes a gradient signal, that can be thought as a practical optimization gradient: 
acceptance, failing tests, runtime, memory, edge cases, code shape, and reasoning quality all point the system toward better solutions.

The final output is a structured, validated example that can be used to train stronger models. Since the agent has the access to comment sections
at coding competition platforms, as well as solutions submitted by other users, it results in exceptionally high quality of solutions and hence in
high density of training data, which becomes a much better input than unstructured data collected via web scraping.

---

## Why Browser Access?

Browser access is a core design choice, because most coding platforms usually do not provide APIs. 
In that sense the browser contains the real task environment. For example, in case of LeetCode this is:

- the exact problem statement shown to the user;
- the current editor signature and language stub;
- authenticated access state;
- platform-specific submission behavior;
- visible runtime, memory, and status feedback;
- real-world UI changes, delays, and failure modes.

Using Playwright gives the agent the same interface a human solver uses. 
That matters because the goal is to collect trustworthy execution-grounded data, not synthetic examples detached from the actual platform.

Browser access also keeps the system general. The same architecture can later work across other coding sites, 
interview platforms, internal benchmark tools, or custom web-based evaluation environments.

The browser is the agent’s laboratory in our product.

---

## Why We Need Many Solutions

A single accepted solution is useful, but a sequence of attempts is far more valuable.

For each problem, AI-Salad should eventually generate and evaluate many candidates:

- simple baseline solutions;
- optimized solutions;
- alternative algorithms;
- memory-saving variants;
- language-specific idioms;
- repaired versions after failures;
- heuristic-guided attempts;
- edge-case-focused rewrites.

Sequential submissions create an empirical search process. 
The system can compare attempts using correctness first, then runtime, memory, simplicity, robustness, and explanation quality.

This allows the project to move from:

```text
Can the model solve the problem?
```

to:

```text
Can the agent discover the best solution family, explain why it works, and preserve the evidence as training data?
```

That is the central loop.

---

## What the System Does Today

The current prototype already implements the foundation:

1. Opens a coding problem in a real browser.
2. Scrapes the problem statement.
3. Scrapes the editor signature or stub.
4. Builds a stable problem input.
5. Uses Gemini to generate direct submission code.
6. Sanitizes the model output.
7. Injects the code into the editor.
8. Submits the solution, unless running in dry-run mode.
9. Polls or reads submission feedback.
10. Stores runtime, memory, status, and pass-rate signals.
11. Writes a structured run folder.
12. Optionally stores a result pack in MongoDB.
13. Optionally runs as a Google Cloud Run Job.
14. Optionally records traces through Overmind.

This is enough to start collecting validated solution attempts.

---

## Architecture

```text
Coding Platform
    │
    ▼
Browser Agent
Playwright scraper + submitter
    │
    ▼
Stable Problem Input
problem text + signature + language + hash
    │
    ▼
Solver Agent
Gemini today, multi-agent search later
    │
    ▼
Solution Sanitizer
code-only extraction for direct submission
    │
    ▼
Submission Loop
browser injection + platform feedback
    │
    ▼
Result Pack
problem + code + metrics + score + rationale
    │
    ▼
Dataset Factory
filter + rank + dedupe + export
```

---

## Core Components

### `leetcode_submitter.py`

The browser automation runner.

It opens the problem page, extracts the problem and editor signature, injects code into Monaco, submits the solution, 
waits for results, and writes the final JSON output.

### `leetcode_llm_gemini.py`

The current solver module.

It runs a two-step LLM pipeline:

1. Generate final submission code only.
2. Generate a compact audit record with rationale, complexity, edge cases, confidence, and possible failure modes.

### `result_pack_store.py`

The evidence and persistence layer.

It builds solution packs, extracts key metrics, computes a correctness-first score, stores packs in MongoDB, 
and supports querying previous attempts by problem.

### `cloud_run_job.py`

The production entrypoint.

It lets the system run as a Cloud Run Job where the only execution-time argument is the problem URL. 
All fixed behavior comes from environment variables and secrets.

---

## Result Packs

Every serious attempt should become a result pack containing:

```text
problem_id
problem statement
editor signature
language
generated solution
solution hash
submission status
accepted flag
runtime
memory
pass rate
score
rationale
edge cases
model configuration
trace metadata
run directory
```

This is the atomic unit of the project.

Result packs are used for:

- ranking solutions;
- comparing attempts;
- detecting duplicates;
- mining heuristics;
- debugging failures;
- creating fine-tuning examples;
- building evaluation sets.

---

## The Optimization Loop

AI-Salad is designed around sequential improvement.

```text
Attempt 1: generate obvious solution
Attempt 2: repair correctness bug
Attempt 3: reduce complexity
Attempt 4: optimize memory
Attempt 5: simplify code
Attempt 6: validate edge cases
```

Each attempt produces feedback.

The system can score attempts with a correctness-first policy:

```text
accepted > partial pass rate > runtime > memory > simplicity
```

Over time, this creates an automatic search process over solution space.


---

## Heuristics

Accepted solutions are not only examples. They are evidence for reusable strategies.

From successful runs, the system can extract heuristics such as:

```text
Use a hash map when the problem asks for complements or previously seen values.
Use two pointers when constraints involve sorted arrays or shrinking windows.
Use a monotonic stack when each element needs the next greater or smaller element.
Use BFS when the task asks for shortest path in an unweighted state graph.
Use dynamic programming when the optimal answer depends on overlapping subproblems.
```

These heuristics can later be used to guide new agents before code generation.

The project therefore creates a flywheel:

```text
solutions → heuristics → better solutions → better datasets → better models
```

---

## Autonomous Dataset Creation

The end goal is a dataset pipeline that does not require human labeling.

A high-quality training row should be created only when the system has evidence:

- the problem input is stable;
- the editor signature is known;
- the code was actually submitted or validated;
- the solution passed or has measurable feedback;
- runtime and memory are recorded;
- the solution is deduplicated;
- the rationale and edge cases are clean;
- the example is traceable back to a result pack.

A future dataset row can look like this:

```json
{
  "id": "two-sum:python3:abc123",
  "input": {
    "problem": "...",
    "signature": "class Solution: ..."
  },
  "output": {
    "code": "class Solution: ...",
    "rationale": "Use a hash map to store previously seen values..."
  },
  "metadata": {
    "accepted": true,
    "runtime_ms": 52,
    "memory_mb": 17.8,
    "heuristics": ["hash-map-complement"],
    "source_pack_id": "abc123"
  }
}
```

The dataset is not built from model guesses. It is built from validated attempts.

---

## Local Quick Start


```bash
git clone https://github.com/akaliutau/ai-salad
cd ai-salad
```

3. **Create and activate a Conda environment**

```bash
conda create -n ai-salad python=3.12 -y
conda activate ai-salad
pip install -r requirements.txt
python -m playwright install chromium
```

Log in once:

```bash
python login.py lc-auth.json
```

Run with Gemini:

```bash
python leetcode_submitter.py 'https://leetcode.com/problems/two-sum/' \
  --auth lc-auth.json \
  --lang python3 \
  --llm
```

Run without submitting:

```bash
python leetcode_submitter.py 'https://leetcode.com/problems/two-sum/' \
  --auth lc-auth.json \
  --lang python3 \
  --llm \
  --dry-run
```

---

## Environment

For Vertex AI Gemini:

```bash
export GOOGLE_GENAI_USE_VERTEXAI=True
export GOOGLE_CLOUD_PROJECT='your-project-id'
export GOOGLE_CLOUD_LOCATION='global'
export LEETCODE_CODE_MODEL='gemini-2.5-flash'
export LEETCODE_RATIONALE_MODEL='gemini-2.5-flash'
```

For Gemini API-key mode:

```bash
export GOOGLE_GENAI_USE_VERTEXAI=False
export GEMINI_API_KEY='...'
```

For MongoDB result packs:

```bash
export MONGODB_URI='mongodb+srv://...'
export MONGODB_DB='leetcode_solver'
export MONGODB_COLLECTION='solution_packs'
```

For tracing:

```bash
export OVERMIND_API_KEY='...'
export OVERMIND_SERVICE_NAME='ai-salad'
export OVERMIND_ENVIRONMENT='production'
```

---

## Cloud Run

The Cloud Run Job entrypoint is designed for URL-only execution:

```bash
gcloud run jobs execute leetcode-solver-job \
  --region us-central1 \
  --args 'https://leetcode.com/problems/two-sum/' \
  --wait
```

The job reads all fixed configuration from environment variables:

```text
LC_AUTH_PATH
LC_LANG
LC_LLM
LC_HEADLESS
LC_DRY_RUN
RUN_ROOT
GOOGLE_CLOUD_PROJECT
GOOGLE_CLOUD_LOCATION
MONGODB_URI
OUTPUT_GCS_URI
```

This makes production runs reproducible and easy to schedule at scale.

---

## Run Artifacts

Each LLM run creates a structured folder:

```text
runs/<timestamp>_<problem-slug>/
  input/
    stable_problem_input.json
    problem.txt
    signature.txt
  prompts/
    01_generate_code_only.txt
    02_rationale_and_reasoning_summary.txt
  llm/
    01_raw_code_response.txt
    01_solution_sanitized.py
    02_rationale.json
  result/
    leetcode-result.json
    solution-pack.json
  llm_debug.jsonl
  run_summary.json
```

These artifacts make each solution auditable and reproducible.

---

## Roadmap

### 1. Single-agent solving

Generate, submit, record, and store solution attempts.

### 2. Sequential optimization

Run multiple attempts per problem and improve using real feedback.

### 3. Agentic repair

Use failed submissions to generate targeted fixes.

### 4. Heuristic discovery

Mine accepted solutions for reusable algorithmic patterns.

### 5. Autonomous dataset builder

Filter, rank, deduplicate, and export validated fine-tuning examples.

### 6. Fine-tune and evaluate

Train models on the generated dataset and evaluate on held-out problems.

---

## Disclaimer

This project should only be used with accounts, platforms, and problem sources where automation is permitted.

It does not bypass login, CAPTCHA, paywalls, premium restrictions, rate limits, or access controls.

Keep secrets private:

```text
lc-auth.json
.env
Gemini keys
MongoDB URI
Overmind API key
Cloud service account credentials
```

Before publishing datasets, verify that the underlying problem statements, solutions, and metadata can be used for that purpose.

---

## Vision

AI-Salad is a system for turning coding agents into their own data engine: generate, submit, observe, improve, remember, and train.

The browser provides the environment.
Submissions provide the gradient.
Result packs provide the memory.
Heuristics provide the strategy.
Datasets provide the compounding effect.

The destination is a self-improving loop eventually giving coding models the superhuman abilities in CS tasks 
