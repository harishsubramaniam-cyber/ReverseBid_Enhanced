# Who sees what

One installation can hold any number of buying companies, and inside one
company a buyer and a bidder are shown very different things. This is the
whole list, in one place, so it can be checked rather than assumed.
`tests/test_access.py` tries to break every line of it.

## Between companies

A company is a wall, not a filter. Another company's buyer or supplier is
told an auction, a document, a supplier record, an item, an email or a report
**does not exist** — not that they may not see it, which would itself be
news. That holds whether they arrive by a link, by guessing a number in the
address bar, or by editing a form before sending it: a ticked bidder, an item,
a unit or a bidder id belonging to another company is refused, so nobody can
build their auction out of somebody else's records.

Signing up is open, and it always makes a brand-new company with you as its
first buyer — never a way into an existing one. The same email address may
hold an account with several companies; each is a separate login with its own
password, and neither can see the other's work.

## Inside one company

**Buyers and admins** see everything their company owns: every auction, every
bid with the real supplier name against it, every document, every
conversation, the audit trail, the reports and the outbox.

**A supplier** sees an auction only if they were invited to it, and only once
it has been published. On it they see the items, the buyer's documents, their
own bids — every one they have placed, with the prices, delivery costs and tax
rates exactly as they typed them and the moment each went in — their own
documents, and their own conversation with the buyer. They never see another
supplier's name,
email address, bid, delivery costs, documents or messages — not on the board,
not on the refreshing panel behind it, not on any tab, and not in the page
source. The audit trail is closed to them entirely: it names everybody.

A supplier can invite a colleague, and that invitation can only ever create
another supplier login tied to the same supplier record. There is no form
anywhere that promotes an account to the buyer's side.

## What the buyer chooses to publish

Two settings on the auction decide how much of the contest bidders see:

| Setting | On | Off |
| --- | --- | --- |
| **Bidders see their rank** | "You are at L2", the rank badge, the rank in the bid confirmation email | none of it, anywhere |
| **Bidders see the lowest price** | the current lowest, and "your bid must be ₹X or less" | neither, and nothing worked out from them |

Off means off. With the lowest price hidden, the board does not print the
price to beat, the largest price the bidder may type, the suggestion chip, or
the hidden limits the bid box carries for the browser — every one of those is
the standing bid in disguise. A bid that is too high is still refused, in
words that do not name the figure, and the outbid email says somebody has gone
below you without saying by how much. The rule the auction runs on does not
change; only what is published about it does.

## Signing in and out

A session lasts twelve hours. Signing out ends it **everywhere**, not just in
the browser where the button was pressed, so a cookie copied off a shared
computer stops working the moment its owner signs out. Switching an account
off has the same effect immediately.

Every form carries a token tying it to the page it was drawn on, so another
website cannot make your browser act on your behalf. A wrong password answers
exactly the same way whether or not the address is known here, so nobody can
use the sign-in page to find out who has an account.

## Uploads

A document keeps the name it arrived with only for showing and downloading;
on disk it is stored under a name of the app's own choosing, inside that
auction's own folder. A file called `../../../../etc/passwd` lands beside
every other attachment and nowhere else. Only a listed file type is accepted,
and only PDFs and images are ever handed back in a form a browser will render
— nothing uploaded here can run as a page.
