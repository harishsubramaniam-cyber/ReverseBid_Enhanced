"""Delivered cost, the way a supplier actually quotes it.

The original version asked the *buyer* to record each supplier's freight as an
amount per unit. That is not how anybody quotes: freight is a figure for the
consignment, the supplier knows it and the buyer does not, and "freight per
pen" is a number nobody has.

So in this version the **bidder** enters three figures, once, for the whole
auction — freight, packaging and other costs — and a list of **taxes per item**,
each as a percentage. Everything else follows from those:

* The three whole-auction figures are **spread across the items** in
  proportion to what each item is worth, so every item still has a delivered
  price of its own and L1 on an item still means something. An item worth half
  the basket carries half the freight.
* Each item's **taxes are applied to its delivered value** — the bid plus its
  share of the charges — because that is what a tax is charged on. The amounts
  are never typed; they are worked out, so they cannot disagree with the price.
* What the auction ranks on is the **all-in delivered price**: bid + share of
  charges + tax.

The result is expressed as an ``Adders`` — an amount per unit and a percentage
— which is exactly what the engine already knew how to rank, validate and
convert back into "the most you may type". The share of the charges is the
amount per unit; the taxes are the percentage.

One consequence worth naming: a bidder's share of the charges depends on which
items they have priced, so pricing another item changes the share on the first.
``reprice_vendor`` recalculates that bidder's stored delivered prices whenever
their basket, their charges or their taxes change. It never touches anybody
else's, because nothing about one bidder can move another.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from .models import Auction, AuctionLine, Bid, LineTax, Participant


def per_line_costs(auction: Auction) -> bool:
    """Are delivery costs quoted item by item, or once for the consignment?

    It follows how the buyer said the business would be handed out, because
    that is what the bidder is actually quoting for. Item by item, a bidder
    may win one item and not another, so freight has to be that item's own.
    All to one supplier, and there is a single consignment, so there is a
    single freight figure and it is shared across the items by what each is
    worth.
    """
    return (auction.award_mode or "line") != "basket"

#: The three things a bidder is asked for, in the order they appear on screen.
CHARGE_FIELDS = (("bidder_freight", "Freight"),
                 ("bidder_packaging", "Packaging"),
                 ("bidder_other", "Other costs"))


@dataclass
class Charges:
    """What one bidder says it costs to deliver this whole auction."""
    freight: float = 0.0
    packaging: float = 0.0
    other: float = 0.0
    other_label: str = ""
    declared: bool = False        # they have saved the panel at least once

    @property
    def total(self) -> float:
        return round(self.freight + self.packaging + self.other, 2)

    @property
    def any(self) -> bool:
        return self.total > 0

    def parts(self) -> list[tuple[str, float]]:
        named = [("Freight", self.freight), ("Packaging", self.packaging),
                 (self.other_label.strip() or "Other costs", self.other)]
        return [(label, value) for label, value in named if value]

    def describe(self) -> str:
        from .utils import fmt_money
        return ", ".join(f"{label} {fmt_money(value)}" for label, value in self.parts()) \
            or "none"


NO_CHARGES = Charges()


def line_charges(db: Session, line_id: int, vendor_id: int | None) -> Charges:
    """What this bidder quoted to deliver ONE item, as it stands now.

    Item-by-item auctions carry the costs on the bid itself: they were typed
    on the same form, at the same moment, and they are part of that offer.
    """
    if not vendor_id:
        return NO_CHARGES
    from .engine import vendor_best
    bid = vendor_best(db, line_id, vendor_id)
    if bid is None:
        return NO_CHARGES
    return Charges(freight=round(float(bid.freight or 0.0), 2),
                   packaging=round(float(bid.packaging or 0.0), 2),
                   other=round(float(bid.other or 0.0), 2),
                   other_label=(bid.other_label or ""), declared=True)


def charges_for(db: Session, auction: Auction, vendor_id: int | None) -> Charges:
    """This bidder's own freight, packaging and other costs for this auction."""
    if not auction.compare_landed or not vendor_id:
        return NO_CHARGES
    part = (db.query(Participant)
              .filter_by(auction_id=auction.id, vendor_id=vendor_id).first())
    return charges_from(part)


