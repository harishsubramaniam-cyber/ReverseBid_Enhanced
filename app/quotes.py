"""Bidding for the whole auction at once, and taking a bid back.

How an auction is bid for follows how the buyer said it would be handed out,
because those are the same question asked twice:

* **Item by item.** A bidder may win one item and not another, so they bid on
  one item at a time and quote that item's own freight, packaging and other
  costs. That lives in ``engine.place_bid``.
* **All of it to one supplier.** There is one order and one consignment, so
  there is one bid: a price for every item on a single form, one set of
  delivery costs for the lot, and the taxes on each item. It is ranked on the
  grand total, because the grand total is what the buyer would pay. That is
  ``place_basket`` below.

Both are recorded as a ``Quote`` - the submission, with the moment it was made
and every figure exactly as it was typed - so a bidder can always be shown
what they actually offered.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from . import landed, notify
from .audit import record
from .engine import (BidError, MIN_PRICE, decrement_value, line_label, maybe_extend)
from .models import Auction, AuctionStatus, Bid, LineTax, Participant, Quote, User
from .utils import fmt_money

_auction_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _auction_lock(auction_id: int) -> threading.Lock:
    with _locks_guard:
        return _auction_locks.setdefault(auction_id, threading.Lock())


@dataclass
class TaxRow:
    """A tax as the bidder typed it: a name and a rate."""
    name: str = "Tax"
    percent: float = 0.0


def whole_auction_bidding(auction: Auction) -> bool:
    """Is this auction bid for as one lot?"""
    return (auction.award_mode or "line") == "basket"


# ------------------------------------------------------------------ standings
def standing_quote(db: Session, auction: Auction, vendor_id: int) -> Quote | None:
    """This bidder's whole-auction offer as it stands - their latest, not withdrawn."""
    return (db.query(Quote)
              .filter(Quote.auction_id == auction.id, Quote.vendor_id == vendor_id,
                      Quote.scope == "auction", Quote.withdrawn.is_(False))
              .order_by(Quote.id.desc()).first())


def standings(db: Session, auction: Auction) -> list[dict]:
    """Every bidder's whole-auction offer, cheapest first."""
    rows: list[dict] = []
    for part in auction.participants:
        quote = standing_quote(db, auction, part.vendor_id)
        if quote is None:
            continue
        rows.append({"vendor": part.vendor, "vendor_id": part.vendor_id,
                     "quote": quote, "total": round(float(quote.total_all_in or 0.0), 2),
                     "alias": part.alias or ""})
    rows.sort(key=lambda row: (row["total"], row["quote"].id))
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows


def best_total(db: Session, auction: Auction) -> float | None:
    ranked = standings(db, auction)
    return ranked[0]["total"] if ranked else None


def baseline_total(auction: Auction) -> float:
    """What the buyer's own starting prices come to for the whole auction."""
    total = 0.0
    for line in auction.lines:
        if line.has_ceiling:
            total += float(line.qty or 0.0) * float(line.starting_price)
    return round(total, 2)


@dataclass
class BasketWindow:
    """What a whole-auction bid has to come in under, and how it is worked out."""
    reference: float | None = None          # the total to beat
    reference_is_ceiling: bool = False      # ...or the buyer's own starting prices
    max_allowed: float | None = None        # the most this bid may total
    min_step: float = 0.0
    mine: float | None = None               # this bidder's own standing total
    blind: bool = False                     # the buyer hides the lowest price
    exhausted: bool = False

    @property
    def open_ended(self) -> bool:
        return self.max_allowed is None and not self.exhausted


def basket_window(db: Session, auction: Auction, vendor_id: int | None) -> BasketWindow:
    """The rules a whole-auction bid has to satisfy, ready to put on screen.

    The minimum decrement is the buyer's, applied to the total: a percentage
    is a percentage of the total, and a fixed amount is that amount off the
    total. The screen says which, in money, so nobody has to guess.
    """
    best = best_total(db, auction)
    mine = None
    if vendor_id:
        quote = standing_quote(db, auction, vendor_id)
        mine = round(float(quote.total_all_in or 0.0), 2) if quote else None
    baseline = baseline_total(auction)
    leader_is_me = False
    ranked = standings(db, auction)
    if ranked and vendor_id:
        leader_is_me = ranked[0]["vendor_id"] == vendor_id
    blind = bool(best is not None and not auction.show_lowest_bid and not leader_is_me)

    if best is None:
        if baseline <= 0:
            return BasketWindow(reference=None, reference_is_ceiling=True, mine=mine)
        return BasketWindow(reference=baseline, reference_is_ceiling=True,
                            max_allowed=baseline, min_step=0.0, mine=mine)
    step = decrement_value(auction, best, auction.min_decrement or 0.0)
    allowed = round(best - max(step, 0.01), 2)
    if allowed < MIN_PRICE:
        return BasketWindow(reference=best, max_allowed=None, min_step=step, mine=mine,
                            blind=blind, exhausted=True)
    return BasketWindow(reference=best, max_allowed=allowed, min_step=step, mine=mine,
                        blind=blind)


