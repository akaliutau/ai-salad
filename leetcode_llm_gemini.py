#!/usr/bin/env python3
"""Gemini add-on for the LeetCode Playwright submitter.

This module is intentionally separate from browser automation.  It receives a
stable problem payload that was already scraped by Playwright and returns a
sanitized LeetCode solution string that can be written into Monaco.

Pipeline:
  1. Generate code only.
  2. Generate a concise rationale/debug explanation for the generated code.

All inputs, prompts, raw-ish structured outputs, sanitized code, and diagnostics
are written under an auto-generated run folder.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # Optional locally; Cloud Run should provide env vars directly.
    import dotenv
except Exception:  # pragma: no cover
    dotenv = None  # type: ignore[assignment]

try:
    from google import genai
except Exception as exc:  # pragma: no cover
    genai = None  # type: ignore[assignment]
    GENAI_IMPORT_ERROR = str(exc)
else:
    GENAI_IMPORT_ERROR = ""


DEFAULT_CODE_MODEL = os.getenv("LEETCODE_CODE_MODEL", os.getenv("STAGE1_MODEL", "gemini-2.5-flash"))
DEFAULT_RATIONALE_MODEL = os.getenv("LEETCODE_RATIONALE_MODEL", os.getenv("STAGE1_MODEL", DEFAULT_CODE_MODEL))
DEFAULT_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", os.getenv("VERTEX_LOCATION", "global"))


@dataclass
class GeminiSolutionResult:
    run_dir: Path
    stable_input_path: Path
    sanitized_solution_path: Path
    rationale_path: Path
    debug_jsonl_path: Path
    solution_code: str
    rationale: dict[str, Any]


def log(message: str) -> None:
    print(f"[lc-llm] {message}", flush=True)


def maybe_load_dotenv(path: str | Path | None = None) -> None:
    """Load .env locally when python-dotenv is installed; harmless in Cloud Run."""
    if dotenv is None:
        return
    try:
        dotenv.load_dotenv(dotenv_path=str(path) if path else None)
    except Exception as exc:
        log(f"warning: could not load .env: {exc.__class__.__name__}: {exc}")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def dump_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(to_plain_json(data), indent=2, ensure_ascii=False), encoding="utf-8")


def dump_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(to_plain_json(data), ensure_ascii=False) + "\n")


def to_plain_json(value: Any, *, max_string: int = 4000) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_string else value[: max_string - 3] + "..."
    if isinstance(value, (bytes, bytearray)):
        return {"bytes_len": len(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_plain_json(v, max_string=max_string) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_plain_json(v, max_string=max_string) for v in value]
    if dataclasses.is_dataclass(value):
        return to_plain_json(dataclasses.asdict(value), max_string=max_string)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return to_plain_json(model_dump(exclude_none=True), max_string=max_string)
        except Exception:
            pass
    return repr(value)[:max_string]


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def short_error(exc: BaseException, limit: int = 1600) -> str:
    text = f"{exc.__class__.__name__}: {exc}"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def slugify(value: str | None) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "leetcode-problem").strip().lower()).strip("-")
    return text[:72] or "leetcode-problem"


def create_run_dir(root: str | Path, slug: str | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = Path(root or os.getenv("RUN_ROOT", "runs"))
    run_dir = base / f"{stamp}_{slugify(slug)}"
    # Extremely unlikely, but keeps reruns deterministic enough for local smoke tests.
    suffix = 1
    candidate = run_dir
    while candidate.exists():
        suffix += 1
        candidate = Path(f"{run_dir}_{suffix}")
    ensure_dir(candidate)
    return candidate


def make_gemini_client(project: str | None = None, location: str | None = None) -> Any:
    """Create a Gemini client for Vertex AI by default, or API-key mode when configured.

    Cloud Run-friendly defaults:
      GOOGLE_GENAI_USE_VERTEXAI=True
      GOOGLE_CLOUD_PROJECT=<project id> or PROJECT_ID=<project id>
      GOOGLE_CLOUD_LOCATION=global/us-central1/etc.

    Local API-key mode:
      GOOGLE_GENAI_USE_VERTEXAI=False
      GEMINI_API_KEY=<key> or GOOGLE_API_KEY=<key>
    """
    if genai is None:
        raise RuntimeError(f"google-genai is not installed: {GENAI_IMPORT_ERROR}")

    use_vertex = env_flag("GOOGLE_GENAI_USE_VERTEXAI", True)
    if use_vertex:
        resolved_project = project or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("PROJECT_ID")
        resolved_location = location or DEFAULT_LOCATION
        if not resolved_project:
            raise RuntimeError(
                "Vertex Gemini mode requires GOOGLE_CLOUD_PROJECT or PROJECT_ID. "
                "Set GOOGLE_GENAI_USE_VERTEXAI=False and GEMINI_API_KEY for API-key mode."
            )
        log(f"Gemini client: Vertex AI project={resolved_project} location={resolved_location}")
        return genai.Client(
            vertexai=True,
            project=resolved_project,
            location=resolved_location,
            http_options={"api_version": "v1"},
        )

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("API-key Gemini mode requires GEMINI_API_KEY or GOOGLE_API_KEY.")
    log("Gemini client: API-key mode")
    return genai.Client(api_key=api_key)


def build_stable_problem_input(
    *,
    url: str,
    problem: dict[str, Any],
    signature: dict[str, Any],
    lang: str | None,
) -> dict[str, Any]:
    """Build the stable payload sent to Gemini and persisted for reproducibility."""
    problem_text = str(problem.get("text") or "").strip()
    selected_signature = str(signature.get("selected") or "").strip()
    payload = {
        "url": url,
        "language": lang or "python3",
        "title": problem.get("title"),
        "titleSlug": problem.get("titleSlug"),
        "questionId": problem.get("questionId"),
        "questionFrontendId": problem.get("questionFrontendId"),
        "problemText": problem_text,
        "signature": selected_signature,
        "source": problem.get("source"),
        "inputHash": hashlib.sha256(
            json.dumps(
                {
                    "url": url,
                    "lang": lang or "python3",
                    "title": problem.get("title"),
                    "problemText": problem_text,
                    "signature": selected_signature,
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
    }
    return payload


def language_name(lang: str | None) -> str:
    aliases = {
        "python3": "Python 3",
        "python": "Python 3",
        "cpp": "C++17",
        "java": "Java",
        "javascript": "JavaScript",
        "typescript": "TypeScript",
        "golang": "Go",
        "csharp": "C#",
        "rust": "Rust",
    }
    return aliases.get((lang or "python3").lower(), lang or "Python 3")


def code_extension(lang: str | None) -> str:
    mapping = {
        "python3": "py",
        "python": "py",
        "cpp": "cpp",
        "java": "java",
        "javascript": "js",
        "typescript": "ts",
        "golang": "go",
        "csharp": "cs",
        "rust": "rs",
        "kotlin": "kt",
        "swift": "swift",
        "ruby": "rb",
        "php": "php",
        "scala": "scala",
    }
    return mapping.get((lang or "python3").lower(), "txt")


def build_code_prompt(stable_input: dict[str, Any]) -> str:
    lang = language_name(str(stable_input.get("language") or "python3"))
    return f"""
