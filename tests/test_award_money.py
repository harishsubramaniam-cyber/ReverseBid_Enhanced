"""Round 2 of the deep check: awarding, savings and the reports.

Every figure this app puts on a screen about money is checked here against
arithmetic worked out by hand, not against the app's own opinion of itself.
The three parts are:

  Part one (1-9)    the savings arithmetic - budgets, negotiated prices,
                    part-awarded auctions, re-awarding, and the reports.
  Part two (10-16)  the awkward shapes - no ceiling, no bids, delivered
                    prices, the dashboard tile, the CSV and the PDFs.
  Part three (17-20) what the award screen must refuse.

Each part builds its own organisation so nothing leaks between them, and
part three builds a brand-new auction for every single case for the same
reason.

    python tests/test_award_money.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="ra-award-money-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ.pop("RA_SMTP_HOST", None)

from fastapi.testclient import TestClient                       # noqa: E402

from app import engine, reporting                               # noqa: E402
from app.db import Base, SessionLocal, engine as db_engine      # noqa: E402
from app.main import app                                        # noqa: E402
from app.models import (Auction, AuctionLine, AuctionStatus, Award,   # noqa: E402
                        DecrementType, Item, Organisation, Participant,
                        Role, Unit, User, Vendor)
from app.security import hash_password                          # noqa: E402

Base.metadata.create_all(bind=db_engine)
PW = "test1234"
FAILS: list[str] = []


def check(label, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


def close(a, b, tol=0.011):
    return abs(float(a) - float(b)) <= tol


class Client(TestClient):
    """A browser that carries the anti-forgery token, as a real one does."""

    def post(self, url, **kwargs):
        token = self.cookies.get("ra_csrf")
        if token:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CSRF-Token", token)
            kwargs["headers"] = headers
        return super().post(url, **kwargs)


def login(email):
    client = Client(app, base_url="http://test")
    client.get("/login")
    client.post("/login", data={"email": email, "password": PW, "next": "/"},
                follow_redirects=False)
    client.headers.update({"accept": "text/html"})
    return client


def flash_of(response):
    raw = response.headers.get("set-cookie", "")
    if "ra_flash" not in raw:
        return ""
    jar = SimpleCookie()
    jar.load(raw)
    try:
        return json.loads(jar["ra_flash"].value)["m"]
    except Exception:                                  # pragma: no cover
        return ""


def _people(db, org, suffix, vendors=2, item_names=("Pens", "Paper", "Rope")):
    """A buyer, a unit, some items and some invited suppliers."""
    buyer = User(name="Buyer", email=f"buyer@{suffix}", role=Role.BUYER, org_id=org.id,
                 password_hash=hash_password(PW))
    db.add(buyer)
    unit = Unit(code="NOS", org_id=org.id)
    items = [Item(name=name, org_id=org.id) for name in item_names]
    db.add(unit)
    db.add_all(items)
    db.flush()
    sellers = []
    for i in range(vendors):
        vendor = Vendor(name=f"Supplier {i + 1}", email=f"s{i}@{suffix}", org_id=org.id)
        db.add(vendor)
        db.flush()
        db.add(User(name=f"Supplier {i + 1}", email=f"s{i}@{suffix}", role=Role.VENDOR,
                    org_id=org.id, vendor_id=vendor.id, password_hash=hash_password(PW)))
        db.flush()
        sellers.append(vendor)
    db.commit()
    return buyer, unit, items, sellers


# --------------------------------------------------------------------------
# Part one: the savings arithmetic
# --------------------------------------------------------------------------
def part_one():
    db = SessionLocal()
    org = Organisation(name="Round two")
    db.add(org)
    db.flush()
    buyer, unit, items, vs = _people(db, org, "r2")
    one, two = login("s0@r2"), login("s1@r2")
    boss = login("buyer@r2")

    def make(specs, tag="", landed=False, mode="line"):
        now = datetime.utcnow()
        auction = Auction(reference=f"R2-{now.timestamp()}{tag}", title=f"Auction {tag}",
                          creator_id=buyer.id, org_id=org.id, status=AuctionStatus.LIVE,
                          start_at=now - timedelta(minutes=5), end_at=now + timedelta(hours=2),
                          original_end_at=now + timedelta(hours=2),
                          decrement_type=DecrementType.ABSOLUTE, min_decrement=0.0,
                          compare_landed=landed, auto_extend=False, award_mode=mode,
                          published_at=now)
        db.add(auction)
        db.flush()
        for item, qty, ceiling in specs:
            db.add(AuctionLine(auction_id=auction.id, item_id=item.id, unit_id=unit.id,
                               qty=qty, starting_price=ceiling))
        for i, vendor in enumerate(vs):
            db.add(Participant(auction_id=auction.id, vendor_id=vendor.id, alias=f"Bidder {i}"))
        db.commit()
        db.refresh(auction)
        return auction

    def bid(client, auction, line, price, freight=0, tax=0):
        """A bid as the screen sends it: price, delivery costs and tax together."""
        data = {"line_id": str(line.id), "unit_price": str(price)}
        if auction.compare_landed:
            data.update({"freight": str(freight), "packaging": "0", "other": "",
                         "tax_name": "GST", "tax_percent": str(tax)})
        return client.post(f"/auctions/{auction.id}/bid", data=data,
                           follow_redirects=False)

    def award(auction, picks, prices=None):
        data = {}
        for line_id, vendor_id in picks.items():
            data[f"winner_{line_id}"] = str(vendor_id) if vendor_id else ""
            if prices and line_id in prices:
                data[f"price_{line_id}"] = str(prices[line_id])
        return boss.post(f"/auctions/{auction.id}/award", data=data, follow_redirects=False)

    print("\n1. The simplest case, worked out by hand")
    # 100 pens with a ceiling of 10 is a budget of 1,000. Won at 8, so 800 is
    # spent and 200 saved - 20%.
    auction = make([(items[0], 100, 10.0)], tag="simple")
    line = auction.lines[0]
    bid(one, auction, line, 9.0)
    bid(two, auction, line, 8.0)
    db.expire_all()
    summary = engine.auction_summary(db, auction)
    check("the budget is quantity x ceiling", close(summary["baseline"], 1000))
    check("the expected spend is the best bid", close(summary["final_value"], 800))
    check("the saving is the difference", close(summary["savings"], 200))
    check("...and the percentage matches", close(summary["savings_pct"], 20))
    auction.status = AuctionStatus.CLOSED
    db.commit()
    award(auction, {line.id: vs[1].id}, {line.id: 8.0})
    db.expire_all()
    summary = engine.auction_summary(db, auction)
    check("after awarding, the figures are unchanged",
          close(summary["savings"], 200) and summary["basis"] == "awarded")

    print("\n2. Awarding at a negotiated price, above and below the bid")
    for label, price, spend in (("below the bid", 7.5, 750), ("above the bid", 8.5, 850)):
        auction = make([(items[0], 100, 10.0)], tag=f"neg{price}")
        line = auction.lines[0]
        bid(one, auction, line, 9.0)
        bid(two, auction, line, 8.0)
        auction.status = AuctionStatus.CLOSED
        db.commit()
        award(auction, {line.id: vs[1].id}, {line.id: price})
        db.expire_all()
        summary = engine.auction_summary(db, auction)
        check(f"awarding {label} books what will really be paid",
              close(summary["final_value"], spend), summary["final_value"])
        check(f"...and the saving follows it ({label})",
              close(summary["savings"], 1000 - spend), summary["savings"])

    print("\n3. A line left unawarded claims no saving")
    auction = make([(items[0], 100, 10.0), (items[1], 100, 20.0)], tag="part")
    pens, paper = auction.lines
    bid(one, auction, pens, 8.0)
    bid(one, auction, paper, 15.0)
    auction.status = AuctionStatus.CLOSED
    db.commit()
    award(auction, {pens.id: vs[0].id, paper.id: None}, {pens.id: 8.0})
    db.expire_all()
    summary = engine.auction_summary(db, auction)
    # Budget 1,000 + 2,000 = 3,000. Pens bought for 800; the paper was not
    # bought, so it still costs its budget of 2,000. Spend 2,800, saving 200.
    check("the unawarded line is counted at its budget, not its best bid",
          close(summary["final_value"], 2800), summary["final_value"])
    check("...so the saving is only what was really saved",
          close(summary["savings"], 200), summary["savings"])
    row = engine.line_result(db, paper)
    check("...and that line's own row says it saved nothing",
          close(row["savings"], 0) and row["basis"] == "not awarded",
          f"{row['basis']} {row['savings']}")

    print("\n4. The line rows always add up to the auction total")
    for tag, specs in (("fractions", [(items[0], 3.5, 7.77), (items[1], 1.5, 19.99)]),
                       ("three", [(items[0], 10, 10.0), (items[1], 7, 3.33),
                                  (items[2], 1, 999.99)])):
        auction = make(specs, tag=tag)
        for line in auction.lines:
            bid(one, auction, line, round(line.starting_price * 0.9, 2))
            bid(two, auction, line, round(line.starting_price * 0.8, 2))
        auction.status = AuctionStatus.CLOSED
        db.commit()
        award(auction, {line.id: vs[1].id for line in auction.lines},
              {line.id: round(line.starting_price * 0.8, 2) for line in auction.lines})
        db.expire_all()
        summary = engine.auction_summary(db, auction)
        rows = [engine.line_result(db, line) for line in auction.lines]
        check(f"[{tag}] the line spends add up to the auction spend",
              close(sum(r["final_value"] for r in rows), summary["final_value"], 0.02),
              f'{sum(r["final_value"] for r in rows):.2f} vs {summary["final_value"]:.2f}')
        check(f"[{tag}] the line savings add up to the auction saving",
              close(sum(r["savings"] for r in rows), summary["savings"], 0.02),
              f'{sum(r["savings"] for r in rows):.2f} vs {summary["savings"]:.2f}')

    print("\n5. Re-awarding replaces, never doubles")
    auction = make([(items[0], 100, 10.0)], tag="re")
    line = auction.lines[0]
    bid(one, auction, line, 9.0)
    bid(two, auction, line, 8.0)
    auction.status = AuctionStatus.CLOSED
    db.commit()
    award(auction, {line.id: vs[1].id}, {line.id: 8.0})
    db.expire_all()
    award(auction, {line.id: vs[0].id}, {line.id: 9.0})
    db.expire_all()
    check("there is still exactly one award",
          db.query(Award).filter_by(auction_id=auction.id).count() == 1)
    summary = engine.auction_summary(db, auction)
    check("...and the figures follow the new winner",
          close(summary["final_value"], 900), summary["final_value"])

    print("\n6. The report and the dashboard agree with the auction")
    auction = make([(items[0], 100, 10.0)], tag="rep")
    line = auction.lines[0]
    bid(one, auction, line, 9.0)
    bid(two, auction, line, 7.5)
    auction.status = AuctionStatus.CLOSED
    db.commit()
    award(auction, {line.id: vs[1].id}, {line.id: 7.5})
    db.expire_all()
    start = datetime.utcnow() - timedelta(days=1)
    end = datetime.utcnow() + timedelta(days=1)
    report = reporting.total_savings(db, start, end, org_id=org.id)
    mine = [r for r in report["rows"] if r["auction"].id == auction.id]
    check("the auction appears in the period report", len(mine) == 1)
    summary = engine.auction_summary(db, auction)
    check("...with the same saving the auction screen shows",
          bool(mine) and close(mine[0]["savings"], summary["savings"]),
          f'{mine[0]["savings"]} vs {summary["savings"]}' if mine else "")
    check("...and the report totals are the sum of its rows",
          close(report["totals"]["savings"], sum(r["savings"] for r in report["rows"])),
          f'{report["totals"]["savings"]} vs {sum(r["savings"] for r in report["rows"])}')
    page = boss.get("/").text
    check("the dashboard page loads", "Dashboard" in page or "Hello" in page)

    print("\n7. Dates: the report follows the date it shows")
    auction = make([(items[0], 100, 10.0)], tag="date")
    line = auction.lines[0]
    bid(one, auction, line, 8.0)
    auction.status = AuctionStatus.CLOSED
    db.commit()
    award(auction, {line.id: vs[0].id}, {line.id: 8.0})
    db.expire_all()
    db.refresh(auction)
    awarded_on = auction.awarded_at

    def ids(rep):
        return [r["auction"].id for r in rep["rows"]]

    before = reporting.total_savings(db, awarded_on - timedelta(days=2),
                                     awarded_on - timedelta(seconds=1), org_id=org.id)
    after = reporting.total_savings(db, awarded_on + timedelta(seconds=1),
                                    awarded_on + timedelta(days=2), org_id=org.id)
    inside = reporting.total_savings(db, awarded_on - timedelta(minutes=1),
                                     awarded_on + timedelta(minutes=1), org_id=org.id)
    check("a period ending before the award excludes it", auction.id not in ids(before))
    check("a period starting after the award excludes it", auction.id not in ids(after))
    check("a period containing the award includes it", auction.id in ids(inside))

    print("\n8. A cancelled auction stops counting")
    auction = make([(items[0], 100, 10.0)], tag="cancel")
    line = auction.lines[0]
    bid(one, auction, line, 8.0)
    auction.status = AuctionStatus.CLOSED
    db.commit()
    award(auction, {line.id: vs[0].id}, {line.id: 8.0})
    db.expire_all()
    db.refresh(auction)
    counted = auction.id in ids(reporting.total_savings(db, start, end, org_id=org.id))
    auction.status = AuctionStatus.CANCELLED
    db.commit()
    db.expire_all()
    still = auction.id in ids(reporting.total_savings(db, start, end, org_id=org.id))
    check("an awarded auction is in the savings report", counted)
    check("...and a cancelled one is not", not still)

    print("\n9. Another organisation's money never appears")
    other = Organisation(name="Someone else")
    db.add(other)
    db.flush()
    db.add(User(name="Their buyer", email="buyer@elsewhere", role=Role.BUYER,
                org_id=other.id, password_hash=hash_password(PW)))
    db.commit()
    theirs = reporting.total_savings(db, start, end, org_id=other.id)
    check("their report is empty", theirs["totals"]["count"] == 0, theirs["totals"])
    check("...while ours is not",
          reporting.total_savings(db, start, end, org_id=org.id)["totals"]["count"] > 0)

    db.close()


# --------------------------------------------------------------------------
# Part two: the awkward shapes, the dashboard and the exports
# --------------------------------------------------------------------------
def part_two():
    db = SessionLocal()
    org = Organisation(name="Round two, part two")
    db.add(org)
    db.flush()
    buyer, unit, items, vs = _people(db, org, "r2b", item_names=("Pens", "Paper"))
    one = login("s0@r2b")
    two = login("s1@r2b")
    boss = login("buyer@r2b")

    def make(specs, tag="", landed_on=False, freight=None):
        now = datetime.utcnow()
        auction = Auction(reference=f"R2b-{now.timestamp()}{tag}", title=f"Auction {tag}",
                          creator_id=buyer.id, org_id=org.id, status=AuctionStatus.LIVE,
                          start_at=now - timedelta(minutes=5), end_at=now + timedelta(hours=2),
                          original_end_at=now + timedelta(hours=2),
                          decrement_type=DecrementType.ABSOLUTE, min_decrement=0.0,
                          compare_landed=landed_on, auto_extend=False, published_at=now)
        db.add(auction)
        db.flush()
        for item, qty, ceiling in specs:
            db.add(AuctionLine(auction_id=auction.id, item_id=item.id, unit_id=unit.id,
                               qty=qty, starting_price=ceiling))
        for i, vendor in enumerate(vs):
            part = Participant(auction_id=auction.id, vendor_id=vendor.id, alias=f"Bidder {i}")
            if freight and i in freight:
                part.bidder_freight = freight[i]
                part.charges_updated_at = now
            db.add(part)
        db.commit()
        db.refresh(auction)
        return auction

    def bid(client, auction, line, price, freight=0, tax=0):
        """A bid as the screen sends it: price, delivery costs and tax together."""
        data = {"line_id": str(line.id), "unit_price": str(price)}
        if auction.compare_landed:
            data.update({"freight": str(freight), "packaging": "0", "other": "",
                         "tax_name": "GST", "tax_percent": str(tax)})
        return client.post(f"/auctions/{auction.id}/bid", data=data,
                           follow_redirects=False)

    def award(auction, picks, prices=None):
        data = {}
        for line_id, vendor_id in picks.items():
            data[f"winner_{line_id}"] = str(vendor_id) if vendor_id else ""
            if prices and line_id in prices:
                data[f"price_{line_id}"] = str(prices[line_id])
        return boss.post(f"/auctions/{auction.id}/award", data=data, follow_redirects=False)

    print("\n10. An item with no ceiling")
    auction = make([(items[0], 100, None)], tag="open")
    line = auction.lines[0]
    bid(one, auction, line, 50.0)
    bid(two, auction, line, 40.0)
    db.expire_all()
    summary = engine.auction_summary(db, auction)
    # No budget, so the worst price offered stands in: 100 x 50 = 5,000
    # against 4,000 spent.
    check("the budget falls back to the worst price offered",
          close(summary["baseline"], 5000, 0.02), summary["baseline"])
    check("...and the saving is measured from there",
          close(summary["savings"], 1000, 0.02), summary["savings"])
    auction.status = AuctionStatus.CLOSED
    db.commit()
    award(auction, {line.id: vs[1].id}, {line.id: 40.0})
    db.expire_all()
    summary = engine.auction_summary(db, auction)
    check("...and it holds after the award", close(summary["savings"], 1000, 0.02),
          summary["savings"])

    print("\n11. An auction nobody bid on")
    auction = make([(items[0], 100, 10.0)], tag="nobids")
    db.expire_all()
    summary = engine.auction_summary(db, auction)
    check("the budget still stands", close(summary["baseline"], 1000), summary["baseline"])
    check("...the spend is the budget", close(summary["final_value"], 1000),
          summary["final_value"])
    check("...and the saving is nothing, not a negative",
          close(summary["savings"], 0), summary["savings"])

    print("\n12. An open item nobody bid on")
    auction = make([(items[0], 100, None)], tag="openbare")
    db.expire_all()
    summary = engine.auction_summary(db, auction)
    check("no budget and no bids means no figures, not a crash",
          close(summary["baseline"], 0) and close(summary["savings"], 0),
          f'{summary["baseline"]}/{summary["savings"]}')
    check("...and the percentage is zero, not an error", close(summary["savings_pct"], 0))

    print("\n13. Savings on a delivered-price auction are the delivered savings")
    delivered = make([(items[0], 100, 100.0)], tag="landed", landed_on=True)
    line = delivered.lines[0]
    # 1,000 to deliver the hundred, quoted on the bid: 80 + 10 = 90 all in.
    bid(one, delivered, line, 80.0, freight=1000)
    db.expire_all()
    summary = engine.auction_summary(db, delivered)
    check("the spend counts the freight", close(summary["final_value"], 9000, 0.02),
          summary["final_value"])
    check("...so the saving is 1,000, not 2,000", close(summary["savings"], 1000, 0.02),
          summary["savings"])
    delivered.status = AuctionStatus.CLOSED
    db.commit()
    award(delivered, {line.id: vs[0].id}, {line.id: 80.0})
    db.expire_all()
    summary = engine.auction_summary(db, delivered)
    check("...and the award books the delivered figure too",
          close(summary["final_value"], 9000, 0.02), summary["final_value"])
    row = engine.line_result(db, line)
    check("the line row agrees with the auction",
          close(row["final_value"], summary["final_value"], 0.02),
          f'{row["final_value"]} vs {summary["final_value"]}')

    print("\n14. The dashboard adds up the auctions")
    page = boss.get("/").text
    awarded = db.query(Auction).filter(Auction.org_id == org.id,
                                       Auction.status == AuctionStatus.AWARDED).all()
    by_hand = sum(engine.auction_summary(db, a)["savings"] for a in awarded)
    shown = re.findall(r'class="v">([^<]+)<', page)
    check("the dashboard names a savings figure", bool(shown), shown[:4])
    tile = re.search(r'(?is)savings.{0,400}?class="v">\s*([^<]+)', page)
    figure = float(re.sub(r"[^\d.]", "", tile.group(1)) or 0) if tile else None
    check("...and it equals the sum of the awarded auctions",
          figure is not None and close(figure, round(by_hand, 2), 1.0),
          f"page {figure} vs hand {by_hand:.2f}")

    print("\n15. The exports carry the same numbers")
    start = datetime.utcnow() - timedelta(days=1)
    end = datetime.utcnow() + timedelta(days=1)
    data = reporting.total_savings(db, start, end, org_id=org.id)
    blob = reporting.savings_csv(data).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(blob)))
    body = [r for r in rows if r and r[0].startswith("R2b-")]
    check("the spreadsheet has a row per auction", len(body) == len(data["rows"]),
          f'{len(body)} vs {len(data["rows"])}')
    if body:
        numbers = [float(x.replace(",", "")) for x in body[0]
                   if re.fullmatch(r"-?[\d,]+\.?\d*", x or "")]
        check("...and its figures are the report's figures",
              any(close(n, data["rows"][0]["savings"], 0.02) for n in numbers),
              f'{numbers} should contain {data["rows"][0]["savings"]}')
    try:
        pdf = reporting.savings_pdf(data)
        check("the PDF builds", pdf[:4] == b"%PDF", pdf[:8])
    except Exception as exc:                           # pragma: no cover
        check("the PDF builds", False, f"{type(exc).__name__}: {exc}")
    try:
        one_report = reporting.auction_summary_report(db, delivered)
        check("the single-auction PDF builds",
              reporting.auction_pdf(one_report)[:4] == b"%PDF")
    except Exception as exc:                           # pragma: no cover
        check("the single-auction PDF builds", False, f"{type(exc).__name__}: {exc}")

    print("\n16. The reports screen itself")
    check("the reports page loads", boss.get("/reports").status_code == 200)
    check("...with a date range",
          boss.get("/reports?date_from=2020-01-01&date_to=2039-12-31").status_code == 200)
    check("...and survives a nonsense date",
          boss.get("/reports?date_from=not-a-date&date_to=2039-12-31"
                   ).status_code in (200, 303, 422))
    check("...and a range that runs backwards",
          boss.get("/reports?date_from=2039-01-01&date_to=2020-01-01"
                   ).status_code in (200, 303, 422))
    check("a single auction's report loads",
          boss.get(f"/reports/auction/{delivered.id}").status_code == 200)
    check("a supplier cannot open the reports", one.get("/reports").status_code in (403, 404),
          one.get("/reports").status_code)
    check("...nor another organisation's auction report",
          one.get(f"/reports/auction/{delivered.id}").status_code in (403, 404))

    db.close()


# --------------------------------------------------------------------------
# Part three: what the award screen must refuse
# --------------------------------------------------------------------------
def part_three():
    db = SessionLocal()
    org = Organisation(name="Round two, part three")
    db.add(org)
    db.flush()
    buyer, unit, items, vs = _people(db, org, "r2c", vendors=3, item_names=("Pens",))
    item = items[0]
    elsewhere = Organisation(name="Elsewhere entirely")
    db.add(elsewhere)
    db.flush()
    outsider = Vendor(name="Outsider", email="outsider@elsewhere", org_id=elsewhere.id)
    db.add(outsider)
    db.commit()
    one = login("s0@r2c")
    two = login("s1@r2c")
    boss = login("buyer@r2c")
    counter = [0]

    def fresh():
        """A closed auction with two bids on it, brand new every time."""
        counter[0] += 1
        now = datetime.utcnow()
        auction = Auction(reference=f"R2c-{counter[0]}", title="Pens", creator_id=buyer.id,
                          org_id=org.id, status=AuctionStatus.LIVE,
                          start_at=now - timedelta(minutes=5), end_at=now + timedelta(hours=2),
                          original_end_at=now + timedelta(hours=2),
                          decrement_type=DecrementType.ABSOLUTE, min_decrement=0.0,
                          compare_landed=False, auto_extend=False, published_at=now)
        db.add(auction)
        db.flush()
        db.add(AuctionLine(auction_id=auction.id, item_id=item.id, unit_id=unit.id,
                           qty=100, starting_price=10.0))
        for i, vendor in enumerate(vs[:2]):
            db.add(Participant(auction_id=auction.id, vendor_id=vendor.id, alias=f"Bidder {i}"))
        db.commit()
        db.refresh(auction)
        line = auction.lines[0]
        one.post(f"/auctions/{auction.id}/bid",
                 data={"line_id": str(line.id), "unit_price": "9"}, follow_redirects=False)
        two.post(f"/auctions/{auction.id}/bid",
                 data={"line_id": str(line.id), "unit_price": "8"}, follow_redirects=False)
        auction.status = AuctionStatus.CLOSED
        db.commit()
        db.refresh(auction)
        return auction, line

    print("\n17. What the award screen refuses")
    who = {"invited": lambda: str(vs[1].id), "uninvited": lambda: str(vs[2].id),
           "outsider": lambda: str(outsider.id), "ghost": lambda: "99999",
           "junk": lambda: "abc"}
    for label, winner, price in [
        ("a price of zero", "invited", "0"),
        ("a negative price", "invited", "-5"),
        ("a price that is words", "invited", "cheap"),
        # 1e13 is ten million crore. The bidding side refuses it, so the award
        # screen has to as well - it is the same slipped finger, and here there
        # is no second party to notice.
        ("an absurd price", "invited", "1e13"),
        ("a price with a comma", "invited", "8,50"),
        ("a bidder who was never invited", "uninvited", "8"),
        ("a bidder from another company", "outsider", "8"),
        ("a bidder that does not exist", "ghost", "8"),
        ("a made-up bidder id", "junk", "8"),
    ]:
        auction, line = fresh()
        response = boss.post(f"/auctions/{auction.id}/award",
                             data={f"winner_{line.id}": who[winner](),
                                   f"price_{line.id}": price}, follow_redirects=False)
        db.expire_all()
        made = db.query(Award).filter_by(auction_id=auction.id).all()
        db.refresh(auction)
        check(f"{label} is refused",
              len(made) == 0 and auction.status != AuctionStatus.AWARDED,
              f"{len(made)} award(s), {auction.status.value}, http {response.status_code}"
              + (f", booked {made[0].unit_price}" if made else ""))

    print("\n18. Awarding nobody at all")
    auction, line = fresh()
    boss.post(f"/auctions/{auction.id}/award", data={f"winner_{line.id}": ""},
              follow_redirects=False)
    db.expire_all()
    db.refresh(auction)
    check("no award row is written",
          db.query(Award).filter_by(auction_id=auction.id).count() == 0)
    check("...and the auction is not called awarded",
          auction.status != AuctionStatus.AWARDED, auction.status.value)

    print("\n19. A supplier cannot award")
    auction, line = fresh()
    response = one.post(f"/auctions/{auction.id}/award",
                        data={f"winner_{line.id}": str(vs[0].id), f"price_{line.id}": "1"},
                        follow_redirects=False)
    db.expire_all()
    check("a bidder is refused", response.status_code in (403, 404), response.status_code)
    check("...and nothing was written",
          db.query(Award).filter_by(auction_id=auction.id).count() == 0)

    print("\n20. Awarding a cancelled auction")
    auction, line = fresh()
    auction.status = AuctionStatus.CANCELLED
    db.commit()
    boss.post(f"/auctions/{auction.id}/award",
              data={f"winner_{line.id}": str(vs[1].id), f"price_{line.id}": "8"},
              follow_redirects=False)
    db.expire_all()
    check("it is refused", db.query(Award).filter_by(auction_id=auction.id).count() == 0)

    db.close()


def main():
    part_one()
    part_two()
    part_three()
    print("\n" + "-" * 64)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All award, savings and report checks passed.")
    return 0


def test_award_money():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
