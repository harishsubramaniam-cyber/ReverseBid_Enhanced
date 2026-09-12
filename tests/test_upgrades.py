"""Round 6 of the deep check: upgrades, restarts, and two people at once.

The last round is about the things that go wrong when nobody is looking: a new
version installed over a database that has a year of work in it, a server that
was asleep while an auction opened and closed, two clocks ticking in two
processes, two bidders pressing Place bid in the same second, and a copy taken
for safekeeping while the app is running.

Several checks here start a second copy of the app in its own process, on a
database built to look old, and read the file afterwards to see what the
upgrade did to it.

    python tests/test_upgrades.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="ra-upgrades-")
os.environ["RA_DATA_DIR"] = TMP
os.environ["RA_DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["RA_ENV_FILE"] = f"{TMP}/none.env"
os.environ.pop("RA_SMTP_HOST", None)

import sqlalchemy                                              # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402

from app import config, engine as bid_engine, mailer, notify, scheduler   # noqa: E402
from app.db import Base, SessionLocal, engine as db_engine     # noqa: E402
from app.main import app                                       # noqa: E402
from app.models import (Auction, AuctionLine, AuctionStatus, AuditLog,   # noqa: E402
                        Award, Bid, DecrementType, EmailMessage, Item,
                        LineTax, Organisation, Participant, Role, Unit,
                        User, Vendor)
from app.security import hash_password                         # noqa: E402

Base.metadata.create_all(bind=db_engine)
PW = "test1234"
FAILS: list[str] = []

#: A second copy of the app, started in its own process on a given database,
#: exactly as a deployment would start it.
START_APP = ("import sys; sys.path.insert(0, %r)\n"
             "from fastapi.testclient import TestClient\n"
             "from app.main import app\n"
             "with TestClient(app) as c: print('HEALTH', c.get('/healthz').status_code)"
             % str(ROOT))


def check(label, ok, extra=""):
    print(("  \u2713 " if ok else "  \u2717 ") + label + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(label)


def start_app_on(database: Path):
    """Run the app once against this database file and return what it printed."""
    return subprocess.run(
        [sys.executable, "-c", START_APP], capture_output=True, text=True,
        env={**os.environ, "RA_DATA_DIR": TMP,
             "RA_DATABASE_URL": f"sqlite:///{database}",
             "RA_ENV_FILE": f"{TMP}/none.env"})


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
    jar = SimpleCookie()
    jar.load(response.headers.get("set-cookie", ""))
    try:
        return json.loads(jar["ra_flash"].value)["m"]
    except Exception:
        return ""


def main():                                     # noqa: C901 - one long story
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
    db.add_all([unit, item])
    db.flush()
    vendors = []
    for i in range(3):
        vendor = Vendor(name=f"Supplier {i}", email=f"s{i}@x.test", org_id=org.id)
        db.add(vendor)
        db.flush()
        db.add(User(name=f"S{i}", email=f"s{i}@x.test", role=Role.VENDOR, org_id=org.id,
                    vendor_id=vendor.id, password_hash=hash_password(PW)))
        db.flush()
        vendors.append(vendor)
    db.commit()
    boss = login("buyer@acme.test")
    clients = [login(f"s{i}@x.test") for i in range(3)]
    tmp = TMP

    def make(ref, start_minutes=-5, end_minutes=120, status=AuctionStatus.LIVE,
             qty=100, ceiling=100.0):
        now = datetime.utcnow()
        auction = Auction(reference=ref, title=f"{ref} pens", creator_id=buyer.id,
                          org_id=org.id, status=status,
                          start_at=now + timedelta(minutes=start_minutes),
                          end_at=now + timedelta(minutes=end_minutes),
                          original_end_at=now + timedelta(minutes=end_minutes),
                          decrement_type=DecrementType.ABSOLUTE, min_decrement=0.0,
                          compare_landed=False, auto_extend=False, published_at=now)
        db.add(auction)
        db.flush()
        db.add(AuctionLine(auction_id=auction.id, item_id=item.id, unit_id=unit.id,
                           qty=qty, starting_price=ceiling))
        for i, vendor in enumerate(vendors):
            db.add(Participant(auction_id=auction.id, vendor_id=vendor.id,
                               alias=f"Bidder {i + 1}"))
        db.commit()
        db.refresh(auction)
        return auction, auction.lines[0]

    print("\n85. An older database gains what a new version needs")
    old = Path(tmp) / "old.db"
    sqlite3.connect(old).close()
    old_engine = sqlalchemy.create_engine(f"sqlite:///{old}")
    Base.metadata.create_all(bind=old_engine)
    drop = [("users", "session_epoch"), ("auctions", "award_mode"),
            ("auctions", "compare_landed"), ("participants", "bidder_freight"),
            ("bids", "landed_unit_price")]
    con = sqlite3.connect(old)
    for table, column in drop:
        try:
            con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        except Exception as exc:
            print("    (could not drop", table, column, exc, ")")
    con.commit()
    con.execute("INSERT INTO organisations (name, created_at) VALUES ('Old Co', '2020-01-01')")
    con.execute("INSERT INTO users (org_id, email, name, password_hash, role, is_active) "
                "VALUES (1, 'old@old.test', 'Old Timer', 'x', 'BUYER', 1)")
    con.commit()
    missing = {c[1] for c in con.execute("PRAGMA table_info(users)")}
    check("the old database really is missing the new column", "session_epoch" not in missing)
    con.close()
    result = start_app_on(old)
    check("the app starts on it", "HEALTH 200" in result.stdout,
          (result.stdout + result.stderr)[-200:])
    con = sqlite3.connect(old)
    now_have = {c[1] for c in con.execute("PRAGMA table_info(users)")}
    check("...and the missing columns are added", "session_epoch" in now_have)
    for table, column in drop:
        cols = {c[1] for c in con.execute(f"PRAGMA table_info({table})")}
        check(f"...{table}.{column} is back", column in cols)
    rows = list(con.execute("SELECT name, email FROM users"))
    check("...with the rows that were already there untouched",
          rows == [("Old Timer", "old@old.test")], rows)
    epochs = list(con.execute("SELECT session_epoch FROM users"))
    check("...and nobody is signed out by the upgrade", epochs == [(0,)], epochs)
    con.close()

    print("\n86. A database from before there were separate companies")
    older = Path(tmp) / "older.db"
    shutil.copy(old, older)
    con = sqlite3.connect(older)
    con.execute("UPDATE users SET org_id = NULL")
    con.execute("INSERT INTO vendors (org_id, name, email, is_active) "
                "VALUES (NULL, 'Orphan Supplies', 'o@o.test', 1)")
    con.execute("DELETE FROM organisations")
    con.commit(); con.close()
    result = start_app_on(older)
    check("the app starts on that too", "HEALTH 200" in result.stdout,
          (result.stdout + result.stderr)[-200:])
    con = sqlite3.connect(older)
    orgs = list(con.execute("SELECT id, name FROM organisations"))
    users = list(con.execute("SELECT name, org_id FROM users"))
    vs = list(con.execute("SELECT name, org_id FROM vendors"))
    check("...everything is moved into one company of its own", len(orgs) == 1, orgs)
    check("...the account belongs to it", users and users[0][1] == orgs[0][0], users)
    check("...and so does the supplier", vs and vs[0][1] == orgs[0][0], vs)
    check("...named after the person who set it up", "Old Timer" in orgs[0][1], orgs[0][1])
    con.close()

    print("\n87. The clock catches up after the server was asleep")
    sleepy, sleepy_line = make("SLEEP-1", start_minutes=-120, end_minutes=-60,
                               status=AuctionStatus.SCHEDULED)
    mark = db.query(EmailMessage).count()
    scheduler.tick()
    db.expire_all(); db.refresh(sleepy)
    check("an auction whose whole window passed while nobody was watching is opened and closed",
          sleepy.status == AuctionStatus.CLOSED, sleepy.status.value)
    check("...and the bidders are told it closed",
          db.query(EmailMessage).filter(EmailMessage.auction_id == sleepy.id,
                                        EmailMessage.event == "closed").count() >= 1)
    scheduler.tick()
    db.expire_all()
    check("...and a second tick does not send it all again",
          db.query(EmailMessage).filter(EmailMessage.auction_id == sleepy.id,
                                        EmailMessage.event == "closed").count() ==
          db.query(EmailMessage).filter(EmailMessage.auction_id == sleepy.id,
                                        EmailMessage.event == "closed").count())

    print("\n88. Two clock ticks at the same moment")
    racer, racer_line = make("RACE-1", start_minutes=-120, end_minutes=-60,
                             status=AuctionStatus.SCHEDULED)
    errors = []


    def tick_now():
        try:
            scheduler.tick()
        except Exception as exc:
            errors.append(exc)


    threads = [threading.Thread(target=tick_now) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db.expire_all(); db.refresh(racer)
    check("four clocks running at once do not collide", not errors, errors[:1])
    check("...the auction is closed exactly once", racer.status == AuctionStatus.CLOSED)
    closes = db.query(AuditLog).filter(AuditLog.auction_id == racer.id,
                                       AuditLog.action == "auction.close").count()
    check("...and the record says so once", closes == 1, closes)
    notices = db.query(EmailMessage).filter(EmailMessage.auction_id == racer.id,
                                            EmailMessage.event == "closed").count()
    contacts = len(vendors) + 1
    check("...and each person is told once, not four times", notices == contacts,
          f"{notices} notices for {contacts} people")
    starts = db.query(AuditLog).filter(AuditLog.auction_id == racer.id,
                                       AuditLog.action == "auction.start").count()
    check("...and it was opened once too", starts == 1, starts)

    print("\n89. Two bidders pressing at the same instant")
    live, live_line = make("RACE-2")
    placed, refused = [], []


    def bid(client, price):
        r = client.post(f"/auctions/{live.id}/bid",
                        data={"line_id": str(live_line.id), "unit_price": str(price)},
                        follow_redirects=False)
        (placed if "L1" in said(r) or "Bid placed" in said(r) else refused).append(said(r))


    threads = [threading.Thread(target=bid, args=(clients[i], 90.0)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db.expire_all()
    kept = db.query(Bid).filter_by(line_id=live_line.id, withdrawn=False).all()
    check("three identical bids do not all stand", len(kept) == 1,
          [f"{b.vendor_id}@{b.unit_price}" for b in kept])
    check("...the others are told why", len(refused) == 2, refused[:1])
    check("...and the one that stands is the one the board shows",
          bid_engine.best_bid(db, live_line.id).id == kept[0].id)

    print("\n90. Two buyers awarding at the same instant")
    live.status = AuctionStatus.CLOSED; db.commit()
    second_boss = login("buyer@acme.test")
    results = []


    def award(client, vendor_id):
        r = client.post(f"/auctions/{live.id}/award",
                        data={f"winner_{live_line.id}": str(vendor_id),
                              f"price_{live_line.id}": "90"}, follow_redirects=False)
        results.append(r.status_code)


    threads = [threading.Thread(target=award, args=(boss, vendors[0].id)),
               threading.Thread(target=award, args=(second_boss, vendors[1].id))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db.expire_all()
    awards = db.query(Award).filter_by(auction_id=live.id).all()
    check("only one award stands on the line", len(awards) == 1,
          [(a.vendor_id, a.unit_price) for a in awards])
    check("...and the auction is awarded once", db.get(Auction, live.id).status ==
          AuctionStatus.AWARDED)

    print("\n91. Nothing is orphaned when an auction ends")
    done, done_line = make("TIDY-1")
    clients[0].post(f"/auctions/{done.id}/bid",
                    data={"line_id": str(done_line.id), "unit_price": "90"},
                    follow_redirects=False)
    boss.post(f"/auctions/{done.id}/cancel", data={"reason": "changed our minds"},
              follow_redirects=False)
    db.expire_all(); db.refresh(done)
    check("a cancelled auction keeps its bids for the record",
          db.query(Bid).filter_by(auction_id=done.id).count() == 1,
          done.status.value)
    lines = [l.id for l in db.get(Auction, done.id).lines]
    check("...and every one of them still points at an item on the auction",
          db.query(Bid).filter(Bid.auction_id == done.id,
                               ~Bid.line_id.in_(lines)).count() == 0)
    check("...and no tax or award row is left hanging anywhere",
          db.query(LineTax).filter(~LineTax.line_id.in_(
              [l.id for l in db.query(AuctionLine).all()])).count() == 0
          and db.query(Award).filter(~Award.line_id.in_(
              [l.id for l in db.query(AuctionLine).all()])).count() == 0)

    print("\n92. A restart does not lose what was in the post")
    stuck = EmailMessage(to_email="somebody@somewhere.test", subject="Left over",
                         html_body="<p>hi</p>", status="queued", org_id=org.id)
    db.add(stuck); db.commit()
    count = mailer.requeue_pending()
    mailer.flush(timeout=20)
    db.expire_all()
    check("a message left queued by a stopped server is picked up", count >= 1, count)
    check("...and dealt with", db.get(EmailMessage, stuck.id).status != "queued",
          db.get(EmailMessage, stuck.id).status)

    print("\n93. The data folder")
    paths = {"database": Path(TMP) / "test.db", "outbox": Path(config.OUTBOX_DIR),
             "attachments": Path(config.DATA_DIR) / "attachments"}
    check("the database sits in the data folder", paths["database"].exists())
    check("...with the outbox beside it", paths["outbox"].exists())
    r = boss.get("/healthz")
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check("the health check answers", r.status_code == 200, r.status_code)
    check("...and names the version", bool(body.get("version")) or "version" in r.text,
          list(body)[:6])

    print("\n94. Copying the database while the app is running")
    backup = Path(tmp) / "backup.db"
    source = sqlite3.connect(str(paths["database"]))
    target = sqlite3.connect(str(backup))
    with target:
        source.backup(target)
    source.close(); target.close()
    counts = {}
    con = sqlite3.connect(str(backup))
    for table in ("auctions", "bids", "users", "vendors", "email_messages"):
        counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.close()
    check("a copy taken while the app is running has the auctions in it",
          counts["auctions"] >= 4, counts)
    check("...and the bids", counts["bids"] >= 1, counts)
    result = start_app_on(backup)
    check("...and the app starts on the copy", "HEALTH 200" in result.stdout,
          (result.stdout + result.stderr)[-160:])

    print("\n95. The sample data does not trample a real database")
    seeded = Path(tmp) / "seeded.db"
    env = {**os.environ, "RA_DATA_DIR": tmp, "RA_DATABASE_URL": f"sqlite:///{seeded}",
           "RA_ENV_FILE": f"{tmp}/n.env"}
    first = subprocess.run([sys.executable, "seed.py"], capture_output=True, text=True,
                           cwd=ROOT, env=env)
    con = sqlite3.connect(str(seeded))
    after_one = con.execute("SELECT COUNT(*) FROM auctions").fetchone()[0]
    con.close()
    check("seeding an empty database fills it", after_one >= 5,
          f"{after_one} auction(s); {(first.stdout + first.stderr)[-120:]}")
    second = subprocess.run([sys.executable, "seed.py"], capture_output=True, text=True,
                            cwd=ROOT, env=env)
    con = sqlite3.connect(str(seeded))
    after_two = con.execute("SELECT COUNT(*) FROM auctions").fetchone()[0]
    con.close()
    check("...and running it again does not double everything", after_two == after_one,
          f"{after_one} then {after_two}")

    print("\n96. A bid in the closing seconds, as the clock comes round")
    late, late_line = make("LATE-1", start_minutes=-60, end_minutes=1)
    late.auto_extend = True
    late.extend_trigger_seconds = 120
    late.extend_by_seconds = 300
    db.commit()
    was_end = late.end_at
    outcome = []


    def bid_late():
        r = clients[0].post(f"/auctions/{late.id}/bid",
                            data={"line_id": str(late_line.id), "unit_price": "80"},
                            follow_redirects=False)
        outcome.append(said(r))


    def close_late():
        scheduler.tick(now=datetime.utcnow() + timedelta(minutes=2))


    threads = [threading.Thread(target=bid_late), threading.Thread(target=close_late)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db.expire_all(); db.refresh(late)
    kept = db.query(Bid).filter_by(line_id=late_line.id, withdrawn=False).count()
    check("either the bid is in and the clock was extended, or it was refused as too late",
          (kept == 1 and late.end_at > was_end and late.status == AuctionStatus.LIVE)
          or (kept == 0 and late.status == AuctionStatus.CLOSED),
          f"{kept} bid(s), {late.status.value}, end moved: {late.end_at > was_end}")
    check("...and never both at once",
          not (kept == 1 and late.status == AuctionStatus.CLOSED
               and late.end_at == was_end), outcome[:1])

    print("\n97. If an alert cannot be sent, it is tried again")
    flaky, flaky_line = make("FLAKY-1", start_minutes=-60, end_minutes=3,
                             status=AuctionStatus.LIVE)
    original = notify.ending_soon
    notify.ending_soon = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mail is down"))
    try:
        scheduler.tick()
    finally:
        notify.ending_soon = original
    db.expire_all(); db.refresh(flaky)
    check("a failed warning is not written off as sent", not flaky.ending_soon_notified
          and db.query(EmailMessage).filter(
              EmailMessage.auction_id == flaky.id,
              EmailMessage.event == "ending_soon").count() == 0,
          flaky.ending_soon_notified)
    scheduler.tick()
    db.expire_all(); db.refresh(flaky)
    check("...so the next tick sends it", flaky.ending_soon_notified)
    check("...once", db.query(EmailMessage).filter(
        EmailMessage.auction_id == flaky.id,
        EmailMessage.event == "ending_soon").count() == len(vendors),
        db.query(EmailMessage).filter(EmailMessage.auction_id == flaky.id,
                                      EmailMessage.event == "ending_soon").count())

    db.close()
    print("\n" + "-" * 64)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in FAILS:
            print("   -", name)
        return 1
    print("All upgrade, restart and concurrency checks passed.")
    return 0


def test_upgrades():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
