# The deep check

Six rounds of deliberately trying to break ReverseBid, one part of it at a
time. Every check written here is kept, so none of these faults can come back
quietly: `python tests/test_<name>.py` runs any suite on a throwaway database.

The rule throughout was that a check is only worth having if it could fail.
Figures were worked out by hand and compared with the screen, not read off the
screen and compared with itself.

---

## Round 1 — the bidding engine
`tests/test_bidding_rules.py`

The part that decides money. The centrepiece is a randomised sweep: for a few
hundred differently shaped auctions it takes the price the *screen* offers the
bidder and proves the engine accepts exactly that, and refuses a paisa outside
it. A mismatch there is the worst fault this app can have — the platform
inviting a bid and then refusing it, or accepting one that breaks the buyer's
own rule.

Also: decrements as money and as percentages, the ceiling, auto-extension,
withdrawal and re-ranking, two bids in the same instant, and the delivered
price arithmetic.

## Round 2 — awarding, savings and reports
`tests/test_award_money.py`

Every money figure on every screen, against arithmetic done by hand: budgets,
awards at a negotiated price above or below the bid, part-awarded auctions,
re-awarding, items with no ceiling, auctions nobody bid on, delivered-price
savings, the dashboard tile, the spreadsheet and both PDFs, the date filters,
cancellation, and nine ways of abusing the award screen.

**Found:** the award screen accepted any price at all, so a slipped finger
could book an order at ten million crore, email the supplier that figure and
carry it into the month's spend. It now refuses an impossible price in the
same words the bidding side uses.

## Round 3 — who can see what
`tests/test_access.py`, written up in `docs/WHO-SEES-WHAT.md`

Two companies on one installation, with real secrets in one of them, and every
attempt to reach them from outside: signed out, the other company's buyer, the
other company's supplier, a supplier here who was not invited, forged forms,
tampered ids, sign-up, invitations, uploads, and what one bidder can learn
about another.

**Found, two:**

- Signing out only deleted the cookie in that browser. A copy taken off a
  shared computer went on working for another twelve hours. Sessions now carry
  a number that goes up when somebody signs out, so signing out ends the
  session everywhere.
- "Bidders see the lowest price" and "bidders see their rank", switched off,
  were undone by everything around them — the bid box printed the price to
  beat and the exact maximum allowed, the page source carried it for the
  browser, the refusal message quoted it, and the emails gave both. Off now
  means off; the bid is still refused, in words that name no price.

## Round 4 — emails and invitations
`tests/test_notifications.py`

An auction run from invitation to award with three deliberately awkward
suppliers — one with a login and a second contact address, one with no login
at all, one invited to nothing — reading the outbox back after every step:
who was written to, what the letter said, and where its buttons led. Including
the whole journey from an emailed invitation to a placed bid.

**Found:** email paragraphs are written in our own markup and rendered as
HTML, but they were built around words other people type — a supplier's name,
chosen by that supplier, and the text of every message they send. A name of
`<a href=...>` put a working link of a stranger's choosing inside an email
from this platform, which is how a convincing phishing message gets sent with
our letterhead on it. Everything a person typed is now escaped, so it can only
ever be text. A subject line is a header, so titles are flattened to one line
before they are stored.

## Round 5 — the forms, the lists and the documents
`tests/test_forms.py`

Everything people type in, typed wrongly: twenty-five bad auction forms, the
supplier and item lists and their duplicates, archiving a supplier mid-auction,
the document rules (kind, size, how many, who may remove one and when), the
clock, and a dozen addresses typed into the bar by hand. Then the odd but
honest entries — three and a half units, ₹49.99, no ceiling, an emoji, Hindi —
which must survive exactly as typed.

**Found:** editing a delivered-price auction after a bidder had declared their
taxes deleted the items out from under those tax rows; the database refused
and the buyer got an error page with the edit lost. An edit now keeps the row
that already carries an item and updates it in place, so a change of quantity
leaves the bidder's taxes where they are.

## Round 6 — upgrades, restarts and two people at once
`tests/test_upgrades.py`

The things that go wrong when nobody is looking. A new version started over a
database built to look old — columns missing, and one from before the app had
separate companies at all — read back afterwards to see what the upgrade did
to it. A server that was asleep while an auction opened and closed. Four
clocks ticking at once. Three bidders pressing at the same instant. Two buyers
awarding at the same instant. A bid landing in the closing seconds as the
clock comes round. A copy of the database taken while the app is running, and
started up on.

**Found:** with more than one clock running — several web workers, or a loop
plus a cron job — the same auction could be closed twice, so bidders got the
closing notice twice and the audit trail claimed two closes. Each step of the
clock is now claimed in the database before it is taken, so whoever gets there
first does it and everybody else stops. If sending the alert then fails, the
claim is handed back, so an alert is never lost either.

---

## What this does and does not mean

Every fault above was real, and is fixed and kept fixed. What the checks
cover is written down check by check, so what they do *not* cover is visible
too: this is a thorough test of the app's own behaviour, not a security audit
by a third party, and not a load test. The parts most worth trusting are the
ones where the figures were worked out by hand — the bidding engine, the
savings, and the delivered price.
