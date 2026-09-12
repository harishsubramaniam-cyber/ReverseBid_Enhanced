"""Round 4 of the deep check: every email the app sends.

``test_email.py`` is about the plumbing — a mail server that is wrong, missing
or slow. This one is about the letters themselves: who is written to, what is
in them, and where their buttons lead. An auction is run from invitation to
award, one contact at a time, and after every step the outbox is read back and
checked name by name.

The fixture is built so the awkward cases are the normal ones:

  Alpha Supplies   has a login, and a second contact address that does not
  Bharat Traders   no login at all — only an address the buyer typed
  Never Invited    on the supplier list, invited to nothing

    python tests/test_notifications.py
"""
from __future__ import annotations

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
TMP = tempfile.mkdtemp(prefix="ra-notify-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ.pop("RA_SMTP_HOST", None)

from fastapi.testclient import TestClient                       # noqa: E402

from app import config, engine, mailer, notify, scheduler       # noqa: E402
from app.db import Base, SessionLocal, engine as db_engine      # noqa: E402
from app.main import app                                        # noqa: E402
from app.models import (Auction, AuctionLine, AuctionStatus, Bid,  # noqa: E402
                        DecrementType, EmailMessage, Item, Notification,
                        Organisation, Participant, Role, Unit, User, Vendor)
from app.security import hash_password, read_invite             # noqa: E402

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
    client = Client(app, base_url="http://test")
    client.get("/login")
    client.post("/login", data={"email": email, "password": PW, "next": "/"},
                follow_redirects=False)
    client.headers.update({"accept": "text/html"})
    return client


def flash_of(response):
    jar = SimpleCookie()
    jar.load(response.headers.get("set-cookie", ""))
    try:
        return json.loads(jar["ra_flash"].value)["m"]
    except Exception:
        return ""


def main():                                      # noqa: C901 - one long story
    db = SessionLocal()
    org = Organisation(name="Acme")
    db.add(org)
    db.flush()
    buyer = User(name="Bea Buyer", email="buyer@acme.test", role=Role.BUYER, org_id=org.id,
                 password_hash=hash_password(PW))
    db.add(buyer)
    unit = Unit(code="NOS", org_id=org.id)
    items = [Item(name=name, org_id=org.id) for name in ("Pens", "Paper")]
    db.add(unit)
    db.add_all(items)
    db.flush()
    alpha = Vendor(name="Alpha Supplies", email="alpha@alpha.test", org_id=org.id,
                   extra_emails="accounts@alpha.test")
    bharat = Vendor(name="Bharat Traders", email="bharat@bharat.test", org_id=org.id)
    never = Vendor(name="Never Invited", email="nobody@never.test", org_id=org.id)
    db.add_all([alpha, bharat, never])
    db.flush()
    db.add(User(name="Ann Alpha", email="alpha@alpha.test", role=Role.VENDOR, org_id=org.id,
                vendor_id=alpha.id, password_hash=hash_password(PW)))
    db.commit()

    boss = login("buyer@acme.test")
    ann = login("alpha@alpha.test")

    def make(ref, status=AuctionStatus.LIVE, cc="", invited=(alpha, bharat)):
        now = datetime.utcnow()
        auction = Auction(reference=ref, title=f"{ref} stationery", creator_id=buyer.id,
                          org_id=org.id, status=status,
                          start_at=now - timedelta(minutes=5),
                          end_at=now + timedelta(hours=2),
                          original_end_at=now + timedelta(hours=2),
                          decrement_type=DecrementType.ABSOLUTE, min_decrement=0.0,
                          compare_landed=False, auto_extend=False, cc_emails=cc,
                          published_at=None if status == AuctionStatus.DRAFT else now)
        db.add(auction)
        db.flush()
        for item in items:
            db.add(AuctionLine(auction_id=auction.id, item_id=item.id, unit_id=unit.id,
                               qty=100, starting_price=50.0))
        for i, vendor in enumerate(invited):
            db.add(Participant(auction_id=auction.id, vendor_id=vendor.id,
                               alias=f"Bidder {i + 1}"))
        db.commit()
        db.refresh(auction)
        return auction, auction.lines

    def mark():
        row = db.query(EmailMessage).order_by(EmailMessage.id.desc()).first()
        return row.id if row else 0

    def letters(auction=None, event=None, since=0):
        query = db.query(EmailMessage).filter(EmailMessage.id > since)
        if auction is not None:
            query = query.filter(EmailMessage.auction_id == auction.id)
        if event:
            query = query.filter(EmailMessage.event == event)
        return query.order_by(EmailMessage.id).all()

    def addressed(rows):
        return sorted({row.to_email for row in rows})

    print("\n49. Publishing an auction writes to exactly the right people")
    at = mark()
    auction, lines = make("RA-1", cc="cc@acme.test")
    notify.auction_invited(db, auction)
    db.expire_all()
    invitations = letters(auction, since=at)
    check("every invited bidder contact is written to",
          addressed(invitations) == ["accounts@alpha.test", "alpha@alpha.test",
                                     "bharat@bharat.test"], addressed(invitations))
    check("...and nobody who was not invited",
          "nobody@never.test" not in addressed(invitations))
    check("...one email each, not two",
          len(invitations) == len(addressed(invitations)))

    print("\n50. Every email carries a link that works")
    wrong = []
    for row in invitations:
        urls = [u for u in re.findall(r'href="([^"]+)"', row.html_body or "")
                if "/auctions/" in u or "/join/" in u]
        if not urls:
            wrong.append(f"{row.to_email}: no link at all")
        wrong += [f"{row.to_email}: {u[:40]}" for u in urls
                  if not u.startswith(config.base_url())]
    check("every link is a full web address for this site", not wrong, "; ".join(wrong[:2]))
    for row in invitations:
        token = re.search(r"/join/([A-Za-z0-9_.\-]+)", row.html_body or "")
        if row.to_email == "alpha@alpha.test":
            check("a bidder who already has a login is sent to the auction, not to a sign-up",
                  token is None)
            continue
        invite = read_invite(token.group(1), config.INVITE_DAYS) if token else None
        check(f"{row.to_email} gets a sign-up link that works", bool(invite))
        if invite:
            check("...addressed to them, at the right company and supplier",
                  invite["e"] == row.to_email and invite["o"] == org.id
                  and invite["v"] in (alpha.id, bharat.id),
                  f'{invite["e"]} org {invite["o"]} supplier {invite["v"]}')

    print("\n51. Bidding: confirmations and outbid warnings")
    at = mark()
    ann.post(f"/auctions/{auction.id}/bid",
             data={"line_id": str(lines[0].id), "unit_price": "40"}, follow_redirects=False)
    db.expire_all()
    check("a bid is confirmed to the person who placed it",
          [m.to_email for m in letters(auction, event="bid_received", since=at)]
          == ["alpha@alpha.test"])
    check("...and to nobody else",
          not [m for m in letters(auction, since=at) if m.to_email == "bharat@bharat.test"])
    at = mark()
    # Bharat has no login yet, so their bid goes in through the engine.
    engine.place_bid(db, auction, lines[0],
                     User(name="Bharat", email="bharat@bharat.test", role=Role.VENDOR,
                          org_id=org.id, vendor_id=bharat.id, id=None),
                     38.0, "", ip="1.2.3.4")
    db.commit()
    db.expire_all()
    outbid = letters(auction, event="outbid", since=at)
    check("the bidder who lost the lead is told — at both their addresses",
          addressed(outbid) == ["accounts@alpha.test", "alpha@alpha.test"], addressed(outbid))
    check("...and the email names the price that beat them",
          all("38" in (m.html_body or "") for m in outbid))

    print("\n52. Closing, and then awarding")
    at = mark()
    notify.ending_soon(db, auction)
    db.expire_all()
    check("the closing warning goes to all three contacts",
          len(letters(auction, event="ending_soon", since=at)) == 3)
    at = mark()
    auction.status = AuctionStatus.CLOSED
    db.commit()
    notify.auction_closed(db, auction)
    db.expire_all()
    check("the closing notice reaches the bidders, the buyer and the copy list",
          addressed(letters(auction, event="closed", since=at)) ==
          ["accounts@alpha.test", "alpha@alpha.test", "bharat@bharat.test",
           "buyer@acme.test", "cc@acme.test"])
    at = mark()
    boss.post(f"/auctions/{auction.id}/award",
              data={f"winner_{lines[0].id}": str(bharat.id), f"price_{lines[0].id}": "38",
                    f"winner_{lines[1].id}": ""}, follow_redirects=False)
    db.expire_all()
    won = letters(auction, event="awarded", since=at)
    lost = letters(auction, event="not_awarded", since=at)
    check("the winner is told", "bharat@bharat.test" in addressed(won), addressed(won))
    check("...with the price they will be paid",
          any("38" in (m.html_body or "") for m in won if m.to_email == "bharat@bharat.test"))
    check("the buyer gets a summary, not the supplier's letter",
          all("Congratulations" not in (m.subject or "") for m in won
              if m.to_email in ("buyer@acme.test", "cc@acme.test")))
    check("the bidder who did not win is told too",
          addressed(lost) == ["accounts@alpha.test", "alpha@alpha.test"], addressed(lost))
    check("...without naming the winner",
          not any("Bharat Traders" in (m.html_body or "") for m in lost))
    check("...or the winning price",
          not any("₹ 38" in (m.html_body or "") for m in lost))

    print("\n53. A draft tells nobody")
    at = mark()
    draft, _ = make("RA-DRAFT", status=AuctionStatus.DRAFT)
    db.expire_all()
    check("creating a draft sends nothing", not letters(draft, since=at))

    print("\n54. One person, one email")
    at = mark()
    alpha.extra_emails = "  ALPHA@Alpha.test \n accounts@alpha.test\naccounts@alpha.test "
    db.commit()
    tidy, _ = make("RA-2", invited=(alpha,))
    notify.auction_invited(db, tidy)
    db.expire_all()
    check("the same address twice, in different case, is written to once",
          addressed(letters(tidy, since=at)) == ["accounts@alpha.test", "alpha@alpha.test"],
          addressed(letters(tidy, since=at)))
    alpha.extra_emails = "accounts@alpha.test"
    db.commit()

    print("\n55. An address typed against one auction replaces the usual list")
    at = mark()
    special, _ = make("RA-3", invited=(alpha,))
    seat = db.query(Participant).filter_by(auction_id=special.id, vendor_id=alpha.id).first()
    seat.notify_emails = "tender.desk@alpha.test"
    db.commit()
    notify.auction_invited(db, special)
    db.expire_all()
    check("only the address typed for this auction is used",
          addressed(letters(special, since=at)) == ["tender.desk@alpha.test"],
          addressed(letters(special, since=at)))
    check("...but the bidder with a login still gets the alert inside the app",
          db.query(Notification).filter(Notification.title.like("%RA-3%")).count() >= 1)

    print("\n56. Nothing secret travels by email")
    spilled = [m.id for m in db.query(EmailMessage).all()
               if "pbkdf2" in (m.html_body or "") or PW in (m.html_body or "")
               or "ra_session" in (m.html_body or "")]
    check("no password, hash or session token in any email ever sent", not spilled, spilled[:3])

    print("\n57. Words somebody else typed cannot become part of an email")
    at = mark()
    ann_user = db.query(User).filter_by(email="alpha@alpha.test").first()
    ann_user.name = 'Ann <a href="http://evil.test">confirm your bank details</a>'
    db.commit()
    nasty, _ = make("RA-5", invited=(alpha,))
    ann.post(f"/auctions/{nasty.id}/messages",
             data={"body": '<script>alert(1)</script> and <a href="http://evil.test">a link</a>'},
             follow_redirects=False)
    db.expire_all()
    posted = letters(nasty, event="message", since=at)
    body = "".join(m.html_body or "" for m in posted)
    check("the message reaches the buyer", bool(posted))
    check("...with no live link of the sender's making in it",
          '<a href="http://evil.test"' not in body and "<a href='http://evil.test'" not in body)
    check("...and no script either", "<script>" not in body)
    check("...while the words themselves still read as typed",
          "alert(1)" in body and "&lt;a href=" in body)
    ann_user.name = "Ann Alpha"
    db.commit()

    print("\n58. A title with a line break cannot forge an email header")
    at = mark()
    forged, _ = make("RA-6", invited=(alpha,))
    forged.title = "Pens\r\nBcc: everyone@elsewhere.test"
    db.commit()
    notify.auction_invited(db, forged)
    db.expire_all()
    rows = letters(forged, since=at)
    check("the subject is one line",
          all("\n" not in m.subject and "\r" not in m.subject for m in rows),
          [m.subject for m in rows][:1])
    mime = mailer._build_mime(rows[0])
    check("...and the message that goes out has no extra recipients",
          not mime.get("Bcc"), mime.get("Bcc"))

    print("\n59. Editing a published auction tells the right two groups")
    at = mark()
    edited, _ = make("RA-7", invited=(alpha,))
    notify.auction_changed(db, edited, newly_invited=[bharat],
                           changes=["The closing time moved"])
    db.expire_all()
    check("the bidder just added gets an invitation",
          addressed(letters(edited, event="invited", since=at)) == ["bharat@bharat.test"])
    check("...and the ones already there are told what changed",
          addressed(letters(edited, event="updated", since=at)) ==
          ["accounts@alpha.test", "alpha@alpha.test"])
    check("...with the change spelled out",
          all("closing time moved" in (m.html_body or "")
              for m in letters(edited, event="updated", since=at)))

    print("\n60. Cancelling tells everybody, with the reason")
    at = mark()
    notify.auction_cancelled(db, edited, "Budget pulled")
    db.expire_all()
    told = letters(edited, event="cancelled", since=at)
    check("the bidders and the buyer are told",
          {"alpha@alpha.test", "buyer@acme.test"} <= set(addressed(told)), addressed(told))
    check("...and the reason is in the letter",
          all("Budget pulled" in (m.html_body or "") for m in told))

    print("\n61. A withdrawal tells the buyer, and only the buyer")
    withdrawn, w_lines = make("RA-8", invited=(alpha,))
    ann.post(f"/auctions/{withdrawn.id}/bid",
             data={"line_id": str(w_lines[0].id), "unit_price": "40"}, follow_redirects=False)
    db.expire_all()
    bid = (db.query(Bid).filter_by(auction_id=withdrawn.id)
             .order_by(Bid.id.desc()).first())
    at = mark()
    boss.post(f"/auctions/{withdrawn.id}/bids/{bid.id}/withdraw",
              data={"reason": "asked to"}, follow_redirects=False)
    db.expire_all()
    after = letters(withdrawn, since=at)
    check("the buyer hears about it",
          "buyer@acme.test" in [m.to_email for m in after if m.event == "withdrawn"])
    check("...and no rival is told anything", not [m for m in after
                                                   if m.to_email == "bharat@bharat.test"])

    print("\n62. Publishing from the screen, and pressing it twice")
    at = mark()
    published, p_lines = make("RA-9", status=AuctionStatus.DRAFT)
    boss.post(f"/auctions/{published.id}/publish", follow_redirects=False)
    db.expire_all()
    check("publishing invites the bidders and tells the buyer",
          addressed(letters(published, since=at)) ==
          ["accounts@alpha.test", "alpha@alpha.test", "bharat@bharat.test",
           "buyer@acme.test"], addressed(letters(published, since=at)))
    again = mark()
    response = boss.post(f"/auctions/{published.id}/publish", follow_redirects=False)
    db.expire_all()
    check("pressing publish a second time sends nothing more",
          not letters(published, since=again), flash_of(response)[:50])

    print("\n63. A bidder with no login goes from the email to a bid")
    invitation = [m for m in letters(published, since=at)
                  if m.to_email == "bharat@bharat.test"][0]
    token = re.search(r"/join/([A-Za-z0-9_.\-]+)", invitation.html_body or "")
    check("the invitation carries a sign-up link", token is not None)
    if token:
        newcomer = Client(app, base_url="http://test")
        newcomer.headers.update({"accept": "text/html"})
        newcomer.get(f"/join/{token.group(1)}")
        newcomer.post(f"/join/{token.group(1)}",
                      data={"name": "Bee Bharat", "password": PW, "confirm": PW},
                      follow_redirects=False)
        db.expire_all()
        made = db.query(User).filter_by(email="bharat@bharat.test").first()
        check("...which creates their login, tied to their own supplier record",
              made is not None and made.vendor_id == bharat.id)
        check("...lets them open the auction they were invited to",
              newcomer.get(f"/auctions/{published.id}").status_code == 200)
        newcomer.post(f"/auctions/{published.id}/bid",
                      data={"line_id": str(p_lines[0].id), "unit_price": "45"},
                      follow_redirects=False)
        db.expire_all()
        check("...and bid on it",
              db.query(Bid).filter_by(auction_id=published.id,
                                      vendor_id=bharat.id).count() == 1)

    print("\n64. Time-based alerts fire once, not every minute")
    at = mark()
    now = datetime.utcnow()
    soon, _ = make("RA-10", invited=(alpha,))
    soon.status = AuctionStatus.SCHEDULED
    soon.start_at = now + timedelta(minutes=5)
    soon.end_at = now + timedelta(hours=1)
    db.commit()
    for _ in range(3):
        scheduler.tick()
    db.expire_all()
    starting = letters(soon, event="starting_soon", since=at)
    check("the 'starts soon' warning goes out once per contact",
          len(starting) == len(addressed(starting)), [m.to_email for m in starting])
    db.refresh(soon)
    check("...and the auction remembers it was sent", soon.starting_soon_notified)

    print("\n65. With no mail server, nothing is lost")
    mailer.flush(timeout=30)
    db.expire_all()
    rows = db.query(EmailMessage).all()
    resting = {}
    for row in rows:
        resting[row.status] = resting.get(row.status, 0) + 1
    check("nothing is left saying 'queued'", resting.get("queued", 0) == 0, resting)
    check("...everything is in the outbox instead", resting.get("outbox", 0) == len(rows),
          resting)
    check("...with a file on disk for each",
          all(row.file_path and Path(row.file_path).is_file() for row in rows))
    page = boss.get("/outbox").text
    check("the outbox page lists them", "alpha@alpha.test" in page)
    response = boss.post("/outbox/test", data={"to_email": "someone@example.test"},
                         follow_redirects=False)
    check("the test button answers in plain words",
          len(flash_of(response)) > 20 and "Traceback" not in flash_of(response),
          flash_of(response)[:60])
    response = boss.post("/outbox/test", data={"to_email": "not-an-address"},
                         follow_redirects=False)
    check("...and refuses a nonsense address kindly",
          "not-an-address" in flash_of(response), flash_of(response)[:60])

    print("\n66. With a mail server that refuses")
    was_enabled = config.EMAIL_ENABLED
    original = mailer._smtp_send

    def refuse(mime, to_email):
        raise OSError(101, "Network is unreachable")

    try:
        config.EMAIL_ENABLED = True
        mailer._smtp_send = refuse
        at = mark()
        notify.ending_soon(db, published)
        db.expire_all()
        for row in letters(published, since=at):
            mailer.deliver(row.id)
        db.expire_all()
        failed = letters(published, since=at)
        check("every message that could not go out says so",
              all(row.status == "failed" for row in failed), [r.status for r in failed])
        check("...in words, not a stack trace",
              all(len(row.error or "") > 30 and "Traceback" not in (row.error or "")
                  for row in failed), (failed[0].error or "")[:60] if failed else "")
        check("...and the outbox offers to try again",
              "Try them again" in boss.get("/outbox").text)
        mailer._smtp_send = original
        config.EMAIL_ENABLED = False
        boss.post("/outbox/retry", follow_redirects=False)
        mailer.flush(timeout=30)
        db.expire_all()
        check("retrying gets them out once the problem is fixed",
              all(row.status in ("sent", "outbox") for row in letters(published, since=at)),
              [row.status for row in letters(published, since=at)])
    finally:
        mailer._smtp_send = original
        config.EMAIL_ENABLED = was_enabled

    db.close()
    print("\n" + "-" * 64)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All email and invitation checks passed.")
    return 0


def test_notifications():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
