"""Fill an empty database with sample data you can click through immediately.

    python seed.py            # build data/reverse_auction.db
    python seed.py --reset    # wipe it first and build again

Five of everything, with plain names that are obviously not real: five units,
five suppliers, five items and five auctions across the states an auction can
be in - draft, scheduled, live and finished - with bids already on the live
one so the screens have something to show.

This is also what the deployed demo runs on: ``RA_DEMO_SEED=1`` calls
``build()`` at startup, and it does nothing unless the database is completely
empty. The data therefore lives in this file rather than in a database file
checked into the repository - a database in the repository gets copied over
the server's own at every deploy, so the site can never be cleared.
"""
from __future__ import annotations

import argparse
import pathlib
from datetime import datetime, timedelta

from app.db import Base, SessionLocal, engine
from app.landed import _share_of, reprice_vendor, snapshot
from app.models import (Auction, AuctionLine, AuctionStatus, Award, Bid, DecrementType, Quote,
                        Item, LineTax, Message, Organisation, Participant, Role, Unit,
                        User, Vendor)
from app.security import hash_password
from app.utils import alias_for

#: The published demo password. Everyone who opens the link uses it, so never
#: point this deployment at anything real.
PASSWORD = "demo1234"

UNITS = [("NOS", "Numbers"), ("KG", "Kilogram"), ("MTR", "Metre"),
         ("BOX", "Box"), ("LTR", "Litre")]

#: name, email, contact, and what it costs to get this supplier's goods to
#: the door - freight per unit, and duty as a percentage. Those two are what
#: make the delivered-price comparison worth looking at: the cheapest quote
#: from three states away is often not the cheapest delivered.
VENDORS = [
    ("Alpha Supplies Pvt Ltd", "supplier1@example.com", "A Kumar", 0.9, 0.0),
    ("Bharat Traders", "supplier2@example.com", "B Sharma", 2.4, 0.0),
    ("Coastal Components", "supplier3@example.com", "C Nair", 3.1, 0.0),
    ("Delta Industrial", "supplier4@example.com", "D Rao", 1.0, 2.0),
    ("Eastern Packaging Co", "supplier5@example.com", "E Iyer", 1.6, 0.0),
]

ITEMS = [("Ball pen, blue", "NOS"), ("Copier paper A4, 75 gsm", "BOX"),
         ("Mild steel rod, 12 mm", "KG"), ("Nylon rope, 10 mm", "MTR"),
         ("Lubricating oil, SAE 40", "LTR")]

#: Five auctions, one in each state an auction can be in, so every screen has
#: something real to show: the dashboard needs a finished one to report savings
#: on, the bidding board needs a live one.
#:
#: title, status, when it starts relative to now (hours), lines as
#: (item index, quantity, ceiling price per unit), and the bids placed on it
#: as (vendor index, line position, price per unit) in the order they came in.
AUCTIONS = [
    ("Stationery — quarterly refill", AuctionStatus.DRAFT, 48,
     [(0, 5000, 18.0), (1, 400, 260.0)], []),
    ("Steel rod — March requirement", AuctionStatus.SCHEDULED, 2,
     [(2, 12000, 68.0)], []),
    ("Packaging consumables", AuctionStatus.LIVE, -1,
     [(1, 1200, 255.0), (3, 800, 44.0)],
     [(0, 0, 248.0), (1, 0, 242.5), (0, 1, 41.0), (1, 1, 40.25), (0, 0, 239.0)]),
    ("Lubricants and rope — delivered price, one supplier", AuctionStatus.LIVE, -1,
     [(4, 900, 340.0), (3, 1500, 46.0)],
     [(1, 0, 306.0), (0, 0, 300.0), (0, 1, 43.0), (1, 1, 41.5)]),
    ("Bearings — delivered price, item by item", AuctionStatus.LIVE, -1,
     [(2, 400, 190.0), (0, 2000, 15.0)],
     [(0, 0, 168.0), (1, 0, 171.0), (1, 1, 13.4), (0, 1, 13.9)]),
    ("Rope and cordage", AuctionStatus.AWARDED, -72,
     [(3, 2500, 42.0)],
     [(0, 0, 40.5), (1, 0, 39.75), (2, 0, 39.0), (1, 0, 38.4)]),
    ("Workshop lubricants", AuctionStatus.AWARDED, -170,
     [(4, 900, 315.0), (0, 3000, 18.0)],
     [(0, 0, 305.0), (1, 0, 298.5), (0, 0, 294.0),
      (1, 1, 17.1), (2, 1, 16.8), (0, 1, 16.5)]),
]


