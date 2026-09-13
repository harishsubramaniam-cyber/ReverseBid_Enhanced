"""Placing a bid, and taking one back.

A bid is everything that makes up the offer, submitted at once: the price, what
it costs to deliver, and the tax charged on it. The shape of the form follows
how the buyer said the auction would be handed out - one item at a time, or
the whole thing in one go - because that is what the bidder is competing for.
"""
from __future__ import annotations

import math

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from .. import landed, quotes
from ..db import get_db
from ..engine import BidError, line_label, place_bid, withdraw_bid
from ..models import Auction, AuctionLine, AuctionStatus, Bid, User
from ..utils import fmt_money
from ..security import current_user, vendor_only
from ..web import client_ip, redirect

router = APIRouter(prefix="/auctions")


def _row_id(raw: str) -> int | None:
    """A database id out of a form field, or None. Never raises."""
    raw = (raw or "").strip()
    if not raw.isdecimal() or len(raw) > 18:
        return None
    try:
        value = int(raw)
    except ValueError:                 # pragma: no cover - isdecimal covers this
        return None
    return value if 0 < value < 2 ** 62 else None


class Typed(ValueError):
    """Something in the form is not a number anybody could use."""


def _money(raw: str, what: str) -> float:
    """A money box as typed. Blank is nothing; anything unreadable is refused."""
    raw = (raw or "").strip()
    if not raw:
        return 0.0
    if "," in raw:
        # Half the world writes 12,50 for twelve and a half and the other half
        # writes 1,250 for one thousand two hundred and fifty. Quietly dropping
        # the comma reads the first as 1250, so the box takes neither.
        raise Typed(f"{what} — type it without commas: 1250.50, not 1,250.50.")
    try:
        value = float(raw)
    except ValueError:
        raise Typed(f"{what} — “{raw[:20]}” is not an amount. Digits only, like 12000.")
    if not math.isfinite(value) or value < 0:
        raise Typed(f"{what} cannot be less than zero.")
    if value > 1e12:
        raise Typed(f"{what} is too large to be real. Check for an extra digit.")
    return round(value, 2)


def _charges(form, auction: Auction) -> landed.Charges:
    """The delivery costs on this form — for one item, or for the consignment."""
    if not auction.compare_landed:
        return landed.Charges()
    return landed.Charges(
        freight=_money(form.get("freight"), "Freight"),
        packaging=_money(form.get("packaging"), "Packaging"),
        other=_money(form.get("other"), "Other costs"),
        other_label=(form.get("other_label") or "").strip()[:60],
        declared=True)


def _taxes(names: list[str], percents: list[str], where: str) -> list[quotes.TaxRow]:
    """The taxes on one item, as typed. A rate of zero counts; a blank box does not."""
    rows: list[quotes.TaxRow] = []
    for name, percent in zip(names, percents):
        raw = (percent or "").strip().replace("%", "")
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            raise Typed(f"{where}: “{raw[:20]}” is not a percentage. Type the rate as a "
                        "number — 18 for 18%.")
        if not math.isfinite(value) or value < 0:
            raise Typed(f"{where}: a tax rate cannot be less than zero.")
        if value > 100:
            raise Typed(f"{where}: {value:g}% is not a rate anybody charges. Percentages "
                        "only — 18 for 18%, not the amount.")
        rows.append(quotes.TaxRow(name=(name or "Tax").strip()[:60] or "Tax",
                                  percent=round(value, 4)))
    if rows and sum(row.percent for row in rows) > 100:
        raise Typed(f"{where}: those taxes add up to more than 100%. Check the rates.")
    return rows


@router.post("/{auction_id}/bid")
async def post_bid(auction_id: int, request: Request, user: User = Depends(vendor_only),
                   db: Session = Depends(get_db)):
    """A bid on one item: its price, its delivery costs and its taxes, together."""
    form = await request.form()
    auction = db.get(Auction, auction_id)
    key = _row_id(str(form.get("line_id") or ""))
    line = db.get(AuctionLine, key) if key else None
    if (not auction or auction.org_id != user.org_id
            or not line or line.auction_id != auction.id):
        raise HTTPException(404, "That item is not part of this auction.")
    back = f"/auctions/{auction_id}"
    if quotes.whole_auction_bidding(auction):
        return redirect(back, "This auction is awarded to one supplier for everything, so "
                              "it is bid for as one lot — use the form at the top of the "
                              "page.", kind="error")

    raw_price = str(form.get("unit_price") or "").strip()
    if not raw_price:
        return redirect(back, f"Type a price for {line_label(line)} before pressing "
                              "Place bid.", kind="error")
    try:
        price = float(raw_price) if "," not in raw_price else float("nan")
    except ValueError:
        price = float("nan")
    if not math.isfinite(price):
        return redirect(back, f"“{raw_price[:20]}” is not a price. Use digits only, "
                              "like 970.50 — no commas.", kind="error")
    try:
        charges = _charges(form, auction)
        taxes = _taxes(form.getlist("tax_name"), form.getlist("tax_percent"),
                       line_label(line))
    except Typed as exc:
        return redirect(back, str(exc), kind="error")

    try:
        # Always as a submission, even on an auction with no extras to
        # collect: it is what the bidder's own record of their bids is built
        # from, and what "take back my last bid" takes back.
        place_bid(db, auction, line, user, price, str(form.get("note") or ""),
                  ip=client_ip(request), charges=charges,
                  taxes=taxes if auction.compare_landed else [])
    except BidError as exc:
        return redirect(back, str(exc), kind="error")

    from ..engine import vendor_rank
    rank = vendor_rank(db, line.id, user.vendor_id)
    if not auction.show_rank:
        good = f"Bid placed on {line_label(line)}."
    elif rank == 1:
        good = f"You are now L1 on {line_label(line)} — the lowest bid."
    else:
        good = f"Bid placed on {line_label(line)}. You are at L{rank}."
    return redirect(back, f"{good} We emailed you a confirmation.")