You are an expert competitive programmer solving a LeetCode problem.

Return ONLY the final LeetCode submission code in {lang}.
Do not use markdown fences, comments explaining the solution, prose, tests, stdin/stdout wrappers, or imports that are not needed by LeetCode.
Preserve the required LeetCode class/function signature from the provided stub when possible.
The code must be deterministic and suitable for direct paste into LeetCode's editor.

Problem title: {stable_input.get('title') or ''}
Problem URL: {stable_input.get('url') or ''}
Requested language: {stable_input.get('language') or 'python3'}

LeetCode editor stub/signature:
{stable_input.get('signature') or '[not available]'}

Problem statement:
{stable_input.get('problemText') or '[not available]'}
""".strip()


def build_rationale_prompt(stable_input: dict[str, Any], solution_code: str) -> str:
    return f"""
You are reviewing a generated LeetCode solution for debugging and auditability.

Return JSON only with these keys:
- rationale: concise explanation of the algorithm in 4-8 sentences.
- reasoning_summary: high-level reasoning summary only; do not include hidden chain-of-thought or token-by-token private deliberation.
- complexity: object with time and space.
- edge_cases: list of important cases.
- confidence_0_to_100: integer.
- possible_failure_modes: list of concise caveats.

Problem title: {stable_input.get('title') or ''}
Requested language: {stable_input.get('language') or 'python3'}

