"""Delivered cost the way a supplier quotes it: the Enhanced behaviour.

Everything here is new in this version, and all of it is arithmetic somebody
will be paid on, so it is checked against figures worked out by hand rather
than against the code's own opinion.

What is being asserted:

* A bidder gives **one** freight, packaging and other-costs figure for the
  whole auction - not per item, not per unit - and it is **shared across the
  items they priced** in proportion to what each is worth.
* Taxes are **per item**, as many as apply, each a percentage; the amounts are
  worked out from the item's delivered value, never typed.
* Ranking, the ceiling, the decrement rules and the award all run on the
  **all-in** price: bid + share of delivery + tax.
* One bidder's numbers never move another's.

    python tests/test_enhanced.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="ra-enhanced-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ.pop("RA_SMTP_HOST", None)

from fastapi.testclient import TestClient           # noqa: E402

from app import engine, landed, mailer              # noqa: E402
from app.db import Base, SessionLocal, engine as db_engine   # noqa: E402
from app.main import app                            # noqa: E402
from app.models import (Auction, AuctionLine, AuctionStatus, Award, Bid,  # noqa: E402
                        DecrementType, EmailMessage, Item, LineTax, Organisation,
                        Participant, Role, Unit, User, Vendor)
from app.security import hash_password              # noqa: E402

Base.metadata.create_all(bind=db_engine)
PW = "test1234"
BROWSER = {"accept": "text/html,application/xhtml+xml"}
FAILS: list[str] = []


def check(label, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


def close(a, b, tol=0.011):
    return abs(float(a) - float(b)) <= tol


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
    c.headers.update(BROWSER)
    return c


def flash_of(response) -> str:
    import json
    from http.cookies import SimpleCookie
    header = response.headers.get("set-cookie", "")
    if "ra_flash" not in header:
        return ""
    jar = SimpleCookie()
    jar.load(header)
    try:
        return json.loads(jar["ra_flash"].value)["m"]
    except Exception:
        return ""


def make_auction(db, buyer, vendors, specs, *, landed_on=True, min_dec=0.0,
                 title="Delivered", minutes=90):
    """``specs`` is a list of (item, unit, qty, ceiling)."""
    now = datetime.utcnow()
    auction = Auction(reference=f"RA-E-{now.timestamp():.6f}", title=title,
                      creator_id=buyer.id, org_id=buyer.org_id, status=AuctionStatus.LIVE,
                      start_at=now - timedelta(minutes=5),
                      end_at=now + timedelta(minutes=minutes),
                      original_end_at=now + timedelta(minutes=minutes),
                      decrement_type=DecrementType.ABSOLUTE, min_decrement=min_dec,
                      compare_landed=landed_on, auto_extend=False,
                      published_at=now - timedelta(hours=1))
    db.add(auction)
    db.flush()
    for item, unit, qty, ceiling in specs:
        db.add(AuctionLine(auction_id=auction.id, item_id=item.id, unit_id=unit.id,
                           qty=qty, starting_price=ceiling))
    for index, vendor in enumerate(vendors):
        db.add(Participant(auction_id=auction.id, vendor_id=vendor.id,
                           alias=f"Bidder {chr(65 + index)}"))
    db.commit()
    db.refresh(auction)
    return auction


def bid(client, auction, line, price):
    return client.post(f"/auctions/{auction.id}/bid",
                       data={"line_id": str(line.id), "unit_price": str(price)},
                       follow_redirects=False)


def set_charges(client, auction, freight=0, packaging=0, other=0, label=""):
    return client.post(f"/auctions/{auction.id}/charges",
                       data={"freight": str(freight), "packaging": str(packaging),
                             "other": str(other), "other_label": label},
                       follow_redirects=False)


def set_taxes(client, auction, line, rows):
    # Repeated form fields, exactly as the browser sends one row per tax.
    data = {"tax_name": [name for name, _ in rows] or [""],
            "tax_percent": [str(percent) for _, percent in rows] or [""]}
    return client.post(f"/auctions/{auction.id}/lines/{line.id}/taxes", data=data,
                       follow_redirects=False)


def main() -> int:                                                   # noqa: C901
    db = SessionLocal()
    org = Organisation(name="Enhanced Ltd")
    db.add(org)
    db.flush()
    buyer = User(name="Buyer", email="buyer@e.local", role=Role.BUYER, org_id=org.id,
                 password_hash=hash_password(PW))
    db.add(buyer)
    unit = Unit(code="NOS", org_id=org.id)
    pens = Item(name="Pens", org_id=org.id)
    paper = Item(name="Paper", org_id=org.id)
    rope = Item(name="Rope", org_id=org.id)
    db.add_all([unit, pens, paper, rope])
    db.flush()
    vendors = []
    for i in (1, 2, 3):
        v = Vendor(name=f"Supplier {i}", email=f"s{i}@e.local", org_id=org.id)
        db.add(v)
        db.flush()
        db.add(User(name=f"Bidder {i}", email=f"s{i}@e.local", role=Role.VENDOR,
                    org_id=org.id, vendor_id=v.id, password_hash=hash_password(PW)))
        vendors.append(v)
    db.commit()
    buyer_c = login("buyer@e.local")
    one, two, three = (login(f"s{i}@e.local") for i in (1, 2, 3))

    # ------------------------------------------------------------------ 1
    print("\n1. Freight is quoted once, for the whole auction")
    # Two items: pens are worth 10,000 at the ceiling, paper 40,000. So a
    # freight bill should split one to four.
    auction = make_auction(db, buyer, vendors,
                           [(pens, unit, 1000, 10.0), (paper, unit, 200, 200.0)])
    pens_line, paper_line = auction.lines[0], auction.lines[1]

    page = one.get(f"/auctions/{auction.id}").text
    check("the bidder is asked for freight, packaging and other costs",
          all(word in page for word in ("Freight", "Packaging", "Other costs")))
    check("...once, for the whole auction, not per item",
          "for the whole\n        auction" in page or "whole auction" in page)
    check("...and is warned while it is still blank",
          "have not filled these in yet" in page)

    response = set_charges(one, auction, freight=4000, packaging=800, other=200,
                           label="Unloading")
    told = flash_of(response)
    check("saving them says what was understood",
          "5,000" in told and "Freight" in told and "Unloading" in told, told[:90])

    charges = landed.charges_for(db, auction, vendors[0].id)
    check("the three figures are kept apart", close(charges.freight, 4000)
          and close(charges.packaging, 800) and close(charges.other, 200))
    check("...and add up to one number", close(charges.total, 5000), charges.total)

    # ------------------------------------------------------------------ 2
    print("\n2. It is shared across the items, by what each is worth")
    bid(one, auction, pens_line, 9.0)          # 9,000
    bid(one, auction, paper_line, 180.0)       # 36,000
    db.expire_all()
    shares = landed.shares_for(db, auction, vendors[0].id)
    # Ceilings are the weights: 10,000 and 40,000 - one fifth and four fifths.
    check("the cheaper item carries the smaller share",
          close(shares[pens_line.id], 1000), shares.get(pens_line.id))
    check("...and the dearer item the larger", close(shares[paper_line.id], 4000),
          shares.get(paper_line.id))
    check("the shares add up to exactly what was quoted",
          close(sum(shares.values()), 5000), sum(shares.values()))

    quote = landed.breakdown(db, auction, pens_line, vendors[0].id, 9.0)
    check("an item's delivered total is its bid plus its share",
          close(quote.bare_total, 9000) and close(quote.charge_share, 1000)
          and close(quote.delivered_total, 10000), quote)

    # ------------------------------------------------------------------ 3
    print("\n3. A share does not move as the bidder works through the items")
    # Sharing only across what had been priced so far made the FIRST bid carry
    # the whole freight bill, so the same bidder was refused on a small item
    # and accepted on a large one depending only on which they typed first.
    solo = make_auction(db, buyer, vendors,
                        [(pens, unit, 100, 10.0), (paper, unit, 100, 10.0),
                         (rope, unit, 100, 10.0)], title="Three equal items")
    set_charges(two, solo, freight=90)
    db.expire_all()
    before = landed.shares_for(db, solo, vendors[1].id)
    check("three items of equal value take an equal share, before any bid",
          all(close(before[line.id], 30) for line in solo.lines), before)
    check("...and the shares still add up to the whole bill",
          close(sum(before.values()), 90), sum(before.values()))
    first = bid(two, solo, solo.lines[0], 9.0)
    check("the first bid is accepted — its share is a third, not the lot",
          "L1" in flash_of(first), flash_of(first)[:70])
    db.expire_all()
    after = landed.shares_for(db, solo, vendors[1].id)
    check("...and pricing it changed nobody's share, including their own",
          after == before, after)
    placed = engine.vendor_best(db, solo.lines[0].id, vendors[1].id)
    check("the delivered price is the bid plus that share, per unit",
          close(placed.landed_unit_price, 9 + 30 / 100), placed.landed_unit_price)
    bid(two, solo, solo.lines[1], 9.0)
    db.expire_all()
    check("a second item is priced on exactly the same footing",
          close(engine.vendor_best(db, solo.lines[1].id, vendors[1].id).landed_unit_price,
                9.3))

    # ------------------------------------------------------------------ 4
    print("\n4. Taxes: the rate is typed, the money is worked out")
    # Its own auction, with room under the ceiling for tax: 1,000 pens at a
    # ceiling of 20, freight of 5,000 (5 a unit), a bid of 9 - so 14 delivered
    # and 16.80 once 20% of tax is on it.
    taxes_auction = make_auction(db, buyer, vendors, [(pens, unit, 1000, 20.0)],
                                 title="Taxed properly")
    tax_line = taxes_auction.lines[0]
    set_charges(one, taxes_auction, freight=5000)
    bid(one, taxes_auction, tax_line, 9.0)
    response = set_taxes(one, taxes_auction, tax_line, [("GST", 18), ("Cess", 2)])
    told = flash_of(response)
    check("both taxes are saved", "GST 18%" in told and "Cess 2%" in told, told[:90])
    db.expire_all()
    quote = landed.breakdown(db, taxes_auction, tax_line, vendors[0].id, 9.0)
    check("the delivered value is the bid plus the freight share",
          close(quote.bare_total, 9000) and close(quote.charge_share, 5000)
          and close(quote.delivered_total, 14000), quote)
    check("each amount is worked out from that delivered value",
          close(quote.taxes[0][2], 2520) and close(quote.taxes[1][2], 280), quote.taxes)
    check("...and the all-in total is delivered plus tax",
          close(quote.tax_total, 2800) and close(quote.all_in_total, 16800), quote)
    check("the bid's ranked price is the all-in price per unit",
          close(engine.compare_price(engine.vendor_best(db, tax_line.id, vendors[0].id)),
                16.8))

    response = set_taxes(one, taxes_auction, tax_line, [("Nonsense", 250)])
    check("a rate of 250% is refused, in words",
          "not a rate anybody charges" in flash_of(response), flash_of(response)[:70])
    check("...and the sensible taxes are still there",
          landed.tax_rate(landed.taxes_for(db, tax_line.id, vendors[0].id)) == 20)
    response = set_taxes(one, taxes_auction, tax_line, [("GST", "eighteen")])
    check("a rate typed as words is refused too",
          "is not a percentage" in flash_of(response), flash_of(response)[:70])
    check("...and still nothing was changed",
          landed.tax_rate(landed.taxes_for(db, tax_line.id, vendors[0].id)) == 20)

    response = set_taxes(one, taxes_auction, tax_line, [])
    check("clearing the taxes is allowed and says so",
          "no taxes on" in flash_of(response), flash_of(response)[:70])
    db.expire_all()
    check("...and the ranked price drops back to the delivered price",
          close(engine.compare_price(engine.vendor_best(db, tax_line.id, vendors[0].id)),
                14.0))
    set_taxes(one, taxes_auction, tax_line, [("GST", 18), ("Cess", 2)])

    # ------------------------------------------------------------------ 5
    print("\n5. The cheapest bid does not always win — and should not")
    near = make_auction(db, vendors and buyer, vendors,
                        [(pens, unit, 1000, 100.0)], title="Near and far")
    line = near.lines[0]
    set_charges(one, near, freight=0)                 # next door
    set_charges(two, near, freight=12000)             # three states away
    # The far-away bidder opens at a keener headline price; the one next door
    # then beats them on the delivered price without matching that headline.
    near_two = bid(two, near, line, 80.0)             # 80 + 12 freight = 92 delivered
    near_one = bid(one, near, line, 90.0)             # 90, nothing on top
    check("the far-away bidder opens, under the ceiling once their freight is in",
          "L" in flash_of(near_two), flash_of(near_two)[:90])
    check("the nearby bidder takes the lead at a higher headline price",
          "L1" in flash_of(near_one), flash_of(near_one)[:90])
    db.expire_all()
    ranked = engine.best_per_vendor(db, line.id)
    check("the bidder whose delivered cost is lower ranks first, not the cheaper price",
          ranked[0].vendor_id == vendors[0].id, [b.unit_price for b in ranked])
    check("...and the headline prices are the other way round",
          ranked[0].unit_price > ranked[1].unit_price)
    check("the all-in price is what the ranking used",
          close(engine.compare_price(ranked[0]), 90) and close(engine.compare_price(ranked[1]), 92),
          [engine.compare_price(b) for b in ranked])

    print("   taxes change the answer as well")
    taxed = make_auction(db, buyer, vendors, [(pens, unit, 100, 120.0)],
                         title="Taxed", minutes=90)
    tline = taxed.lines[0]
    # Declared before bidding, which is the order the screen asks for: a tax
    # added afterwards that would breach the ceiling is refused (section 7).
    set_taxes(one, taxed, tline, [("GST", 5)])        # 90 -> 94.50
    set_taxes(two, taxed, tline, [("GST", 18)])       # 85 -> 100.30
    bid(two, taxed, tline, 85.0)
    bid(one, taxed, tline, 90.0)
    db.expire_all()
    ranked = engine.best_per_vendor(db, tline.id)
    check("the bidder in the lower tax slab wins on the all-in price",
          ranked[0].vendor_id == vendors[0].id, [engine.compare_price(b) for b in ranked])
    check("...at the figure worked out by hand",
          close(engine.compare_price(ranked[0]), 94.5)
          and close(engine.compare_price(ranked[1]), 100.3),
          [engine.compare_price(b) for b in ranked])

    # ------------------------------------------------------------------ 6
    print("\n6. Changing your costs re-ranks your bids at once")
    before = engine.compare_price(engine.vendor_best(db, line.id, vendors[1].id))
    set_charges(two, near, freight=2000)
    db.expire_all()
    after = engine.compare_price(engine.vendor_best(db, line.id, vendors[1].id))
    check("cutting the freight cuts the delivered price",
          close(before, 92) and close(after, 82), f"{before} -> {after}")
    ranked = engine.best_per_vendor(db, line.id)
    check("...and the ranking follows immediately",
          ranked[0].vendor_id == vendors[1].id, [b.vendor_id for b in ranked])
    mine = engine.vendor_best(db, line.id, vendors[0].id)
    check("the other bidder's price is untouched by all of it",
          close(engine.compare_price(mine), 90) and close(mine.unit_price, 90))

    # ------------------------------------------------------------------ 7
    print("\n7. The ceiling and the decrements are the all-in price")
    tight = make_auction(db, buyer, vendors, [(pens, unit, 100, 100.0)],
                         title="Tight ceiling", min_dec=1.0)
    tline = tight.lines[0]
    set_charges(three, tight, freight=1000)           # 10 per unit over 100 units
    response = bid(three, tight, tline, 95.0)         # 95 + 10 = 105 delivered
    told = flash_of(response)
    check("a bid whose delivered price breaks the ceiling is refused",
          "above the starting price" in told, told[:90])
    check("...and the message shows the delivered arithmetic",
          "delivered" in told.lower(), told[:120])
    response = bid(three, tight, tline, 90.0)         # 90 + 10 = 100, exactly the ceiling
    check("a bid that lands exactly on the ceiling is accepted",
          "L1" in flash_of(response), flash_of(response)[:60])

    response = set_taxes(three, tight, tline, [("GST", 18)])
    told = flash_of(response)
    check("adding tax that would push an accepted bid over the ceiling is refused",
          "cannot be saved as they stand" in told, told[:120])
    check("...naming the item and the arithmetic",
          "Pens" in told and "above the buyer" in told, told[:160])
    db.expire_all()
    standing = engine.vendor_best(db, tline.id, vendors[2].id)
    check("...and the bid is left exactly as it was, still under the ceiling",
          close(engine.compare_price(standing), 100.0), engine.compare_price(standing))

    print("   declaring the tax first is the way round it")
    fresh = make_auction(db, buyer, vendors, [(pens, unit, 100, 100.0)],
                         title="Tax declared first", min_dec=1.0)
    fline = fresh.lines[0]
    set_charges(three, fresh, freight=1000)           # 10 per unit
    set_taxes(three, fresh, fline, [("GST", 18)])
    db.expire_all()
    window = engine.bid_window(db, fresh, fline, vendors[2].id)
    # (100 / 1.18) - 10 = 74.74 to the paisa, rounded down so it lands under.
    check("the suggested price allows for freight and tax together",
          close(window.max_allowed, 74.74), window.max_allowed)
    response = bid(three, fresh, fline, window.max_allowed)
    check("...and that exact price is accepted", "L1" in flash_of(response),
          flash_of(response)[:70])
    db.expire_all()
    best = engine.vendor_best(db, fline.id, vendors[2].id)
    check("...landing at or under the ceiling, all in",
          engine.compare_price(best) <= 100.0, engine.compare_price(best))
    cut = landed.breakdown(db, fresh, fline, vendors[2].id, best.unit_price)
    check("...and the breakdown on screen agrees with the price it ranked on",
          close(cut.all_in_unit, engine.compare_price(best)),
          f"{cut.all_in_unit} vs {engine.compare_price(best)}")
    check("...built from the bid, the freight share and the tax, in that order",
          close(cut.bare_total, 7474) and close(cut.charge_share, 1000)
          and close(cut.delivered_total, 8474) and close(cut.tax_total, 1525.32), cut)

    # ------------------------------------------------------------------ 8
    print("\n8. Withdrawing an item leaves the others where they were")
    db.expire_all()
    before = landed.shares_for(db, solo, vendors[1].id)
    doomed = engine.vendor_best(db, solo.lines[1].id, vendors[1].id)
    two.post(f"/auctions/{solo.id}/bids/{doomed.id}/withdraw",
             data={"reason": "quoted in error"}, follow_redirects=False)
    db.expire_all()
    check("the item still quoted is priced exactly as it was",
          close(engine.vendor_best(db, solo.lines[0].id, vendors[1].id).landed_unit_price,
                9.3))
    check("...and the shares have not moved either",
          landed.shares_for(db, solo, vendors[1].id) == before)
    check("the withdrawn bid is out of the ranking altogether",
          engine.vendor_best(db, solo.lines[1].id, vendors[1].id) is None)

    # ------------------------------------------------------------------ 9
    print("\n9. The buyer sees the breakdown, the bidder sees only their own")
    page = buyer_c.get(f"/auctions/{near.id}").text
    check("the buyer's board has a column for the delivery share",
          "Delivery share" in page)
    check("...and one for tax", ">Tax<" in page)
    check("...and shows the all-in price it ranked on", "All-in per unit" in page)
    page = one.get(f"/auctions/{near.id}").text
    check("a bidder sees their own costs spelled out",
          "share of your" in page or "All-in" in page)
    check("...and never another bidder's costs", "Supplier 2" not in page)

    # ------------------------------------------------------------------ 10
    print("\n10. An ordinary auction is untouched by any of this")
    plain = make_auction(db, buyer, vendors, [(pens, unit, 100, 50.0)],
                         landed_on=False, title="No delivered comparison")
    pline = plain.lines[0]
    bid(one, plain, pline, 40.0)
    db.expire_all()
    best = engine.vendor_best(db, pline.id, vendors[0].id)
    check("the ranked price is the bid itself", close(engine.compare_price(best), 40))
    response = set_charges(one, plain, freight=5000)
    check("the costs panel refuses to take figures that would mean nothing",
          "decided on the bid price alone" in flash_of(response), flash_of(response)[:70])
    response = set_taxes(one, plain, pline, [("GST", 18)])
    check("...and so does the tax panel",
          "taxes are not collected" in flash_of(response), flash_of(response)[:70])
    page = one.get(f"/auctions/{plain.id}").text
    check("neither panel is even drawn", "Your delivery costs for this auction" not in page)

    # ------------------------------------------------------------------ 11
    print("\n11. The award books the price the buyer was shown")
    db.expire_all()
    auction_row = db.get(Auction, near.id)
    auction_row.status = AuctionStatus.CLOSED
    db.commit()
    winner = engine.best_bid(db, line.id)
    buyer_c.post(f"/auctions/{near.id}/award",
                 data={f"winner_{line.id}": str(winner.vendor_id),
                       f"price_{line.id}": str(winner.unit_price)},
                 follow_redirects=False)
    db.expire_all()
    award = db.query(Award).filter(Award.line_id == line.id).first()
    check("the award is recorded against the bidder who was L1 all-in",
          award is not None and award.vendor_id == winner.vendor_id)
    check("...at their own headline price", close(award.unit_price, winner.unit_price))
    check("...with the all-in price kept beside it",
          close(award.landed_unit_price, engine.compare_price(winner)),
          f"{award.landed_unit_price} vs {engine.compare_price(winner)}")

    # ------------------------------------------------------------------ 12
    print("\n12. Nobody can reach anybody else's numbers")
    response = set_charges(three, auction, freight=1)
    db.expire_all()
    check("a bidder saving costs only ever changes their own",
          close(landed.charges_for(db, auction, vendors[0].id).total, 5000)
          and close(landed.charges_for(db, auction, vendors[2].id).total, 1))
    other_org = Organisation(name="Somebody else")
    db.add(other_org)
    db.flush()
    outsider_vendor = Vendor(name="Outsider", email="out@x.local", org_id=other_org.id)
    db.add(outsider_vendor)
    db.flush()
    db.add(User(name="Outsider", email="out@x.local", role=Role.VENDOR,
                org_id=other_org.id, vendor_id=outsider_vendor.id,
                password_hash=hash_password(PW)))
    db.commit()
    outsider = login("out@x.local")
    response = outsider.post(f"/auctions/{auction.id}/charges",
                             data={"freight": "999"}, follow_redirects=False)
    check("a bidder from another organisation cannot touch this auction's costs",
          response.status_code in (403, 404), response.status_code)
    response = outsider.post(f"/auctions/{auction.id}/lines/{pens_line.id}/taxes",
                             data={"tax_name": "GST", "tax_percent": "18"},
                             follow_redirects=False)
    check("...nor its taxes", response.status_code in (403, 404), response.status_code)
    check("...and nothing of theirs was written",
          db.query(LineTax).filter_by(vendor_id=outsider_vendor.id).count() == 0)
    response = buyer_c.post(f"/auctions/{auction.id}/charges", data={"freight": "1"},
                            follow_redirects=False)
    check("the buyer cannot fill in a supplier's costs for them",
          response.status_code in (403, 404), response.status_code)

    # ------------------------------------------------------------------ 13
    print("\n13. What an audit of this code found, and what was done about it")
    # Each of these failed before the fix beside it.

    print("   the screen and the engine do one arithmetic, not two")
    fine = make_auction(db, buyer, vendors, [(pens, unit, 1000, 20.0)], title="Rounding")
    fline = fine.lines[0]
    set_charges(one, fine, freight=1234.56)
    set_taxes(one, fine, fline, [("GST", 18)])
    bid(one, fine, fline, 10.0)
    db.expire_all()
    cut = landed.breakdown(db, fine, fline, vendors[0].id, 10.0)
    placed = engine.vendor_best(db, fline.id, vendors[0].id)
    check("the total the bidder is shown is the price they are ranked at",
          close(cut.all_in_total, engine.compare_price(placed) * 1000),
          f"{cut.all_in_total} vs {engine.compare_price(placed) * 1000:.2f}")
    check("...and the named taxes add up to the tax total",
          close(sum(amount for _, _, amount in cut.taxes), cut.tax_total), cut.taxes)

    print("   a share too small to show per unit is still charged for")
    # 4,900 of freight over a million units is 0.0049 each: rounded to the
    # paisa it vanished, and the dearer supplier ranked first.
    huge = make_auction(db, buyer, vendors, [(pens, unit, 1000000, 20.0)], title="A million")
    hline = huge.lines[0]
    set_charges(one, huge, freight=4900)
    bid(one, huge, hline, 10.0)
    db.expire_all()
    mine = engine.vendor_best(db, hline.id, vendors[0].id)
    check("the ranked price carries it", engine.compare_price(mine) > 10.0,
          engine.compare_price(mine))
    check("...to the paisa it is really worth",
          close(engine.compare_price(mine), 10.0049, 1e-6), engine.compare_price(mine))

    print("   an item with no ceiling cannot drag another over its own")
    mixed = make_auction(db, buyer, vendors,
                         [(pens, unit, 100, 100.0), (paper, unit, 100, None)],
                         title="One capped, one open")
    capped, open_line = mixed.lines[0], mixed.lines[1]
    set_charges(one, mixed, freight=2000)
    db.expire_all()
    offered = engine.bid_window(db, mixed, capped, vendors[0].id).max_allowed
    bid(one, mixed, open_line, 500.0)
    db.expire_all()
    again = engine.bid_window(db, mixed, capped, vendors[0].id).max_allowed
    check("the price offered on the capped item does not move when the open one is priced",
          close(offered, again), f"{offered} -> {again}")
    bid(one, mixed, capped, again)
    bid(one, mixed, open_line, 100.0)
    db.expire_all()
    placed = engine.vendor_best(db, capped.id, vendors[0].id)
    check("...and a bid accepted under the ceiling stays under it",
          placed is not None and engine.compare_price(placed) <= 100.0001,
          engine.compare_price(placed) if placed else "no bid")

    print("   the minimum and maximum decrement cannot contradict each other")
    lock = make_auction(db, buyer, vendors, [(pens, unit, 10, 1000.0)], title="Locked",
                        min_dec=5.0)
    lock.max_decrement = 5.0
    db.commit()
    lline = lock.lines[0]
    bid(two, lock, lline, 1000.0)
    set_taxes(one, lock, lline, [("GST", 18)])
    db.expire_all()
    window = engine.bid_window(db, lock, lline, vendors[0].id)
    check("the two bounds do not cross",
          window.max_allowed is None or window.min_allowed <= window.max_allowed,
          f"{window.min_allowed} .. {window.max_allowed}")
    bid(one, lock, lline, window.max_allowed)
    db.expire_all()
    check("...and the price the screen offers is accepted",
          engine.vendor_best(db, lline.id, vendors[0].id) is not None)

    print("   an uninvited supplier cannot write taxes onto an auction")
    stray = Vendor(name="Never invited", email="stray@e.local", org_id=org.id)
    db.add(stray)
    db.flush()
    db.add(User(name="Stray", email="stray@e.local", role=Role.VENDOR, org_id=org.id,
                vendor_id=stray.id, password_hash=hash_password(PW)))
    db.commit()
    set_taxes(login("stray@e.local"), fine, fline, [("GST", 18)])
    db.expire_all()
    check("nothing of theirs is written",
          db.query(LineTax).filter_by(vendor_id=stray.id).count() == 0)

    print("   a stranger cannot withdraw somebody else's bid")
    theirs = engine.vendor_best(db, fline.id, vendors[0].id)
    outsider_buyer = Organisation(name="Passing trade")
    db.add(outsider_buyer)
    db.flush()
    db.add(User(name="Passer by", email="passer@x.local", role=Role.BUYER,
                org_id=outsider_buyer.id, password_hash=hash_password(PW)))
    db.commit()
    response = login("passer@x.local").post(
        f"/auctions/{fine.id}/bids/{theirs.id}/withdraw", data={"reason": "mischief"},
        follow_redirects=False)
    db.expire_all()
    check("a buyer from another organisation is refused",
          response.status_code in (403, 404), response.status_code)
    check("...and the bid is still standing", not db.get(Bid, theirs.id).withdrawn)

    print("   revising your own costs tells whoever it displaces")
    quiet = make_auction(db, buyer, vendors, [(pens, unit, 100, 200.0)], title="Quiet change",
                         min_dec=5.0)
    qline = quiet.lines[0]
    set_charges(one, quiet, freight=1000)             # 10 a unit
    bid(one, quiet, qline, 80.0)                      # 90 all in
    bid(two, quiet, qline, 85.0)                      # 85, and the lead
    db.expire_all()
    check("the second bidder leads to begin with",
          engine.best_bid(db, qline.id).vendor_id == vendors[1].id)
    before = db.query(EmailMessage).filter(EmailMessage.event == "outbid").count()
    set_charges(one, quiet, freight=1)                # 0.01 a unit -> 80.01
    db.expire_all()
    check("cutting your own freight can take the lead without a bid",
          engine.best_bid(db, qline.id).vendor_id == vendors[0].id,
          engine.compare_price(engine.best_bid(db, qline.id)))
    mailer.flush(10)
    check("...and the bidder it displaced is told, as an ordinary outbid would",
          db.query(EmailMessage).filter(EmailMessage.event == "outbid").count() > before)

    # ------------------------------------------------------------------ 14
    print("\n14. Splitting the auction, or giving it all to one supplier")
    # Pens: S1 is best. Paper: S2 is best. So splitting costs 2,600 and the
    # cheapest single supplier costs 2,700 - the price of dealing with one.
    split = make_auction(db, buyer, vendors, [(pens, unit, 100, 10.0),
                                              (paper, unit, 100, 20.0)],
                         landed_on=False, title="Split or whole")
    a_line, b_line = split.lines
    # The loser on each item bids first: a later bid always has to come in
    # below the standing one, so the order is not decoration.
    bid(two, split, a_line, 9.0)
    bid(one, split, a_line, 8.0)
    bid(one, split, b_line, 19.0)
    bid(two, split, b_line, 18.0)
    db.expire_all()
    choice = engine.award_comparison(db, split)
    check("item by item is priced at each item's own winner",
          close(choice["split_total"], 2600), choice["split_total"])
    check("...and it takes two suppliers to get it", choice["split_vendor_count"] == 2)
    check("the cheapest single supplier is named and priced",
          choice["best_whole"]["name"] == "Supplier 1"
          and close(choice["best_whole"]["total"], 2700), choice["best_whole"])
    check("the difference between the two is spelled out",
          close(choice["difference"], 100) and close(choice["difference_percent"], 3.85),
          f"{choice['difference']} / {choice['difference_percent']}%")
    check("every bidder's price for the whole lot is listed",
          len(choice["baskets"]) == 2 and all(b["complete"] for b in choice["baskets"]),
          [(b["name"], b["total"]) for b in choice["baskets"]])

    print("   a bidder who skipped an item cannot be given the whole auction")
    partial = make_auction(db, buyer, vendors, [(pens, unit, 100, 10.0),
                                                (paper, unit, 100, 20.0)],
                           landed_on=False, title="One of them skipped an item")
    p_line, q_line = partial.lines
    bid(one, partial, p_line, 8.0)
    bid(one, partial, q_line, 19.0)
    bid(two, partial, p_line, 7.0)            # cheaper, but only on one item
    db.expire_all()
    choice = engine.award_comparison(db, partial)
    check("the partial bidder is shown as unable to take it all",
          any(not b["complete"] and b["vendor_id"] == vendors[1].id
              for b in choice["baskets"]), choice["baskets"])
    check("...and the cheapest complete basket is the one offered",
          choice["best_whole"]["vendor_id"] == vendors[0].id)
    check("splitting still uses the cheaper bid on the item they did price",
          close(choice["split_total"], 700 + 1900), choice["split_total"])

    print("   when one supplier is best on everything, the choice is free")
    clean = make_auction(db, buyer, vendors, [(pens, unit, 100, 10.0),
                                              (paper, unit, 100, 20.0)],
                         landed_on=False, title="One supplier wins both")
    c_line, d_line = clean.lines
    bid(two, clean, c_line, 9.0)
    bid(one, clean, c_line, 8.0)
    bid(two, clean, d_line, 18.0)
    bid(one, clean, d_line, 17.0)
    db.expire_all()
    choice = engine.award_comparison(db, clean)
    check("the two totals are the same", choice["same_answer"]
          and close(choice["difference"], 0), choice["difference"])

    print("   the buyer's choice is enforced when the award is made")
    split.award_mode = "basket"
    split.status = AuctionStatus.CLOSED
    db.commit()
    response = buyer_c.post(f"/auctions/{split.id}/award",
                            data={f"winner_{a_line.id}": str(vendors[0].id),
                                  f"price_{a_line.id}": "8",
                                  f"winner_{b_line.id}": str(vendors[1].id),
                                  f"price_{b_line.id}": "18"},
                            follow_redirects=False)
    db.expire_all()
    check("a split award is refused on a single-supplier auction",
          db.query(Award).filter(Award.auction_id == split.id).count() == 0)
    check("...and the refusal says why, and how to change it",
          "split between" in response.text and "award item by item" in response.text)
    response = buyer_c.post(f"/auctions/{split.id}/award",
                            data={f"winner_{a_line.id}": str(vendors[0].id),
                                  f"price_{a_line.id}": "8",
                                  f"winner_{b_line.id}": str(vendors[0].id),
                                  f"price_{b_line.id}": "19"},
                            follow_redirects=False)
    db.expire_all()
    awarded = db.query(Award).filter(Award.auction_id == split.id).all()
    check("giving the lot to one supplier is accepted",
          len(awarded) == 2 and {a.vendor_id for a in awarded} == {vendors[0].id},
          [(a.line_id, a.vendor_id) for a in awarded])

    print("   ...and leaving an item out of a whole-auction award is refused")
    leftout = make_auction(db, buyer, vendors, [(pens, unit, 100, 10.0),
                                                (paper, unit, 100, 20.0)],
                           landed_on=False, title="Left one out")
    e_line, f_line = leftout.lines
    bid(one, leftout, e_line, 8.0)
    bid(one, leftout, f_line, 19.0)
    leftout.award_mode = "basket"
    leftout.status = AuctionStatus.CLOSED
    db.commit()
    response = buyer_c.post(f"/auctions/{leftout.id}/award",
                            data={f"winner_{e_line.id}": str(vendors[0].id),
                                  f"price_{e_line.id}": "8"},
                            follow_redirects=False)
    db.expire_all()
    check("nothing is awarded", db.query(Award).filter(Award.auction_id == leftout.id).count() == 0)
    check("...and the item left out is named", "Left out:" in response.text)

    print("   the record of a finished auction keeps the comparison")
    # Whitespace-normalised: the templates wrap, so a sentence to look for can
    # sit across two lines in the HTML and a plain substring would miss it.
    page = " ".join(buyer_c.get(f"/auctions/{split.id}?tab=award").text.split())
    check("the Award tab of a finished auction shows what the choice cost",
          "What the choice cost" in page and "2,600" in page and "2,700" in page)
    check("...and says which way this one went",
          "went to one supplier" in page, page.count("chosen"))
    check("...without claiming a saving the award did not actually make",
          "that is what the splitting saved" not in page
          and "what was actually awarded may differ" in page,
          " ".join(page.split("What the choice cost")[1][:900].split())[-260:]
          if "What the choice cost" in page else "no panel on the page")

    print("   the comparison is on the screen either way")
    for mode, expected in (("line", "Item by item"), ("basket", "All of it to one supplier")):
        clean.award_mode = mode
        clean.status = AuctionStatus.CLOSED
        db.commit()
        page = buyer_c.get(f"/auctions/{clean.id}/award").text
        check(f"...on a '{mode}' auction, both totals are shown",
              "Split it, or give it all to one supplier?" in page and expected in page)

    # ------------------------------------------------------------------ 15
    print("\n15. Before awarding: the quotes as written, or what they really cost")
    # S2 quotes the keener price and far more freight, so each view puts a
    # different bidder first - which is the whole reason for having both.
    views = make_auction(db, buyer, vendors, [(pens, unit, 100, 200.0)], title="Two views")
    vline = views.lines[0]
    set_charges(one, views, freight=1000)            # 10 a unit
    set_charges(two, views, freight=4000)            # 40 a unit
    bid(two, views, vline, 100.0)                    # 140 all in
    bid(one, views, vline, 120.0)                    # 130 all in
    db.expire_all()
    all_in = engine.award_comparison(db, views)
    bare = engine.award_comparison(db, views, basis="bare")
    check("the all-in view adds up what would actually be paid",
          close(all_in["split_total"], 13000), all_in["split_total"])
    check("the bid-only view adds up the quotes as written",
          close(bare["split_total"], 10000), bare["split_total"])
    check("...and they name different winners",
          all_in["split_rows"][0]["vendor_id"] == vendors[0].id
          and bare["split_rows"][0]["vendor_id"] == vendors[1].id,
          f"{all_in['split_rows'][0]['vendor_id']} vs {bare['split_rows'][0]['vendor_id']}")
    check("each view says which one it is", all_in["bare"] is False and bare["bare"] is True)

    views.status = AuctionStatus.CLOSED
    db.commit()
    page_all_in = buyer_c.get(f"/auctions/{views.id}/award").text
    page_bare = buyer_c.get(f"/auctions/{views.id}/award?basis=bare").text

    def leader_on_screen(page):
        """Whoever the first line's table puts at L1."""
        first = page.split('<div class="line-card">')[1]
        for row in first.split("<tr"):
            if "L1" in row:
                return "Supplier 1" if "Supplier 1" in row else "Supplier 2"
        return "?"

    check("both views are offered before the award",
          "Bid only" in page_all_in and "All-in" in page_all_in
          and "basis=bare" in page_all_in)
    check("the all-in view ranks on what would be paid",
          leader_on_screen(page_all_in) == "Supplier 1", leader_on_screen(page_all_in))
    check("the bid-only view ranks on the quotes as written",
          leader_on_screen(page_bare) == "Supplier 2", leader_on_screen(page_bare))
    check("the bid-only view warns that the figures leave the extras out",
          "leave out freight" in page_bare and "leave out freight" not in page_all_in)
    check("...and says the award is still decided on the all-in price",
          "decided on the all-in price" in page_bare)
    check("...and still names the all-in total, so nobody is misled by ₹10,000",
          "13,000" in page_bare)
    check("the comparison panel says which prices it is using",
          "bid prices only" in page_bare and "all-in prices" in page_all_in)
    check("each bid shows both figures side by side",
          "of extras" in page_bare)

    print("   an auction with no extras is not offered a choice it does not have")
    plain_view = make_auction(db, buyer, vendors, [(pens, unit, 100, 50.0)],
                              landed_on=False, title="Nothing to strip out")
    bid(one, plain_view, plain_view.lines[0], 40.0)
    plain_view.status = AuctionStatus.CLOSED
    db.commit()
    page = buyer_c.get(f"/auctions/{plain_view.id}/award").text
    check("no toggle is drawn", "basis=bare" not in page)
    check("...and asking for the bare view anyway changes nothing",
          "leave out freight" not in
          buyer_c.get(f"/auctions/{plain_view.id}/award?basis=bare").text)

    print("   the view survives an award that is refused")
    views.award_mode = "basket"
    db.commit()
    response = buyer_c.post(f"/auctions/{views.id}/award",
                            data={f"winner_{vline.id}": str(vendors[0].id),
                                  f"price_{vline.id}": "not a price", "basis": "bare"},
                            follow_redirects=False)
    check("the bid-only view is still the one on screen after an error",
          "leave out freight" in response.text and "not a price" in response.text)

    # ------------------------------------------------------------------ 16
    print("\n16. What the second audit found, and what was done about it")

    print("   a basket auction nobody priced in full can still be awarded")
    stuck = make_auction(db, buyer, vendors, [(pens, unit, 100, 10.0),
                                              (paper, unit, 100, 20.0)],
                         landed_on=False, title="Nobody priced both")
    s_line, t_line = stuck.lines
    bid(one, stuck, s_line, 8.0)
    bid(two, stuck, t_line, 18.0)
    stuck.award_mode = "basket"
    stuck.status = AuctionStatus.CLOSED
    db.commit()
    page = buyer_c.get(f"/auctions/{stuck.id}/award").text
    check("the screen says plainly that no single supplier can take it",
          "No single supplier can take" in page)
    check("...and offers the way out, on the screen where the wall is",
          "Award it item by item instead" in page)
    buyer_c.post(f"/auctions/{stuck.id}/award-mode", data={"award_mode": "line"},
                 follow_redirects=False)
    db.expire_all()
    check("the setting can be changed from there", db.get(Auction, stuck.id).award_mode == "line")
    buyer_c.post(f"/auctions/{stuck.id}/award",
                 data={f"winner_{s_line.id}": str(vendors[0].id), f"price_{s_line.id}": "8",
                       f"winner_{t_line.id}": str(vendors[1].id), f"price_{t_line.id}": "18"},
                 follow_redirects=False)
    db.expire_all()
    check("...and the bids can then be awarded",
          db.query(Award).filter(Award.auction_id == stuck.id).count() == 2)

    print("   an item nobody bid on does not block a whole-auction award")
    gap = make_auction(db, buyer, vendors, [(pens, unit, 100, 10.0),
                                            (paper, unit, 100, 20.0)],
                       landed_on=False, title="One item had no bids")
    g_line, h_line = gap.lines
    bid(one, gap, g_line, 8.0)
    gap.award_mode = "basket"
    gap.status = AuctionStatus.CLOSED
    db.commit()
    buyer_c.post(f"/auctions/{gap.id}/award",
                 data={f"winner_{g_line.id}": str(vendors[0].id), f"price_{g_line.id}": "8"},
                 follow_redirects=False)
    db.expire_all()
    check("the award goes through", db.query(Award).filter(Award.auction_id == gap.id).count() == 1)
    page = buyer_c.get(f"/auctions/{gap.id}/award").text
    check("...and the screen says the unbid item is in neither total",
          "had no bids at all" in page)
    check("...and does not claim one supplier was best on everything",
          "best on everything" not in page)

    print("   the quick-fill button follows the basis the award is decided on")
    both = make_auction(db, buyer, vendors, [(pens, unit, 100, 200.0)], title="Both views")
    b_line = both.lines[0]
    set_charges(one, both, freight=1000)          # 10 a unit
    set_charges(two, both, freight=4000)          # 40 a unit
    bid(two, both, b_line, 100.0)                 # cheapest bid, dearest all-in
    bid(one, both, b_line, 120.0)
    both.status = AuctionStatus.CLOSED
    db.commit()

    def l1_button_target(page):
        """Which bidder the “every item to its own L1” button would pick."""
        first = page.split('<div class="line-card">')[1]
        for row in first.split("<tr"):
            if 'data-l1="1"' in row:
                return "Supplier 1" if "Supplier 1" in row else "Supplier 2"
        return "?"

    for query, view in (("", "all-in"), ("?basis=bare", "bid-only")):
        page = buyer_c.get(f"/auctions/{both.id}/award{query}").text
        check(f"in the {view} view it targets the bidder who is best all-in",
              l1_button_target(page) == "Supplier 1", l1_button_target(page))

    def savings_by_bidder(page):
        first = page.split('<div class="line-card">')[1]
        found = {}
        for row in first.split("<tr")[1:]:
            who = ("Supplier 1" if "Supplier 1" in row
                   else ("Supplier 2" if "Supplier 2" in row else None))
            money = re.findall(r'color:var\(--(?:good|bad)\)">([^<]+)<', row)
            if who and money:
                found[who] = money[-1].strip()
        return found

    check("the saving shown for each bidder is the same in both views",
          savings_by_bidder(buyer_c.get(f"/auctions/{both.id}/award").text)
          == savings_by_bidder(buyer_c.get(f"/auctions/{both.id}/award?basis=bare").text),
          savings_by_bidder(buyer_c.get(f"/auctions/{both.id}/award?basis=bare").text))

    print("   one supplier is never cheaper than splitting, however it rounds")
    fractions = make_auction(db, buyer, vendors, [(pens, unit, 1.5, 10.0),
                                                  (paper, unit, 1.5, 10.0),
                                                  (rope, unit, 1.5, 10.0)],
                             landed_on=False, title="Fractional quantities")
    for fline in fractions.lines:
        bid(one, fractions, fline, 8.33)
    db.expire_all()
    choice = engine.award_comparison(db, fractions)
    check("the difference is never negative", choice["difference"] >= 0,
          f"{choice['difference']} (split {choice['split_total']}, "
          f"whole {choice['best_whole']['total']})")

    print("   totals that round away to nothing do not take the screen down")
    tiny = make_auction(db, buyer, vendors, [(pens, unit, 0.4, 100.0),
                                             (paper, unit, 0.4, 100.0)],
                        landed_on=False, title="Rounds to nothing")
    x_line, y_line = tiny.lines
    bid(one, tiny, x_line, 0.01)
    bid(one, tiny, y_line, 100.0)
    bid(two, tiny, y_line, 0.01)
    tiny.status = AuctionStatus.CLOSED
    db.commit()
    check("the award screen loads", buyer_c.get(f"/auctions/{tiny.id}/award").status_code == 200)
    check("...and so does the auction's own page",
          buyer_c.get(f"/auctions/{tiny.id}?tab=award").status_code == 200)

    print("   the setting cannot be changed by anyone who should not")
    response = one.post(f"/auctions/{stuck.id}/award-mode", data={"award_mode": "basket"},
                        follow_redirects=False)
    db.expire_all()
    check("a bidder cannot change how an auction is awarded",
          response.status_code in (403, 404)
          and db.get(Auction, stuck.id).award_mode == "line", response.status_code)
    response = login("passer@x.local").post(f"/auctions/{stuck.id}/award-mode",
                                            data={"award_mode": "basket"},
                                            follow_redirects=False)
    db.expire_all()
    check("...nor can a buyer from another organisation",
          response.status_code in (403, 404)
          and db.get(Auction, stuck.id).award_mode == "line", response.status_code)

    db.close()
    print("\n" + "-" * 64)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All enhanced delivered-cost checks passed.")
    return 0


def test_enhanced():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
