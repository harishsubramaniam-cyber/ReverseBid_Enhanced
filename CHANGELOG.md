# What changed, and when

The `VERSION` file holds the one-line stamp the running build reports on the
Outbox page and at `/healthz`. This is the longer story behind each stamp.

## 2026-09-13 — a bid is the whole offer; the buyer can be kept blind

**Bidding was reworked so that a bid is everything the buyer would pay**, sent
in one go, in the shape of the thing being awarded:

* price + freight/packaging/other + taxes, submitted as one bid. On a
  delivered-price auction taxes are compulsory — 0% is allowed, but it must be
  typed, because a blank box is not an answer.
* **Awarded to one supplier** → the whole auction is bid for as one lot, the
  delivery costs are quoted once for the consignment, and the ranking is on the
  grand total. No partial baskets.
* **Awarded item by item** → each item is bid for on its own, with its own costs
  and taxes, and the button names the item.
* A bidder can take back **only their most recent bid**; the one before it
  stands again. The buyer keeps the power to strike out any bid.
* **Your bids on this auction**: every submission kept verbatim, with the prices
  and tax rates as typed and the exact time each went in.

**Hide bidder names from Buyer.** The old "hide bidder names from each other"
setting now does something worth having: it keeps the names from the *buyer*
until the auction is awarded, so the winner is picked on the figures alone. It
covers the board, the award screen, documents, messages, the audit trail, the
reports and their downloads, the buyer's email alerts, and the Outbox — where an
email to a bidder on a blind auction is sealed. Awarding lifts it everywhere at
once. `docs/WHO-SEES-WHAT.md` has the whole list.

**Fixed: the live board wiped what you were typing.** The board redraws itself
every few seconds and only kept boxes that had an `id` — which the tax name, the
tax rate and the delivery-cost boxes do not. Anything typed into them was
cleared mid-word, and a tax row added with "Add another tax" disappeared. The
refresh now puts back everything the person changed by hand, the rows they
added, and the caret. The tax name box also offers the usual names (GST, IGST,
CGST, SGST, cess and the rest) and is wider.

## 2026-09-12 — the deep check

Every part of the platform gone through in six rounds — the bidding rules, the
savings figures, who can see what, the emails, the forms, and what happens when
the server restarts. Seven real faults found and fixed:

* signing out now ends the session on every computer, not just the one it was
  pressed on;
* "hide the lowest price" and "hide the rank" are genuinely hidden — on the
  screen, in the page source, and in the emails;
* an award price with an extra digit in it is refused instead of booked;
* editing an auction no longer fails after a bidder has declared their taxes;
* two schedulers can no longer close the same auction twice;
* supplier-chosen text can no longer inject HTML into an email;
* a buyer from another organisation can no longer withdraw a stranger's bid.

`docs/THE-DEEP-CHECK.md` has the detail.