def charges_from(part: Participant | None) -> Charges:
    if part is None:
        return NO_CHARGES
    return Charges(freight=round(float(part.bidder_freight or 0.0), 2),
                   packaging=round(float(part.bidder_packaging or 0.0), 2),
                   other=round(float(part.bidder_other or 0.0), 2),
                   other_label=(part.bidder_other_label or ""),
                   declared=part.charges_updated_at is not None)


def save_charges(db: Session, auction: Auction, vendor_id: int, *, freight: float,
                 packaging: float, other: float, other_label: str = "") -> Charges:
    """Record what a bidder says delivery costs, and re-rank their bids."""
    part = (db.query(Participant)
              .filter_by(auction_id=auction.id, vendor_id=vendor_id).first())
    if part is None:
        raise ValueError("That bidder is not on this auction.")
    part.bidder_freight = round(max(0.0, float(freight or 0.0)), 2)
    part.bidder_packaging = round(max(0.0, float(packaging or 0.0)), 2)
    part.bidder_other = round(max(0.0, float(other or 0.0)), 2)
    part.bidder_other_label = (other_label or "").strip()[:60]
    part.charges_updated_at = datetime.utcnow()
    was_leading = _leaders(db, auction)
    db.flush()
    reprice_vendor(db, auction, vendor_id)
    problems = _ceiling_problems(db, auction, vendor_id)
    if problems:
        db.rollback()
        raise ValueError(
            "Those costs cannot be saved as they stand: " + "; ".join(problems) +
            ". Lower that bid first, or quote less for delivery.")
    db.commit()
    _tell_the_displaced(db, auction, was_leading, vendor_id)
    return charges_from(part)



def _ceiling_problems(db: Session, auction: Auction, vendor_id: int) -> list[str]:
    """Bids of this bidder's that no longer fit under the buyer's ceiling.

    Charges and taxes are declared separately from the bid, so a bidder could
    place a bid that fits, then add 18% tax to it and sit above the ceiling
    with a bid the engine would never have accepted. The ceiling is the
    buyer's rule and it has to hold whichever order things are typed in, so a
    change that would break it is refused and the bidder is told what to do
    about it.
    """
    from .utils import fmt_money
    lines = {line.id: line for line in auction.lines}
    problems: list[str] = []
    for bid in (db.query(Bid).filter(Bid.auction_id == auction.id,
                                     Bid.vendor_id == vendor_id,
                                     Bid.withdrawn.is_(False)).all()):
        line = lines.get(bid.line_id)
        if line is None or not line.has_ceiling:
            continue
        all_in = bid.landed_unit_price or bid.unit_price
        ceiling = round(float(line.starting_price), 2)
        if round(all_in, 2) > ceiling + 0.0001:
            from .engine import line_label
            problems.append(
                f"your bid of {fmt_money(bid.unit_price)} on {line_label(line)} would come "
                f"to {fmt_money(all_in)} all in, above the buyer's starting price of "
                f"{fmt_money(ceiling)}")
    return problems



def _leaders(db: Session, auction: Auction) -> dict[int, int]:
    """Who is L1 on each item right now: line id -> vendor id."""
    from .engine import best_bid
    out: dict[int, int] = {}
    for line in auction.lines:
        best = best_bid(db, line.id)
        if best is not None:
            out[line.id] = best.vendor_id
    return out


def _tell_the_displaced(db: Session, auction: Auction, was_leading: dict[int, int],
                        actor_vendor_id: int) -> None:
    """Email whoever has just lost the lead because somebody revised their costs.

    A bidder who lowers their own declared freight can take L1 without placing
    a bid. That is allowed - it is their own number, and correcting it is not
    a bid - but the bidder it displaces was silently demoted: no email, and a
    screen that simply changed the next time they looked at it. They are now
    told, in the same words an ordinary outbid would use.
    """
    from . import notify
    from .engine import best_bid, compare_price, line_label
    for line in auction.lines:
        before = was_leading.get(line.id)
        if not before or before == actor_vendor_id:
            continue
        now_best = best_bid(db, line.id)
        if now_best is None or now_best.vendor_id == before:
            continue
        beaten = next((b for b in auction.bids
                       if b.line_id == line.id and b.vendor_id == before
                       and not b.withdrawn), None)
        if beaten is None:
            continue
        try:
            notify.outbid(db, auction, beaten.vendor, line_label(line),
                          new_best=compare_price(now_best),
                          your_price=compare_price(beaten), landed=True)
        except Exception:                # pragma: no cover - never block a save
            pass