Problem statement:
{stable_input.get('problemText') or '[not available]'}

Generated solution code:
{solution_code}
""".strip()


def extract_response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    parts: list[str] = []
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                parts.append(str(part_text))
    return "\n".join(parts).strip()


def response_debug_summary(response: Any, *, started: float) -> dict[str, Any]:
    usage = getattr(response, "usage_metadata", None)
    candidates = getattr(response, "candidates", []) or []
    return {
        "latency_sec": round(time.time() - started, 3),
        "response_text_chars": len(extract_response_text(response)),
        "candidate_count": len(candidates),
        "usage_metadata": to_plain_json(usage),
    }


def call_gemini_text(
    *,
    client: Any,
    model: str,
    prompt: str,
    temperature: float,
    debug_jsonl: Path,
    op_name: str,
    response_mime_type: str | None = None,
) -> str:
    started = time.time()
    record: dict[str, Any] = {
        "time_epoch": started,
        "op": op_name,
        "model": model,
        "temperature": temperature,
        "prompt_chars": len(prompt),
        "prompt_sha256_12": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
    }
    log(f"LLM start op={op_name} model={model} prompt_chars={len(prompt)}")
    try:
        config: dict[str, Any] = {"temperature": temperature}
        if response_mime_type:
            config["response_mime_type"] = response_mime_type
        response = client.models.generate_content(model=model, contents=[prompt], config=config)
        text = extract_response_text(response)
        record.update(response_debug_summary(response, started=started))
        record.update({"status": "ok", "output_sha256_12": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]})
        append_jsonl(debug_jsonl, record)
        log(f"LLM done op={op_name} status=ok latency={record['latency_sec']}s response_chars={len(text)}")
        return text
    except Exception as exc:
        record.update({"status": "error", "latency_sec": round(time.time() - started, 3), "error": short_error(exc)})
        append_jsonl(debug_jsonl, record)
        log(f"LLM done op={op_name} status=error error={record['error']}")
        raise


def sanitize_solution_code(raw_text: str) -> str:
    """Remove common LLM wrapping while preserving the actual LeetCode code."""
    text = (raw_text or "").strip()

    # Some models still return JSON despite the code-only prompt.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            for key in ("solution_code", "code", "solution"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
    except Exception:
        pass

    fence = re.search(r"```(?:[a-zA-Z0-9_+.#-]+)?\s*\n(?P<code>.*?)\n```", text, flags=re.S)
    if fence:
        text = fence.group("code").strip()

    # Strip accidental prose before the first plausible code line.
    lines = text.splitlines()
    code_start_patterns = [
        r"^\s*class\s+Solution\b",
        r"^\s*def\s+\w+\s*\(",
        r"^\s*from\s+typing\s+import\b",
        r"^\s*import\s+\w+",
        r"^\s*public\s+class\s+Solution\b",
        r"^\s*class\s+Solution\s*\{",
        r"^\s*impl\s+Solution\b",
        r"^\s*function\s+\w+\s*\(",
        r"^\s*func\s+\w+\s*\(",
    ]
    for idx, line in enumerate(lines):
        if any(re.search(pattern, line) for pattern in code_start_patterns):
            lines = lines[idx:]
            break

    code = "\n".join(lines).strip()
    # Drop common trailing commentary markers.
    code = re.split(r"\n\s*(?:Explanation|Rationale|Complexity)\s*:\s*", code, maxsplit=1, flags=re.I)[0].strip()

    if not code:
        raise RuntimeError("Gemini returned empty solution code after sanitization.")
    if len(code) > 200_000:
        raise RuntimeError(f"Gemini solution code is unexpectedly large: {len(code)} chars")
    return code + "\n"


def parse_rationale_json(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    fence = re.search(r"```(?:json)?\s*\n(?P<json>.*?)\n```", text, flags=re.S | re.I)
    if fence:
        text = fence.group("json").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"rationale": text, "parse_warning": "Model did not return valid JSON."}


def generate_solution_with_gemini(
    *,
    problem: dict[str, Any],
    signature: dict[str, Any],
    url: str,
    lang: str | None = "python3",
    run_root: str | Path = "runs",
    run_dir: str | Path | None = None,
    model: str | None = None,
    rationale_model: str | None = None,
    project: str | None = None,
    location: str | None = None,
    temperature: float = 0.2,
    rationale_temperature: float = 0.2,
) -> GeminiSolutionResult:
    """Run the two-call Gemini pipeline and return sanitized code."""
    maybe_load_dotenv()
    stable_input = build_stable_problem_input(url=url, problem=problem, signature=signature, lang=lang)
    actual_run_dir = Path(run_dir) if run_dir else create_run_dir(run_root, str(problem.get("titleSlug") or problem.get("title") or "problem"))
    ensure_dir(actual_run_dir)
    ensure_dir(actual_run_dir / "input")
    ensure_dir(actual_run_dir / "prompts")
    ensure_dir(actual_run_dir / "llm")

    debug_jsonl = actual_run_dir / "llm_debug.jsonl"
    stable_input_path = actual_run_dir / "input" / "stable_problem_input.json"
    dump_json(stable_input_path, stable_input)
    dump_text(actual_run_dir / "input" / "problem.txt", str(stable_input.get("problemText") or ""))
    dump_text(actual_run_dir / "input" / "signature.txt", str(stable_input.get("signature") or ""))

    log(f"run_dir={actual_run_dir}")
    log(
        "stable input saved: "
        f"problem_chars={len(str(stable_input.get('problemText') or ''))} "
        f"signature_chars={len(str(stable_input.get('signature') or ''))} "
        f"hash={stable_input.get('inputHash', '')[:12]}"
    )

    client = make_gemini_client(project=project, location=location)
    code_model = model or DEFAULT_CODE_MODEL
    rationale_model = rationale_model or DEFAULT_RATIONALE_MODEL

    code_prompt = build_code_prompt(stable_input)
    dump_text(actual_run_dir / "prompts" / "01_generate_code_only.txt", code_prompt)
    raw_code = call_gemini_text(
        client=client,
        model=code_model,
        prompt=code_prompt,
        temperature=temperature,
        debug_jsonl=debug_jsonl,
        op_name="01_generate_code_only",
        response_mime_type="text/plain",
    )
    dump_text(actual_run_dir / "llm" / "01_raw_code_response.txt", raw_code)

    solution_code = sanitize_solution_code(raw_code)
    ext = code_extension(str(stable_input.get("language") or "python3"))
    sanitized_path = actual_run_dir / "llm" / f"01_solution_sanitized.{ext}"
    dump_text(sanitized_path, solution_code)
    dump_text(actual_run_dir / "llm" / "01_solution_sanitized.txt", solution_code)
    log(f"sanitized solution saved: chars={len(solution_code)} path={sanitized_path}")

    rationale_prompt = build_rationale_prompt(stable_input, solution_code)
    dump_text(actual_run_dir / "prompts" / "02_rationale_and_reasoning_summary.txt", rationale_prompt)
    raw_rationale = call_gemini_text(
        client=client,
        model=rationale_model,
        prompt=rationale_prompt,
        temperature=rationale_temperature,
        debug_jsonl=debug_jsonl,
        op_name="02_rationale_and_reasoning_summary",
        response_mime_type="application/json",
    )
    dump_text(actual_run_dir / "llm" / "02_raw_rationale_response.json", raw_rationale)
    rationale = parse_rationale_json(raw_rationale)
    rationale_path = actual_run_dir / "llm" / "02_rationale.json"
    dump_json(rationale_path, rationale)
    log(f"rationale saved: path={rationale_path}")

    summary = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "language": stable_input.get("language"),
        "model": code_model,
        "rationaleModel": rationale_model,
        "stableInput": str(stable_input_path),
        "sanitizedSolution": str(sanitized_path),
        "rationale": str(rationale_path),
        "debugJsonl": str(debug_jsonl),
        "inputHash": stable_input.get("inputHash"),
    }
    dump_json(actual_run_dir / "run_summary.json", summary)
    log("LLM pipeline complete")

    return GeminiSolutionResult(
        run_dir=actual_run_dir,
        stable_input_path=stable_input_path,
        sanitized_solution_path=sanitized_path,
        rationale_path=rationale_path,
        debug_jsonl_path=debug_jsonl,
        solution_code=solution_code,
        rationale=rationale,
    )
