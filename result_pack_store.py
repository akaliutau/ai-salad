#!/usr/bin/env python3
"""Result-pack helpers for LeetCode solver runs.

This module is intentionally optional at runtime: MongoDB is used only when
MONGODB_URI is set, so local runs keep working without database credentials.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

_MONGO_CLIENT: Any | None = None
_INDEXED_COLLECTIONS: set[tuple[str, str]] = set()


DEFAULT_DB = os.getenv("MONGODB_DB", "leetcode_solver")
DEFAULT_COLLECTION = os.getenv("MONGODB_COLLECTION", "solution_packs")


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def problem_id_from_url(raw_url: str) -> str:
    """Return the LeetCode slug after /problems/, e.g. two-sum."""
    parsed = urlparse(raw_url or "")
    match = re.search(r"/problems/([^/?#]+)/?", parsed.path)
    if match:
        return match.group(1).strip().lower()
    return "unknown-problem"


def _parse_number(value: Any) -> float | None:
    if value is None:
        return None
    match = re.search(r"[0-9]+(?:\.[0-9]+)?", str(value))
    return float(match.group(0)) if match else None


def runtime_to_ms(value: Any) -> float | None:
    number = _parse_number(value)
    if number is None:
        return None
    text = str(value).lower()
    return number * 1000 if re.search(r"\bs\b|sec|second", text) and "ms" not in text else number


def memory_to_mb(value: Any) -> float | None:
    number = _parse_number(value)
    if number is None:
        return None
    text = str(value).lower()
    return number / 1024 if "kb" in text else number


def extract_key_metrics(result: dict[str, Any]) -> dict[str, Any]:
    submission = result.get("submission") if isinstance(result.get("submission"), dict) else {}
    visible = result.get("visibleMetrics") if isinstance(result.get("visibleMetrics"), dict) else {}

    status = submission.get("status") or visible.get("status")
    runtime = submission.get("runtime") or visible.get("runtime")
    memory = submission.get("memory") or visible.get("memory")
    total_correct = submission.get("totalCorrect")
    total_testcases = submission.get("totalTestcases")

    try:
        pass_rate = float(total_correct) / float(total_testcases) if total_testcases else None
    except Exception:
        pass_rate = None

    return {
        "status": status,
        "accepted": str(status or "").lower() == "accepted",
        "runtime": runtime,
        "runtime_ms": runtime_to_ms(runtime),
        "memory": memory,
        "memory_mb": memory_to_mb(memory),
        "total_correct": total_correct,
        "total_testcases": total_testcases,
        "pass_rate": pass_rate,
        "dry_run": bool(result.get("dryRun")),
    }


def score_from_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Stable score: correctness dominates, then runtime, then memory."""
    accepted = 1.0 if metrics.get("accepted") else 0.0
    pass_rate = float(metrics.get("pass_rate") or 0.0)
    runtime_ms = metrics.get("runtime_ms")
    memory_mb = metrics.get("memory_mb")

    runtime_component = 1.0 / (1.0 + float(runtime_ms)) if runtime_ms is not None else 0.0
    memory_component = 1.0 / (1.0 + float(memory_mb)) if memory_mb is not None else 0.0

    score = accepted * 1_000_000 + pass_rate * 100_000 + runtime_component * 1_000 + memory_component * 100
    return {
        "score": round(score, 6),
        "components": {
            "accepted": accepted,
            "pass_rate": round(pass_rate, 6),
            "runtime_component": round(runtime_component, 9),
            "memory_component": round(memory_component, 9),
        },
    }


def _solution_sha(solution_code: str) -> str:
    return hashlib.sha256((solution_code or "").encode("utf-8")).hexdigest()