# ------------------------------------------------------------------ taxes
def taxes_for(db: Session, line_id: int, vendor_id: int | None) -> list[LineTax]:
    if not vendor_id:
        return []
    return (db.query(LineTax)
              .filter_by(line_id=line_id, vendor_id=vendor_id)
              .order_by(LineTax.id.asc()).all())


def tax_rate(taxes: list[LineTax]) -> float:
    """The taxes added together, as a percentage."""
    return round(sum(float(t.percent or 0.0) for t in taxes), 4)


def save_taxes(db: Session, auction: Auction, line: AuctionLine, vendor_id: int,
               rows: list[tuple[str, float]]) -> list[LineTax]:
    """Replace this bidder's taxes on this item, then re-rank their bids.

    Rows with no percentage are dropped rather than stored as zero: a tax of
    nothing is not a tax, and leaving it on screen invites the question.
    """
    # The same check save_charges makes. Without it a supplier who could not
    # even open the auction could still write rows against it, which would
    # come quietly into force if they were ever invited.
    if not (db.query(Participant)
              .filter_by(auction_id=auction.id, vendor_id=vendor_id).first()):
        raise ValueError("You are not on the invited bidder list for this auction.")
    db.query(LineTax).filter_by(line_id=line.id, vendor_id=vendor_id).delete()
    kept: list[LineTax] = []
    for name, percent in rows:
        percent = round(float(percent or 0.0), 4)
        if percent <= 0:
            continue
        if percent > 100:
            raise ValueError(f"A tax of {percent:g}% is not a rate anybody charges. "
                             "Percentages only — 18 for 18%, not the amount.")
        row = LineTax(auction_id=auction.id, line_id=line.id, vendor_id=vendor_id,
                      name=(name or "Tax").strip()[:60] or "Tax", percent=percent)
        db.add(row)
        kept.append(row)
    if tax_rate(kept) > 100:
        raise ValueError("Those taxes add up to more than 100%. Check the rates.")
    was_leading = _leaders(db, auction)
    db.flush()
    reprice_vendor(db, auction, vendor_id)
    problems = _ceiling_problems(db, auction, vendor_id)
    if problems:
        db.rollback()
        raise ValueError(
            "Those taxes cannot be saved as they stand: " + "; ".join(problems) +
            ". Declare your taxes before you bid, or lower that bid first.")
    db.commit()
    _tell_the_displaced(db, auction, was_leading, vendor_id)
    return kept


# ------------------------------------------------------- spreading the charges
def _weight(db: Session, auction: Auction, line: AuctionLine, vendor_id: int) -> float:
    """What this item is worth, for the purpose of sharing out the charges.

    The buyer's own ceiling is used where there is one: it is the same for
    every bidder and it does not move while the auction runs, so a bidder's
    share of their own freight does not jump about as prices fall. Where there
    is no ceiling the bidder's own latest price stands in, and where there is
    neither, the quantity alone.
    """
    qty = float(line.qty or 0.0)
    if line.has_ceiling:
        return round(qty * float(line.starting_price), 4)
    # No ceiling: the quantity alone. It must not depend on anybody's bid.
    # Weighting an open line by the bidder's own price meant their freight
    # moved between items every time they bid, so a bid accepted at 99.99
    # delivered could be repriced to 106.66 - above the buyer's ceiling on a
    # different item - by a later bid on this one, with nobody told.
    return round(qty, 4)


