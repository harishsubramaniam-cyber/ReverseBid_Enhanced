# Comparing on the delivered price

This is what **ReverseBid_Enhanced** changes. Everything else in the platform
works as it did.

## The problem it solves

A reverse auction ranks on price. But the cheapest quote is not always the
cheapest purchase: one supplier is across town and one is three states away,
one charges for pallets and one does not, and they may sit in different tax
slabs. Ranking on the headline price picks the wrong supplier, confidently.

The earlier version could add delivery costs, but it asked the **buyer** to
record each supplier's freight **per unit**. That was wrong in two ways. The
buyer does not know what a supplier's freight is — the supplier does. And
"freight per pen" is a number nobody has: freight is quoted for the
consignment.

## A bid is the whole offer, sent in one go

Tick **Compare on the delivered price** when you create the auction. That is
the buyer's only decision.

From then on a bid is never a bare price. One form collects, together:

- **the price**,
- **what it costs to deliver** — Freight, Packaging and Other costs, with a
  box to say what "other" means, and
- **the taxes** — as many as apply, each a named rate: GST, cess, a state
  levy.

All of it goes in with one button. There is no way to leave the taxes for
later and no way to bid on the goods alone, because half an offer is not an
offer: the buyer cannot pay it and the board cannot rank it. **Taxes are
compulsory** on a delivered-price auction — a bid arrives without them is
turned away with a plain reason. A rate of nothing is a perfectly good
answer, but it has to be typed: `0` means "no tax on this", an empty box
means "I have not said", and those are different things.

Rates only, never amounts. The amount is worked out and shown beside the box
as it is typed, so the money can never disagree with the rate, and a running
**all-in** figure under the form shows exactly what the bid will come to
before it is sent — with a warning in the same place if it is already above
what the buyer will pay.

## The form follows how the auction will be handed out

The bidder is asked for what they are actually competing for, which depends on
the buyer's **How the business is handed out** setting:

**Item by item** — every item goes to whoever was best on it, so **each item
is bid for on its own**: its price, its own freight, packaging and other
costs, and its own taxes. The delivery costs belong to that one item, and the
button says so by name — *Place bid for Ball pen, blue* — so nobody can send a
bid thinking it covered the lot.

**All of it to one supplier** — one order, one delivery, one invoice, so **the
auction is bid for as one lot**. A single form lists every item with a price
box against each, the delivery costs are asked for **once, for the whole
consignment**, and the button sends the lot together. The ranking is on the
**grand total, all in**. Every item has to be priced: a basket with a hole in
it cannot win an auction that is awarded whole, so the form says which item is
missing rather than taking a bid that could never be accepted.

The two are not a display choice. On an item-by-item auction the freight boxes
sit inside each item's own form; on a whole-auction one there is a single set
of them at the top of the page, and no per-item bid button at all.

## The arithmetic, in full

For one bidder on one item:

```
delivered = bid × quantity + delivery costs for that item
tax       = delivered × (the rates they declared, added up)
all-in    = delivered + tax
```

Where "delivery costs for that item" comes from depends on the auction:

- **Item by item** — the freight, packaging and other costs quoted on that
  item's own bid. No sharing, no arithmetic: what they typed is what that
  item carries.
- **All to one supplier** — the one set of costs quoted for the consignment,
  shared across the items by value: `costs × (this item's value ÷ all items'
  value)`. The bidder never sees the split as a decision of theirs; it exists
  only so each line shows a sensible figure and the ceiling can be checked
  item by item.

Two points worth being explicit about, because each was a choice:

**An item's value, for that share, is the buyer's own ceiling** (quantity ×
starting price), not the current bid. The ceiling does not move while the
auction runs, so a bidder's share does not lurch about as prices fall, and
the price the screen offers is the price the engine accepts.

**Tax goes on last, on the delivered value** — the goods *and* the freight —
because that is what a tax is charged on. Applying it to the bid alone
understated every taxed bid by the tax on its own delivery.

## What the starting price means

The most the buyer will pay **all-in**, per unit. A bid whose all-in price is
above it is refused, and the bidder is shown the exact price to type instead.

Because the costs and the taxes arrive with the bid, they are weighed with it:
the refusal is worked out on the whole offer, not on the price alone. When a
bidder's own freight and tax come to more than the price to beat before they
have quoted a thing, the refusal says exactly that — it is the costs that
would have to change, not the price — rather than sending them off to try a
smaller number that could never have worked.

## Your bid record, and taking one back

Every submission is kept exactly as it was sent. **Your bids on this auction**
lists them newest first with each item's price, each tax by name and rate, the
delivery costs, the totals, and **the moment the bid went in**. Nothing is
recalculated — these are the figures that were on the screen when the button
was pressed, which is what a bidder needs when they are asked to stand behind
one of them.

Each is marked *Standing bid*, *Replaced by a later bid* or *Withdrawn*, so
which one is in force is never a question.

**A bidder can take back their most recent bid, and only that one.** The bid
underneath it — whatever they offered before — stands again, at the figures
they sent it with, and their rank goes back to what that bid earns. Earlier
bids cannot be picked out and removed: an offer that has already been beaten
or bettered is part of the record, and a bidder who could quietly unpick it
could walk their own price back up. If one of them really was a mistake, the
buyer can strike out any bid at all — that power stays with the buyer, and the
bidder is told when it is used.

