"""Which correspondence the buyer may not read yet.

An auction can be run with the bidders' names kept from the buyer until it is
awarded, so that the winner is picked on the figures alone. The board, the
reports and the audit trail all speak in "Bidder A" while that is true - but
the Outbox holds a copy of every email the platform has sent, addressed to the
people it went to. Left alone it is a decoder ring: a "Bid received" mail to
someone@alpha.example at 11:04 beside a bid on the board at 11:04 names them.

So an email about a blind auction, sent to one of its bidders, is **sealed**:
the Outbox still shows that it went, when, and whether it arrived - which is
everything that screen is for - but not who to, and not what it said. Sealing
lifts with the blind, at the moment the auction is awarded.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import engine
from .models import Auction, EmailMessage, User

#: Letters that go to **every** invited bidder at once, whatever they have or
#: have not done. Reading one tells the buyer nothing they did not already know
#: - they chose the invitation list - so these stay open even on a blind
#: auction, and "did my invitation actually go out?" remains answerable.
#:
#: Everything else is sealed, because it is sent *because of* something one
#: bidder did: a bid landed, somebody was outbid, a bid was taken back, a
#: message was written. The time on that letter lines up with a row on the
#: board, and the two together give the name away.
BROADCAST = {"invited", "published", "updated", "starting_soon", "started",
             "extended", "ending_soon", "closed", "cancelled"}


class Seal:
    """Answers "may this buyer read this email?" without asking twice."""

    def __init__(self, db: Session, viewer: User):
        self.db = db
        self.viewer = viewer
        self._our_side: dict[int, set[str]] = {}
        self._blind: dict[int, bool] = {}

    def _auction_is_blind(self, auction_id: int) -> bool:
        if auction_id not in self._blind:
            auction = self.db.get(Auction, auction_id)
            self._blind[auction_id] = bool(
                auction is not None
                and auction.org_id == self.viewer.org_id
                and engine.blind_to_buyer(auction))
        return self._blind[auction_id]

    def _buyer_addresses(self, auction_id: int) -> set[str]:
        """Every address on the buyer's own side of this auction.

        Anything else on an auction email went to a bidder. Working it out
        this way round means a bidder who was written to at an address nobody
        recorded against them is still covered.
        """
        if auction_id not in self._our_side:
            auction = self.db.get(Auction, auction_id)
            addresses = {(row.email or "").lower()
                         for row in self.db.query(User).filter(
                             User.org_id == self.viewer.org_id,
                             User.vendor_id.is_(None)).all()}
            if auction is not None:
                addresses.update(part.strip().lower()
                                 for part in (auction.cc_emails or "").replace(",", "\n").split("\n")
                                 if part.strip())
            self._our_side[auction_id] = addresses
        return self._our_side[auction_id]

    def sealed(self, message: EmailMessage) -> bool:
        if not message.auction_id or not self.viewer.is_buyer_side:
            return False
        if (message.event or "") in BROADCAST:
            return False
        if not self._auction_is_blind(message.auction_id):
            return False
        return (message.to_email or "").lower() not in self._buyer_addresses(message.auction_id)

    def label(self, message: EmailMessage) -> str:
        """What the Outbox may call the person this went to."""
        return "A bidder on this auction" if self.sealed(message) else (message.to_name or "")