def _record_submissions(db, auction, lines, placed, whole: bool) -> None:
    """Write the Quote rows that stand behind the bids just created.

    A bid is a submission - a price with its delivery costs and its taxes, at
    a moment. The sample data builds the bids directly, so the submissions are
    made here to match: one per bidder on a whole-auction auction, one per bid
    on an item-by-item one.
    """
    by_line = {line.id: line for line in lines}
    if whole:
        for vendor_id in {bid.vendor_id for bid in placed}:
            theirs = [bid for bid in placed if bid.vendor_id == vendor_id]
            latest = {}
            for bid in sorted(theirs, key=lambda b: b.created_at):
                latest[bid.line_id] = bid
            part = (db.query(Participant)
                      .filter_by(auction_id=auction.id, vendor_id=vendor_id).first())
            quote = Quote(auction_id=auction.id, vendor_id=vendor_id, scope="auction",
                          freight=(part.bidder_freight or 0.0),
                          packaging=(part.bidder_packaging or 0.0),
                          other=(part.bidder_other or 0.0),
                          created_at=max(bid.created_at for bid in theirs))
            db.add(quote)
            db.flush()
            rows = []
            for bid in latest.values():
                bid.quote_id = quote.id
                rows.append((by_line[bid.line_id], bid))
            quote.total_all_in = round(sum((bid.landed_unit_price or bid.unit_price)
                                           * float(by_line[bid.line_id].qty or 0.0)
                                           for bid in latest.values()), 2)
            quote.detail = snapshot(db, auction, rows, quote)
        db.flush()
        return
    for bid in placed:
        line = by_line.get(bid.line_id)
        if line is None:                       # pragma: no cover - defensive
            continue
        quote = Quote(auction_id=auction.id, vendor_id=bid.vendor_id, scope="line",
                      line_id=line.id, freight=(bid.freight or 0.0),
                      packaging=(bid.packaging or 0.0), other=(bid.other or 0.0),
                      created_at=bid.created_at,
                      total_all_in=round((bid.landed_unit_price or bid.unit_price)
                                         * float(line.qty or 0.0), 2))
        db.add(quote)
        db.flush()
        bid.quote_id = quote.id
        quote.detail = snapshot(db, auction, [(line, bid)], quote)
    db.flush()


def reset() -> None:
    """Start from nothing. For SQLite we remove the file - dropping the tables
    trips over the mutual users <-> vendors foreign keys."""
    url = str(engine.url)
    if url.startswith("sqlite") and engine.url.database:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            path = pathlib.Path(engine.url.database + suffix)
            if path.exists():
                path.unlink()
        return
    Base.metadata.drop_all(bind=engine)