def build_solution_pack(result: dict[str, Any], *, solution_code: str) -> dict[str, Any]:
    problem = result.get("problem") if isinstance(result.get("problem"), dict) else {}
    signature = result.get("signature") if isinstance(result.get("signature"), dict) else {}
    llm = result.get("llm") if isinstance(result.get("llm"), dict) else None
    metrics = extract_key_metrics(result)
    scoring = score_from_metrics(metrics)
    problem_id = str(result.get("problemId") or problem.get("titleSlug") or problem_id_from_url(str(result.get("url") or "")))
    solution_hash = _solution_sha(solution_code)
    created_at = result.get("scrapedAt") or datetime.now(timezone.utc).isoformat()
    pack_id = hashlib.sha256(
        json.dumps(
            {
                "problem_id": problem_id,
                "created_at": created_at,
                "solution_hash": solution_hash,
                "submission_id": (result.get("submission") or {}).get("id") if isinstance(result.get("submission"), dict) else None,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]

    return {
        "pack_id": pack_id,
        "problem_id": problem_id,
        "created_at": created_at,
        "url": result.get("url"),
        "problem": {
            "title": problem.get("title"),
            "title_slug": problem.get("titleSlug") or problem_id,
            "question_id": problem.get("questionId"),
            "question_frontend_id": problem.get("questionFrontendId"),
            "source": problem.get("source"),
            "text": problem.get("text"),
        },
        "signature": {
            "selected": signature.get("selected"),
            "editor_model": signature.get("editorModel"),
        },
        "solution": {
            "language": result.get("language") or ((result.get("llm") or {}).get("language") if isinstance(result.get("llm"), dict) else None),
            "code": solution_code,
            "sha256": solution_hash,
            "path": (llm or {}).get("sanitizedSolution") if llm else None,
        },
        "metrics": metrics,
        "score": scoring["score"],
        "score_components": scoring["components"],
        "submission": result.get("submission"),
        "visible_metrics": result.get("visibleMetrics"),
        "llm": llm,
        "trace": result.get("trace"),
        "run_dir": result.get("runDir"),
        "dry_run": result.get("dryRun"),
    }


def compact_pack(pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "pack_id": pack.get("pack_id"),
        "problem_id": pack.get("problem_id"),
        "created_at": pack.get("created_at"),
        "status": (pack.get("metrics") or {}).get("status"),
        "runtime": (pack.get("metrics") or {}).get("runtime"),
        "memory": (pack.get("metrics") or {}).get("memory"),
        "score": pack.get("score"),
        "solution_sha256": (pack.get("solution") or {}).get("sha256"),
        "run_dir": pack.get("run_dir"),
        "trace": pack.get("trace"),
    }


def mongodb_uri_from_env() -> str | None:
    """Resolve MongoDB URI from env or a mounted Secret Manager file."""
    uri = os.getenv("MONGODB_URI")
    if uri:
        return uri.strip()
    uri_file = os.getenv("MONGODB_URI_FILE")
    if uri_file:
        try:
            return open(uri_file, "r", encoding="utf-8").read().strip()
        except OSError:
            return None
    return None


def mongo_collection(uri: str | None = None, db_name: str | None = None, collection_name: str | None = None) -> Any:
    from pymongo import ASCENDING, DESCENDING, MongoClient

    global _MONGO_CLIENT
    resolved_uri = uri or mongodb_uri_from_env()
    if not resolved_uri:
        raise RuntimeError("MONGODB_URI is not set")

    # Reuse the client within a process. For a Cloud Run Job this is mostly a
    # small efficiency win; it also follows the Atlas guidance for serverless
    # runtimes where multiple operations may happen in one invocation.
    if _MONGO_CLIENT is None:
        _MONGO_CLIENT = MongoClient(resolved_uri, serverSelectionTimeoutMS=int(os.getenv("MONGODB_TIMEOUT_MS", "5000")))

    dbn = db_name or DEFAULT_DB
    coln = collection_name or DEFAULT_COLLECTION
    collection = _MONGO_CLIENT[dbn][coln]
    index_key = (dbn, coln)
    if index_key not in _INDEXED_COLLECTIONS:
        collection.create_index([("problem_id", ASCENDING), ("score", DESCENDING), ("created_at", DESCENDING)])
        collection.create_index("pack_id", unique=True)
        _INDEXED_COLLECTIONS.add(index_key)
    return collection


def ping_mongodb() -> dict[str, Any]:
    collection = mongo_collection()
    collection.database.client.admin.command("ping")
    return {"ok": True, "db": collection.database.name, "collection": collection.name}


def store_solution_pack(pack: dict[str, Any], *, uri: str | None = None) -> dict[str, Any]:
    collection = mongo_collection(uri=uri)
    doc = dict(pack)
    doc["_id"] = pack["pack_id"]
    result = collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    return {
        "enabled": True,
        "db": DEFAULT_DB,
        "collection": DEFAULT_COLLECTION,
        "pack_id": pack["pack_id"],
        "problem_id": pack["problem_id"],
        "upserted_id": str(result.upserted_id) if result.upserted_id is not None else None,
        "matched_count": result.matched_count,
    }


def maybe_store_solution_pack(pack: dict[str, Any]) -> dict[str, Any]:
    if not mongodb_uri_from_env() or env_flag("MONGODB_DISABLED", False):
        return {"enabled": False, "reason": "MONGODB_URI not set"}
    try:
        return store_solution_pack(pack)
    except Exception as exc:
        return {"enabled": False, "error": f"{exc.__class__.__name__}: {exc}"}


def list_solution_packs(problem_id: str, *, limit: int = 50, include_solution: bool = False) -> list[dict[str, Any]]:
    projection = None if include_solution else {"solution.code": 0, "problem.text": 0, "submission.raw": 0}
    cursor = mongo_collection().find({"problem_id": problem_id}, projection).sort(
        [("score", -1), ("created_at", -1)]
    ).limit(limit)
    docs = []
    for doc in cursor:
        doc.pop("_id", None)
        docs.append(doc)
    return docs


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Query stored LeetCode solution packs by problem_id.")
    parser.add_argument("problem_id", nargs="?", help="LeetCode problem slug, e.g. two-sum")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--include-solution", action="store_true")
    parser.add_argument("--ping", action="store_true", help="Check MongoDB connectivity and exit.")
    ns = parser.parse_args()
    if ns.ping:
        print(json.dumps(ping_mongodb(), indent=2, ensure_ascii=False))
    else:
        if not ns.problem_id:
            parser.error("problem_id is required unless --ping is used")
        print(json.dumps(list_solution_packs(ns.problem_id, limit=ns.limit, include_solution=ns.include_solution), indent=2, ensure_ascii=False))