def shares_for(db: Session, auction: Auction, vendor_id: int | None,
               including: int | None = None) -> dict[int, float]:
    """How much of this bidder's charges each item carries, in money.

    Every item in the auction takes a share, weighted by what it is worth -
    not only the items this bidder has priced so far. That choice matters, and
    it was made the other way first:

    Sharing only across the items they had already priced meant the very first
    bid carried the whole freight bill, and a bidder whose freight was a tenth
    of the auction could be refused on a small item and accepted on a large
    one - the same bidder, the same costs, a different answer depending on
    which item they happened to price first. Nobody could be told why.

    Spreading it over the whole auction is stable: an item's share is the same
    before the first bid and after the last, so the price the screen offers is
    the price the engine accepts, and a bidder can work in any order. The
    trade-off is that a bidder who quotes only some of the items carries only
    those items' share of their own costs - which is defensible, since a part
    load is genuinely a smaller delivery, and the overall standing already
    says plainly when a basket is incomplete.

    ``including`` is accepted for callers that want to be explicit about the
    item being priced; it changes nothing, because that item already has a
    share.
    """
    if per_line_costs(auction):
        # Nothing to share: each item carries the costs quoted against it.
        return {}
    charges = charges_for(db, auction, vendor_id)
    if not charges.any or not vendor_id:
        return {}
    lines = list(auction.lines)
    if not lines:
        return {}
    weights = {line.id: max(_weight(db, auction, line, vendor_id), 0.0001) for line in lines}
    total = sum(weights.values())
    if total <= 0:                       # pragma: no cover - guarded by the max above
        return {}
    shares = {line_id: round(charges.total * weight / total, 2)
              for line_id, weight in weights.items()}
    # Rounding each share to the paisa can lose or gain a paisa overall; the
    # largest share absorbs it, so the shares always add up to what was quoted.
    drift = round(charges.total - sum(shares.values()), 2)
    if drift and shares:
        biggest = max(shares, key=lambda line_id: shares[line_id])
        shares[biggest] = round(shares[biggest] + drift, 2)
    return shares


def delivery_on(db: Session, auction: Auction, line: AuctionLine,
                vendor_id: int | None, charges: Charges | None = None) -> float:
    """The delivery cost this one item carries for this bidder, in money.

    ``charges`` lets a submission that has not been saved yet be priced with
    the figures being typed, which is what the bid screen and the check on a
    new bid both need: the costs arrive with the bid, so they cannot be read
    back out of the database until it is accepted.
    """
    if not auction.compare_landed or not vendor_id:
        return 0.0
    if per_line_costs(auction):
        quoted = charges if charges is not None else line_charges(db, line.id, vendor_id)
        return quoted.total
    if charges is not None:
        # A whole-auction submission being checked: share the figures typed.
        return _share_of(auction, line, charges.total)
    return shares_for(db, auction, vendor_id).get(line.id, 0.0)


def _share_of(auction: Auction, line: AuctionLine, total: float) -> float:
    """One item's share of a whole-auction charge, by what each item is worth."""
    if total <= 0:
        return 0.0
    lines = list(auction.lines)
    weights = {row.id: max(round(float(row.qty or 0.0)
                                 * (float(row.starting_price) if row.has_ceiling else 1.0), 4),
                           0.0001) for row in lines}
    grand = sum(weights.values())
    if grand <= 0:                        # pragma: no cover - guarded above
        return 0.0
    return round(total * weights.get(line.id, 0.0) / grand, 2)


@dataclass
class Breakdown:
    """One item, one bidder, priced out in full - what every screen shows."""
    unit_price: float = 0.0
    qty: float = 0.0
    bare_total: float = 0.0
    charge_share: float = 0.0
    delivered_total: float = 0.0
    taxes: list[tuple[str, float, float]] = field(default_factory=list)  # name, %, amount
    tax_total: float = 0.0
    all_in_total: float = 0.0

    @property
    def all_in_unit(self) -> float:
        """The per-unit price the ranking turns on, to the paisa for display."""
        return round(self.all_in_total / self.qty, 2) if self.qty else 0.0


