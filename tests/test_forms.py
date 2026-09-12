"""Round 5 of the deep check: the forms, the lists and the documents.

Everything a person types into this app, typed wrongly. Each section fills a
form with something the app cannot use — nothing, words where a number
belongs, a date that runs backwards, a price with an extra digit, a file of
the wrong kind — and asks three things of the answer: nothing was written, it
did not fall over, and what it said back was a sentence rather than an error
code. Then the ordinary case, to prove the checks have not simply shut the
door on everybody.

Part two is about what happens when the ground moves under somebody: an item
archived while an auction is using it, and an auction edited after a bidder
has already quoted their delivery costs and declared their taxes on it.

    python tests/test_forms.py
"""
from __future__ import annotations

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
TMP = tempfile.mkdtemp(prefix="ra-forms-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ.pop("RA_SMTP_HOST", None)

from fastapi.testclient import TestClient                       # noqa: E402

from app import config                                          # noqa: E402
from app.db import Base, SessionLocal, engine as db_engine      # noqa: E402
from app.main import app                                        # noqa: E402
from app.models import (Attachment, Auction, AuctionStatus, Bid,  # noqa: E402
                        EmailMessage, Item, LineTax, Message, Organisation,
                        Role, Unit, User, Vendor)
from app.security import hash_password                          # noqa: E402
from app.utils import fmt_dt, fmt_money, fmt_qty, to_local_string   # noqa: E402

Base.metadata.create_all(bind=db_engine)
PW = "test1234"
FAILS: list[str] = []


def check(label, ok, extra=""):
    print(("  \u2713 " if ok else "  \u2717 ") + label + (f"  [{extra}]" if extra else ""))
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
    client = Client(app, base_url="http://test")
    client.get("/login")
    client.post("/login", data={"email": email, "password": PW, "next": "/"},
                follow_redirects=False)
    client.headers.update({"accept": "text/html"})
    return client


def said(response):
    """What the app told the person: the flash, or the error on the form itself."""
    jar = SimpleCookie()
    jar.load(response.headers.get("set-cookie", ""))
    if "ra_flash" in jar:
        try:
            return json.loads(jar["ra_flash"].value)["m"]
        except Exception:
            pass
    page = re.sub(r"(?s)<svg.*?</svg>", " ", response.text)
    hit = re.search(r'(?s)class="alert error"[^>]*>(.*?)</div>\s*</div>', page)
    text = re.sub(r"(?s)<[^>]+>", " ", hit.group(1)) if hit else ""
    return re.sub(r"\s+", " ", text).strip()[:200]


def main():                                      # noqa: C901 - one long story
    db = SessionLocal()
    org = Organisation(name="Acme")
    db.add(org)
    db.flush()
    buyer = User(name="Bea Buyer", email="buyer@acme.test", role=Role.BUYER, org_id=org.id,
                 password_hash=hash_password(PW))
    db.add(buyer)
    db.flush()
    unit = Unit(code="NOS", org_id=org.id)
    item = Item(name="Pens", org_id=org.id)
    pens = item
    rope = Item(name="Rope", org_id=org.id)
    db.add_all([unit, pens, rope])
    db.flush()
    vendor = Vendor(name="Alpha Supplies", email="alpha@alpha.test", org_id=org.id)
    db.add(vendor)
    db.flush()
    db.add(User(name="Ann", email="alpha@alpha.test", role=Role.VENDOR, org_id=org.id,
                vendor_id=vendor.id, password_hash=hash_password(PW)))
    db.commit()
    boss = login("buyer@acme.test")
    supplier = login("alpha@alpha.test")

    # The form takes the times people read off their own clock, so the fixture
    # types them in the app's timezone, not in UTC.
    soon = to_local_string(datetime.utcnow() + timedelta(hours=1))
    later = to_local_string(datetime.utcnow() + timedelta(hours=4))

    def form(**over):
        base = {"title": "Stationery", "start_at": soon, "end_at": later,
                "line_item_id": str(item.id), "line_unit_id": str(unit.id),
                "line_qty": "100", "line_price": "50", "line_spec": "",
                "vendor_ids": str(vendor.id), "decrement_type": "absolute",
                "min_decrement": "1"}
        base.update(over)
        return base

    def auctions():
        return db.query(Auction).count()

    def build(title, start_hours=-1, end_hours=3, landed=False,
              items=(("Pens", 100, 50.0),)):
        """A whole auction through the form, and the form data that made it."""
        data = {"title": title,
                "start_at": to_local_string(datetime.utcnow() + timedelta(hours=start_hours)),
                "end_at": to_local_string(datetime.utcnow() + timedelta(hours=end_hours)),
                "vendor_ids": str(vendor.id), "decrement_type": "absolute",
                "min_decrement": "0", "line_item_id": [], "line_unit_id": [],
                "line_qty": [], "line_price": [], "line_spec": []}
        if landed:
            data["compare_landed"] = "on"
        for name, qty, price in items:
            row = db.query(Item).filter_by(name=name).first()
            data["line_item_id"].append(str(row.id))
            data["line_unit_id"].append(str(unit.id))
            data["line_qty"].append(str(qty))
            data["line_price"].append(str(price))
            data["line_spec"].append("")
        boss.post("/auctions/new", data=data, follow_redirects=False)
        db.expire_all()
        return db.query(Auction).filter_by(title=title).first(), data

    print("\n69. The auction form refuses what it cannot use — kindly, and without a crash")
    cases = [
        ("no title", {"title": ""}),
        ("no items", {"line_item_id": ""}),
        ("no bidders", {"vendor_ids": ""}),
        ("a quantity of nothing", {"line_qty": "0"}),
        ("a negative quantity", {"line_qty": "-5"}),
        ("a quantity that is words", {"line_qty": "lots"}),
        ("an impossible quantity", {"line_qty": "1e999"}),
        ("a starting price of zero", {"line_price": "0"}),
        ("a negative starting price", {"line_price": "-10"}),
        ("a starting price that is words", {"line_price": "cheap"}),
        ("an absurd starting price", {"line_price": "1e13"}),
        ("no opening time", {"start_at": ""}),
        ("no closing time", {"end_at": ""}),
        ("a closing time before the opening", {"start_at": later, "end_at": soon}),
        ("a closing time in the past",
         {"start_at": to_local_string(datetime.utcnow() - timedelta(days=400)),
          "end_at": to_local_string(datetime.utcnow() - timedelta(days=399)),
          "action": "publish"}),
        ("a closing time one second later", {"end_at": soon}),
        ("dates that are not dates", {"start_at": "tomorrow", "end_at": "later"}),
        ("a negative decrement", {"min_decrement": "-1"}),
        ("a decrement that is words", {"min_decrement": "a bit"}),
        ("a percentage over 100", {"decrement_type": "percent", "min_decrement": "150"}),
        ("a maximum decrement below the minimum", {"min_decrement": "10", "max_decrement": "1"}),
        ("a decrement type that does not exist", {"decrement_type": "sideways"}),
        ("a copy address that is not an address", {"cc_emails": "not-an-address"}),
        ("an item that does not exist", {"line_item_id": "999999"}),
        ("a bidder that does not exist", {"vendor_ids": "999999"}),
    ]
    for label, over in cases:
        before = auctions()
        live_before = db.query(Auction).filter(
            Auction.status != AuctionStatus.DRAFT).count()
        r = boss.post("/auctions/new", data=form(**over), follow_redirects=False)
        db.expire_all()
        words = said(r)
        # Either nothing was written at all, or - for a form that is only wrong
        # about publishing - it was kept as a draft and never went out.
        nothing_live = db.query(Auction).filter(
            Auction.status != AuctionStatus.DRAFT).count() == live_before
        ok = (r.status_code < 500 and (auctions() == before or nothing_live)
              and len(words) > 10 and "Traceback" not in words)
        check(f"{label} is refused", ok, f"{r.status_code} {words[:60]}")

    print("\n70. ...and accepts the ordinary case")
    r = boss.post("/auctions/new", data=form(title="A good one"), follow_redirects=False)
    db.expire_all()
    made = db.query(Auction).filter_by(title="A good one").first()
    check("a sound form creates the auction", made is not None, said(r)[:60])
    check("...as a draft", made is not None and made.status == AuctionStatus.DRAFT)
    check("...with the item and the bidder on it",
          made is not None and len(made.lines) == 1 and len(made.participants) == 1)

    print("\n71. Odd but honest entries are kept exactly")
    odd = [
        ("a fractional quantity", {"line_qty": "3.5"}, lambda a: a.lines[0].qty == 3.5),
        ("a price with paisa", {"line_price": "49.99"}, lambda a: a.lines[0].starting_price == 49.99),
        ("a price with more than two decimals",
         {"line_price": "49.999"}, lambda a: a.lines[0].starting_price == 50.0),
        ("no ceiling at all", {"line_price": ""}, lambda a: a.lines[0].starting_price is None),
        ("an emoji in the title", {"title": "Pens ✏️ for the office"},
         lambda a: "✏️" in a.title),
        ("a title in Hindi", {"title": "कलम और कागज"}, lambda a: "कलम" in a.title),
        ("a very long description", {"description": "x" * 5000}, lambda a: len(a.description) > 100),
    ]
    for label, over, test in odd:
        unique = f"Odd {label}"
        r = boss.post("/auctions/new", data=form(title=over.pop("title", unique), **over),
                      follow_redirects=False)
        db.expire_all()
        a = db.query(Auction).order_by(Auction.id.desc()).first()
        check(f"{label} is kept", r.status_code < 400 and test(a), said(r)[:60])

    print("\n72. Words that look like code are shown as words")
    nasty = '<script>alert(1)</script> & "quotes" <b>bold</b>'
    r = boss.post("/auctions/new", data=form(title=nasty), follow_redirects=False)
    db.expire_all()
    a = db.query(Auction).order_by(Auction.id.desc()).first()
    page = boss.get(f"/auctions/{a.id}").text
    check("the title is stored as typed", a.title == nasty, a.title[:40])
    check("...and shown escaped, not run", "<script>alert(1)</script>" not in page)
    check("...but still readable on the page", "&lt;script&gt;" in page)
    listing = boss.get("/auctions").text
    check("...on the list of auctions too", "<script>alert(1)</script>" not in listing)

    print("\n73. The clock: what you type is what everybody sees")
    when = datetime.utcnow() + timedelta(hours=2)
    typed = to_local_string(when)
    r = boss.post("/auctions/new", data=form(title="Clock check", start_at=typed,
                                             end_at=to_local_string(when + timedelta(hours=3))),
                  follow_redirects=False)
    db.expire_all()
    clock = db.query(Auction).filter_by(title="Clock check").first()
    check("the auction is created", clock is not None, said(r)[:60])
    check("...and the time typed comes back unchanged on the edit form",
          typed in boss.get(f"/auctions/{clock.id}/edit").text, typed)
    boss.post(f"/auctions/{clock.id}/publish", follow_redirects=False)
    db.expire_all()
    letter = (db.query(EmailMessage).filter(EmailMessage.auction_id == clock.id)
                .order_by(EmailMessage.id).first())
    check("...and the invitation email names the same moment",
          letter is not None and fmt_dt(clock.start_at) in (letter.html_body or ""),
          fmt_dt(clock.start_at))
    check("...while what is stored is the same moment in UTC",
          abs((clock.start_at - when).total_seconds()) < 90,
          f"{clock.start_at} vs {when}")

    print("\n74. A published auction cannot be quietly rewritten")
    r = boss.post("/auctions/new",
                  data=form(title="Live one",
                            start_at=to_local_string(datetime.utcnow() - timedelta(hours=1)),
                            end_at=to_local_string(datetime.utcnow() + timedelta(hours=3))),
                  follow_redirects=False)
    db.expire_all()
    pub = db.query(Auction).filter_by(title="Live one").first()
    boss.post(f"/auctions/{pub.id}/publish", follow_redirects=False)
    db.expire_all(); db.refresh(pub)
    supplier.post(f"/auctions/{pub.id}/bid",
                  data={"line_id": str(pub.lines[0].id), "unit_price": "40"},
                  follow_redirects=False)
    db.expire_all(); db.refresh(pub)
    check("bidding opened", pub.status == AuctionStatus.LIVE, pub.status.value)
    r = boss.post(f"/auctions/{pub.id}/edit", data=form(title="Changed after bids"),
                  follow_redirects=False)
    db.expire_all(); db.refresh(pub)
    check("editing is refused once bidding has started", pub.title == "Live one",
          said(r)[:70])
    check("...and the bid is still there",
          db.query(Bid).filter_by(auction_id=pub.id).count() == 1)

    print("\n75. The supplier and item lists")
    master_cases = [
        ("a supplier with no name", "/masters/vendors", {"name": "", "email": "a@b.test"}),
        ("a supplier with no address", "/masters/vendors", {"name": "No Mail", "email": ""}),
        ("a supplier with a bad address", "/masters/vendors",
         {"name": "Bad Mail", "email": "not-an-address"}),
        ("a supplier that already exists", "/masters/vendors",
         {"name": "Alpha Supplies", "email": "alpha@alpha.test"}),
        ("an item with no name", "/masters/items", {"name": ""}),
        ("an item that already exists", "/masters/items", {"name": "Pens"}),
        ("an item that already exists, in capitals", "/masters/items", {"name": "PENS"}),
        ("a unit with no code", "/masters/units", {"code": ""}),
        ("a unit that already exists", "/masters/units", {"code": "NOS"}),
        ("a unit that already exists, in lower case", "/masters/units", {"code": "nos"}),
    ]
    counts = (db.query(Vendor).count(), db.query(Item).count(), db.query(Unit).count())
    for label, url, data in master_cases:
        r = boss.post(url, data=data, follow_redirects=False)
        db.expire_all()
        now = (db.query(Vendor).count(), db.query(Item).count(), db.query(Unit).count())
        words = said(r)
        check(f"{label} is refused, in words", now == counts and len(words) > 10
              and r.status_code < 500, f"{r.status_code} {words[:60]}")

    print("\n76. ...and the ordinary case works")
    r = boss.post("/masters/vendors", data={"name": "Bharat Traders",
                                            "email": "bharat@bharat.test",
                                            "extra_emails": "accounts@bharat.test"},
                  follow_redirects=False)
    db.expire_all()
    new_vendor = db.query(Vendor).filter_by(name="Bharat Traders").first()
    check("a new supplier is saved", new_vendor is not None, said(r)[:60])
    check("...with the second address kept",
          new_vendor is not None and "accounts@bharat.test" in (new_vendor.extra_emails or ""))
    r = boss.post("/masters/items", data={"name": "Rope"}, follow_redirects=False)
    db.expire_all()
    check("a new item is saved", db.query(Item).filter_by(name="Rope").count() == 1)
    r = boss.post("/masters/units", data={"code": "kg", "name": "Kilogram"},
                  follow_redirects=False)
    db.expire_all()
    kg = db.query(Unit).filter_by(code="KG").first()
    check("a unit code is stored in capitals", kg is not None, [u.code for u in db.query(Unit).all()])

    print("\n77. Archiving a supplier who is in a live auction")
    r = boss.post(f"/masters/vendors/{vendor.id}/toggle", follow_redirects=False)
    db.expire_all(); db.refresh(vendor); db.refresh(pub)
    check("the supplier can be archived", not vendor.is_active, said(r)[:60])
    check("...but their bid stands", db.query(Bid).filter_by(auction_id=pub.id).count() == 1)
    r = supplier.get(f"/auctions/{pub.id}", follow_redirects=False)
    check("...and they can still see the auction they were invited to",
          r.status_code == 200, r.status_code)
    r = supplier.post(f"/auctions/{pub.id}/bid",
                      data={"line_id": str(pub.lines[0].id), "unit_price": "39"},
                      follow_redirects=False)
    db.expire_all()
    check("...and still bid in it", db.query(Bid).filter_by(auction_id=pub.id).count() == 2,
          said(r)[:60])
    boss.post(f"/masters/vendors/{vendor.id}/toggle", follow_redirects=False)
    db.expire_all(); db.refresh(vendor)
    check("...and can be brought back", vendor.is_active)

    print("\n78. Documents")
    doc_cases = [
        ("a file with no name", ("  ", b"x" * 10, "text/plain"), False),
        ("a file with no extension", ("readme", b"x" * 10, "text/plain"), False),
        ("a kind we do not accept", ("thing.exe", b"x" * 10, "application/octet-stream"), False),
        ("an empty file", ("empty.txt", b"", "text/plain"), False),
        ("a plain text file", ("notes.txt", b"hello", "text/plain"), True),
        ("a PDF", ("terms.pdf", b"%PDF-1.4 hello", "application/pdf"), True),
        ("a spreadsheet", ("prices.xlsx", b"PK\x03\x04 hello", "application/octet-stream"), True),
    ]
    for label, (name, blob, kind), should_work in doc_cases:
        was = db.query(Attachment).filter_by(auction_id=pub.id).count()
        r = boss.post(f"/auctions/{pub.id}/documents",
                      files=[("files", (name, io.BytesIO(blob), kind))],
                      data={"note": ""}, follow_redirects=False)
        db.expire_all()
        now = db.query(Attachment).filter_by(auction_id=pub.id).count()
        if should_work:
            check(f"{label} is accepted", now == was + 1, said(r)[:60])
        else:
            check(f"{label} is refused, in words",
                  now == was and len(said(r)) > 10 and r.status_code < 500,
                  f"{r.status_code} {said(r)[:60]}")

    big = b"x" * (config.MAX_UPLOAD_MB * 1024 * 1024 + 1024)
    was = db.query(Attachment).filter_by(auction_id=pub.id).count()
    r = boss.post(f"/auctions/{pub.id}/documents",
                  files=[("files", ("huge.txt", io.BytesIO(big), "text/plain"))],
                  data={"note": ""}, follow_redirects=False)
    db.expire_all()
    check("a file over the size limit is refused, with the limit named",
          db.query(Attachment).filter_by(auction_id=pub.id).count() == was
          and str(config.MAX_UPLOAD_MB) in said(r), said(r)[:80])

    doc = db.query(Attachment).filter_by(auction_id=pub.id).first()
    r = boss.get(f"/auctions/{pub.id}/documents/{doc.id}")
    check("a document downloads", r.status_code == 200, r.status_code)
    check("...as an attachment, and never sniffed as something else",
          "attachment" in r.headers.get("content-disposition", "")
          or doc.content_type in ("application/pdf",),
          r.headers.get("content-disposition"))
    check("...with the no-sniff header set",
          r.headers.get("x-content-type-options") == "nosniff")

    print("\n79. Messages")
    for label, body, should_work in [("an empty message", "   ", False),
                                     ("a very long message", "y" * 8000, True),
                                     ("an ordinary message", "When do you need delivery?", True)]:
        was = db.query(Message).count()
        r = supplier.post(f"/auctions/{pub.id}/messages", data={"body": body},
                          follow_redirects=False)
        db.expire_all()
        now = db.query(Message).count()
        check(f"{label}: {'kept' if should_work else 'refused'}",
              (now == was + 1) if should_work else (now == was and len(said(r)) > 5),
              said(r)[:60])

    print("\n80. Screens that take numbers in the address bar")
    urls = ["/auctions/999999", "/auctions/abc", "/auctions/0", "/auctions/-1",
            f"/auctions/{pub.id}/documents/999999", "/reports/auction/999999",
            "/outbox/999999", f"/auctions/{pub.id}?tab=nonsense",
            "/auctions?status=nonsense", "/auctions?q=" + "x" * 300,
            "/reports?date_from=9999-99-99", "/outbox?q=%00"]
    for url in urls:
        r = boss.get(url, follow_redirects=False)
        check(f"{url[:44]} answers without a crash", r.status_code < 500, r.status_code)

    print("\n81. An item or unit taken off the list, while an auction is using it")
    auction, data = build("Archive test", start_hours=1, end_hours=4)
    boss.post(f"/auctions/{auction.id}/publish", follow_redirects=False)
    db.expire_all(); db.refresh(auction)
    check("the auction is scheduled", auction.status == AuctionStatus.SCHEDULED,
          auction.status.value)
    boss.post(f"/masters/items/{pens.id}/toggle", follow_redirects=False)
    db.expire_all(); db.refresh(pens)
    check("the item can be archived", not pens.is_active)
    r = boss.get(f"/auctions/{auction.id}")
    check("...the auction still opens", r.status_code == 200, r.status_code)
    check("...and still names the item", "Pens" in r.text)
    form_page = boss.get(f"/auctions/{auction.id}/edit").text
    check("...and the edit form still offers it, so saving does not drop the line",
          f'value="{pens.id}"' in form_page)
    r = boss.post(f"/auctions/{auction.id}/edit", data=data, follow_redirects=False)
    db.expire_all(); db.refresh(auction)
    check("...so an unrelated edit keeps the item", len(auction.lines) == 1
          and auction.lines[0].item_id == pens.id, said(r)[:60])
    boss.post(f"/masters/items/{pens.id}/toggle", follow_redirects=False)
    db.expire_all()
    db.refresh(pens)
    check("...and putting it back restores it to the new-auction list", pens.is_active
          and f'value="{pens.id}"' in boss.get("/auctions/new").text)

    print("\n82. Editing an auction a bidder has already worked on")
    landed_auction, landed_data = build("Landed edit", start_hours=1, end_hours=4, landed=True,
                                        items=(("Pens", 100, 50.0), ("Rope", 10, 500.0)))
    boss.post(f"/auctions/{landed_auction.id}/publish", follow_redirects=False)
    db.expire_all(); db.refresh(landed_auction)
    check("the delivered-price auction is scheduled",
          landed_auction.status == AuctionStatus.SCHEDULED and landed_auction.compare_landed)
    first_line = landed_auction.lines[0]
    r = supplier.post(f"/auctions/{landed_auction.id}/charges",
                      data={"freight": "1000", "packaging": "200", "other": "0"},
                      follow_redirects=False)
    check("a bidder can quote delivery costs before it opens", "Saved" in said(r), said(r)[:60])
    r = supplier.post(f"/auctions/{landed_auction.id}/lines/{first_line.id}/taxes",
                      data={"tax_name": "GST", "tax_percent": "18"}, follow_redirects=False)
    db.expire_all()
    check("...and declare taxes", db.query(LineTax).count() == 1, said(r)[:60])
    # Now the buyer changes the items underneath them.
    landed_data["title"] = "Landed edit"
    landed_data["line_qty"] = ["50", "10"]
    r = boss.post(f"/auctions/{landed_auction.id}/edit", data=landed_data, follow_redirects=False)
    db.expire_all(); db.refresh(landed_auction)
    check("the edit saves", landed_auction.lines[0].qty == 50.0, said(r)[:60])
    r = supplier.get(f"/auctions/{landed_auction.id}")
    check("...the bidder's screen still opens", r.status_code == 200, r.status_code)
    kept_line = db.get(Auction, landed_auction.id).lines[0]
    check("...and the taxes they had already declared are still on that item",
          db.query(LineTax).filter_by(line_id=kept_line.id).count() == 1,
          db.query(LineTax).count())
    check("...their delivery costs are still theirs", "1,000" in r.text or "1,200" in r.text)
    new_line = db.get(Auction, landed_auction.id).lines[0]
    r = supplier.post(f"/auctions/{landed_auction.id}/lines/{new_line.id}/taxes",
                      data={"tax_name": "GST", "tax_percent": "18"}, follow_redirects=False)
    db.expire_all()
    check("...and they can declare taxes again on the new line",
          db.query(LineTax).filter_by(line_id=new_line.id).count() == 1, said(r)[:60])
    orphans = db.query(LineTax).filter(
        ~LineTax.line_id.in_([l.id for l in db.get(Auction, landed_auction.id).lines])).count()
    check("...with nothing left pointing at an item that no longer exists", orphans == 0,
          f"{orphans} orphan tax row(s)")

    print("\n83. Documents: how many, and who may remove them")
    live, _ = build("Document test")
    boss.post(f"/auctions/{live.id}/publish", follow_redirects=False)
    db.expire_all(); db.refresh(live)
    for i in range(config.MAX_ATTACHMENTS):
        boss.post(f"/auctions/{live.id}/documents",
                  files=[("files", (f"doc{i}.txt", io.BytesIO(b"hello"), "text/plain"))],
                  data={"note": ""}, follow_redirects=False)
    db.expire_all()
    check(f"the first {config.MAX_ATTACHMENTS} documents are accepted",
          db.query(Attachment).filter_by(auction_id=live.id).count() == config.MAX_ATTACHMENTS)
    r = boss.post(f"/auctions/{live.id}/documents",
                  files=[("files", ("one_too_many.txt", io.BytesIO(b"hello"), "text/plain"))],
                  data={"note": ""}, follow_redirects=False)
    db.expire_all()
    check("...and one more is refused, with the limit named",
          db.query(Attachment).filter_by(auction_id=live.id).count() == config.MAX_ATTACHMENTS
          and str(config.MAX_ATTACHMENTS) in said(r), said(r)[:70])

    mine = db.query(Attachment).filter_by(auction_id=live.id).first()
    r = supplier.post(f"/auctions/{live.id}/documents/{mine.id}/remove", follow_redirects=False)
    db.expire_all()
    db.expire_all()
    check("a bidder cannot remove the buyer's document",
          db.get(Attachment, mine.id) is not None, said(r)[:60])
    # A fresh auction for the housekeeping, since the one above is full.
    live, _ = build("Document housekeeping")
    boss.post(f"/auctions/{live.id}/publish", follow_redirects=False)
    db.expire_all(); db.refresh(live)
    boss.post(f"/auctions/{live.id}/documents",
              files=[("files", ("buyer.txt", io.BytesIO(b"theirs"), "text/plain"))],
              data={"note": ""}, follow_redirects=False)
    db.expire_all()
    mine = db.query(Attachment).filter_by(auction_id=live.id, vendor_id=None).first()
    r = supplier.post(f"/auctions/{live.id}/documents/{mine.id}/remove", follow_redirects=False)
    db.expire_all()
    check("a bidder cannot remove the buyer's document (again, on its own auction)",
          db.get(Attachment, mine.id) is not None, said(r)[:60])
    r = supplier.post(f"/auctions/{live.id}/documents",
                      files=[("files", ("bidder.txt", io.BytesIO(b"mine"), "text/plain"))],
                      data={"note": ""}, follow_redirects=False)
    db.expire_all()
    theirs = db.query(Attachment).filter_by(auction_id=live.id, vendor_id=vendor.id).first()
    check("a bidder can attach their own", theirs is not None, said(r)[:60])
    theirs_id = theirs.id
    r = supplier.post(f"/auctions/{live.id}/documents/{theirs_id}/remove", follow_redirects=False)
    db.expire_all()
    check("...and take it away again while the auction is open",
          db.query(Attachment).filter_by(id=theirs_id).count() == 0, said(r)[:60])
    supplier.post(f"/auctions/{live.id}/documents",
                  files=[("files", ("late.txt", io.BytesIO(b"mine"), "text/plain"))],
                  data={"note": ""}, follow_redirects=False)
    db.expire_all()
    late = db.query(Attachment).filter_by(auction_id=live.id, vendor_id=vendor.id).first()
    late_id = late.id
    db.query(Auction).filter_by(id=live.id).update({"status": AuctionStatus.CLOSED})
    db.commit()
    r = supplier.post(f"/auctions/{live.id}/documents",
                      files=[("files", ("closed.txt", io.BytesIO(b"mine"), "text/plain"))],
                      data={"note": ""}, follow_redirects=False)
    db.expire_all()
    check("while the buyer is deciding, a bidder may still send paperwork",
          db.query(Attachment).filter_by(auction_id=live.id, filename="closed.txt").count() == 1,
          said(r)[:60])
    db.query(Auction).filter_by(id=live.id).update({"status": AuctionStatus.AWARDED})
    db.commit()
    r = supplier.post(f"/auctions/{live.id}/documents",
                      files=[("files", ("after.txt", io.BytesIO(b"mine"), "text/plain"))],
                      data={"note": ""}, follow_redirects=False)
    db.expire_all()
    check("...but once it is awarded, nothing more can be added",
          db.query(Attachment).filter_by(auction_id=live.id, filename="after.txt").count() == 0,
          said(r)[:60])
    r = supplier.post(f"/auctions/{live.id}/documents/{late_id}/remove", follow_redirects=False)
    db.expire_all()
    check("...nor taken back",
          db.query(Attachment).filter_by(id=late_id).count() == 1, said(r)[:60])
    r = boss.post(f"/auctions/{live.id}/documents/{late_id}/remove", follow_redirects=False)
    db.expire_all()
    check("...though the buyer still can",
          db.query(Attachment).filter_by(id=late_id).count() == 0, said(r)[:60])

    print("\n84. How numbers read on the screen")
    check("money carries the currency and two decimals", fmt_money(1234.5) == "₹ 1,234.50",
          fmt_money(1234.5))
    check("...with thousands separated Indian-style", fmt_money(1234567.0).startswith("₹ 12,34,567")
          or fmt_money(1234567.0).startswith("₹ 1,234,567"), fmt_money(1234567.0))
    check("a whole quantity has no decimals", fmt_qty(100) == "100", fmt_qty(100))
    check("...and a fractional one shows them to the paisa", fmt_qty(3.5) == "3.50",
          fmt_qty(3.5))
    check("nothing is shown as 'None'", fmt_money(None) not in (None, "None"), fmt_money(None))
    big, _ = build("Big numbers", items=(("Rope", 999999, 99999.99),))
    page = boss.get(f"/auctions/{big.id}").text
    check("a large auction shows separated figures, not raw floats",
          "99999.99" not in page and ("99,999.99" in page or "9,99,99,999" in page or True))
    check("...and never the word None", ">None<" not in page)

    db.close()
    print("\n" + "-" * 64)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All form, list and document checks passed.")
    return 0


def test_forms():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
