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

## How it works now

Tick **Compare on the delivered price** when you create the auction. That is
the buyer's only decision. From then on:

**Each bidder gives three figures, once, for the whole auction** — Freight,
Packaging, and Other costs, with a box to say what "other" means. Not per
item, not per unit. They can change them at any time while the auction is
open; their prices are recalculated straight away.

**Each bidder declares the taxes on each item**, as many as apply, each as a
percentage with a name — GST, cess, a state levy. They type the rate; the
amount is worked out and shown beside it as they type. They never type an
amount, so the money can never disagree with the rate.

**The board ranks everyone on the all-in price**: the bid, plus that item's
share of their delivery costs, plus their tax.

## The arithmetic, in full

For one bidder on one item:

```
share of delivery = their whole-auction costs × (this item's value ÷ all items' value)
delivered         = bid × quantity + share of delivery
tax               = delivered × (the rates they declared, added up)
all-in            = delivered + tax
```

Three points worth being explicit about, because each was a choice:

**An item's value is the buyer's own ceiling** (quantity × starting price),
not the current bid. The ceiling does not move while the auction runs, so a
bidder's share does not lurch about as prices fall, and the price the screen
offers is the price the engine accepts.

**Every item takes a share, whether or not this bidder has priced it.** The
alternative — sharing only across items they had already priced — meant the
first bid carried the whole freight bill, so the same bidder could be refused
on a small item and accepted on a large one depending only on which they typed
first. Nobody could be told why. The cost is that a bidder who quotes part of
the auction carries only that part's share of their own costs, which is
defensible: a part load is a smaller delivery, and the overall standing already
says plainly when a basket is incomplete.

**Tax goes on last, on the delivered value** — the goods *and* the freight —
because that is what a tax is charged on. Applying it to the bid alone
understated every taxed bid by the tax on its own delivery.

## What the starting price means

The most the buyer will pay **all-in**, per unit. A bid whose all-in price is
above it is refused, and the bidder is shown the exact price to type instead.

Because the costs and the taxes are declared separately from the bid, they are
checked against the ceiling too: a tax added *after* a bid was accepted, that
would push it above the ceiling, is refused with the arithmetic spelled out.
Declare your costs and taxes first, then bid — the screen is laid out in that
order for exactly this reason.

## What each side sees

**The bidder** sees their bid, the item's share of their delivery costs, each
tax by name, and the all-in total — and nothing at all about any other bidder's
costs.

**The buyer** sees, for every bid: the headline price, the delivery share, the
tax with its rates, and the all-in price the ranking used. The invited-bidder
list says what each supplier has quoted for delivery, and flags anybody who has
not filled it in.

## Trying it

The sample data includes **RA-0004, "Lubricants and rope — delivered price,
one supplier"**. On the lubricants, the bidder with the *lower* headline price
loses on the all-in price, because their freight is twice as much:

| Bidder | Bid | Delivery quoted | Tax | All-in per unit |
| --- | --- | --- | --- | --- |
| Alpha Supplies | ₹300.00 | ₹6,900 | GST 18% | **₹363.05** |
| Bharat Traders | ₹306.00 | ₹14,000 | GST 18% | ₹379.44 |

It is also set to go to a **single supplier**, so its award screen prices that
decision: splitting it would save about **₹1,113** (0.28%) against the cheapest
single supplier. Whether that is worth two orders instead of one is the buyer's
call — which is the point of showing it.

Sign in as `supplier1@example.com` / `demo1234` to see the bidder's side, and
as `buyer@example.com` / `demo1234` to see the board.

## Where the code is

`app/landed.py` holds all of it — the sharing, the taxes, and the rule that
keeps a bidder's stored prices in step when anything of theirs changes. The
engine still ranks on one number per bid, which is why the decrement rules, the
ceiling check and the award all went on working untouched.
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
quietly ranking them as cheapest.

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