@router.post("/{auction_id}/bid-all")
async def post_basket_bid(auction_id: int, request: Request,
                          user: User = Depends(vendor_only),
                          db: Session = Depends(get_db)):
    """One bid for the whole auction: every item priced on a single form."""
    auction = db.get(Auction, auction_id)
    if not auction or auction.org_id != user.org_id:
        raise HTTPException(404, "That auction does not exist.")
    back = f"/auctions/{auction_id}"
    if not quotes.whole_auction_bidding(auction):
        return redirect(back, "This auction is awarded item by item, so each item is bid "
                              "for on its own.", kind="error")
    form = await request.form()
    prices: dict[int, float] = {}
    taxes: dict[int, list[quotes.TaxRow]] = {}
    try:
        charges = _charges(form, auction)
        for line in auction.lines:
            raw = str(form.get(f"price_{line.id}") or "").strip()
            if raw:
                try:
                    value = float(raw) if "," not in raw else float("nan")
                    if not math.isfinite(value):
                        raise ValueError
                except ValueError:
                    raise Typed(f"On “{line_label(line)}”, “{raw[:20]}” is not a price. "
                                "Use digits only, like 970.50 — no commas.")
                prices[line.id] = value
            taxes[line.id] = _taxes(form.getlist(f"tax_name_{line.id}"),
                                    form.getlist(f"tax_percent_{line.id}"),
                                    line_label(line))
    except Typed as exc:
        return redirect(back, str(exc), kind="error")

    try:
        quote = quotes.place_basket(db, auction, user, prices=prices, charges=charges,
                                    taxes=taxes, note=str(form.get("note") or ""),
                                    ip=client_ip(request))
    except BidError as exc:
        return redirect(back, str(exc), kind="error")

    ranked = quotes.standings(db, auction)
    rank = next((row["rank"] for row in ranked if row["vendor_id"] == user.vendor_id), 1)
    total = fmt_money(quote.total_all_in)
    if not auction.show_rank:
        good = f"Bid placed for the whole auction — {total} all in."
    elif rank == 1:
        good = f"You are now L1 for the whole auction at {total} all in."
    else:
        good = f"Bid placed for the whole auction — {total} all in. You are at L{rank}."
    return redirect(back, f"{good} We emailed you a confirmation.")


@router.post("/{auction_id}/withdraw-last")
def post_withdraw_last(auction_id: int, request: Request, reason: str = Form(""),
                       user: User = Depends(vendor_only), db: Session = Depends(get_db)):
    """A bidder taking back the last bid they placed."""
    auction = db.get(Auction, auction_id)
    if not auction or auction.org_id != user.org_id:
        raise HTTPException(404, "That auction does not exist.")
    try:
        quotes.withdraw_latest(db, auction, user, reason, ip=client_ip(request))
    except BidError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    standing = quotes.latest_quote(db, auction, user.vendor_id)
    if standing is None:
        message = ("Your last bid has been taken back. You have no bid standing on this "
                   "auction now.")
    else:
        detail = standing.as_detail()
        when = detail.get("placed_at", "")
        message = ("Your last bid has been taken back. The bid you placed"
                   + (f" on {when}" if when else " before it")
                   + f" — {fmt_money(standing.total_all_in)} all in — stands again.")
    return redirect(f"/auctions/{auction_id}", message)


@router.post("/{auction_id}/bids/{bid_id}/withdraw")
def post_withdraw(auction_id: int, bid_id: int, request: Request, reason: str = Form(""),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The buyer striking one bid out.

    Bidders do not come this way any more: they take back their own last
    submission, which puts the one before it back in force. This is the
    buyer's power to strike out any bid at all, and it stays with them.
    """
    bid = db.get(Bid, bid_id)
    auction = db.get(Auction, auction_id)
    # The organisation check that every other route here does. Without it a
    # buyer who had signed up for their own organisation - anyone at all, on an
    # open sign-up - could withdraw the leading bid from a stranger's live
    # auction: bid ids are sequential, and the supplier got a plausible-looking
    # "your bid was withdrawn" email.
    if (not bid or not auction or bid.auction_id != auction_id
            or auction.org_id != user.org_id):
        raise HTTPException(404, "That bid does not exist.")
    if not user.is_buyer_side:
        # A bidder reaching this from an older page, or from the item itself:
        # it only works on the bid they placed last, and it goes through the
        # same door as the button, so the bid before it stands again.
        latest = quotes.latest_quote(db, auction, user.vendor_id)
        if (bid.vendor_id != user.vendor_id or latest is None
                or bid.quote_id != latest.id):
            return redirect(f"/auctions/{auction_id}",
                            "You can only take back your most recent bid. Your earlier "
                            "bids stand — speak to the buyer if one of them was a "
                            "mistake.", kind="error")
        try:
            quotes.withdraw_latest(db, auction, user, reason, ip=client_ip(request))
        except BidError as exc:
            return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
        return redirect(f"/auctions/{auction_id}",
                        "Your last bid has been taken back. Whatever you bid before it "
                        "stands again.")
    try:
        withdraw_bid(db, bid, user, reason, ip=client_ip(request))
    except BidError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    return redirect(f"/auctions/{auction_id}",
                    "Bid withdrawn. Ranks have been recalculated and the bidder told.")