# ------------------------------------------------------------------ placing
def _check_common(db: Session, auction: Auction, user: User) -> None:
    if not user.vendor_id:
        raise BidError("Only vendor users can bid.")
    if auction.status != AuctionStatus.LIVE:
        raise BidError("This auction is not open for bidding right now.")
    if datetime.utcnow() >= auction.end_at:
        raise BidError("The auction has just closed, so no further bids can be accepted.")
    if not db.query(Participant).filter_by(auction_id=auction.id,
                                           vendor_id=user.vendor_id).first():
        raise BidError("You are not on the invited bidder list for this auction.")


def place_basket(db: Session, auction: Auction, user: User, *, prices: dict[int, float],
                 charges: landed.Charges | None = None,
                 taxes: dict[int, list[TaxRow]] | None = None,
                 note: str = "", ip: str = "") -> Quote:
    """One bid for the whole auction: every item, priced on one form.

    Every item has to carry a price. A whole-auction auction is awarded in one
    piece, so a bid that leaves an item out is not an offer for the auction -
    and ranking it against bids that do cover everything would put the wrong
    supplier at the top.
    """
    _check_common(db, auction, user)
    taxes = taxes or {}
    charges = charges or landed.Charges()

    with _auction_lock(auction.id):
        db.refresh(auction)
        lines = list(auction.lines)
        if not lines:
            raise BidError("This auction has no items on it yet.")

        missing = [line_label(line) for line in lines
                   if prices.get(line.id) in (None, "")]
        if missing:
            raise BidError(
                "This auction is awarded to one supplier for everything, so a bid has to "
                "cover every item. Still to price: " + ", ".join(missing[:4])
                + ("…" if len(missing) > 4 else "") + ".")
        for line in lines:
            price = prices.get(line.id)
            if price is None or not math.isfinite(price) or price <= 0:
                raise BidError(f"On “{line_label(line)}”, enter a real price greater "
                               "than zero.")
            if price > 1e12:
                raise BidError(f"On “{line_label(line)}”, that price is too large to be "
                               "real. Check for an extra digit.")
            if auction.compare_landed and not taxes.get(line.id):
                raise BidError(
                    f"Say what tax you charge on “{line_label(line)}” before you bid. "
                    "If there is none, type 0 — a bid with the tax left out is not "
                    "comparable with one that has it in.")

        # Price the whole basket with the figures being typed, item by item.
        priced: list[dict] = []
        total_all_in = 0.0
        for line in lines:
            price = round(float(prices[line.id]), 2)
            rows = taxes.get(line.id) or []
            cut = landed.breakdown(db, auction, line, user.vendor_id, price,
                                   charges=(charges if auction.compare_landed else None),
                                   taxes=rows)
            if line.has_ceiling and cut.all_in_unit > round(line.starting_price, 2) + 0.0001:
                raise BidError(
                    f"On “{line_label(line)}” your price comes to "
                    f"{fmt_money(cut.all_in_unit)} all in, above the buyer's starting "
                    f"price of {fmt_money(line.starting_price)}. Lower that item, or "
                    "quote less for delivery or tax.")
            priced.append({"line": line, "price": price, "cut": cut, "taxes": rows})
            total_all_in += cut.all_in_total
        total_all_in = round(total_all_in, 2)

        window = basket_window(db, auction, user.vendor_id)
        if window.exhausted:
            raise BidError("Bidding on this auction has gone as far as it can — there is "
                           "no total left that would beat the standing bid by the "
                           "minimum decrement.")
        # Your own last bid comes first: when you are the one in front, "the
        # best so far" is your own figure, and being told to beat yourself by
        # the decrement reads as nonsense.
        if window.mine is not None and total_all_in >= window.mine - 0.0001:
            raise BidError(
                f"Your own last bid for the whole auction was {fmt_money(window.mine)}. A "
                "new bid has to come in below it.")
        if window.max_allowed is not None and total_all_in > window.max_allowed + 0.0001:
            if window.reference_is_ceiling:
                raise BidError(
                    f"Your bid comes to {fmt_money(total_all_in)} for the whole auction, "
                    f"above the buyer's starting prices of {fmt_money(window.reference)}. "
                    "The starting prices are the most they will pay.")
            if window.blind:
                raise BidError(
                    f"Your bid comes to {fmt_money(total_all_in)} for the whole auction, "
                    "which does not beat the standing bid by the minimum decrement. The "
                    "buyer has chosen not to show the lowest price, so we cannot tell you "
                    "what it is. Try lower.")
            raise BidError(
                f"Your bid comes to {fmt_money(total_all_in)} for the whole auction. The "
                f"best so far is {fmt_money(window.reference)} and you have to come in at "
                f"{fmt_money(window.max_allowed)} or less.")
        previous_leader = standings(db, auction)
        was_leader = previous_leader[0] if previous_leader else None

        quote = Quote(auction_id=auction.id, vendor_id=user.vendor_id, user_id=user.id,
                      scope="auction", line_id=None,
                      freight=charges.freight, packaging=charges.packaging,
                      other=charges.other, other_label=charges.other_label,
                      note=note[:400], total_all_in=total_all_in)
        db.add(quote)
        db.flush()

        # The costs are the consignment's, so they stay on the auction seat -
        # that is what every screen reads to share them across the items.
        if auction.compare_landed:
            part = (db.query(Participant)
                      .filter_by(auction_id=auction.id, vendor_id=user.vendor_id).first())
            part.bidder_freight = charges.freight
            part.bidder_packaging = charges.packaging
            part.bidder_other = charges.other
            part.bidder_other_label = charges.other_label
            part.charges_updated_at = datetime.utcnow()

        made: list[tuple] = []
        for row in priced:
            line, price, cut = row["line"], row["price"], row["cut"]
            if auction.compare_landed:
                landed.save_line_taxes(db, auction, line, user.vendor_id, row["taxes"],
                                       quote_id=quote.id)
            bid = Bid(auction_id=auction.id, line_id=line.id, vendor_id=user.vendor_id,
                      user_id=user.id, unit_price=price, qty=line.qty,
                      total=round(price * float(line.qty or 0.0), 2),
                      landed_unit_price=cut.all_in_unit, note=note[:400],
                      quote_id=quote.id)
            db.add(bid)
            db.flush()
            made.append((line, bid))

        landed.reprice_vendor(db, auction, user.vendor_id)
        quote.detail = landed.snapshot(db, auction, made, quote)
        record(db, action="bid.place", entity_type="quote", entity_id=quote.id, actor=user,
               auction_id=auction.id, ip=ip,
               detail={"scope": "whole auction", "items": len(made),
                       "total_all_in": total_all_in})
        extended = maybe_extend(db, auction, user)
        extended_by = auction.extend_by_seconds
        db.commit()

    ranked = standings(db, auction)
    rank = next((row["rank"] for row in ranked if row["vendor_id"] == user.vendor_id), 1)
    notify.bid_received(db, auction, user, f"the whole auction ({len(made)} items)",
                        total_all_in, rank)
    if was_leader and was_leader["vendor_id"] != user.vendor_id \
            and total_all_in < was_leader["total"]:
        notify.outbid(db, auction, was_leader["vendor"], "the whole auction",
                      new_best=total_all_in, your_price=was_leader["total"],
                      landed=bool(auction.compare_landed))
    if extended:
        notify.auction_extended(db, auction, extended_by)
    return quote