def breakdown(db: Session, auction: Auction, line: AuctionLine, vendor_id: int | None,
              unit_price: float, charges: Charges | None = None,
              taxes: list | None = None) -> Breakdown:
    """Price one item out for one bidder, at a price they might type.

    Deliberately built on exactly the arithmetic the engine ranks on - per
    unit, rounded once at the end - because this is what the bidder is shown
    above the words "what you are ranked on". Working the line out separately
    and rounding the delivered price per unit as well left the two disagreeing
    by a few rupees on a large quantity, and the screen was the one that lied.

    Tax carries the rounding, because tax is the derived figure here: the bid
    and the delivery share are amounts somebody actually quoted.
    """
    qty = float(line.qty or 0.0)
    price = round(float(unit_price), 2)
    bare = round(price * qty, 2)
    share = delivery_on(db, auction, line, vendor_id, charges)
    delivered = round(bare + share, 2)
    rows = (taxes if taxes is not None
            else (taxes_for(db, line.id, vendor_id) if auction.compare_landed else []))
    rate = tax_rate(rows)
    share_unit = share / qty if qty else 0.0
    all_in_unit = round((price + share_unit) * (1 + rate / 100.0), 4)
    all_in = round(all_in_unit * qty, 2)
    tax_total = round(all_in - delivered, 2)
    # Each named tax takes its share of that total, in proportion to its rate,
    # so the parts always add up to the whole the ranking used.
    taxes = []
    for index, row in enumerate(rows):
        percent = float(row.percent)
        amount = (round(tax_total * percent / rate, 2) if rate else 0.0)
        taxes.append((row.name, percent, amount))
    if taxes:
        drift = round(tax_total - sum(amount for _, _, amount in taxes), 2)
        if drift:
            biggest = max(range(len(taxes)), key=lambda i: taxes[i][2])
            name, percent, amount = taxes[biggest]
            taxes[biggest] = (name, percent, round(amount + drift, 2))
    return Breakdown(unit_price=price, qty=qty, bare_total=bare, charge_share=share,
                     delivered_total=delivered, taxes=taxes, tax_total=tax_total,
                     all_in_total=all_in)


def adders_for(db: Session, auction: Auction, line: AuctionLine | None,
               vendor_id: int | None):
    """This bidder's delivered costs on this item, as an amount per unit and a
    percentage — the shape the engine ranks and validates with.

    The share of the charges is fixed for the item, so it is an amount per
    unit; the taxes are a percentage of everything before them. Keeping it in
    this shape is what lets the decrement rules, the ceiling check and "the
    most you may type" all go on working unchanged.
    """
    from .engine import Adders, NO_ADDERS
    if not auction.compare_landed or not vendor_id or line is None:
        return NO_ADDERS
    qty = float(line.qty or 0.0) or 1.0
    share = delivery_on(db, auction, line, vendor_id)
    rows = taxes_for(db, line.id, vendor_id)
    percent = tax_rate(rows)
    parts: list[tuple[str, float, str]] = []
    if share:
        parts.append(("delivery", share / qty, "unit"))
    for row in rows:
        parts.append((row.name, float(row.percent), "percent"))
    # Not rounded: the award screen and the ranking both build on this, and
    # rounding it here made them disagree by a paisa a unit.
    return Adders(per_unit=share / qty, percent=percent, lines=parts)


def adders_from(db: Session, auction: Auction, line: AuctionLine, vendor_id: int | None,
                charges: "Charges | None", taxes: list | None):
    """The same shape as ``adders_for``, but for figures being typed right now."""
    from .engine import Adders, NO_ADDERS
    if not auction.compare_landed or not vendor_id or line is None:
        return NO_ADDERS
    qty = float(line.qty or 0.0) or 1.0
    share = delivery_on(db, auction, line, vendor_id, charges)
    rows = taxes if taxes is not None else taxes_for(db, line.id, vendor_id)
    percent = tax_rate(rows)
    parts: list[tuple[str, float, str]] = []
    if share:
        parts.append(("delivery", share / qty, "unit"))
    for row in rows:
        parts.append((row.name, float(row.percent), "percent"))
    return Adders(per_unit=share / qty, percent=percent, lines=parts)


