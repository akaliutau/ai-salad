#!/usr/bin/env python3
"""Cloud Run Job entrypoint for the LeetCode Gemini submitter.

The deployed job is intentionally configured so the only execution-time argument is
one LeetCode problem URL, for example:

  gcloud run jobs execute leetcode-solver-job \
    --region us-central1 \
    --args 'https://leetcode.com/problems/two-sum/' \
    --wait

All fixed behaviour comes from environment variables set by deploy_leetcode_job.sh.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

from leetcode_submitter import SubmitterError, parse_args, run


def log(message: str) -> None:
    print(f"[lc-job] {message}", flush=True)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_text(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def append_flag(argv: list[str], flag: str, value: str | None) -> None:
    if value is not None and str(value).strip() != "":
        argv.extend([flag, str(value)])


def build_submitter_argv(problem_url: str) -> list[str]:
    run_root = env_text("RUN_ROOT", "/tmp/leetcode-runs")
    tmp_root = Path(run_root)
    tmp_root.mkdir(parents=True, exist_ok=True)

    argv = [problem_url]
    append_flag(argv, "--auth", env_text("LC_AUTH_PATH", "/secrets/lc-auth.json"))
    append_flag(argv, "--lang", env_text("LC_LANG", "python3"))
    append_flag(argv, "--run-root", run_root)
    append_flag(argv, "--out", env_text("LC_RESULT_OUT", str(tmp_root / "leetcode-result.json")))
    append_flag(argv, "--problem-out", env_text("LC_PROBLEM_OUT", str(tmp_root / "problem.txt")))
    append_flag(argv, "--signature-out", env_text("LC_SIGNATURE_OUT", str(tmp_root / "signature.txt")))
    append_flag(argv, "--timeout-ms", env_text("LC_TIMEOUT_MS", "120000"))

    if env_flag("LC_HEADLESS", True):
        argv.append("--headless")
    if env_flag("LC_DRY_RUN", False):
        argv.append("--dry-run")
    if env_flag("LC_LLM", True):
        argv.append("--llm")

    # Optional explicit overrides. The submitter/LLM module also reads the same
    # model/project/location values from env, so these are only passed when set.
    append_flag(argv, "--llm-model", env_text("LEETCODE_CODE_MODEL"))
    append_flag(argv, "--llm-rationale-model", env_text("LEETCODE_RATIONALE_MODEL"))
    append_flag(argv, "--llm-project", env_text("GOOGLE_CLOUD_PROJECT", env_text("PROJECT_ID")))
    append_flag(argv, "--llm-location", env_text("GOOGLE_CLOUD_LOCATION", env_text("VERTEX_LOCATION", "global")))

    slow_mo = env_text("LC_SLOW_MO")
    if slow_mo:
        append_flag(argv, "--slow-mo", slow_mo)

    # When LC_LLM=false, use a mounted or baked solution file.
    if not env_flag("LC_LLM", True):
        append_flag(argv, "--solution", env_text("LC_SOLUTION_PATH", "sol.txt"))

    return argv


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    match = re.match(r"^gs://([^/]+)(?:/(.*))?$", uri.strip())
    if not match:
        raise ValueError(f"Not a GCS URI: {uri}")
    return match.group(1), (match.group(2) or "").strip("/")


def iter_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def upload_run_dir_to_gcs(run_dir: Path, output_gcs_uri: str) -> dict[str, object]:
    """Optional Cloud Run persistence: copy the structured run folder to GCS."""
    from google.cloud import storage  # imported lazily so local dry-runs stay lightweight

    bucket_name, prefix = parse_gcs_uri(output_gcs_uri)
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    destination_prefix = "/".join(part for part in [prefix, run_dir.name] if part)

    uploaded: list[str] = []
    for file_path in iter_files(run_dir):
        rel = file_path.relative_to(run_dir).as_posix()
        blob_name = f"{destination_prefix}/{rel}"
        bucket.blob(blob_name).upload_from_filename(str(file_path))
        uploaded.append(f"gs://{bucket_name}/{blob_name}")

    manifest = {
        "run_dir": str(run_dir),
        "output_gcs_uri": output_gcs_uri,
        "destination_prefix": f"gs://{bucket_name}/{destination_prefix}",
        "uploaded_count": len(uploaded),
        "uploaded": uploaded,
    }
    (run_dir / "gcs_upload_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    # Upload manifest after writing it.
    manifest_blob = f"{destination_prefix}/gcs_upload_manifest.json"
    bucket.blob(manifest_blob).upload_from_filename(str(run_dir / "gcs_upload_manifest.json"))
    manifest["manifest"] = f"gs://{bucket_name}/{manifest_blob}"
    return manifest


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if len(raw_args) != 1 or raw_args[0] in {"-h", "--help"}:
        print("Usage: cloud_run_job.py <leetcode-problem-url>", file=sys.stderr)
        print("All other options are configured through Cloud Run environment variables.", file=sys.stderr)
        return 2

    problem_url = raw_args[0]
    submitter_argv = build_submitter_argv(problem_url)
    log("starting LeetCode job")
    log("submitter argv=" + json.dumps(submitter_argv))

    try:
        args = parse_args(submitter_argv)
        result = run(args)
    except SubmitterError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    run_dir_value = result.get("runDir") if isinstance(result, dict) else None
    output_gcs_uri = env_text("OUTPUT_GCS_URI")
    if run_dir_value and output_gcs_uri:
        run_dir = Path(str(run_dir_value))
        if run_dir.exists():
            try:
                manifest = upload_run_dir_to_gcs(run_dir, output_gcs_uri)
                log("uploaded run artifacts: " + json.dumps({
                    "destination_prefix": manifest.get("destination_prefix"),
                    "uploaded_count": manifest.get("uploaded_count"),
                    "manifest": manifest.get("manifest"),
                }))
            except Exception as exc:
                log(f"warning: GCS upload failed: {exc.__class__.__name__}: {exc}")
        else:
            log(f"warning: runDir does not exist, skipping upload: {run_dir}")
    elif output_gcs_uri:
        log("OUTPUT_GCS_URI is set but no structured LLM runDir was produced; skipping upload")

    log("job complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
