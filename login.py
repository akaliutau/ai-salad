#!/usr/bin/env python3
"""Save a reusable LeetCode login state for the submitter prototype.

Usage:
    python login.py lc-auth.json
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    state_path = Path(sys.argv[1] if len(sys.argv) > 1 else "lc-auth.json")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.goto("https://leetcode.com/accounts/login/", wait_until="domcontentloaded")

        print("\nLog in to LeetCode in the opened browser window.")
        print("After login completes, return here and press Enter to save auth state.\n")
        input()

        context.storage_state(path=str(state_path))
        browser.close()

    print(f"Saved authenticated browser state to {state_path}")


if __name__ == "__main__":
    main()
