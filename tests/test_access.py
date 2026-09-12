"""Round 3 of the deep check: who can see what, and who can do what.

Two companies, Acme and Globex, share one installation. Every check here is
an attempt to get at something that belongs to somebody else - by signing in
as the wrong person, by editing a form's hidden numbers, by keeping a cookie,
or simply by asking for a page nobody linked to.

  Part one (21-35)  the walls: signed out, another company, an uninvited
                    supplier, buyer-only screens, forged forms, invitations.
  Part two (36-42)  stealing by id: building an auction out of another
                    company's records, signing up, signing out, uploads.
  Part three (43-47) what one bidder can learn about another on the same
                    auction, including by email.

    python tests/test_access.py
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="ra-access-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ.pop("RA_SMTP_HOST", None)

from fastapi.testclient import TestClient                       # noqa: E402

from app import config, documents                               # noqa: E402
from app.db import Base, SessionLocal, engine as db_engine      # noqa: E402
from app.main import app                                        # noqa: E402
from app.models import (Attachment, Auction, AuctionLine, AuctionStatus,  # noqa: E402
                        Award, Bid, DecrementType, EmailMessage, Item,
                        Message, Notification, Organisation, Participant,
                        Role, Unit, User, Vendor)
from app.security import hash_password, make_invite             # noqa: E402

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


def login(email, password=PW):
    client = Client(app, base_url="http://test")
    client.get("/login")
    response = client.post("/login", data={"email": email, "password": password, "next": "/"},
                           follow_redirects=False)
    client.headers.update({"accept": "text/html"})
    return client, response


def anonymous():
    client = Client(app, base_url="http://test")
    client.headers.update({"accept": "text/html"})
    return client


def blocked(response):
    """Refused by any honest means: 401/403/404, or a bounce to the sign-in page."""
    if response.status_code in (401, 403, 404, 422):
        return True
    if response.status_code in (302, 303, 307):
        return "/login" in response.headers.get("location", "")
    return False


def company(db, name, tag, suppliers=3):
    org = Organisation(name=name)
    db.add(org)
    db.flush()
    buyer = User(name=f"{name} buyer", email=f"buyer@{tag}.test", role=Role.BUYER,
                 org_id=org.id, password_hash=hash_password(PW))
    admin = User(name=f"{name} admin", email=f"admin@{tag}.test", role=Role.ADMIN,
                 org_id=org.id, password_hash=hash_password(PW))
    db.add_all([buyer, admin])
    unit = Unit(code="NOS", org_id=org.id)
    item = Item(name=f"{name} pens", org_id=org.id)
    db.add_all([unit, item])
    db.flush()
    vendors = []
    for i in range(suppliers):
        vendor = Vendor(name=f"{name} supplier {i}", email=f"s{i}@{tag}.test", org_id=org.id)
        db.add(vendor)
        db.flush()
        db.add(User(name=f"S{i}", email=f"s{i}@{tag}.test", role=Role.VENDOR, org_id=org.id,
                    vendor_id=vendor.id, password_hash=hash_password(PW)))
        db.flush()
        vendors.append(vendor)
    db.commit()
    return org, buyer, unit, item, vendors


def live_auction(db, org, buyer, unit, item, vendors, ref, invited=2, landed=True,
                 show_lowest=True, show_rank=True, ceiling=5000.0):
    now = datetime.utcnow()
    auction = Auction(reference=ref, title=f"{ref} pens", creator_id=buyer.id, org_id=org.id,
                      status=AuctionStatus.LIVE, start_at=now - timedelta(minutes=5),
                      end_at=now + timedelta(hours=2), original_end_at=now + timedelta(hours=2),
                      decrement_type=DecrementType.ABSOLUTE, min_decrement=0.0,
                      compare_landed=landed, auto_extend=False, published_at=now,
                      show_lowest_bid=show_lowest, show_rank=show_rank)
    db.add(auction)
    db.flush()
    db.add(AuctionLine(auction_id=auction.id, item_id=item.id, unit_id=unit.id, qty=100,
                       starting_price=ceiling))
    for i, vendor in enumerate(vendors[:invited]):
        db.add(Participant(auction_id=auction.id, vendor_id=vendor.id, alias=f"Bidder {i + 1}"))
    db.commit()
    db.refresh(auction)
    return auction, auction.lines[0]


# --------------------------------------------------------------------------
# Part one: the walls
# --------------------------------------------------------------------------
def part_one():
    db = SessionLocal()
    acme, a_buyer, a_unit, a_item, a_vendors = company(db, "Acme", "acme")
    globex, g_buyer, g_unit, g_item, g_vendors = company(db, "Globex", "globex")
    auction, line = live_auction(db, acme, a_buyer, a_unit, a_item, a_vendors, "ACME-1")

    boss, _ = login("buyer@acme.test")
    one, _ = login("s0@acme.test")
    two, _ = login("s1@acme.test")
    outsider, _ = login("s2@acme.test")            # at Acme, but not invited
    their_boss, _ = login("buyer@globex.test")
    their_supplier, _ = login("s0@globex.test")
    nobody = anonymous()

    # Something worth stealing.
    one.post(f"/auctions/{auction.id}/bid",
             data={"line_id": str(line.id), "unit_price": "4000"}, follow_redirects=False)
    two.post(f"/auctions/{auction.id}/bid",
             data={"line_id": str(line.id), "unit_price": "3900"}, follow_redirects=False)
    one.post(f"/auctions/{auction.id}/charges",
             data={"freight": "1000", "packaging": "0", "other": "0"}, follow_redirects=False)
    one.post(f"/auctions/{auction.id}/messages",
             data={"body": "Acme secret question"}, follow_redirects=False)
    boss.post(f"/auctions/{auction.id}/documents",
              files=[("files", ("terms.txt", io.BytesIO(b"buyer only secret"), "text/plain"))],
              data={"note": ""}, follow_redirects=False)
    one.post(f"/auctions/{auction.id}/documents",
             files=[("files", ("mine.txt", io.BytesIO(b"supplier one secret"), "text/plain"))],
             data={"note": ""}, follow_redirects=False)
    db.expire_all()
    docs = db.query(Attachment).filter_by(auction_id=auction.id).all()
    buyer_doc = [d for d in docs if d.vendor_id is None][0]
    vendor_doc = [d for d in docs if d.vendor_id == a_vendors[0].id][0]
    their_bid = db.query(Bid).filter_by(auction_id=auction.id,
                                        vendor_id=a_vendors[1].id).first()
    letter = db.query(EmailMessage).filter_by(org_id=acme.id).first()

    secrets = ["Acme supplier 0", "Acme supplier 1", "s0@acme.test", "s1@acme.test",
               "buyer only secret", "Acme secret question"]

    def leaks(text):
        return [s for s in secrets if s in text]

    pages = [f"/auctions/{auction.id}", f"/auctions/{auction.id}/live",
             f"/auctions/{auction.id}?tab=history", f"/auctions/{auction.id}?tab=documents",
             f"/auctions/{auction.id}?tab=conversation", f"/auctions/{auction.id}/award",
             f"/reports/auction/{auction.id}", f"/reports/auction/{auction.id}/export/csv",
             f"/reports/auction/{auction.id}/export/pdf",
             f"/auctions/{auction.id}/documents/{buyer_doc.id}",
             f"/auctions/{auction.id}/documents/{vendor_doc.id}",
             f"/outbox/{letter.id}", f"/outbox/{letter.id}/raw"]

    def sweep(heading, client, urls):
        print(heading)
        worst = []
        for url in urls:
            response = client.get(url, follow_redirects=False)
            spilled = leaks(response.text) if response.status_code == 200 else []
            if not blocked(response) or spilled:
                worst.append(f"{url} → {response.status_code} {spilled}")
        check(f"{len(urls)} addresses, every one refused", not worst, "; ".join(worst[:3]))

    sweep("\n21. A stranger who is not signed in", nobody,
          pages + ["/", "/auctions", "/masters", "/reports", "/team", "/outbox",
                   "/notifications", "/auctions/new"])
    sweep("\n22. A buyer at another company", their_boss, pages)
    sweep("\n23. A supplier at another company", their_supplier, pages)
    sweep("\n24. A supplier here who was not invited to this auction", outsider, pages)

    print("\n25. An invited supplier sees the board, not their rivals")
    response = one.get(f"/auctions/{auction.id}")
    page = response.text
    check("the auction opens", response.status_code == 200, response.status_code)
    check("...and never names the rival", "Acme supplier 1" not in page
          and "s1@acme.test" not in page)
    check("...and lists no rival bidders at all",
          "Bidder 2" not in page and "Bidder 1" not in page)
    check("...while the supplier's own standing is there",
          "Your best bid" in page or "You have not bid" in page)
    check("a supplier cannot download another supplier's document",
          blocked(two.get(f"/auctions/{auction.id}/documents/{vendor_doc.id}",
                          follow_redirects=False)))
    check("...but can download their own",
          one.get(f"/auctions/{auction.id}/documents/{vendor_doc.id}").status_code == 200)
    check("...and the buyer's document, which was shared with bidders",
          one.get(f"/auctions/{auction.id}/documents/{buyer_doc.id}").status_code == 200)
    check("no supplier can open the audit trail",
          blocked(two.get(f"/auctions/{auction.id}?tab=history", follow_redirects=False)))
    board = two.get(f"/auctions/{auction.id}/live").text
    check("the refreshing board hides rivals too",
          "s0@acme.test" not in board and "Acme supplier 0" not in board)

    print("\n26. Buyer-only screens are closed to suppliers")
    shut = [url for url in ["/auctions/new", "/masters", "/reports", "/reports/savings.csv",
                            "/outbox", f"/auctions/{auction.id}/award",
                            f"/auctions/{auction.id}/edit"]
            if not blocked(one.get(url, follow_redirects=False))]
    check("all seven are refused", not shut, shut)

    print("\n27. Writing into another company's auction")
    writes = [
        ("publish it", f"/auctions/{auction.id}/publish", {}),
        ("start it", f"/auctions/{auction.id}/go-live", {}),
        ("cancel it", f"/auctions/{auction.id}/cancel", {"reason": "x"}),
        ("close it", f"/auctions/{auction.id}/close-now", {}),
        ("extend it", f"/auctions/{auction.id}/more-time", {"minutes": "30"}),
        ("edit it", f"/auctions/{auction.id}/edit", {"title": "hijacked"}),
        ("change how it is awarded", f"/auctions/{auction.id}/award-mode",
         {"award_mode": "basket"}),
        ("award it", f"/auctions/{auction.id}/award",
         {f"winner_{line.id}": str(a_vendors[0].id), f"price_{line.id}": "1"}),
        ("message a bidder", f"/auctions/{auction.id}/messages",
         {"body": "hello", "vendor_id": str(a_vendors[0].id)}),
        ("delete a document", f"/auctions/{auction.id}/documents/{buyer_doc.id}/remove", {}),
        ("withdraw a bid", f"/auctions/{auction.id}/bids/{their_bid.id}/withdraw",
         {"reason": "x"}),
    ]

    def snapshot():
        db.expire_all()
        db.refresh(auction)
        return (auction.status, auction.title,
                db.query(Award).filter_by(auction_id=auction.id).count(),
                db.query(Attachment).filter_by(auction_id=auction.id).count(),
                db.query(Bid).filter_by(id=their_bid.id).first().withdrawn)

    before = snapshot()
    for who, client in (("A buyer elsewhere", their_boss),
                        ("A supplier elsewhere", their_supplier)):
        broke = []
        for label, url, data in writes:
            response = client.post(url, data=data, follow_redirects=False)
            if not blocked(response) or snapshot() != before:
                broke.append(f"{label} → {response.status_code}")
        check(f"{who} cannot touch any of the eleven", not broke, "; ".join(broke))

    print("\n28. Bidding into an auction you were not asked to")
    for label, url, data in [
        ("bid", f"/auctions/{auction.id}/bid",
         {"line_id": str(line.id), "unit_price": "1"}),
        ("quote delivery costs", f"/auctions/{auction.id}/charges", {"freight": "1"}),
        ("declare taxes", f"/auctions/{auction.id}/lines/{line.id}/taxes",
         {"tax_name": "GST", "tax_percent": "18"}),
    ]:
        response = their_supplier.post(url, data=data, follow_redirects=False)
        db.expire_all()
        check(f"a supplier elsewhere cannot {label}",
              blocked(response)
              and db.query(Bid).filter_by(auction_id=auction.id,
                                          vendor_id=g_vendors[0].id).count() == 0
              and db.query(Participant).filter_by(auction_id=auction.id,
                                                  vendor_id=g_vendors[0].id).count() == 0,
              response.status_code)
    outsider.post(f"/auctions/{auction.id}/bid",
                  data={"line_id": str(line.id), "unit_price": "1"}, follow_redirects=False)
    db.expire_all()
    check("an uninvited supplier here cannot bid either",
          db.query(Bid).filter_by(auction_id=auction.id,
                                  vendor_id=a_vendors[2].id).count() == 0)

    print("\n29. Another company's supplier and item lists")
    their_boss.post(f"/masters/vendors/{a_vendors[0].id}/toggle", follow_redirects=False)
    their_boss.post(f"/masters/items/{a_item.id}/toggle", follow_redirects=False)
    their_boss.post(f"/masters/vendors/{a_vendors[0].id}/emails",
                    data={"extra_emails": "thief@elsewhere.test"}, follow_redirects=False)
    db.expire_all()
    check("a buyer elsewhere cannot archive our supplier",
          db.get(Vendor, a_vendors[0].id).is_active)
    check("...nor our item", db.get(Item, a_item.id).is_active)
    check("...nor add their address to our supplier",
          "thief@" not in (db.get(Vendor, a_vendors[0].id).extra_emails or ""))
    check("...and their own list names none of our suppliers",
          not leaks(their_boss.get("/masters").text))

    print("\n30. Reports and the outbox stay inside the company")
    page = their_boss.get("/reports?date_from=2000-01-01&date_to=2039-12-31").text
    check("their report names none of our auctions", "ACME-1" not in page and not leaks(page))
    check("their outbox holds none of our email", not leaks(their_boss.get("/outbox").text))
    check("...and their spreadsheet is clean",
          "ACME-1" not in their_boss.get("/reports/savings.csv").text)

    print("\n31. Sessions and forged forms")
    check("a signed-out visitor cannot post",
          blocked(nobody.post(f"/auctions/{auction.id}/bid",
                              data={"line_id": str(line.id), "unit_price": "1"},
                              follow_redirects=False)))
    pretend = anonymous()
    pretend.cookies.set("ra_session", "not-a-real-token")
    check("a made-up session cookie signs you out",
          blocked(pretend.get("/", follow_redirects=False)))
    # A form posted from somewhere else: real cookies, no token.
    raw = TestClient(app, base_url="http://test")
    raw.cookies.update({k: v for k, v in boss.cookies.items()})
    response = raw.post(f"/auctions/{auction.id}/cancel", data={"reason": "x"},
                        follow_redirects=False)
    db.expire_all()
    db.refresh(auction)
    check("a form with no anti-forgery token is refused",
          response.status_code == 403 and auction.status != AuctionStatus.CANCELLED,
          response.status_code)
    account = db.query(User).filter_by(email="s0@acme.test").first()
    account.is_active = False
    db.commit()
    check("switching off an account ends its access",
          blocked(one.get(f"/auctions/{auction.id}", follow_redirects=False)))
    account.is_active = True
    db.commit()

    print("\n32. An invitation lands in the company that sent it")
    token = make_invite("newcomer@acme.test", "vendor", vendor_id=a_vendors[2].id,
                        org_id=acme.id)
    guest = anonymous()
    check("the invitation opens", guest.get(f"/join/{token}").status_code == 200)
    guest.post(f"/join/{token}", data={"name": "Newcomer", "password": PW, "confirm": PW},
               follow_redirects=False)
    db.expire_all()
    made = db.query(User).filter_by(email="newcomer@acme.test").first()
    check("it creates the account", made is not None)
    check("...in the inviting company", made is not None and made.org_id == acme.id)
    check("...as a supplier tied to the right record",
          made is not None and made.role == Role.VENDOR and made.vendor_id == a_vendors[2].id)
    guest.post(f"/join/{token}", data={"name": "Again", "password": PW, "confirm": PW},
               follow_redirects=False)
    db.expire_all()
    check("the same invitation cannot make a second account",
          db.query(User).filter_by(email="newcomer@acme.test").count() == 1)
    headcount = db.query(User).count()
    response = guest.get("/join/rubbish-token", follow_redirects=False)
    guest.post("/join/rubbish-token", data={"name": "X", "password": PW, "confirm": PW},
               follow_redirects=False)
    db.expire_all()
    check("a made-up invitation is refused", response.status_code in (200, 303, 400, 403, 404),
          response.status_code)
    check("...and creates nobody", db.query(User).count() == headcount)

    print("\n33. A supplier inviting a colleague cannot promote them")
    response = one.post("/team/invite", data={"email": "sneak@acme.test", "role": "buyer"},
                        follow_redirects=False)
    db.expire_all()
    sent = (db.query(EmailMessage).filter(EmailMessage.to_email == "sneak@acme.test")
              .order_by(EmailMessage.id.desc()).first())
    check("the invitation is sent", sent is not None, response.status_code)
    link = re.search(r"/join/([A-Za-z0-9_.\-]+)",
                     (sent.html_body or "") + (sent.text_body or "")) if sent else None
    check("...and it carries a join link", link is not None)
    if link:
        colleague = anonymous()
        colleague.get(f"/join/{link.group(1)}")
        colleague.post(f"/join/{link.group(1)}",
                       data={"name": "Sneak", "password": PW, "confirm": PW},
                       follow_redirects=False)
        db.expire_all()
        joined = db.query(User).filter_by(email="sneak@acme.test").first()
        check("...but the account it makes is a supplier, never a buyer",
              joined is not None and joined.role == Role.VENDOR,
              joined.role.value if joined else "nobody")
        check("...tied to the inviter's own supplier record",
              joined is not None and joined.vendor_id == a_vendors[0].id)
        check("...and it cannot open the buyer's screens",
              blocked(colleague.get("/masters", follow_redirects=False)))
    check("a supplier's team page names no buyers",
          "buyer@acme.test" not in one.get("/team").text)

    print("\n34. Your own letters only")
    db.add(Notification(user_id=a_buyer.id, title="Acme private note", body="x", link="/"))
    db.commit()
    check("another company's notifications are invisible",
          "Acme private note" not in their_boss.get("/notifications").text)
    check("...and so are a colleague's",
          "Acme private note" not in one.get("/notifications").text)

    print("\n35. The assistant answers out of the handbook, not the database")
    answer = one.post("/assistant/ask",
                      data={"question": "what is the lowest bid on ACME-1?",
                            "context": "auction_detail_vendor"}).text
    check("it gives no prices or names away", not leaks(answer) and "3,900" not in answer)

    db.close()


# --------------------------------------------------------------------------
# Part two: stealing by id, signing up, signing out
# --------------------------------------------------------------------------
def part_two():
    db = SessionLocal()
    acme, a_buyer, a_unit, a_item, a_vendors = company(db, "Acme two", "acme2", suppliers=1)
    globex, g_buyer, g_unit, g_item, g_vendors = company(db, "Globex two", "globex2",
                                                         suppliers=1)
    boss, _ = login("buyer@globex2.test")
    soon = (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    later = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")

    def form(**over):
        base = {"title": "Try it on", "start_at": soon, "end_at": later,
                "line_item_id": str(g_item.id), "line_unit_id": str(g_unit.id),
                "line_qty": "10", "line_price": "100", "line_spec": "",
                "vendor_ids": str(g_vendors[0].id), "decrement_type": "absolute",
                "min_decrement": "0"}
        base.update(over)
        return base

    print("\n36. Building an auction out of another company's records")
    before = db.query(Auction).count()
    boss.post("/auctions/new", data=form(vendor_ids=str(a_vendors[0].id)),
              follow_redirects=False)
    db.expire_all()
    check("a ticked bidder from another company is refused",
          db.query(Participant).filter_by(vendor_id=a_vendors[0].id).count() == 0
          and db.query(Auction).count() == before)
    boss.post("/auctions/new", data=form(line_item_id=str(a_item.id)), follow_redirects=False)
    db.expire_all()
    check("an item from another company is refused",
          db.query(AuctionLine).filter_by(item_id=a_item.id).count() == 0)
    boss.post("/auctions/new", data=form(line_unit_id=str(a_unit.id)), follow_redirects=False)
    db.expire_all()
    check("a unit from another company is refused",
          db.query(AuctionLine).filter_by(unit_id=a_unit.id).count() == 0)
    boss.post("/masters/quick/item", data={"name": "Sneaky",
                                           "default_unit_id": str(a_unit.id)})
    db.expire_all()
    check("a quick-added item cannot borrow another company's unit",
          db.query(Item).filter_by(name="Sneaky").count() == 0)

    print("\n37. A real auction, then edited to let a stranger in")
    boss.post("/auctions/new", data=form(title="Globex proper"), follow_redirects=False)
    db.expire_all()
    mine = db.query(Auction).filter_by(title="Globex proper").first()
    check("the honest auction is created", mine is not None)
    boss.post(f"/auctions/{mine.id}/edit",
              data=form(title="Globex proper",
                        vendor_ids=[str(g_vendors[0].id), str(a_vendors[0].id)]),
              follow_redirects=False)
    db.expire_all()
    check("...and cannot be edited to invite another company's supplier",
          db.query(Participant).filter_by(auction_id=mine.id,
                                          vendor_id=a_vendors[0].id).count() == 0)
    boss.post(f"/auctions/{mine.id}/messages",
              data={"body": "hello stranger", "vendor_id": str(a_vendors[0].id)},
              follow_redirects=False)
    db.expire_all()
    check("...nor used to message one",
          db.query(Message).filter_by(vendor_id=a_vendors[0].id).count() == 0)

    print("\n38. Signing up from the street")
    walk_in = anonymous()
    walk_in.get("/signup")
    walk_in.post("/signup", data={"name": "Walk in", "email": "walkin@nowhere.test",
                                  "password": PW, "company": "Nowhere Ltd", "role": "admin"},
                 follow_redirects=False)
    db.expire_all()
    made = db.query(User).filter_by(email="walkin@nowhere.test").first()
    check("the account is created", made is not None)
    check("...in a company of its own",
          made is not None and made.org_id not in (acme.id, globex.id))
    check("...as a buyer, whatever the form asked for",
          made is not None and made.role == Role.BUYER, made.role.value if made else "")
    check("...which can see none of ours", "Globex proper" not in walk_in.get("/auctions").text)
    check("...and cannot open ours by its number",
          blocked(walk_in.get(f"/auctions/{mine.id}", follow_redirects=False)))

    print("\n39. The same address at two companies")
    again = anonymous()
    again.get("/signup")
    again.post("/signup", data={"name": "Same person", "email": "buyer@globex2.test",
                                "password": "another-one", "company": "Third Co"},
               follow_redirects=False)
    db.expire_all()
    rows = db.query(User).filter_by(email="buyer@globex2.test").all()
    check("it is allowed — one address, two companies, two logins", len(rows) == 2, len(rows))
    if len(rows) == 2:
        check("...and they sit in different companies", rows[0].org_id != rows[1].org_id)
        other, _ = login("buyer@globex2.test", "another-one")
        check("...and the new one sees none of the old one's auctions",
              "Globex proper" not in other.get("/auctions").text)

    print("\n40. A document with a dangerous name")
    fresh, _ = login("buyer@globex2.test")
    fresh.post(f"/auctions/{mine.id}/documents",
               files=[("files", ("../../../../etc/hijacked.txt",
                                 io.BytesIO(b"x" * 20), "text/plain"))],
               data={"note": ""}, follow_redirects=False)
    db.expire_all()
    doc = (db.query(Attachment).filter_by(auction_id=mine.id)
             .order_by(Attachment.id.desc()).first())
    check("the file is stored", doc is not None)
    if doc:
        where = documents.path_for(mine.id, doc.stored_name).resolve()
        check("...inside the app's own folder, under a name of our choosing",
              str(where).startswith(str(Path(config.DATA_DIR).resolve())) and ".." not in doc.stored_name,
              doc.stored_name)
        check("...with the dangerous path stripped from what is shown",
              "/" not in doc.filename and "\\" not in doc.filename, doc.filename)
    check("...and nothing appeared where it was aimed", not Path("/etc/hijacked.txt").exists())

    print("\n41. Wrong passwords give nothing away")
    _, real = login("buyer@globex2.test", "wrong-password")
    _, invented = login("nobody@nowhere.test", "wrong-password")
    check("a real address and a made-up one answer the same way",
          (real.status_code, real.headers.get("location", "")) ==
          (invented.status_code, invented.headers.get("location", "")))

    # Last, because signing out ends this person's other sessions too.
    print("\n42. Signing out")
    going, _ = login("buyer@globex2.test")
    kept = dict(going.cookies)
    going.post("/logout", follow_redirects=False)
    copied = anonymous()
    for name, value in kept.items():
        copied.cookies.set(name, value)
    response = copied.get("/auctions", follow_redirects=False)
    check("a cookie copied before signing out stops working", blocked(response),
          response.status_code)
    check("...and the browser's own cookie is cleared", not going.cookies.get("ra_session"))
    back, _ = login("buyer@globex2.test")
    check("...while signing in again works normally",
          back.get("/auctions").status_code == 200)

    db.close()


# --------------------------------------------------------------------------
# Part three: one bidder against another
# --------------------------------------------------------------------------
def part_three():
    db = SessionLocal()
    org, buyer, unit, item, vendors = company(db, "Rivals", "rivals", suppliers=2)
    one, _ = login("s0@rivals.test")
    two, _ = login("s1@rivals.test")
    boss, _ = login("buyer@rivals.test")

    print("\n43. One bidder's costs are their own")
    auction, line = live_auction(db, org, buyer, unit, item, vendors, "RIV-1")
    one.post(f"/auctions/{auction.id}/charges",
             data={"freight": "7777", "packaging": "0", "other": "0"}, follow_redirects=False)
    two.post(f"/auctions/{auction.id}/charges",
             data={"freight": "2222", "packaging": "0", "other": "0"}, follow_redirects=False)
    one.post(f"/auctions/{auction.id}/lines/{line.id}/taxes",
             data={"tax_name": "GST", "tax_percent": "18"}, follow_redirects=False)
    one.post(f"/auctions/{auction.id}/bid",
             data={"line_id": str(line.id), "unit_price": "4000"}, follow_redirects=False)
    two.post(f"/auctions/{auction.id}/bid",
             data={"line_id": str(line.id), "unit_price": "3900"}, follow_redirects=False)
    page = two.get(f"/auctions/{auction.id}").text
    check("a bidder never sees a rival's freight", "7,777" not in page and "7777" not in page)
    check("...but does see their own", "2,222" in page)
    page = one.get(f"/auctions/{auction.id}").text
    check("...and the same the other way round", "2,222" not in page and "2222" not in page)
    board = boss.get(f"/auctions/{auction.id}").text
    check("the buyer sees both", "7,777" in board and "2,222" in board)

    print("\n44. A conversation is between one bidder and the buyer")
    one.post(f"/auctions/{auction.id}/messages",
             data={"body": "Supplier zero private question"}, follow_redirects=False)
    boss.post(f"/auctions/{auction.id}/messages",
              data={"body": "Answer for supplier zero only", "vendor_id": str(vendors[0].id)},
              follow_redirects=False)
    page = two.get(f"/auctions/{auction.id}?tab=conversation").text
    check("the rival cannot read the question", "Supplier zero private question" not in page)
    check("...nor the buyer's answer", "Answer for supplier zero only" not in page)
    page = one.get(f"/auctions/{auction.id}?tab=conversation").text
    check("...while the bidder who asked sees both",
          "Supplier zero private question" in page and "Answer for supplier zero only" in page)
    check("the refreshing board carries no messages either",
          "Supplier zero private question" not in two.get(f"/auctions/{auction.id}/live").text)

    print("\n45. When the buyer hides the standings")
    quiet, quiet_line = live_auction(db, org, buyer, unit, item, vendors, "RIV-2",
                                     landed=False, show_lowest=False, show_rank=False)
    one.post(f"/auctions/{quiet.id}/bid",
             data={"line_id": str(quiet_line.id), "unit_price": "3000"}, follow_redirects=False)
    two.post(f"/auctions/{quiet.id}/bid",
             data={"line_id": str(quiet_line.id), "unit_price": "2900"}, follow_redirects=False)
    page = one.get(f"/auctions/{quiet.id}").text
    check("the leading price is nowhere on the page",
          "2,900" not in page and "2900" not in page)
    check("...not even in the bid box's hidden limits", 'data-max="2899' not in page
          and 'data-max="2,899' not in page)
    check("...nor is a rank", "You are at L" not in page and "You are L1" not in page)
    check("...but their own bid still is", "3,000" in page)
    check("the refreshing board hides it too",
          "2,900" not in one.get(f"/auctions/{quiet.id}/live").text)

    print("\n46. ...the refusal cannot be read backwards either")
    response = one.post(f"/auctions/{quiet.id}/bid",
                        data={"line_id": str(quiet_line.id), "unit_price": "2950"},
                        follow_redirects=False)
    from http.cookies import SimpleCookie
    import json as _json
    jar = SimpleCookie()
    jar.load(response.headers.get("set-cookie", ""))
    said = (_json.loads(jar["ra_flash"].value)["m"] if "ra_flash" in jar else "")
    check("a bid that is too high is still refused", "Too high" in said, said[:70])
    check("...without naming the price to beat", "2,900" not in said and "2900" not in said,
          said[:90])
    db.expire_all()
    check("...and nothing was recorded",
          db.query(Bid).filter_by(line_id=quiet_line.id, unit_price=2950.0).count() == 0)

    print("\n47. ...and the emails keep the secret too")
    db.expire_all()
    letters = (db.query(EmailMessage).filter(EmailMessage.auction_id == quiet.id).all())
    confirmations = [m for m in letters if m.event == "bid_received"]
    outbids = [m for m in letters if m.event == "outbid"]
    check("a bid confirmation was sent", bool(confirmations), len(confirmations))
    check("...with no rank in it",
          all("Your rank" not in (m.html_body or "") and "rank L" not in (m.html_body or "")
              for m in confirmations))
    check("an outbid warning was sent", bool(outbids), len(outbids))
    check("...telling them they are behind, without the price",
          all("2,900" not in (m.html_body or "") for m in outbids))

    print("\n48. ...while an ordinary auction still says everything")
    open_one, open_line = live_auction(db, org, buyer, unit, item, vendors, "RIV-3",
                                       landed=False)
    one.post(f"/auctions/{open_one.id}/bid",
             data={"line_id": str(open_line.id), "unit_price": "3000"}, follow_redirects=False)
    two.post(f"/auctions/{open_one.id}/bid",
             data={"line_id": str(open_line.id), "unit_price": "2900"}, follow_redirects=False)
    page = one.get(f"/auctions/{open_one.id}").text
    check("the lowest price is shown", "2,900" in page)
    check("...and so is the rank", "You are at L2" in page)
    db.expire_all()
    outbids = [m for m in db.query(EmailMessage).filter(EmailMessage.auction_id == open_one.id)
               if m.event == "outbid"]
    check("...and the outbid email carries the figure",
          any("2,900" in (m.html_body or "") for m in outbids))

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
    print("All access and privacy checks passed.")
    return 0


def test_access():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
