"""Browser tests for the things only a browser can prove.

The two other suites talk to the app over HTTP, which cannot see a JavaScript
defect: a button that quietly does nothing, a dropdown that re-points every
row, a typed price wiped by the four-second refresh. Those are exactly the
bugs that have bitten this project, so they get their own suite.

    pip install playwright && playwright install chromium
    python tests/test_browser.py

It starts its own server on a spare port and its own throwaway database. If
Playwright is not installed it says so and exits 0 — it never fails a machine
that simply does not have a browser.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Kolkata")
FAILS: list[str] = []


def check(label, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(tmp: str, port: int) -> subprocess.Popen:
    env = dict(os.environ,
               RA_DATA_DIR=tmp,
               RA_DATABASE_URL=f"sqlite:///{tmp}/browser.db",
               RA_ENV_FILE=f"{tmp}/none.env",
               RA_TIMEZONE="Asia/Kolkata",
               PYTHONPATH=str(ROOT))
    env.pop("RA_SMTP_HOST", None)
    subprocess.run([sys.executable, "seed.py"], cwd=ROOT, env=env, check=True,
                   stdout=subprocess.DEVNULL)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level",
         "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import urllib.request
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            return proc
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("the test server did not start")


def run(page, base):
    js_errors: list[str] = []
    page.on("pageerror", lambda e: js_errors.append(str(e)))
    page.on("console", lambda m: js_errors.append(m.text) if m.type == "error" else None)
    page.on("dialog", lambda d: d.accept())

    page.goto(base + "/login")
    page.fill("#email", "buyer@example.com")
    page.fill("#password", "demo1234")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    # ------------------------------------------------------------------ 1
    print("\n1. Creating several new items in a row")
    page.goto(base + "/auctions/new")
    page.wait_for_timeout(400)
    stamp = datetime.now().strftime("%H%M%S%f")[:10]
    for n in (1, 2, 3):
        page.locator("button:has-text('Create a new item')").first.click()
        page.wait_for_timeout(250)
        page.fill("#m-item input[name=name]", f"BrowserItem{stamp}-{n}")
        page.click("#m-item button[type=submit]")
        page.wait_for_timeout(600)
    rows = page.locator("#line-rows .item-row").count()
    chosen = page.evaluate("""() => Array.from(document.querySelectorAll('.sel-item'))
        .map(s => s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : '')""")
    check("three new items become three rows", rows == 3, f"{rows} rows")
    check("each row keeps its own item", len(set(chosen)) == 3, ", ".join(chosen))
    check("a row added later can still pick an earlier new item",
          page.evaluate("""() => {
              window.addLineRow();
              const s = document.querySelectorAll('.sel-item');
              const last = s[s.length - 1];
              return Array.from(last.options).length === Array.from(s[0].options).length;
          }"""))
    page.locator("#line-rows .item-row").nth(3).locator("button:has-text('Remove')").click()
    page.wait_for_timeout(200)
    check("Remove takes the row away", page.locator("#line-rows .item-row").count() == 3)
    labels = [page.locator("#line-rows .item-row .top b").nth(i).inner_text() for i in range(3)]
    check("the rows renumber", labels == ["Item 1", "Item 2", "Item 3"], ", ".join(labels))

    # ------------------------------------------------------------------ 2
    print("\n2. Publishing straight from the form")
    now = datetime.now(TZ)
    page.fill("#title", "Browser suite auction")
    for i, (qty, price) in enumerate([("10", "100"), ("20", "200"), ("30", "")]):
        page.fill(f"input[name=line_qty] >> nth={i}", qty)
        if price:
            page.fill(f"input[name=line_price] >> nth={i}", price)
    boxes = page.locator("#vendor-list input[type=checkbox]")
    for i in range(boxes.count()):
        boxes.nth(i).check()
    page.fill("#start_at", (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"))
    page.fill("#end_at", (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"))
    controls = page.locator(".actionbar button, .actionbar a")
    labels = " | ".join(controls.nth(i).inner_text().strip() for i in range(controls.count()))
    check("the form offers draft, publish and cancel",
          all(word in labels.lower() for word in ("draft", "publish", "cancel")), labels)
    page.click("#publish-btn")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)
    check("publishing from the form opens bidding at once",
          "LIVE" in page.locator("main .pill").first.inner_text())
    check("every item made it onto the auction", page.locator(".line-card").count() == 3,
          f"{page.locator('.line-card').count()}")
    url = page.url

    # ------------------------------------------------------------------ 3
    print("\n3. The live board survives its own refresh")
    ctx = page.context.browser.new_context()
    bidder = ctx.new_page()
    bidder.on("pageerror", lambda e: js_errors.append(str(e)))
    bidder.on("dialog", lambda d: d.accept())
    bidder.goto(base + "/login")
    bidder.fill("#email", "supplier1@example.com")
    bidder.fill("#password", "demo1234")
    bidder.click("button[type=submit]")
    bidder.wait_for_load_state("networkidle")
    bidder.goto(url)
    bidder.wait_for_timeout(500)
    bidder.locator("input[name=unit_price]").nth(0).fill("87.5")
    bidder.locator("h1").click()                       # blur, as a person would
    bidder.wait_for_timeout(5200)                      # past one refresh
    check("a typed price survives the refresh",
          bidder.locator("input[name=unit_price]").nth(0).input_value() == "87.5",
          bidder.locator("input[name=unit_price]").nth(0).input_value())
    hint = bidder.locator("[id^=total]").first.inner_text()
    check("...and so does the line total under it", "That is" in hint, hint[:44] or "empty")
    chip = bidder.locator("[data-fill]").nth(1)
    want = chip.get_attribute("data-fill")
    chip.click()
    bidder.wait_for_timeout(200)
    check("the suggested-price chip still works after a refresh",
          bidder.locator("input[name=unit_price]").nth(1).input_value() == want)
    bidder.locator("input[name=unit_price]").nth(0).fill("88")
    bidder.locator("button:has-text('Place bid')").nth(0).click()
    bidder.wait_for_load_state("networkidle")
    check("the bid lands", "L1" in bidder.locator(".alert").first.inner_text(),
          bidder.locator(".alert").first.inner_text()[:44])

    # A second bidder, so the award screen has a real choice to make.
    ctx2 = page.context.browser.new_context()
    bidder2 = ctx2.new_page()
    bidder2.on("pageerror", lambda e: js_errors.append(str(e)))
    bidder2.on("dialog", lambda d: d.accept())
    bidder2.goto(base + "/login")
    bidder2.fill("#email", "supplier2@example.com")
    bidder2.fill("#password", "demo1234")
    bidder2.click("button[type=submit]")
    bidder2.wait_for_load_state("networkidle")
    bidder2.goto(url)
    bidder2.wait_for_timeout(500)
    bidder2.locator("input[name=unit_price]").nth(0).fill("85")
    bidder2.locator("button:has-text('Place bid')").nth(0).click()
    bidder2.wait_for_load_state("networkidle")
    check("the second bidder takes L1",
          "L1" in bidder2.locator(".alert").first.inner_text(),
          bidder2.locator(".alert").first.inner_text()[:44])

    # ------------------------------------------------------------------ 4
    print("\n4. A whole bid typed into the board, against the board's own refresh")
    # The board rewrites itself every few seconds. A bid is now a form with a
    # price, delivery costs and named taxes on it, and none of those boxes has
    # an id - which is exactly how they used to be wiped mid-word.
    bidder.goto(base + "/auctions")
    bidder.wait_for_load_state("networkidle")
    bidder.locator("a[href^='/auctions/']", has_text="Bearings").first.click()
    bidder.wait_for_load_state("networkidle")
    bidder.wait_for_timeout(500)
    form = bidder.locator("form[action$='/bid']").first
    tax_name = form.locator("input[name^=tax_name]").first
    tax_name.click()
    tax_name.fill("")
    tax_name.type("Cess on steel", delay=25)
    form.locator("input[name^=tax_percent]").first.fill("5")
    form.locator("input[name=freight]").fill("1234")
    form.locator("input[name=unit_price]").fill("150")
    form.locator("[data-add-tax]").first.click()
    rows = form.locator(".tax-row")
    rows.nth(rows.count() - 1).locator("input[name^=tax_name]").fill("State levy")
    rows.nth(rows.count() - 1).locator("input[name^=tax_percent]").fill("2")
    added = rows.count()
    bidder.wait_for_timeout(9500)                      # past two refreshes
    check("a tax name survives the refresh", tax_name.input_value() == "Cess on steel",
          tax_name.input_value())
    check("...so does the freight beside it",
          form.locator("input[name=freight]").input_value() == "1234")
    check("...and a tax row the bidder added themselves",
          form.locator(".tax-row").count() == added
          and form.locator(".tax-row").nth(added - 1)
                  .locator("input[name^=tax_name]").input_value() == "State levy",
          f"{form.locator('.tax-row').count()} rows")
    live = " ".join(form.locator(".all-in-note").first.inner_text().split())
    check("...and the all-in total is still worked out", "All in" in live, live[:60])
    check("the tax name box offers the usual names to pick from",
          tax_name.get_attribute("list") == "tax-names"
          and bidder.locator("#tax-names option").count() > 5)
    form.locator("button:has-text('Place bid for')").first.click()
    bidder.wait_for_load_state("networkidle")
    bidder.wait_for_timeout(600)
    record = " ".join(bidder.locator(".bid-record").first.inner_text().split())
    check("the bid goes in with both taxes exactly as typed",
          "Cess on steel" in record and "State levy" in record, record[:90])

    # ------------------------------------------------------------------ 5
    print("\n5. The award screen books the price the buyer can see")
    page.goto(url)
    page.wait_for_timeout(300)
    page.locator("button:has-text('Close bidding now')").click()
    page.wait_for_load_state("networkidle")
    page.goto(page.url.split("?")[0] + "/award")
    page.wait_for_timeout(400)
    chips = page.locator(".chip")
    labels = [chips.nth(i).inner_text() for i in range(chips.count())]
    everything_to = [i for i, text in enumerate(labels) if text.startswith("Everything to")]
    check("the award screen offers a quick fill per bidder", len(everything_to) >= 2,
          " | ".join(labels))
    mismatches = []
    for index in everything_to:
        chips.nth(index).click()
        page.wait_for_timeout(250)
        checked = page.locator("input[type=radio][data-line]:checked").first
        want = checked.get_attribute("data-price")
        got = page.locator("input[name^=price_]").first.input_value()
        if want and float(got or 0) != float(want):
            mismatches.append(f"{labels[index]}: box {got}, winner's bid {want}")
    check("every quick fill puts the winner's own price in the box",
          not mismatches, "; ".join(mismatches))
    winner_labels = page.locator("input[type=radio][data-line]:checked ~ .t")
    check("the chosen row is the one labelled Winner",
          winner_labels.count() >= 1 and "Winner" in winner_labels.first.inner_text(),
          winner_labels.first.inner_text() if winner_labels.count() else "none")

    # ------------------------------------------------------------------ 5
    print("\n6. Nothing threw along the way")
    bidder.goto(base + "/logout")
    bidder.wait_for_timeout(200)
    page.goto(base + "/login")
    page.keyboard.press("Escape")                      # no drawers on this page
    page.wait_for_timeout(200)
    check("no JavaScript errors anywhere", not js_errors, "; ".join(js_errors[:2]))


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed — skipping the browser suite.")
        print("  pip install playwright && playwright install chromium")
        return 0

    tmp = tempfile.mkdtemp(prefix="ra-browser-")
    port = free_port()
    server = start_server(tmp, port)
    base = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:
                print(f"No browser available ({exc}) — skipping.")
                return 0
            page = browser.new_context(viewport={"width": 1400, "height": 1000}).new_page()
            run(page, base)
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)

    print("\n" + "-" * 62)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All browser checks passed.")
    return 0


def test_browser():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