def build() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    if db.query(User).count():
        print("Database already has data — use --reset to start again.")
        db.close()
        return

    now = datetime.utcnow()
    org = Organisation(name="Demo Buying Company")
    db.add(org)
    db.flush()

    buyer = User(name="Demo Buyer", email="buyer@example.com", role=Role.BUYER,
                 org_id=org.id, password_hash=hash_password(PASSWORD))
    db.add(buyer)
    db.flush()

    units = {}
    for code, name in UNITS:
        unit = Unit(code=code, name=name, org_id=org.id)
        db.add(unit)
        db.flush()
        units[code] = unit

    vendors, logins = [], []
    for name, email, person, freight, duty in VENDORS:
        vendor = Vendor(name=name, email=email, contact_person=person,
                        org_id=org.id, created_by_id=buyer.id,
                        default_freight=freight, default_freight_basis="unit",
                        default_duty=duty, default_duty_basis="percent")
        db.add(vendor)
        db.flush()
        vendors.append(vendor)
        db.add(User(name=person, email=email, role=Role.VENDOR, org_id=org.id,
                    vendor_id=vendor.id, password_hash=hash_password(PASSWORD)))
        logins.append(email)
    db.flush()

    items = []
    for name, code in ITEMS:
        item = Item(name=name, default_unit_id=units[code].id, org_id=org.id)
        db.add(item)
        db.flush()
        items.append(item)

    bid_count = 0
    live_auction = None
    for number, (title, status, hours, lines, bids) in enumerate(AUCTIONS, start=1):
        start = now + timedelta(hours=hours)
        end = start + timedelta(hours=2)
        # The delivered-price auction is the one that shows what this version
        # is for: the bidders quote their own freight and tax, and the board
        # ranks them on what the buyer would really pay.
        delivered = "delivered price" in title
        # This one also shows the other choice a buyer makes at the end: it is
        # set to go to a single supplier, and the award screen prices that
        # decision against splitting it.
        whole = "one supplier" in title
        auction = Auction(
            org_id=org.id, reference=f"RA-{number:04d}", title=title,
            compare_landed=delivered, award_mode=("basket" if whole else "line"),
            description="Sample data, for trying the platform out.",
            creator_id=buyer.id, status=status, currency="INR",
            start_at=start, end_at=end, original_end_at=end,
            decrement_type=DecrementType.ABSOLUTE, min_decrement=0.5,
            published_at=(now if status is not AuctionStatus.DRAFT else None))
        db.add(auction)
        db.flush()
        made = []
        for index, qty, ceiling in lines:
            line = AuctionLine(auction_id=auction.id, item_id=items[index].id,
                               unit_id=items[index].default_unit_id, qty=qty,
                               starting_price=ceiling)
            db.add(line)
            db.flush()
            made.append(line)
        if status is not AuctionStatus.DRAFT:
            for position, vendor in enumerate(vendors):
                db.add(Participant(auction_id=auction.id, vendor_id=vendor.id,
                                   alias=alias_for(position)))

        # Bids are stamped a few minutes apart, so the history reads in the
        # order they arrived and the last one really is the leader.
        placed: list[Bid] = []
        for order, (vendor_index, position, price) in enumerate(bids):
            line = made[position]
            bid = Bid(auction_id=auction.id, line_id=line.id,
                      vendor_id=vendors[vendor_index].id, unit_price=price,
                      qty=line.qty, total=round(price * line.qty, 2),
                      created_at=(end if status is AuctionStatus.AWARDED else now)
                      - timedelta(minutes=(len(bids) - order) * 5))
            db.add(bid)
            placed.append(bid)
        bid_count += len(bids)
        db.flush()

        if status is AuctionStatus.LIVE and not delivered:
            live_auction = auction

        if delivered:
            # What each bidder says it costs them to deliver, and the tax they
            # charge - exactly what they would type on the bid form. On a
            # whole-auction auction that is one figure for the consignment; on
            # an item-by-item one it is each item's own.
            costs = [(0, 6000.0, 900.0, "GST", 18.0),
                     (1, 14000.0, 0.0, "GST", 18.0),
                     (2, 2500.0, 0.0, "GST", 12.0)]
            for index, freight, packaging, tax_name, rate in costs:
                part = (db.query(Participant)
                          .filter_by(auction_id=auction.id,
                                     vendor_id=vendors[index].id).first())
                if whole:
                    part.bidder_freight = freight
                    part.bidder_packaging = packaging
                    part.charges_updated_at = now
                for made_line in made:
                    db.add(LineTax(auction_id=auction.id, line_id=made_line.id,
                                   vendor_id=vendors[index].id, name=tax_name,
                                   percent=rate))
                if not whole:
                    # Item by item: the figure is split across the items the
                    # way a supplier would quote each consignment.
                    for made_line in made:
                        share = _share_of(auction, made_line, freight)
                        pack = _share_of(auction, made_line, packaging)
                        for bid in placed:
                            if (bid.line_id == made_line.id
                                    and bid.vendor_id == vendors[index].id):
                                bid.freight = share
                                bid.packaging = pack
            db.flush()
            for index in {v for v, _, _, _, _ in costs}:
                reprice_vendor(db, auction, vendors[index].id)

        # Every bid is a submission: it is what the bidder's own record of
        # their bids is built from, and what they would take back.
        _record_submissions(db, auction, made, placed, whole)

        if status is AuctionStatus.AWARDED:
            # Each line goes to whoever ended up lowest on it, at their own
            # price - the house rule is one winner per line, whole quantity.
            for line in made:
                best = min((b for b in placed if b.line_id == line.id),
                           key=lambda b: b.unit_price, default=None)
                if best is None:
                    continue
                db.add(Award(auction_id=auction.id, line_id=line.id,
                             vendor_id=best.vendor_id, bid_id=best.id, qty=line.qty,
                             unit_price=best.unit_price,
                             total=round(line.qty * best.unit_price, 2),
                             awarded_by_id=buyer.id,
                             awarded_at=end + timedelta(hours=2)))
            auction.closed_at = end
            auction.awarded_at = end + timedelta(hours=2)

    # A question already waiting on the live auction, so the Conversation tab
    # shows what it is for: one private thread per bidder, never a group chat.
    if live_auction is not None:
        vendor_user = db.query(User).filter(User.vendor_id == vendors[0].id).first()
        db.add(Message(auction_id=live_auction.id, vendor_id=vendors[0].id,
                       sender_id=vendor_user.id if vendor_user else buyer.id,
                       body="Is the 5-ply specification firm, or would 3-ply be "
                            "acceptable?"))

    db.commit()
    counts = {"units": len(UNITS), "suppliers": len(VENDORS), "items": len(ITEMS),
              "auctions": len(AUCTIONS), "bids": bid_count}
    db.close()

    print("\nSample data ready — " + ", ".join(f"{n} {k}" for k, n in counts.items()) + ".")
    print(f"\n  Buyer    buyer@example.com      / {PASSWORD}")
    for email in logins:
        print(f"  Bidder   {email:22} / {PASSWORD}")
    print("\n  Everything here is sample data - no address is real, and example.com")
    print("  is the domain reserved for exactly this, so nothing can reach anybody.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the sample database.")
    parser.add_argument("--reset", action="store_true",
                        help="delete the existing database first")
    args = parser.parse_args()
    if args.reset:
        reset()
    build()
