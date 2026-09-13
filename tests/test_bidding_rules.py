"""Round 1 of the deep check: the bidding engine, the part that decides money.

Every check here was written by trying to break the engine rather than to
confirm it. The centrepiece is section A: for a large number of randomly
shaped auctions it takes the price the *screen* offers the bidder and proves
the *engine* accepts exactly that and refuses a paisa outside it. A mismatch
there is the worst class of bug this app can have - the platform inviting a
bid and then refusing it, or accepting one that breaks the buyer's own rule.

    python tests/test_bidding_rules.py
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="ra-bidding-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ.pop("RA_SMTP_HOST", None)

from fastapi.testclient import TestClient           # noqa: E402

from app import engine, scheduler                   # noqa: E402
from app.db import Base, SessionLocal, engine as db_engine    # noqa: E402
from app.main import app                            # noqa: E402
from app.models import (Auction, AuctionLine, AuctionStatus, Bid, DecrementType,  # noqa: E402
                        Item, LineTax, Organisation, Participant, Role, Unit,
                        User, Vendor)
from app.security import hash_password              # noqa: E402

Base.metadata.create_all(bind=db_engine)
PW = "test1234"
FAILS: list[str] = []


def check(label, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


class Client(TestClient):
    def post(self, url, **kwargs):
        token = self.cookies.get("ra_csrf")
        if token:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CSRF-Token", token)
            kwargs["headers"] = headers
        return super().post(url, **kwargs)


def login(email):
    c = Client(app, base_url="http://test")
    c.get("/login")
    c.post("/login", data={"email": email, "password": PW, "next": "/"},
           follow_redirects=False)
    c.headers.update({"accept": "text/html,application/xhtml+xml"})
    return c


def flash_of(response) -> str:
    header = response.headers.get("set-cookie", "")
    if "ra_flash" not in header:
        return ""
    jar = SimpleCookie()
    jar.load(header)
    try:
        return json.loads(jar["ra_flash"].value)["m"]
    except Exception:
        return ""


def main() -> int:                                                   # noqa: C901
    db = SessionLocal()
    org = Organisation(name="Bidding Ltd")
    db.add(org)
    db.flush()
    buyer = User(name="Buyer", email="buyer@b.local", role=Role.BUYER, org_id=org.id,
                 password_hash=hash_password(PW))
    db.add(buyer)
    unit = Unit(code="NOS", org_id=org.id)
    item = Item(name="Widget", org_id=org.id)
    db.add_all([unit, item])
    db.flush()
    vendors, users = [], []
    for i in range(3):
        vendor = Vendor(name=f"Supplier {i}", email=f"v{i}@b.local", org_id=org.id)
        db.add(vendor)
        db.flush()
        user = User(name=f"Bidder {i}", email=f"v{i}@b.local", role=Role.VENDOR,
                    org_id=org.id, vendor_id=vendor.id, password_hash=hash_password(PW))
        db.add(user)
        db.flush()
        vendors.append(vendor)
        users.append(user)
    db.commit()
    clients = [login(f"v{i}@b.local") for i in range(3)]

    def build(**kw):
        now = datetime.utcnow()
        auction = Auction(
            reference=f"RA-B-{now.timestamp():.6f}{kw.get('tag', '')}", title="Bidding",
            creator_id=buyer.id, org_id=org.id,
            status=kw.get("status", AuctionStatus.LIVE),
            start_at=now + timedelta(minutes=kw.get("starts", -5)),
            end_at=now + timedelta(minutes=kw.get("minutes", 120)),
            original_end_at=now + timedelta(minutes=120),
            decrement_type=kw.get("dtype", DecrementType.ABSOLUTE),
            min_decrement=kw.get("min_dec", 0.0), max_decrement=kw.get("max_dec", 0.0),
            compare_landed=kw.get("landed", False),
            auto_extend=kw.get("auto_extend", False),
            extend_trigger_seconds=kw.get("trigger", 120),
            extend_by_seconds=kw.get("by", 180),
            max_extensions=kw.get("max_ext", 3), published_at=now)
        db.add(auction)
        db.flush()
        line = AuctionLine(auction_id=auction.id, item_id=item.id, unit_id=unit.id,
                           qty=kw.get("qty", 10), starting_price=kw.get("ceiling", 100.0))
        db.add(line)
        db.flush()
        for index, vendor in enumerate(vendors[:kw.get("invited", 3)]):
            part = Participant(auction_id=auction.id, vendor_id=vendor.id,
                               alias=f"Bidder {chr(65 + index)}")
            if kw.get("freight"):
                part.bidder_freight = kw["freight"]
                part.charges_updated_at = now
            db.add(part)
        if kw.get("tax"):
            for vendor in vendors:
                db.add(LineTax(auction_id=auction.id, line_id=line.id, vendor_id=vendor.id,
                               name="GST", percent=kw["tax"]))
        db.commit()
        db.refresh(auction)
        return auction, auction.lines[0]

    def place(auction, line, user, price):
        """Bid through the engine. Returns the refusal, or None if accepted."""
        try:
            engine.place_bid(db, auction, line, user, price)
            return None
        except engine.BidError as exc:
            db.rollback()
            return str(exc)
        except Exception as exc:                     # anything else is a defect
            db.rollback()
            return f"CRASH {type(exc).__name__}: {exc}"

    def post_bid(client, auction, line, price):
        return client.post(f"/auctions/{auction.id}/bid",
                           data={"line_id": str(line.id), "unit_price": str(price)},
                           follow_redirects=False)

    # ------------------------------------------------------------------ A
    print("\nA. The price the screen offers is the price the engine takes")
    random.seed(11)
    tested = offered_refused = over_accepted = floor_refused = crashes = 0
    for _ in range(60):
        ceiling = random.choice([None, 10.0, 99.99, 100.0, 1000.0, 12.5])
        qty = random.choice([1, 10, 100, 3.5])
        dtype = random.choice([DecrementType.ABSOLUTE, DecrementType.PERCENT])
        min_dec = random.choice([0.0, 0.01, 0.5, 1.0, 5.0])
        max_dec = random.choice([0.0, 0.0, 5.0, 10.0])
        landed = random.choice([False, True])
        shape = dict(ceiling=ceiling, qty=qty, dtype=dtype, min_dec=min_dec,
                     max_dec=max(max_dec, min_dec) if max_dec else 0.0, landed=landed,
                     freight=random.choice([0.0, 100.0]) if landed else 0.0,
                     tax=random.choice([0.0, 18.0]) if landed else 0.0)
        auction, line = build(**shape)
        opening = (ceiling or 500.0) * (0.4 if shape["freight"] else 1.0)
        if place(auction, line, users[0], round(max(opening, 0.02), 2)) is not None:
            continue
        db.expire_all()
        auction = db.get(Auction, auction.id)
        line = db.get(AuctionLine, line.id)
        window = engine.bid_window(db, auction, line, vendors[1].id)
        if window.exhausted or window.max_allowed is None:
            continue
        tested += 1
        said = place(auction, line, users[1], window.max_allowed)
        if said is not None:
            offered_refused += 1
            crashes += "CRASH" in said
            continue
        # a fresh copy of the same shape, to test the paisa above
        auction2, line2 = build(**shape)
        place(auction2, line2, users[0], round(max(opening, 0.02), 2))
        db.expire_all()
        auction2 = db.get(Auction, auction2.id)
        line2 = db.get(AuctionLine, line2.id)
        window2 = engine.bid_window(db, auction2, line2, vendors[1].id)
        if window2.max_allowed is None:
            continue
        over = place(auction2, line2, users[1], round(window2.max_allowed + 0.01, 2))
        if over is None:
            over_accepted += 1
        elif "CRASH" in over:
            crashes += 1
        if window2.max_step and window2.min_allowed <= window2.max_allowed:
            auction3, line3 = build(**shape)
            place(auction3, line3, users[0], round(max(opening, 0.02), 2))
            db.expire_all()
            auction3 = db.get(Auction, auction3.id)
            line3 = db.get(AuctionLine, line3.id)
            window3 = engine.bid_window(db, auction3, line3, vendors[1].id)
            if window3.max_allowed is not None and place(
                    auction3, line3, users[1], window3.min_allowed) is not None:
                floor_refused += 1

    check(f"across {tested} differently shaped auctions, the offered price is accepted",
          offered_refused == 0, f"{offered_refused} refused")
    check("...a paisa above it is always refused", over_accepted == 0,
          f"{over_accepted} accepted")
    check("...the lowest offered price is accepted too", floor_refused == 0,
          f"{floor_refused} refused")
    check("...and nothing ever crashed", crashes == 0, crashes)

    # ------------------------------------------------------------------ B
    print("\nB. Lower always wins; equal and higher never do")
    auction, line = build(tag="order")
    place(auction, line, users[0], 90.0)
    check("an equal price is refused",
          place(auction, line, users[1], 90.0) is not None)
    check("a paisa lower is accepted",
          place(auction, line, users[1], 89.99) is None)
    db.expire_all()
    check("...and takes the lead", engine.best_bid(db, line.id).vendor_id == vendors[1].id)
    check("a higher price is refused",
          place(auction, line, users[2], 91.0) is not None)

    # ------------------------------------------------------------------ C
    print("\nC. When bidding is not allowed")
    for status in (AuctionStatus.DRAFT, AuctionStatus.SCHEDULED, AuctionStatus.CLOSED,
                   AuctionStatus.AWARDED, AuctionStatus.CANCELLED):
        shut, shut_line = build(status=status, tag=status.value)
        refused = place(shut, shut_line, users[0], 50.0)
        check(f"no bidding on a {status.value} auction",
              refused is not None and "CRASH" not in refused, (refused or "ACCEPTED")[:48])
    private, private_line = build(invited=2, tag="inv")
    refused = place(private, private_line, users[2], 50.0)
    check("a supplier who was not invited is refused",
          refused is not None and "CRASH" not in refused, (refused or "ACCEPTED")[:60])
    late, late_line = build(minutes=-1, tag="late")
    refused = place(late, late_line, users[0], 50.0)
    check("no bidding after the closing time, even before the clock notices",
          refused is not None and "CRASH" not in refused, (refused or "ACCEPTED")[:60])

    # ------------------------------------------------------------------ D
    print("\nD. Withdrawing")
    gone, gone_line = build(tag="wd")
    place(gone, gone_line, users[0], 80.0)
    db.expire_all()
    mine = engine.vendor_best(db, gone_line.id, vendors[0].id)
    engine.withdraw_bid(db, mine, users[0], "changed my mind")
    db.expire_all()
    check("a withdrawn bid leaves the ranking", engine.best_bid(db, gone_line.id) is None)
    # A bid that has been taken back is not an offer any more, so it does not
    # hold the bidder down to it: they may bid again anywhere under the
    # ceiling. What they cannot do is quietly walk an offer back up - only the
    # most recent submission can be taken back, the one underneath it stands
    # again, and the buyer is told each time.
    check("...and the bidder may bid again, the taken-back price no longer binding them",
          place(gone, gone_line, users[0], 85.0) is None)
    check("...but never above the buyer's starting price",
          place(gone, gone_line, users[0], 101.0) is not None)
    check("...and a standing bid still holds them down",
          place(gone, gone_line, users[0], 90.0) is not None)
    check("...while going lower is fine", place(gone, gone_line, users[0], 79.0) is None)
    db.expire_all()
    theirs = engine.vendor_best(db, gone_line.id, vendors[0].id)
    try:
        engine.withdraw_bid(db, theirs, users[1], "not mine")
        db.rollback()
        blocked = False
    except engine.BidError:
        db.rollback()
        blocked = True
    check("one supplier cannot withdraw another's bid", blocked)

    # ------------------------------------------------------------------ E
    print("\nE. The closing clock")
    stretch, stretch_line = build(auto_extend=True, minutes=1, trigger=120, by=60,
                                  max_ext=2, tag="ext")
    was = stretch.end_at
    place(stretch, stretch_line, users[0], 90.0)
    db.expire_all()
    db.refresh(stretch)
    check("a bid in the closing window pushes the clock back", stretch.end_at > was,
          f"{stretch.extensions_used} used")
    place(stretch, stretch_line, users[1], 89.0)
    place(stretch, stretch_line, users[2], 88.0)
    db.expire_all()
    db.refresh(stretch)
    check("...but never more often than the buyer allowed",
          stretch.extensions_used <= stretch.max_extensions, stretch.extensions_used)

    waiting, _ = build(status=AuctionStatus.SCHEDULED, starts=-1, tag="open")
    finished, finished_line = build(minutes=-1, tag="shut")
    scheduler.tick()
    db.expire_all()
    db.refresh(waiting)
    db.refresh(finished)
    check("an auction whose time has come is opened", waiting.status == AuctionStatus.LIVE,
          waiting.status)
    check("one past its end time is closed", finished.status == AuctionStatus.CLOSED,
          finished.status)
    scheduler.tick()
    db.expire_all()
    db.refresh(finished)
    check("...and running the clock again changes nothing",
          finished.status == AuctionStatus.CLOSED)

    # ------------------------------------------------------------------ F
    print("\nF. Prices that are not prices")
    hostile, hostile_line = build(tag="junk")
    for label, value in [("zero", 0.0), ("negative", -5.0), ("not a number", float("nan")),
                         ("infinite", float("inf")), ("absurdly large", 1e13)]:
        refused = place(hostile, hostile_line, users[0], value)
        check(f"a {label} price is refused",
              refused is not None and "CRASH" not in refused, (refused or "ACCEPTED")[:50])
    check("a price with too many decimals is rounded to the paisa",
          place(hostile, hostile_line, users[0], 12.34567) is None)
    db.expire_all()
    stored = engine.vendor_best(db, hostile_line.id, vendors[0].id)
    check("...and stored that way, not raw", stored.unit_price == 12.35, stored.unit_price)
    for junk in ("", "  ", "abc", "9,9", "12.5.5", "1e5000"):
        response = post_bid(clients[1], hostile, hostile_line, junk)
        check(f"the form refuses {junk!r} without breaking",
              response.status_code in (200, 303), response.status_code)

    # ------------------------------------------------------------------ G
    print("\nG. Two bids in the same instant")
    race, race_line = build(min_dec=1.0, tag="race")
    place(race, race_line, users[0], 100.0)
    db.expire_all()
    outcomes: dict[int, str] = {}

    def racer(index, price):
        session = SessionLocal()
        try:
            auction_row = session.get(Auction, race.id)
            line_row = session.get(AuctionLine, race_line.id)
            user_row = session.get(User, users[index].id)
            try:
                engine.place_bid(session, auction_row, line_row, user_row, price)
                outcomes[index] = "accepted"
            except engine.BidError as exc:
                outcomes[index] = f"refused: {exc}"
            except Exception as exc:
                outcomes[index] = f"CRASH {type(exc).__name__}: {exc}"
        finally:
            session.close()

    threads = [threading.Thread(target=racer, args=(1, 99.0)),
               threading.Thread(target=racer, args=(2, 99.0))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    db.expire_all()
    accepted = [index for index, said in outcomes.items() if said == "accepted"]
    check("only one of two identical prices can be taken", len(accepted) == 1, outcomes)
    check("...and neither crashed",
          not any("CRASH" in said for said in outcomes.values()), outcomes)

    # ------------------------------------------------------------------ H
    print("\nH. Odd shapes")
    tiny, tiny_line = build(dtype=DecrementType.PERCENT, min_dec=0.5, ceiling=1.0, tag="pc")
    place(tiny, tiny_line, users[0], 0.50)
    db.expire_all()
    window = engine.bid_window(db, tiny, tiny_line, vendors[1].id)
    check("a percentage step smaller than a paisa is still a real step",
          window.min_step >= 0.01, window.min_step)
    check("...so an equal price is still refused",
          place(tiny, tiny_line, users[1], 0.50) is not None)

    open_line_auction, open_line = build(ceiling=None, min_dec=1.0, tag="noceil")
    window = engine.bid_window(db, open_line_auction, open_line, vendors[0].id)
    check("with no ceiling the first bidder names their own price", window.open_ended)
    place(open_line_auction, open_line, users[0], 5000.0)
    db.expire_all()
    window = engine.bid_window(db, open_line_auction, open_line, vendors[1].id)
    check("...and the next bidder gets a real limit from it",
          window.max_allowed is not None and abs(window.max_allowed - 4999.0) < 0.011,
          window.max_allowed)

    # ------------------------------------------------------------------ I
    print("\nI. The bidding screen's own numbers")
    shown, shown_line = build(min_dec=1.0, max_dec=20.0, tag="screen")
    post_bid(clients[0], shown, shown_line, 90.0)
    db.expire_all()
    check("the first bid landed", engine.best_bid(db, shown_line.id) is not None)
    window = engine.bid_window(db, shown, shown_line, vendors[1].id)
    page = clients[1].get(f"/auctions/{shown.id}").text
    highest = re.search(r'data-max="([\d.]+)"', page)
    lowest = re.search(r'data-min="([\d.]+)"', page)
    chip = re.search(r'data-fill="([\d.]+)"', page)
    check("the highest price the page allows is the engine's",
          highest and abs(float(highest.group(1)) - window.max_allowed) < 0.0011,
          f"{highest.group(1) if highest else None} vs {window.max_allowed}")
    check("the lowest price the page allows is the engine's",
          lowest and abs(float(lowest.group(1)) - window.min_allowed) < 0.0011,
          f"{lowest.group(1) if lowest else None} vs {window.min_allowed}")
    check("the one-click suggestion is the same number again",
          chip and abs(float(chip.group(1)) - window.max_allowed) < 0.0011,
          chip.group(1) if chip else "none")
    response = post_bid(clients[1], shown, shown_line, highest.group(1))
    check("...and bidding exactly that is accepted", "L1" in flash_of(response),
          flash_of(response)[:60])
    response = post_bid(clients[2], shown, shown_line, round(window.max_allowed + 0.01, 2))
    check("...while a paisa more is refused", "Too high" in flash_of(response),
          flash_of(response)[:60])

    db.close()
    print("\n" + "-" * 64)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All bidding-engine checks passed.")
    return 0


def test_bidding_rules():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
