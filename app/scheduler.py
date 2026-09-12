"""Background clock: opens auctions, closes them, and sends time-based alerts."""
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timedelta

from . import config, notify
from .audit import record
from .db import SessionLocal
from .models import Auction, AuctionStatus


def _safely(what: str, action) -> bool:
    """Run one auction's step, and let the rest of the tick carry on if it fails.

    Without this, a single failure - a locked database, a mail server hiccup -
    stopped the whole pass, so auctions later in the list were never opened or
    closed and their alerts were lost.
    """
    try:
        action()
        return True
    except Exception:
        print(f"Scheduler could not {what}; will try again on the next tick.")
        traceback.print_exc()
        return False


def _claim(db, *conditions, values: dict) -> bool:
    """Take one auction's next step - once, even if two clocks are running.

    A deployment can easily end up with more than one of these: several web
    workers each running the background loop, or a loop plus a cron job. They
    all woke at the same second, all read "this one is LIVE and its time is
    up", and all closed it - so the bidders got the closing notice twice and
    the audit trail claimed the auction closed twice.

    Asking the database to make the change *only* while the auction is still
    in the state we read settles it: whoever gets there first changes one row
    and carries on, and everybody else changes none and stops.
    """
    changed = db.query(Auction).filter(*conditions).update(values,
                                                           synchronize_session=False)
    db.commit()
    return bool(changed)


def tick(now: datetime | None = None) -> dict:
    """One pass of the clock. Safe to call directly from tests or a cron job."""
    now = now or datetime.utcnow()
    stats = {"started": 0, "closed": 0, "starting_soon": 0, "ending_soon": 0}
    db = SessionLocal()
    try:
        soon = now + timedelta(minutes=config.STARTING_SOON_MINUTES)
        for auction in db.query(Auction).filter(
                Auction.status == AuctionStatus.SCHEDULED,
                Auction.start_at <= soon,
                Auction.start_at > now,
                Auction.starting_soon_notified.is_(False)).all():
            sent = []

            def send_starting(auction=auction, sent=sent):
                # Claim the alert first, so two clocks cannot both send it;
                # if the sending then fails, hand the claim back and the next
                # tick will try again. Neither twice nor never.
                if not _claim(db, Auction.id == auction.id,
                              Auction.starting_soon_notified.is_(False),
                              values={"starting_soon_notified": True}):
                    return
                try:
                    notify.auction_starting_soon(db, auction)
                except Exception:
                    db.rollback()
                    _claim(db, Auction.id == auction.id,
                           values={"starting_soon_notified": False})
                    raise
                sent.append(True)
            if _safely(f"warn bidders that {auction.reference} starts soon", send_starting):
                stats["starting_soon"] += 1 if sent else 0
            else:
                db.rollback()

        for auction in db.query(Auction).filter(
                Auction.status == AuctionStatus.SCHEDULED,
                Auction.start_at <= now).all():
            opened = []

            def open_it(auction=auction, opened=opened):
                if not _claim(db, Auction.id == auction.id,
                              Auction.status == AuctionStatus.SCHEDULED,
                              values={"status": AuctionStatus.LIVE, "started_at": now}):
                    return                      # another clock opened it first
                db.refresh(auction)
                record(db, action="auction.start", entity_type="auction",
                       entity_id=auction.id, auction_id=auction.id,
                       detail="Opened automatically by the scheduler")
                db.commit()
                notify.auction_started(db, auction)
                opened.append(True)
            if _safely(f"open {auction.reference}", open_it):
                stats["started"] += 1 if opened else 0
            else:
                db.rollback()

        warn_at = now + timedelta(minutes=config.ENDING_SOON_MINUTES)
        for auction in db.query(Auction).filter(
                Auction.status == AuctionStatus.LIVE,
                Auction.end_at <= warn_at,
                Auction.end_at > now,
                Auction.ending_soon_notified.is_(False)).all():
            warned = []

            def send_ending(auction=auction, warned=warned):
                if not _claim(db, Auction.id == auction.id,
                              Auction.ending_soon_notified.is_(False),
                              values={"ending_soon_notified": True}):
                    return
                try:
                    notify.ending_soon(db, auction)
                except Exception:
                    db.rollback()
                    _claim(db, Auction.id == auction.id,
                           values={"ending_soon_notified": False})
                    raise
                warned.append(True)
            if _safely(f"warn bidders that {auction.reference} closes soon", send_ending):
                stats["ending_soon"] += 1 if warned else 0
            else:
                db.rollback()

        for auction in db.query(Auction).filter(
                Auction.status == AuctionStatus.LIVE,
                Auction.end_at <= now).all():
            def close_it(auction=auction):
                # A bid may have extended the clock between the query and now,
                # in another session, so the closing time is part of the claim:
                # an auction that has just been extended is no longer due, and
                # the auto-extension is not silently thrown away.
                if not _claim(db, Auction.id == auction.id,
                              Auction.status == AuctionStatus.LIVE,
                              Auction.end_at <= datetime.utcnow(),
                              values={"status": AuctionStatus.CLOSED, "closed_at": now}):
                    return False
                db.refresh(auction)
                record(db, action="auction.close", entity_type="auction",
                       entity_id=auction.id, auction_id=auction.id,
                       detail="Closed automatically when the clock ran out")
                db.commit()
                notify.auction_closed(db, auction)
                return True
            closed = []
            if _safely(f"close {auction.reference}",
                       lambda auction=auction: closed.append(close_it(auction))):
                if closed and closed[0]:
                    stats["closed"] += 1
            else:
                db.rollback()
        return stats
    finally:
        db.close()


async def run_forever() -> None:  # pragma: no cover - background loop
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(config.SCHEDULER_INTERVAL_SECONDS)