# ------------------------------------------------------------------ taking it back
def latest_quote(db: Session, auction: Auction, vendor_id: int) -> Quote | None:
    """The last submission this bidder made that still stands."""
    return (db.query(Quote)
              .filter(Quote.auction_id == auction.id, Quote.vendor_id == vendor_id,
                      Quote.withdrawn.is_(False))
              .order_by(Quote.id.desc()).first())


def withdraw_latest(db: Session, auction: Auction, user: User, reason: str = "",
                    ip: str = "") -> Quote:
    """Take back the bid just placed, and let the one before it stand again.

    Only the most recent one, and only by the bidder who made it. A bidder who
    could reach back and pull out any bid could rewrite the whole history of
    the auction; taking back the last thing you typed is a correction, which
    is what people actually need. Whatever they bid before it is live again
    from that moment, exactly as it was.
    """
    if not user.vendor_id:
        raise BidError("Only a bidder can take back their own bid.")
    if auction.status != AuctionStatus.LIVE or datetime.utcnow() >= auction.end_at:
        raise BidError("Bidding has finished, so bids can no longer be withdrawn. "
                       "Speak to the buyer if this bid was a mistake.")
    quote = latest_quote(db, auction, user.vendor_id)
    if quote is None:
        raise BidError("You have no bid on this auction to take back.")
    quote.withdrawn = True
    quote.withdrawn_at = datetime.utcnow()
    quote.withdraw_reason = (reason or "")[:400]
    labels: list[str] = []
    for bid in list(quote.bids):
        bid.withdrawn = True
        bid.withdrawn_at = quote.withdrawn_at
        bid.withdraw_reason = quote.withdraw_reason
        labels.append(line_label(bid.line))
    # The taxes declared on the withdrawn submission go with it, and the ones
    # from the bid that now stands again take their place.
    db.query(LineTax).filter(LineTax.quote_id == quote.id).delete(synchronize_session=False)
    db.flush()
    restored = _restore_previous(db, auction, user.vendor_id)
    landed.reprice_vendor(db, auction, user.vendor_id)
    record(db, action="bid.withdraw", entity_type="quote", entity_id=quote.id, actor=user,
           auction_id=auction.id, ip=ip,
           detail={"reason": quote.withdraw_reason, "items": labels,
                   "restored": bool(restored)})
    db.commit()
    notify.bid_withdrawn(db, auction, quote.vendor,
                         ", ".join(labels) or "the whole auction")
    return quote


