#!/usr/bin/env python3
"""Prototype LeetCode solver/submitter using Playwright for Python.

Flow:
  1. Open a LeetCode problem URL.
  2. Scrape the problem statement from Next.js data, with meta-description fallback.
  3. Scrape the current Monaco editor stub/signature.
  4. Read solution code from sol.txt, or a custom --solution path.
  5. Put the code into the Monaco editor.
  6. Click Submit unless --dry-run is set.
  7. Poll LeetCode's submission check endpoint and save runtime/memory metrics.

This is a prototype. It does not bypass login, CAPTCHA, premium restrictions,
rate limits, or access controls. Use it only with your own account and in line
with the site's terms.
"""
from __future__ import annotations

import argparse
import html
import json
import platform
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Page, Response, TimeoutError as PlaywrightTimeoutError, sync_playwright

DEFAULT_TIMEOUT_MS = 120_000
EDITOR_SELECTOR = 'textarea[aria-label="Code editor"], [role="textbox"][aria-label="Code editor"]'
SUBMIT_SELECTOR = '[data-e2e-locator="console-submit-button"], button[aria-label="Submit"]'


class SubmitterError(RuntimeError):
    """Raised for expected prototype failures with actionable messages."""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit an already-saved solution to a LeetCode problem via Playwright."
    )
    parser.add_argument("url", help="LeetCode problem URL, e.g. https://leetcode.com/problems/two-sum/")
    parser.add_argument("--solution", default="sol.txt", help="Path to solution code file. Default: sol.txt")
    parser.add_argument("--auth", default="lc-auth.json", help="Playwright storage state JSON. Default: lc-auth.json")
    parser.add_argument("--out", default="leetcode-result.json", help="Result JSON output path.")
    parser.add_argument("--problem-out", default="problem.txt", help="Scraped problem statement output path.")
    parser.add_argument("--signature-out", default="signature.txt", help="Scraped editor signature/stub output path.")
    parser.add_argument("--lang", default=None, help="Language slug to prefer from codeSnippets, e.g. python3/cpp/java.")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS, help="Submission polling timeout.")
    parser.add_argument("--headless", action="store_true", help="Run Chromium headless. Keep false while developing.")
    parser.add_argument("--dry-run", action="store_true", help="Scrape and inject code, but do not click Submit.")
    parser.add_argument("--slow-mo", type=int, default=0, help="Playwright slow_mo in milliseconds.")
    return parser.parse_args(argv)


def looks_like_leetcode_problem_url(raw_url: str) -> bool:
    parsed = urlparse(raw_url)
    return (
        parsed.scheme in {"http", "https"}
        and re.search(r"(^|\.)leetcode\.com$", parsed.hostname or "") is not None
        and re.search(r"/problems/[^/]+", parsed.path) is not None
    )


