from __future__ import annotations

import math

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from .. import landed
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


@router.post("/{auction_id}/bid")
def post_bid(auction_id: int, request: Request, line_id: str = Form(""),
             unit_price: str = Form(""), note: str = Form(""),
             user: User = Depends(vendor_only), db: Session = Depends(get_db)):
    auction = db.get(Auction, auction_id)
    # isdigit() is true for characters int() cannot parse ("²") and puts no
    # bound on the length, so a tampered or stale form turned what should be a
    # clean 404 into a 500 error page.
    key = _row_id(line_id)
    line = db.get(AuctionLine, key) if key else None
    if (not auction or auction.org_id != user.org_id
            or not line or line.auction_id != auction.id):
        raise HTTPException(404, "That item is not part of this auction.")
    if not unit_price.strip():
        return redirect(f"/auctions/{auction_id}",
                        "Type a price before pressing Place bid.", kind="error")
    try:
        price = float(unit_price)
    except ValueError:
        price = float("nan")
    if not math.isfinite(price):
        return redirect(f"/auctions/{auction_id}",
                        f"“{unit_price.strip()[:20]}” is not a price. Use digits only, "
                        "like 970.50.", kind="error")
    try:
        bid = place_bid(db, auction, line, user, price, note, ip=client_ip(request))
    except BidError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    from ..engine import vendor_rank
    rank = vendor_rank(db, line.id, user.vendor_id)
    if not auction.show_rank:
        # The buyer turned the standings off; the message that lands a second
        # after the bid must not be the one place they still appear.
        good = "Bid placed."
    elif rank == 1:
        good = "You are now L1 — the lowest bid."
    else:
        good = f"Bid placed. You are at L{rank}."
    return redirect(f"/auctions/{auction_id}", f"{good} We emailed you a confirmation.")


def _money(raw: str) -> float:
    """A money box as typed. Blank is nothing; anything unreadable is refused."""
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return 0.0
    value = float(raw)                       # ValueError handled by the caller
    if not math.isfinite(value) or value < 0:
        raise ValueError("negative")
    if value > 1e12:
        raise ValueError("absurd")
    return round(value, 2)


@router.post("/{auction_id}/charges")
def post_charges(auction_id: int, request: Request, freight: str = Form(""),
                 packaging: str = Form(""), other: str = Form(""),
                 other_label: str = Form(""), user: User = Depends(vendor_only),
                 db: Session = Depends(get_db)):
    """What the bidder says it costs to deliver this auction.

    One figure each for the whole auction, because that is how freight is
    quoted. They may change it while the auction is live - it is their own
    number, and their delivered prices are worked out again the moment it
    changes, so the board never shows a price that is no longer true.
    """
    auction = db.get(Auction, auction_id)
    if not auction or auction.org_id != user.org_id:
        raise HTTPException(404, "That auction does not exist.")
    if not auction.compare_landed:
        return redirect(f"/auctions/{auction_id}",
                        "This auction is decided on the bid price alone, so there is "
                        "nothing to add here.", kind="error")
    if auction.status not in (AuctionStatus.LIVE, AuctionStatus.SCHEDULED):
        return redirect(f"/auctions/{auction_id}",
                        "This auction is closed, so its costs can no longer be changed.",
                        kind="error")
    try:
        values = {"freight": _money(freight), "packaging": _money(packaging),
                  "other": _money(other)}
    except ValueError:
        return redirect(f"/auctions/{auction_id}",
                        "Those costs must be plain amounts — digits only, like 12000, "
                        "and never less than zero.", kind="error")
    try:
        saved = landed.save_charges(db, auction, user.vendor_id,
                                    other_label=other_label, **values)
    except ValueError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    if not saved.any:
        return redirect(f"/auctions/{auction_id}",
                        "Saved — you are quoting no delivery costs, so your bid price is "
                        "your delivered price.")
    return redirect(f"/auctions/{auction_id}",
                    f"Saved. {saved.describe()} — {fmt_money(saved.total)} in all, shared "
                    "across every item in the auction by what each is worth. You carry the "
                    "share of the items you price.")


@router.post("/{auction_id}/lines/{line_id}/taxes")
async def post_taxes(auction_id: int, line_id: int, request: Request,
                     user: User = Depends(vendor_only), db: Session = Depends(get_db)):
    """The taxes this bidder charges on one item, as percentages.

    As many as apply, each with a name. The money is never typed - it is
    worked out from the delivered value of the item, so the two can never
    disagree.
    """
    auction = db.get(Auction, auction_id)
    line = db.get(AuctionLine, line_id)
    if (not auction or auction.org_id != user.org_id
            or not line or line.auction_id != auction.id):
        raise HTTPException(404, "That item is not part of this auction.")
    if not auction.compare_landed:
        return redirect(f"/auctions/{auction_id}",
                        "This auction is decided on the bid price alone, so taxes are "
                        "not collected.", kind="error")
    if auction.status not in (AuctionStatus.LIVE, AuctionStatus.SCHEDULED):
        return redirect(f"/auctions/{auction_id}",
                        "This auction is closed, so its taxes can no longer be changed.",
                        kind="error")
    form = await request.form()
    names = form.getlist("tax_name")
    percents = form.getlist("tax_percent")
    rows: list[tuple[str, float]] = []
    for name, percent in zip(names, percents):
        percent = (percent or "").strip().replace("%", "")
        if not percent:
            continue
        try:
            value = float(percent)
        except ValueError:
            return redirect(f"/auctions/{auction_id}",
                            f"“{percent[:20]}” is not a percentage. Type the rate as a "
                            "number — 18 for 18%.", kind="error")
        if not math.isfinite(value) or value < 0:
            return redirect(f"/auctions/{auction_id}",
                            "A tax rate cannot be less than zero.", kind="error")
        rows.append((name, value))
    try:
        kept = landed.save_taxes(db, auction, line, user.vendor_id, rows)
    except ValueError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    label = line_label(line)
    if not kept:
        return redirect(f"/auctions/{auction_id}",
                        f"Saved — no taxes on {label}.")
    listed = ", ".join(f"{row.name} {row.percent:g}%" for row in kept)
    return redirect(f"/auctions/{auction_id}",
                    f"Saved for {label}: {listed}. The amounts are worked out for you.")


@router.post("/{auction_id}/bids/{bid_id}/withdraw")
def post_withdraw(auction_id: int, bid_id: int, request: Request, reason: str = Form(""),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
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
    try:
        withdraw_bid(db, bid, user, reason, ip=client_ip(request))
    except BidError as exc:
        return redirect(f"/auctions/{auction_id}", str(exc), kind="error")
    return redirect(f"/auctions/{auction_id}",
                    "Bid withdrawn. Ranks have been recalculated and the buyer told.")
