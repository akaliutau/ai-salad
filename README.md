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