def _restore_previous(db: Session, auction: Auction, vendor_id: int) -> Quote | None:
    """Put back the taxes of the submission that stands again after a withdrawal."""
    previous = latest_quote(db, auction, vendor_id)
    if previous is None:
        return None
    detail = previous.as_detail()
    lines = {line.id: line for line in auction.lines}
    for item in detail.get("items", []):
        line = lines.get(item.get("line_id"))
        if line is None:
            continue
        rows = [TaxRow(name=tax.get("name", "Tax"), percent=float(tax.get("percent", 0.0)))
                for tax in item.get("taxes", [])]
        if auction.compare_landed:
            landed.save_line_taxes(db, auction, line, vendor_id, rows,
                                   quote_id=previous.id)
    if auction.compare_landed and previous.scope == "auction":
        part = (db.query(Participant)
                  .filter_by(auction_id=auction.id, vendor_id=vendor_id).first())
        if part is not None:
            part.bidder_freight = previous.freight or 0.0
            part.bidder_packaging = previous.packaging or 0.0
            part.bidder_other = previous.other or 0.0
            part.bidder_other_label = previous.other_label or ""
    db.flush()
    return previous


# ------------------------------------------------------------------ the record
def history(db: Session, auction: Auction, vendor_id: int | None) -> list[dict]:
    """Everything this bidder has offered on this auction, newest first.

    Each row is the submission as it was typed - every price, every tax rate
    and the amount it came to - with the moment it was made, so a bidder can
    see exactly what they bid rather than a figure worked out again today.
    """
    if not vendor_id:
        return []
    rows = (db.query(Quote)
              .filter(Quote.auction_id == auction.id, Quote.vendor_id == vendor_id)
              .order_by(Quote.id.desc()).all())
    standing = latest_quote(db, auction, vendor_id)
    out = []
    for quote in rows:
        detail = quote.as_detail()
        out.append({
            "quote": quote,
            "detail": detail,
            # Not "items": a dict has a method of that name and the template
            # would reach the method instead of the list.
            "priced": detail.get("items", []),
            "charges": detail.get("charges", {}),
            "total_all_in": detail.get("total_all_in", quote.total_all_in or 0.0),
            "total_bare": detail.get("total_bare", 0.0),
            "when": quote.created_at,
            "status": ("withdrawn" if quote.withdrawn
                       else "standing" if standing is not None and quote.id == standing.id
                       else "superseded"),
            "can_withdraw": (standing is not None and quote.id == standing.id
                             and not quote.withdrawn
                             and auction.status == AuctionStatus.LIVE
                             and datetime.utcnow() < auction.end_at),
        })
    return out