def strip_html_basic(raw_html: str | None) -> str:
    text = raw_html or ""
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<\s*/p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<\s*/li\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<\s*li[^>]*>", " * ", text, flags=re.I)
    text = re.sub(r"<[^>]*>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def find_question_object(root: Any) -> dict[str, Any] | None:
    stack: list[Any] = [root]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if isinstance(value.get("titleSlug"), str) and (
                isinstance(value.get("content"), str) or isinstance(value.get("codeSnippets"), list)
            ):
                return value
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return None


def read_next_data_question(page: Page) -> dict[str, Any] | None:
    try:
        raw = page.locator("script#__NEXT_DATA__").text_content(timeout=10_000)
    except PlaywrightTimeoutError:
        return None
    except Exception:
        return None

    if not raw:
        return None
    try:
        return find_question_object(json.loads(raw))
    except json.JSONDecodeError:
        return None


def html_to_visible_text(page: Page, problem_html: str | None) -> str:
    if not problem_html:
        return ""
    try:
        return page.evaluate(
            r"""
            (problemHtml) => {
              const container = document.createElement('div');
              container.innerHTML = problemHtml
                .replace(/<\s*br\s*\/?\s*>/gi, '\n')
                .replace(/<\s*\/p\s*>/gi, '\n\n')
                .replace(/<\s*\/li\s*>/gi, '\n')
                .replace(/<\s*li[^>]*>/gi, ' * ');
              return (container.innerText || container.textContent || '')
                .replace(/\n{3,}/g, '\n\n')
                .trim();
            }
            """,
            problem_html,
        )
    except Exception:
        return strip_html_basic(problem_html)


def scrape_problem(page: Page) -> dict[str, Any]:
    question = read_next_data_question(page)
    if question and question.get("content"):
        return {
            "title": question.get("title") or question.get("questionTitle"),
            "titleSlug": question.get("titleSlug"),
            "questionId": question.get("questionId"),
            "questionFrontendId": question.get("questionFrontendId"),
            "text": html_to_visible_text(page, question.get("content")),
            "codeSnippets": question.get("codeSnippets") or [],
            "source": "__NEXT_DATA__.question",
        }

    meta_description = None
    try:
        meta_description = page.locator('meta[name="description"]').get_attribute("content", timeout=5_000)
    except Exception:
        pass

    title = None
    try:
        title = page.title()
    except Exception:
        pass

    return {
        "title": title,
        "titleSlug": None,
        "questionId": None,
        "questionFrontendId": None,
        "text": meta_description or "",
        "codeSnippets": [],
        "source": 'meta[name="description"]',
    }


def wait_for_editor(page: Page) -> None:
    editor = page.locator(EDITOR_SELECTOR).first
    try:
        editor.wait_for(state="attached", timeout=12_000)
        return
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_function(
            "() => Boolean(globalThis.monaco?.editor?.getModels?.()?.length)",
            timeout=30_000,
        )
    except PlaywrightTimeoutError as exc:
        raise SubmitterError(
            "Timed out waiting for the LeetCode Monaco editor. "
            "Check that you are logged in and that the Code tab/editor is visible."
        ) from exc


def get_monaco_models(page: Page) -> list[dict[str, Any]]:
    try:
        return page.evaluate(
            r"""
            () => {
              const models = globalThis.monaco?.editor?.getModels?.() ?? [];
              return models.map((model, index) => ({
                index,
                uri: String(model.uri ?? ''),
                languageId: typeof model.getLanguageId === 'function' ? model.getLanguageId() : null,
                code: typeof model.getValue === 'function' ? model.getValue() : ''
              }));
            }
            """
        )
    except Exception:
        return []


def score_code_model(model: dict[str, Any]) -> int:
    code = model.get("code") or ""
    language_id = model.get("languageId") or ""
    score = 0
    if re.search(r"class\s+Solution\b", code):
        score += 50
    if re.search(r"\bdef\s+\w+\s*\(", code):
        score += 20
    if re.search(r"\bfunction\s+\w+\s*\(", code):
        score += 15
    if re.search(r"\bpublic\s*:", code):
        score += 15
    if re.search(r"Solution\s*\{", code):
        score += 15
    if language_id in {
        "cpp",
        "java",
        "python",
        "python3",
        "javascript",
        "typescript",
        "csharp",
        "golang",
        "rust",
        "swift",
        "kotlin",
        "scala",
        "ruby",
        "php",
    }:
        score += 10
    if 0 < len(code) < 20_000:
        score += 5
    return score


def pick_best_model(models: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not models:
        return None
    return sorted(models, key=score_code_model, reverse=True)[0]


def scrape_editor_signature(page: Page, problem: dict[str, Any], lang: str | None) -> dict[str, Any]:
    models = get_monaco_models(page)
    model = pick_best_model(models)
    snippets = problem.get("codeSnippets") or []

    preferred_snippet = None
    if lang:
        wanted = lang.lower()
        for snippet in snippets:
            if str(snippet.get("langSlug", "")).lower() == wanted or str(snippet.get("lang", "")).lower() == wanted:
                preferred_snippet = snippet
                break

    default_snippet = snippets[0] if snippets else None
    from_next_data = (preferred_snippet or default_snippet or {}).get("code") or ""
    from_editor = model.get("code") if model else ""

    return {
        "fromEditor": from_editor or "",
        "fromNextData": from_next_data,
        "editorModel": (
            {
                "index": model.get("index"),
                "uri": model.get("uri"),
                "languageId": model.get("languageId"),
            }
            if model
            else None
        ),
        "selected": from_editor or from_next_data,
    }


def set_editor_code(page: Page, solution_code: str) -> dict[str, Any]:
    try:
        monaco_set = page.evaluate(
            r"""
            (solutionCode) => {
              const models = globalThis.monaco?.editor?.getModels?.() ?? [];
              if (!models.length) return { ok: false, count: 0 };

              const codeLikeModels = models.filter((model) => {
                const value = model.getValue?.() ?? '';
                const languageId = model.getLanguageId?.() ?? '';
                const uri = String(model.uri ?? '');
                return (
                  /class\s+Solution\b|\bdef\s+\w+\s*\(|\bfunction\s+\w+\s*\(|\bpublic\s*:|impl\s+Solution/.test(value) ||
                  /leetcode|python|cpp|java|javascript|typescript|rust|golang|csharp|kotlin|swift|scala|ruby|php/i.test(`${languageId} ${uri}`)
                );
              });

              const targets = codeLikeModels.length ? codeLikeModels : [models[0]];
              for (const model of targets) model.setValue(solutionCode);
              return { ok: true, count: targets.length };
            }
            """,
            solution_code,
        )
        if isinstance(monaco_set, dict) and monaco_set.get("ok"):
            return monaco_set
    except Exception:
        pass

    # Fallback: use keyboard insertion into Monaco's hidden textarea.
    editor = page.locator(EDITOR_SELECTOR).first
    editor.click(timeout=10_000)
    modifier = "Meta" if platform.system() == "Darwin" else "Control"
    page.keyboard.press(f"{modifier}+A")
    page.keyboard.insert_text(solution_code)
    return {"ok": True, "count": 1, "fallback": "keyboard.insert_text"}


def is_submit_response(response: Response) -> bool:
    try:
        path = urlparse(response.url).path
        return response.request.method == "POST" and re.search(r"/problems/[^/]+/submit/?$", path) is not None
    except Exception:
        return False


def click_submit(page: Page) -> Response | None:
    submit = page.locator(SUBMIT_SELECTOR).first
    submit.wait_for(state="visible", timeout=20_000)

    try:
        with page.expect_response(is_submit_response, timeout=20_000) as response_info:
            submit.click()
        return response_info.value
    except PlaywrightTimeoutError:
        # The click may still have submitted; the UI fallback scraper/poller below can still succeed.
        return None


def extract_submission_id(submit_json: dict[str, Any] | None) -> str | int | None:
    if not submit_json:
        return None
    return (
        submit_json.get("submission_id")
        or submit_json.get("submissionId")
        or (submit_json.get("data") or {}).get("submission_id")
    )


def poll_submission(page: Page, submission_id: str | int, timeout_ms: int) -> dict[str, Any]:
    started = time.monotonic()
    detail_url = f"https://leetcode.com/submissions/detail/{submission_id}/check/"

    while (time.monotonic() - started) * 1000 < timeout_ms:
        try:
            response = page.context.request.get(detail_url, headers={"referer": page.url}, timeout=20_000)
            if response.ok:
                data = response.json()
                if isinstance(data, dict):
                    state = data.get("state")
                    if state == "SUCCESS":
                        return data
                    if state not in {"STARTED", "PENDING", None} and data.get("status_msg"):
                        return data
        except Exception:
            pass

        page.wait_for_timeout(1500)

    raise SubmitterError(f"Timed out waiting for LeetCode submission {submission_id}")


def scrape_visible_metrics(page: Page) -> dict[str, str | None]:
    try:
        body_text = page.locator("body").inner_text(timeout=10_000)
    except Exception:
        body_text = ""

    status_match = re.search(
        r"\b(Accepted|Wrong Answer|Runtime Error|Compile Error|Time Limit Exceeded|Memory Limit Exceeded|Output Limit Exceeded)\b",
        body_text,
        flags=re.I,
    )
    runtime_match = re.search(r"Runtime\s*:?\s*([0-9.]+\s*(?:ms|s))", body_text, flags=re.I) or re.search(
        r"([0-9.]+\s*(?:ms|s))\s*(?:Beats|Runtime)", body_text, flags=re.I
    )
    memory_match = re.search(r"Memory\s*:?\s*([0-9.]+\s*(?:MB|KB))", body_text, flags=re.I) or re.search(
        r"([0-9.]+\s*(?:MB|KB))\s*(?:Beats|Memory)", body_text, flags=re.I
    )

    return {
        "status": status_match.group(1) if status_match else None,
        "runtime": runtime_match.group(1) if runtime_match else None,
        "memory": memory_match.group(1) if memory_match else None,
    }


def save_text(path: str | Path, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def save_json(path: str | Path, data: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not looks_like_leetcode_problem_url(args.url):
        raise SubmitterError(f"Refusing non-LeetCode problem URL: {args.url}")

    solution_path = Path(args.solution)
    if not solution_path.exists():
        raise SubmitterError(f"Solution file not found: {solution_path}")
    solution_code = solution_path.read_text(encoding="utf-8")
    if not solution_code.strip():
        raise SubmitterError(f"Solution file is empty: {solution_path}")

    result: dict[str, Any] = {
        "url": args.url,
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
        "dryRun": args.dry_run,
        "problem": None,
        "signature": None,
        "editorSet": None,
        "submission": None,
        "visibleMetrics": None,
    }

    context_options: dict[str, Any] = {"viewport": {"width": 1440, "height": 1000}}
    auth_path = Path(args.auth)
    if auth_path.exists():
        context_options["storage_state"] = str(auth_path)
    else:
        print(f"Warning: auth state file not found: {auth_path}. Browser will open unauthenticated.", file=sys.stderr)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, slow_mo=args.slow_mo)
        context = browser.new_context(**context_options)
        page = context.new_page()

        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except PlaywrightTimeoutError:
                pass

            wait_for_editor(page)

            problem = scrape_problem(page)
            signature = scrape_editor_signature(page, problem, args.lang)
            result["problem"] = problem
            result["signature"] = signature

            save_text(args.problem_out, f"{problem.get('title') or ''}\n\n{problem.get('text') or ''}\n")
            save_text(args.signature_out, signature.get("selected") or "")

            result["editorSet"] = set_editor_code(page, solution_code)

            if not args.dry_run:
                submit_response = click_submit(page)
                submit_json = None
                if submit_response is not None:
                    try:
                        submit_json = submit_response.json()
                    except Exception:
                        submit_json = None

                submission_id = extract_submission_id(submit_json)

                if submission_id is None:
                    try:
                        page.wait_for_function(
                            "() => /Accepted|Wrong Answer|Runtime Error|Compile Error|Runtime|Memory/i.test(document.body.innerText)",
                            timeout=args.timeout_ms,
                        )
                    except PlaywrightTimeoutError:
                        pass
                    result["visibleMetrics"] = scrape_visible_metrics(page)
                else:
                    check_json = poll_submission(page, submission_id, args.timeout_ms)
                    result["submission"] = {
                        "id": submission_id,
                        "state": check_json.get("state"),
                        "status": check_json.get("status_msg") or check_json.get("status"),
                        "runtime": check_json.get("status_runtime") or check_json.get("runtime"),
                        "memory": check_json.get("status_memory") or check_json.get("memory"),
                        "totalCorrect": check_json.get("total_correct"),
                        "totalTestcases": check_json.get("total_testcases"),
                        "raw": check_json,
                    }
                    result["visibleMetrics"] = scrape_visible_metrics(page)

            save_json(args.out, result)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return result
        finally:
            context.close()
            browser.close()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        run(args)
    except SubmitterError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