def save_line_taxes(db: Session, auction: Auction, line: AuctionLine, vendor_id: int,
                    rows: list, quote_id: int | None = None) -> list[LineTax]:
    """Replace this bidder's taxes on one item with the ones just submitted.

    Unlike the old separate tax panel, a rate of zero is kept. It was typed on
    purpose - the bid could not be placed without it - and "GST 0%" on the
    board is a statement, where an empty space is a question.
    """
    db.query(LineTax).filter_by(line_id=line.id, vendor_id=vendor_id).delete()
    kept: list[LineTax] = []
    for row in rows:
        name = getattr(row, "name", None) or "Tax"
        percent = round(float(getattr(row, "percent", 0.0) or 0.0), 4)
        made = LineTax(auction_id=auction.id, line_id=line.id, vendor_id=vendor_id,
                       name=str(name).strip()[:60] or "Tax", percent=percent,
                       quote_id=quote_id)
        db.add(made)
        kept.append(made)
    db.flush()
    return kept


def snapshot(db: Session, auction: Auction, rows: list, quote) -> str:
    """The submission as typed, as JSON, for the bidder's own record.

    Worked out once, at the moment of the bid, and never again: a bidder
    looking back at what they offered should see what they offered, not a
    figure recalculated under rules or costs that have since moved.
    """
    import json
    from .utils import fmt_dt
    charges = Charges(freight=float(quote.freight or 0.0),
                      packaging=float(quote.packaging or 0.0),
                      other=float(quote.other or 0.0),
                      other_label=quote.other_label or "")
    items = []
    for line, bid in rows:
        taxes = taxes_for(db, line.id, quote.vendor_id)
        cut = breakdown(db, auction, line, quote.vendor_id, bid.unit_price,
                        charges=(charges if quote.scope == "line" else None),
                        taxes=taxes)
        from .engine import line_label
        items.append({
            "line_id": line.id,
            "item": line_label(line),
            "qty": float(line.qty or 0.0),
            "unit": (line.unit.code if line.unit else ""),
            "unit_price": bid.unit_price,
            "bare_total": cut.bare_total,
            "delivery": cut.charge_share,
            "delivered_total": cut.delivered_total,
            "taxes": [{"name": name, "percent": percent, "amount": amount}
                      for name, percent, amount in cut.taxes],
            "tax_total": cut.tax_total,
            "all_in_total": cut.all_in_total,
            "all_in_unit": cut.all_in_unit,
        })
    return json.dumps({
        "scope": quote.scope,
        "placed_at": fmt_dt(quote.created_at or datetime.utcnow(), True),
        "charges": {"freight": charges.freight, "packaging": charges.packaging,
                    "other": charges.other, "other_label": charges.other_label,
                    "total": charges.total},
        "items": items,
        "total_all_in": round(sum(row["all_in_total"] for row in items), 2),
        "total_bare": round(sum(row["bare_total"] for row in items), 2),
    })


def reprice_vendor(db: Session, auction: Auction, vendor_id: int | None) -> int:
    """Recalculate this bidder's delivered prices across the whole auction.

    Their share of the charges depends on which items they have priced, so a
    new bid on one item changes the share carried by the others. Every screen
    reads ``landed_unit_price`` off the bid, so it has to be brought back into
    line the moment anything moves - otherwise the board would rank on prices
    that were true five minutes ago.
    """
    if not vendor_id or not auction.compare_landed:
        return 0
    lines = {line.id: line for line in auction.lines}
    shares = shares_for(db, auction, vendor_id)
    bids = (db.query(Bid).filter(Bid.auction_id == auction.id,
                                 Bid.vendor_id == vendor_id).all())
    changed = 0
    for bid in bids:
        line = lines.get(bid.line_id)
        if line is None:                 # pragma: no cover - orphan bid
            continue
        qty = float(line.qty or 0.0) or 1.0
        if per_line_costs(auction):
            share = 0.0 if bid.withdrawn else bid.charges_total
        else:
            share = 0.0 if bid.withdrawn else shares.get(bid.line_id, 0.0)
        percent = tax_rate(taxes_for(db, bid.line_id, vendor_id))
        all_in_unit = round((bid.unit_price + share / qty) * (1 + percent / 100.0), 4)
        if bid.landed_unit_price != all_in_unit:
            bid.landed_unit_price = all_in_unit
            changed += 1
    if changed:
        db.flush()
    return changed
