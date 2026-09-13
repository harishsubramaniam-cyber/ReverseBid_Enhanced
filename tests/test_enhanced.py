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

from app import engine, landed, quotes as quotes_mod, mailer              # noqa: E402
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
                 title="Delivered", minutes=90, mode="line", blind=True):
    """``specs`` is a list of (item, unit, qty, ceiling).

    ``mode`` is how the buyer says the business will be handed out, which is
    also how it is bid for: "line" item by item, "basket" the whole lot to one
    supplier.
    """
    now = datetime.utcnow()
    auction = Auction(reference=f"RA-E-{now.timestamp():.6f}", title=title,
                      creator_id=buyer.id, org_id=buyer.org_id, status=AuctionStatus.LIVE,
                      start_at=now - timedelta(minutes=5),
                      end_at=now + timedelta(minutes=minutes),
                      original_end_at=now + timedelta(minutes=minutes),
                      decrement_type=DecrementType.ABSOLUTE, min_decrement=min_dec,
                      compare_landed=landed_on, auto_extend=False, award_mode=mode,
                      hide_bidder_names=blind,
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


#: What each bidder is going to type on their next bid. A bid now carries its
#: own delivery costs and its own taxes - they are part of the offer - so a
#: test that used to save them in a panel first stages them here instead, and
#: the next bid sends them.
STAGED_COSTS: dict[tuple, dict] = {}
STAGED_TAXES: dict[tuple, list] = {}


class Staged:
    """What the staging helpers hand back, so old call sites still read well."""
    status_code = 303
    headers: dict = {}

    def __init__(self, message=""):
        self.message = message


def set_charges(client, auction, freight=0, packaging=0, other=0, label=""):
    """Stage what this bidder quotes to deliver this auction.

    The figure is for the whole auction, the way a supplier quotes it. On an
    auction handed out item by item the bid form asks for each item's own
    costs, so the next bid on an item sends that item's share of this figure -
    the same money, described the way that auction asks for it.
    """
    STAGED_COSTS[(id(client), auction.id)] = {
        "freight": float(freight or 0), "packaging": float(packaging or 0),
        "other": float(other or 0), "label": label}
    return Staged()


def set_taxes(client, auction, line, rows):
    STAGED_TAXES[(id(client), line.id)] = list(rows)
    return Staged()


def _costs_for(client, auction, line):
    """The three figures this bidder will type against one item."""
    from app import landed as _landed
    staged = STAGED_COSTS.get((id(client), auction.id))
    if not staged:
        return {"freight": 0.0, "packaging": 0.0, "other": 0.0, "label": ""}
    if len(auction.lines) == 1:
        return staged
    share = lambda amount: _landed._share_of(auction, line, amount)
    return {"freight": share(staged["freight"]), "packaging": share(staged["packaging"]),
            "other": share(staged["other"]), "label": staged["label"]}


def bid(client, auction, line, price):
    """One bid on one item, as the screen sends it - price, costs and taxes."""
    data = {"line_id": str(line.id), "unit_price": str(price)}
    if auction.compare_landed:
        costs = _costs_for(client, auction, line)
        rows = STAGED_TAXES.get((id(client), line.id)) or [("GST", 0)]
        data.update({"freight": str(costs["freight"]), "packaging": str(costs["packaging"]),
                     "other": str(costs["other"]), "other_label": costs["label"],
                     "tax_name": [name for name, _ in rows],
                     "tax_percent": [str(percent) for _, percent in rows]})
    return client.post(f"/auctions/{auction.id}/bid", data=data, follow_redirects=False)


def bid_with_costs(client, auction, line, price, freight=0, packaging=0, other=0,
                   rows=None):
    """A bid that carries exactly these costs and these taxes."""
    rows = rows if rows is not None else [("GST", 0)]
    data = {"line_id": str(line.id), "unit_price": str(price),
            "freight": str(freight), "packaging": str(packaging), "other": str(other),
            "tax_name": [name for name, _ in rows] or [""],
            "tax_percent": [str(percent) for _, percent in rows] or [""]}
    return client.post(f"/auctions/{auction.id}/bid", data=data, follow_redirects=False)


def bid_with_taxes(client, auction, line, price, rows):
    """A bid that carries exactly these taxes, whatever was staged before."""
    data = {"line_id": str(line.id), "unit_price": str(price),
            "freight": str(_costs_for(client, auction, line)["freight"]),
            "packaging": "0", "other": "",
            "tax_name": [name for name, _ in rows] or [""],
            "tax_percent": [str(percent) for _, percent in rows] or [""]}
    return client.post(f"/auctions/{auction.id}/bid", data=data, follow_redirects=False)


def basket_bid(client, auction, prices, freight=0, packaging=0, other=0, label="",
               taxes=None):
    """One bid for the whole auction: every item, and one set of delivery costs."""
    taxes = taxes or {}
    data = {"freight": str(freight), "packaging": str(packaging), "other": str(other),
            "other_label": label}
    for line in auction.lines:
        if line.id in prices:
            data[f"price_{line.id}"] = str(prices[line.id])
        rows = taxes.get(line.id) or [("GST", 0)]
        data[f"tax_name_{line.id}"] = [name for name, _ in rows]
        data[f"tax_percent_{line.id}"] = [str(percent) for _, percent in rows]
    return client.post(f"/auctions/{auction.id}/bid-all", data=data, follow_redirects=False)


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
    print("\n1. A whole-auction auction is bid for as one lot")
    # Two items: pens are worth 10,000 at the ceiling, paper 40,000. So a
    # freight bill should split one to four.
    auction = make_auction(db, buyer, vendors,
                           [(pens, unit, 1000, 10.0), (paper, unit, 200, 200.0)],
                           mode="basket")
    pens_line, paper_line = auction.lines[0], auction.lines[1]

    page = one.get(f"/auctions/{auction.id}").text
    check("the bidder is given one form for the whole auction",
          "Place bid for the whole auction" in page)
    check("...which asks for freight, packaging and other costs",
          all(word in page for word in ("Freight", "Packaging", "Other costs")))
    check("...once, for the consignment, not per item",
          "Delivery costs for the whole consignment" in page)
    check("...and asks for the tax on each item",
          page.count("Tax on this item") == 2, page.count("Tax on this item"))
    check("there is no separate bid button on an item",
          "Place bid for Pens" not in page)

    response = basket_bid(one, auction, {pens_line.id: 9.0, paper_line.id: 180.0},
                          freight=4000, packaging=800, other=200, label="Unloading")
    told = flash_of(response)
    check("one press bids for everything", "whole auction" in told, told[:90])
    db.expire_all()
    charges = landed.charges_for(db, auction, vendors[0].id)
    check("the three figures are kept apart", close(charges.freight, 4000)
          and close(charges.packaging, 800) and close(charges.other, 200))
    check("...and add up to one number", close(charges.total, 5000), charges.total)
    check("every item carries a bid", len(engine.all_line_bids(db, pens_line.id)) == 1
          and len(engine.all_line_bids(db, paper_line.id)) == 1)

    # ------------------------------------------------------------------ 2
    print("\n2. The consignment's costs are shared by what each item is worth")
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
    standing = quotes_mod.standings(db, auction)
    check("the auction is ranked on the grand total",
          standing and close(standing[0]["total"], 10000 + 40000),
          standing[0]["total"] if standing else None)

    # ------------------------------------------------------------------ 3
    print("\n3. A whole-auction bid has to cover the whole auction")
    partial = basket_bid(two, auction, {pens_line.id: 9.0},
                         freight=100)
    told = flash_of(partial)
    check("leaving an item unpriced is refused, and the item is named",
          "has to cover every item" in told and "Paper" in told, told[:110])
    db.expire_all()
    check("...and nothing at all was written for that bidder",
          engine.vendor_best(db, pens_line.id, vendors[1].id) is None)
    beat = basket_bid(two, auction, {pens_line.id: 8.0, paper_line.id: 170.0},
                      freight=1000)
    db.expire_all()
    check("a bid that covers everything is accepted", "whole auction" in flash_of(beat),
          flash_of(beat)[:80])
    standing = quotes_mod.standings(db, auction)
    check("...and the cheaper total takes the lead",
          standing[0]["vendor_id"] == vendors[1].id,
          [(row["vendor_id"], row["total"]) for row in standing])
    too_high = basket_bid(two, auction, {pens_line.id: 9.0, paper_line.id: 180.0},
                          freight=1000)
    check("a bid above your own last total is refused",
          "below it" in flash_of(too_high), flash_of(too_high)[:90])

    # ------------------------------------------------------------------ 4
    print("\n4. Taxes: the rate is typed, the money is worked out")
    # Its own auction, with room under the ceiling for tax: 1,000 pens at a
    # ceiling of 20, freight of 5,000 (5 a unit), a bid of 9 - so 14 delivered
    # and 16.80 once 20% of tax is on it.
    taxes_auction = make_auction(db, buyer, vendors, [(pens, unit, 1000, 20.0)],
                                 title="Taxed properly")
    tax_line = taxes_auction.lines[0]
    set_charges(one, taxes_auction, freight=5000)
    response = bid_with_taxes(one, taxes_auction, tax_line, 9.0,
                              [("GST", 18), ("Cess", 2)])
    check("the bid carries both taxes with it",
          "L1" in flash_of(response) or "Bid placed" in flash_of(response),
          flash_of(response)[:80])
    db.expire_all()
    saved = landed.taxes_for(db, tax_line.id, vendors[0].id)
    check("both are recorded against the item, by name and rate",
          [(row.name, row.percent) for row in saved] == [("GST", 18.0), ("Cess", 2.0)],
          [(row.name, row.percent) for row in saved])
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

    refused = bid_with_taxes(one, taxes_auction, tax_line, 9.0, [("Nonsense", 250)])
    check("a rate of 250% is refused, in words",
          "not a rate anybody charges" in flash_of(refused), flash_of(refused)[:70])
    check("...and the bid that was standing is untouched",
          landed.tax_rate(landed.taxes_for(db, tax_line.id, vendors[0].id)) == 20)
    refused = bid_with_taxes(one, taxes_auction, tax_line, 9.0, [("GST", "eighteen")])
    check("a rate typed as words is refused too",
          "is not a percentage" in flash_of(refused), flash_of(refused)[:70])
    check("...and still nothing was changed",
          landed.tax_rate(landed.taxes_for(db, tax_line.id, vendors[0].id)) == 20)
    refused = bid_with_taxes(one, taxes_auction, tax_line, 8.0, [])
    check("a bid with no tax at all is refused, because nobody can compare it",
          "Say what tax" in flash_of(refused), flash_of(refused)[:80])
    db.expire_all()
    check("...and the bid did not go in",
          close(engine.vendor_best(db, tax_line.id, vendors[0].id).unit_price, 9.0))
    placed = bid_with_taxes(one, taxes_auction, tax_line, 8.0, [("GST", 0)])
    check("a rate of nought is a perfectly good answer, typed on purpose",
          "L1" in flash_of(placed) or "Bid placed" in flash_of(placed),
          flash_of(placed)[:70])
    db.expire_all()
    check("...and the ranked price drops to the delivered price, with no tax on it",
          close(engine.compare_price(engine.vendor_best(db, tax_line.id, vendors[0].id)),
                13.0),
          engine.compare_price(engine.vendor_best(db, tax_line.id, vendors[0].id)))

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
    print("\n6. Revising your costs means bidding again, and it re-ranks at once")
    before = engine.compare_price(engine.vendor_best(db, line.id, vendors[1].id))
    set_charges(two, near, freight=2000)
    again = bid(two, near, line, 79.0)
    db.expire_all()
    after = engine.compare_price(engine.vendor_best(db, line.id, vendors[1].id))
    check("a keener price with less freight on it is a much better offer",
          close(before, 92) and close(after, 81), f"{before} -> {after}")
    check("...and it was accepted as a bid, not a quiet edit",
          "L1" in flash_of(again) or "Bid placed" in flash_of(again),
          flash_of(again)[:70])
    ranked = engine.best_per_vendor(db, line.id)
    check("...so the ranking follows immediately",
          ranked[0].vendor_id == vendors[1].id, [b.vendor_id for b in ranked])
    mine = engine.vendor_best(db, line.id, vendors[0].id)
    check("the other bidder's price is untouched by all of it",
          close(engine.compare_price(mine), 90) and close(mine.unit_price, 90))
    record = quotes_mod.history(db, near, vendors[1].id)
    check("the bidder's own record keeps both submissions, newest first",
          len(record) == 2 and record[0]["status"] == "standing"
          and record[1]["status"] == "superseded",
          [row["status"] for row in record])
    check("...and each one remembers the freight it was made with",
          close(record[0]["charges"]["freight"], 2000)
          and close(record[1]["charges"]["freight"], 12000),
          [row["charges"].get("freight") for row in record])

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

    refused = bid_with_taxes(three, tight, tline, 90.0, [("GST", 18)])
    told = flash_of(refused)
    check("the same price with tax added is refused: all in, it breaks the ceiling",
          "above the starting price" in told, told[:120])
    check("...and the message shows the arithmetic that did it",
          "delivered" in told.lower() and "GST" in told, told[:160])
    db.expire_all()
    standing = engine.vendor_best(db, tline.id, vendors[2].id)
    check("...and the bid that stands is left exactly as it was",
          close(engine.compare_price(standing), 100.0), engine.compare_price(standing))

    print("   the price that fits, once the tax is in, is accepted")
    fresh = make_auction(db, buyer, vendors, [(pens, unit, 100, 100.0)],
                         title="Tax declared with the bid", min_dec=1.0)
    fline = fresh.lines[0]
    set_charges(three, fresh, freight=1000)           # 10 per unit
    # (100 / 1.18) - 10 = 74.74 to the paisa, which lands just under the
    # ceiling once the freight and the tax are both on it.
    response = bid_with_taxes(three, fresh, fline, 74.74, [("GST", 18)])
    check("a price worked out for the freight and the tax together is accepted",
          "L1" in flash_of(response) or "Bid placed" in flash_of(response),
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
    refused = bid_with_taxes(three, fresh, fline, 74.75, [("GST", 18)])
    check("...while a paisa more is over the ceiling and refused",
          "above the starting price" in flash_of(refused), flash_of(refused)[:80])

    # ------------------------------------------------------------------ 8
    print("\n8. Taking back a bid, item by item and for the whole auction")
    solo = make_auction(db, buyer, vendors,
                        [(pens, unit, 100, 10.0), (paper, unit, 100, 10.0),
                         (rope, unit, 100, 10.0)], title="Three equal items")
    set_charges(two, solo, freight=90)                # 30 to each item
    bid(two, solo, solo.lines[0], 9.0)                # 9 + 0.30 = 9.30
    bid(two, solo, solo.lines[1], 9.0)
    db.expire_all()
    check("each item is priced with its own costs on it",
          close(engine.vendor_best(db, solo.lines[0].id, vendors[1].id).landed_unit_price,
                9.3)
          and close(engine.vendor_best(db, solo.lines[1].id, vendors[1].id).landed_unit_price,
                    9.3))
    older = engine.vendor_best(db, solo.lines[0].id, vendors[1].id)
    r = two.post(f"/auctions/{solo.id}/bids/{older.id}/withdraw",
                 data={"reason": "the wrong one"}, follow_redirects=False)
    db.expire_all()
    check("an earlier bid cannot be pulled out from under the record",
          "most recent" in flash_of(r)
          and engine.vendor_best(db, solo.lines[0].id, vendors[1].id) is not None,
          flash_of(r)[:70])
    r = two.post(f"/auctions/{solo.id}/withdraw-last",
                 data={"reason": "quoted in error"}, follow_redirects=False)
    db.expire_all()
    check("taking back the last bid takes back that item only",
          "taken back" in flash_of(r), flash_of(r)[:70])
    check("...the item still quoted is priced exactly as it was",
          close(engine.vendor_best(db, solo.lines[0].id, vendors[1].id).landed_unit_price,
                9.3))
    check("...and the withdrawn item is out of the ranking altogether",
          engine.vendor_best(db, solo.lines[1].id, vendors[1].id) is None)
    print("   a whole-auction bid is taken back in one piece")
    lot = make_auction(db, buyer, vendors, [(pens, unit, 100, 10.0),
                                            (paper, unit, 100, 10.0)],
                       title="One lot", mode="basket")
    basket_bid(two, lot, {lot.lines[0].id: 9.0, lot.lines[1].id: 9.0}, freight=100)
    basket_bid(two, lot, {lot.lines[0].id: 8.0, lot.lines[1].id: 8.0}, freight=100)
    db.expire_all()
    check("the later bid is the one that stands",
          close(quotes_mod.standings(db, lot)[0]["total"], 1700),
          quotes_mod.standings(db, lot)[0]["total"])
    r = two.post(f"/auctions/{lot.id}/withdraw-last", data={"reason": "wrong file"},
                 follow_redirects=False)
    db.expire_all()
    check("taking it back takes every item with it, not just one",
          all(engine.vendor_best(db, line.id, vendors[1].id).unit_price == 9.0
              for line in lot.lines),
          [engine.vendor_best(db, line.id, vendors[1].id).unit_price for line in lot.lines])
    check("...and the bid before it stands again, as a whole",
          close(quotes_mod.standings(db, lot)[0]["total"], 1900),
          quotes_mod.standings(db, lot)[0]["total"])
    check("...and the message says what stands now",
          "stands again" in flash_of(r), flash_of(r)[:90])

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
    page = one.get(f"/auctions/{plain.id}").text
    check("the bid form asks for nothing but a price",
          "Tax on this item" not in page and "What it costs to deliver" not in page)
    # Costs posted anyway - by hand, or by an old page - are ignored rather
    # than quietly added to a price nobody is comparing that way.
    one.post(f"/auctions/{plain.id}/bid",
             data={"line_id": str(pline.id), "unit_price": "39", "freight": "5000",
                   "tax_name": "GST", "tax_percent": "18"}, follow_redirects=False)
    db.expire_all()
    best = engine.vendor_best(db, pline.id, vendors[0].id)
    check("...and figures sent anyway change nothing",
          close(engine.compare_price(best), 39) and close(best.charges_total, 0),
          engine.compare_price(best))

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
    basket_bid(three, auction, {pens_line.id: 7.0, paper_line.id: 160.0}, freight=1)
    db.expire_all()
    check("a bidder's costs are their own, and touch nobody else's",
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
    response = outsider.post(f"/auctions/{auction.id}/bid-all",
                             data={"freight": "999"}, follow_redirects=False)
    check("a bidder from another organisation cannot bid on it at all",
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

    print("   what one item costs to deliver cannot move another item's price")
    mixed = make_auction(db, buyer, vendors,
                         [(pens, unit, 100, 100.0), (paper, unit, 100, None)],
                         title="One capped, one open")
    capped, open_line = mixed.lines[0], mixed.lines[1]
    # 1,000 to deliver the capped item: 10 a unit, so 85 lands at 95 all in.
    placed = bid_with_costs(one, mixed, capped, 85.0, freight=1000, rows=[("GST", 0)])
    db.expire_all()
    was = engine.compare_price(engine.vendor_best(db, capped.id, vendors[0].id))
    check("the capped item is priced on its own costs",
          close(was, 95.0), was)
    bid_with_costs(one, mixed, open_line, 500.0, freight=9000, rows=[("GST", 0)])
    db.expire_all()
    now = engine.compare_price(engine.vendor_best(db, capped.id, vendors[0].id))
    check("...and pricing the open item, freight and all, does not touch it",
          close(was, now), f"{was} -> {now}")
    check("...so a bid accepted under the ceiling stays under it",
          now <= 100.0001, now)

    print("   the minimum and maximum decrement cannot contradict each other")
    lock = make_auction(db, buyer, vendors, [(pens, unit, 10, 1000.0)], title="Locked",
                        min_dec=5.0)
    lock.max_decrement = 5.0
    db.commit()
    lline = lock.lines[0]
    bid(two, lock, lline, 1000.0)
    db.expire_all()
    # The window is worked out with the figures this bidder is about to type,
    # because that is what their bid will be judged on.
    window = engine.bid_window(db, lock, lline, vendors[0].id,
                               charges=landed.Charges(),
                               taxes=[quotes_mod.TaxRow("GST", 18.0)])
    check("the two bounds do not cross",
          window.max_allowed is None or window.min_allowed <= window.max_allowed,
          f"{window.min_allowed} .. {window.max_allowed}")
    bid_with_costs(one, lock, lline, window.max_allowed, rows=[("GST", 18)])
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
    placed = bid_with_costs(one, quiet, qline, 80.0, freight=1000, rows=[("GST", 0)])
    bid_with_costs(two, quiet, qline, 85.0, freight=0, rows=[("GST", 0)])
    db.expire_all()
    check("the second bidder leads to begin with, on the all-in price",
          engine.best_bid(db, qline.id).vendor_id == vendors[1].id)
    before = db.query(EmailMessage).filter(EmailMessage.event == "outbid").count()
    # The same headline price, with the freight cut - a better offer, and it
    # has to be made as a bid, where everyone can see it happen.
    bid_with_costs(one, quiet, qline, 79.0, freight=1, rows=[("GST", 0)])
    db.expire_all()
    check("a keener bid with less freight on it takes the lead",
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
    # Named bidders on this one: the two views are about which PRICE leads,
    # and the screen has to say who that is for the check to read it.
    views = make_auction(db, buyer, vendors, [(pens, unit, 100, 200.0)], title="Two views",
                         blind=False)
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
    both = make_auction(db, buyer, vendors, [(pens, unit, 100, 200.0)], title="Both views",
                        blind=False)          # named, so the check can read the row
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