## What each side sees

**The bidder** sees their bid, that item's delivery costs, each tax by name,
the all-in total, and their own full bid history — and nothing at all about
any other bidder's costs.

**The buyer** sees, for every bid: the headline price, the delivery, the tax
with its rates, and the all-in price the ranking used. On a whole-auction
auction they also see each bidder's grand total, and who has not yet priced
everything.

## Trying it

The sample data has one of each.

**RA-0004, "Lubricants and rope — delivered price, one supplier"** is bid for
as a whole. On the lubricants, the bidder with the *lower* headline price
loses on the all-in price, because their freight is more than twice as much:

| Bidder | Bid | Delivery quoted for the lot | Tax | All-in per unit |
| --- | --- | --- | --- | --- |
| Alpha Supplies | ₹300.00 | ₹6,900 | GST 18% | **₹361.38** |
| Bharat Traders | ₹306.00 | ₹14,000 | GST 18% | ₹376.06 |

and the whole-auction standing, which is what the award is decided on:

| | Alpha Supplies | Bharat Traders |
| --- | --- | --- |
| Everything, all in | **₹4,02,852.09** | ₹4,14,947.04 |

**RA-0005, "Bearings — delivered price, item by item"** is the other shape:
each item carries its own freight, and the same two bidders change places
between the two items because of it.

Sign in as `supplier1@example.com` / `demo1234` to see the bidder's side, and
as `buyer@example.com` / `demo1234` to see the board.

## Where the code is

`app/quotes.py` holds a submission — the whole-auction form, the standings on
the grand total, taking back the last bid and putting the one before it back,
and the bidder's history. `app/landed.py` holds the money: the per-item costs,
the sharing used on a whole-auction bid, and the rule that keeps a bidder's
stored prices in step when anything of theirs changes. The engine still ranks
on one number per bid, which is why the decrement rules, the ceiling check and
the award all went on working untouched.
`tests/test_enhanced.py` checks every figure on this page against arithmetic
done by hand.

---

# Splitting the auction, or giving it all to one supplier

The second thing this version adds. When you create an auction you now choose,
under **How the business is handed out**:

- **Item by item** — every item to whoever was best on it. Any number of
  winners. This is how the platform always behaved, and it is still the
  default.
- **All of it to one supplier** — one order, one delivery, one invoice.

Whichever you choose, **the award screen shows you both totals** when the
bidding closes, because that is the first moment the figures exist:

| | |
| --- | --- |
| **Item by item** | the sum of each item's own winning price |
| **All to one supplier** | the cheapest single supplier's price for everything |

and the difference between them, in money and as a percentage. Beneath that is
every bidder's price for the whole auction, with a **Give them the lot** button
against each one who can actually take it.

Splitting is never dearer — nothing stops each item going to whoever is
cheapest on it — so the question is always *what does one supplier cost me?*
Sometimes the answer is nothing at all, because one bidder was best on
everything; the screen says so plainly when that happens, rather than making
you compare two identical numbers.

**Only a bidder who priced every item can be given the whole auction.** A
basket with a hole in it is not an offer for the auction, however good the
prices in it are, and the table says so against those bidders instead of
quietly ranking them as cheapest. On an auction that was set to one supplier
from the start this can never arise — the bid form there will not send an
incomplete lot — but on an item-by-item auction, where bidders quote whatever
suits them, it often does, and the comparison has to be honest about it.

If you chose **all to one supplier**, an award that splits the items is refused
— naming the suppliers it was split between, and telling you that changing the
auction back to item-by-item is a perfectly good answer. The setting is a
decision, not a wall.

Afterwards, the **Award** tab of the finished auction keeps the comparison, so
anyone reviewing it later can see the road not taken and what it would have
cost. `engine.award_comparison()` does the work;
`tests/test_enhanced.py` section 14 checks it.

---

# Two views of the prices, before you award

On a delivered-price auction the award screen carries a **Show prices** switch:

- **All-in — delivered and taxed.** What you would actually pay. The default,
  and what the award is decided on.
- **Bid only — no freight, packaging or tax.** The quotes exactly as the
  suppliers wrote them.

Both are honest, and neither is the whole truth on its own: the first is what
gets paid, the second is what was negotiated and what a supplier will recognise
if you ring them about it. So the screen gives you both rather than deciding
for you which one you are allowed to see.

Switching the view changes everything on the screen together — the rank order
within each item, the line totals, the savings, and both totals in the
split-or-whole comparison, which is labelled **bid prices only** or **all-in
prices** so a screenshot can never be mistaken for the other one.

The two views often disagree about who is winning, and that is the point. A
supplier who quotes ₹100 with ₹40 a unit of freight beats one who quotes ₹120
with ₹10 — until the freight is counted, when the order reverses. On the
bid-only view the screen says so plainly, in a warning that names the all-in
total as well, so nobody reads ₹10,000 off the screen and forgets it is really
₹13,000. Each bid also shows the other figure beside it, with the extras it
carries.

An auction that is not compared on the delivered price has no extras to strip
out, so no switch is drawn and asking for the bare view changes nothing.
`tests/test_enhanced.py` section 15 covers all of it.
