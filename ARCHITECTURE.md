# Architecture

blocktail is a read-only activity feed for a fixed set of Ethereum addresses.
It indexes into local SQLite and serves a server-rendered dashboard. It holds no
keys, signs nothing, and sends nothing on-chain.

```
wallets.yml ──▶ config ──▶ Indexer ──▶ SQLite ──▶ HTTP ──▶ HTML (HTMX) / JSON
                             ▲
                             │
                    ChainDataProvider
                             │
                    AlchemyProvider (Ethereum mainnet)
```

| Layer | Module | Responsibility |
| --- | --- | --- |
| Chain knowledge | `app/chains/ethereum/chain.py` | Address validation, EIP-55, explorer URLs, truncation |
| Provider | `app/chains/ethereum/alchemy.py` | All upstream HTTP; normalizes to `Transfer` |
| Valuation | `app/prices.py` | `PriceSource` seam; Alchemy Prices implementation |
| Indexing | `app/sync.py` | Watermark, re-scan window, dedup, pruning, balances |
| Persistence | `app/db.py`, `app/migrations/` | Schema, migrations, feed queries |
| HTTP | `app/web/routes.py` | HTML fragments and JSON API |
| Presentation | `app/web/format.py`, `templates/` | View models, exact decimal rendering |

Rendering is server-side throughout; HTMX swaps fragments and the only client
state is a collapsed/expanded flag in `localStorage`. Every list, form and sheet
is a template, so nothing is assembled twice in two languages.

The page scrolls as a page. An inner scroll region was tried and removed: it
made a static document with a small pane moving inside it, which reads as an
embedded terminal. The top bar, filter bar and table head are sticky instead,
at offsets measured at runtime rather than hard-coded.

The block number is not a column. It is scanned past every time and pushed the
transaction hash to the far right; it lives in the row detail and in the hash's
tooltip. A trailing spacer column keeps rows from being stretched to the
container edge — a few hundred pixels of air on the right is fine.

Above 860px the content sits in a centred container capped at 1440px. The limit
is not about taste in whitespace: past that width the parts of one row — wallet,
amount, counterparty, hash — drift far enough apart that scanning a log becomes
head-turning. The viewport keeps its own background, so this is a width bound
rather than a card. Navigator, feed and inspector share one outline, because a
sidebar on the left with unbounded text to the right reads as unfinished.

Which columns a row needs depends on the scope. With one wallet selected the
wallet column is its name repeated on every line, so it goes; with several it
is the only thing distinguishing them, so it stays. That class is applied by
the server *and* re-applied by the client, because htmx swaps the feed without
re-rendering its container — a stale `one-wallet` hides a cell that spans two
columns on transfers between your own wallets, and takes the row with it.

Below 860px the layout is not the same one compressed — it is a different
information architecture. Desktop is a table; the phone is an event log. The
scope moves into the top bar, which is the only thing that never scrolls away
(a sticky child stops sticking the moment its parent scrolls past, so the
summary panel cannot hold it). The portfolio becomes one figure and a line
about it. Holdings are capped at two and never include the native balance,
which the line above already states, so the header's height stops depending on
how many tokens have been sent to the address — not something the owner
controls. A row is two lines using the full width rather than desktop columns
folded: who, then what moved. Everything an event says starts on one vertical.

A number is never cut. Below a billion it is shown exactly; from a billion up
a row shows it compactly — "12.3B", "4.56T", "1×10³⁰", three significant
digits from exact Decimal arithmetic, never stored or summed — and renders the
exact text beside it for a screen reader and the tooltip. On a touch screen
the exact value is one tap away: the inspector states every amount of a
transaction in full, wrapping a long one rather than clipping it, and the
holdings sheet states every holding in full. The same rule holds in the feed,
the folded rows, the summary strip and the sheet; the JSON API always carries
the exact text. What gives way when space runs out is a name or a ticker, never
a figure.

## The transaction inspector

A feed is a list of transfers, which is the right unit for scanning: a swap
really did move four different things. But those four share one cause, and
showing them as unrelated rows is how a feed misleads.

So a transaction is one entry, led by the transfer carrying the most value with
the rest folded behind it — still separate rows, because a token added to an
unrelated token is not a sum, and neither are two amounts of the same asset
going to different places. Which transfer leads is decided by value; with
nothing priced, on-chain order decides, since guessing between two unpriced
amounts of different assets is worse than not guessing.

The cause itself is a place you can go: `GET /tx/{activity_id}` renders every
transfer of that transaction, docked beside the feed on a wide screen and to
the bottom edge on a narrow one — the same component either way, and
deliberately not a modal, since the point of an inspector is that you can keep
reading what it inspects.

Fee and gas are read on demand (`eth_getTransactionReceipt` plus
`eth_getTransactionByHash`), never indexed. It is the one call the app makes
because a person asked rather than on a timer, and it decorates a panel that is
already useful without it — so a provider that is down, rate-limited or has
pruned the receipt costs the reader a line, not the transaction they opened. A
fee the chain will not state is absent, not zero: a fee rendered as 0 is a claim
about what something cost, and the wrong one.

Grouping reorders rows within a transaction, so the pagination cursor comes
from the query's order rather than the display order. Paging from a moved row
would skip or repeat whatever sat between it and the true end of the page.

---

## Provider choice: Alchemy

**Chosen:** Alchemy's JSON-RPC, principally `alchemy_getAssetTransfers`.

The deciding factor was event identity. The spec's hardest requirement is that
one transaction can contain several relevant transfers and that re-syncing must
never duplicate them. Alchemy returns a `uniqueId` per transfer, formed as:

```
0x…hash:external          a plain value-bearing transaction
0x…hash:log:41            an ERC-20 Transfer event, by log index
0x…hash:internal:0_1      a value-moving trace, by trace address
```

That is exactly the identity the feed needs, supplied by the upstream rather
than synthesized locally.

| Requirement | Alchemy | Etherscan API V2 |
| --- | --- | --- |
| ETH + internal + ERC-20 transfers | one call, one shape | three endpoints, three shapes |
| Stable per-transfer identity | `uniqueId`, including trace address | internal-by-address returns no trace index |
| All ERC-20 balances for an address | `alchemy_getTokenBalances` | `addresstokenbalance` is a paid endpoint |
| Block timestamps with transfers | `withMetadata: true` | included |
| Incremental scan | `fromBlock`/`toBlock` | block range supported |
| Deep paging | `pageKey` cursor | page/offset, with a result ceiling |

Etherscan's inability to distinguish two internal transfers to the same address
within one transaction is the disqualifier: it makes idempotent indexing
guesswork. Its token-balance endpoint being paid rules out the dashboard's
ERC-20 balances on a free plan.

### Limitations of this choice

These are properties of the upstream, not bugs, and they shape what the UI can
honestly claim:

1. **Failed transactions are not shown.** The Transfers API reports value that
   actually moved. A reverted transaction transferred nothing, so it never
   appears. There is no "failed" state in the feed.
2. **Gas is not activity.** Fees paid are not transfers and are not indexed, so
   an ETH balance will drift from the sum of the feed by the gas spent.
3. **`fromAddress` and `toAddress` are ANDed**, so each wallet costs two calls
   per cycle — one per direction. This sets the compute-unit budget in
   [README.md](README.md#provider-usage-and-the-free-tier).
4. **Zero-value transfers are excluded** (`excludeZeroValue: true`), which drops
   a common class of spam but also hides genuine zero-value transfers.
5. **NFTs are out of scope**; `erc721`/`erc1155` categories are not requested.
6. **`value` is a float and is never used.** Amounts come from
   `rawContract.value` (hex) plus `decimals`, as exact integers.
7. **Token metadata can be missing.** A token in the vendored list takes its
   decimals from the list, with no lookup. For any other token a failed or
   empty lookup leaves the decimals *unknown* — never 0, which would read a raw
   amount as whole tokens. Such an amount is shown as raw units, marked `raw`,
   and is never valued. The failure is retried after a few minutes rather than
   remembered for the life of the process, and a value learned later replaces
   the unknown one in the database. A stub (a truncated contract address)
   stands in for a missing symbol.
8. **Vendor coupling is confined to one file.** `AlchemyProvider` is the only
   module that knows Alchemy exists; a second implementation of
   `ChainDataProvider` would need no changes elsewhere.

---

## Schema

Migrations are plain SQL in `app/migrations/`, applied in filename order and
recorded in `schema_migrations`. Each runs inside a transaction together with
its version row, so the SQLite file can be carried across upgrades without ever
being deleted.

```
accounts    id, chain_id, address, name, position, active, created_at,
            is_owned, source, backfilled
            UNIQUE (chain_id, address)

assets      id, chain_id, contract_address, symbol, decimals
            UNIQUE (chain_id, contract_address)     -- '' means the native asset

activities  id, chain_id, event_key, tx_hash, block_number, block_timestamp,
            kind, asset_id, amount_raw, from_address, to_address,
            from_account_id, to_account_id
            UNIQUE (chain_id, event_key)

balances    account_id, asset_id, amount_raw, updated_at   PK (account_id, asset_id)

asset_prices asset_id, currency, price, updated_at          PK (asset_id)

labels      chain_id, address, label                       PK (chain_id, address)

sync_state  chain_id, last_synced_block, head_block, last_success_at,
            last_attempt_at, last_error, backfill_done
```

Notes:

* `amount_raw` is the raw integer as a decimal **string**; combined with
  `assets.decimals` it renders exactly. No amount ever passes through a float.
* Column names are chain-neutral (`chain_id`, `address`, `balance`) rather than
  `ethereum_address` or `eth_balance`, so a second chain is an INSERT rather
  than a schema migration across every table.
* `accounts` and `labels` mirror `wallets.yml` for everything the file declares
  (`source = 'config'`), re-applied on every start. Accounts added from the page
  (`source = 'ui'`) exist only here, so a config reload cannot remove them.
* `is_owned` separates "mine" from "watched". Both are indexed identically; only
  owned accounts are summed into a portfolio.
* `backfilled` is per account, so one added after the first sync reaches back
  through the backfill window instead of starting at the current head.

### Indexes

```sql
idx_activities_feed       (block_timestamp DESC, id DESC)   -- default ordering, keyset paging
idx_activities_from_acct  (from_account_id, block_timestamp DESC, id DESC)
idx_activities_to_acct    (to_account_id,   block_timestamp DESC, id DESC)
idx_activities_asset      (asset_id,        block_timestamp DESC, id DESC)
idx_activities_tx         (tx_hash)
idx_activities_block      (chain_id, block_number)          -- the re-scan window
```

The feed pages by keyset, not `OFFSET`:

```sql
WHERE (block_timestamp, id) < (:cursor_ts, :cursor_id)
ORDER BY block_timestamp DESC, id DESC
LIMIT 51
```

so page 200 costs the same as page 1, and rows arriving mid-scroll cannot cause
a duplicate or a skip.

---

## Event identity and deduplication

**One row per on-chain value movement**, keyed by `(chain_id, event_key)`.

```
UNIQUE (chain_id, event_key)   -- event_key is the provider's uniqueId
```

Storing is `INSERT … ON CONFLICT DO UPDATE`, so re-scanning a block range is a
no-op. Three consequences fall out of this design:

**Several transfers in one transaction stay distinct.** Three ERC-20 transfers
in one transaction have distinct log indices, so three rows, all sharing a
`tx_hash`.

**A transfer between two monitored wallets is one activity, not two.** Scanning
wallet A's outgoing transfers and wallet B's incoming transfers returns the same
transfer twice with the same `uniqueId`. The account columns are derived from
the *addresses* against the full wallet set, never from which scan produced the
row, so both writes produce a byte-identical row that collapses on the unique
key. When both `from_account_id` and `to_account_id` are set, the UI renders it
as `Main → Cold` with no counterparty column.

**Restarts are free.** The watermark lives in `sync_state`, so a restart resumes
from `last_synced_block - reorg_depth + 1`; anything re-read is already stored.

### Scope

The feed shows everything monitored by default, one account when a row is
picked, or the watched accounts when the `WATCHING` separator is clicked —
clicking it again returns to everything.

Owned accounts have no heading of their own: the summary panel directly above
already names them, and a second identical heading read as noise. The trade-off
is that "only my accounts" is no longer directly selectable; it differs from the
default only by watched-account activity, so the loss is small, but it is a
loss.

The portfolio panel does not follow the feed's scope. It totals owned accounts
whatever the feed is showing, and only narrows when a single account is picked.

### Point of view

A transfer between two accounts blocktail knows is one row, and how it reads
depends on the scope being viewed. The rule is one line: **collapse to `A → B`
when both ends are in scope, otherwise render IN/OUT relative to the end that
is.** That single rule produces every case — a pair inside *All activity*, an
ordinary outgoing transfer once you narrow to one of the two wallets, and the
mirror image from the other side — without any of them being special-cased.

### Reorgs

Each wallet is re-read from `REORG_DEPTH` blocks (default 64, ~13 minutes)
below where *that wallet* was last synced, up to the head. That range is the
wallet's reorg window, and within it the fresh scan is authoritative: a stored
event the chain no longer reports is deleted. After a downtime the window
simply reaches further — everything since the last success was re-read, so a
reorg that happened while nobody was looking is caught.

Windows are per wallet. An event may be deleted when it is absent from every
scan of the cycle *and* one of its active ends had the event's block inside its
own window; a shared event that either side still reports stays. A wallet that
starts elsewhere — new, re-added, reaching back for older history — changes no
other wallet's window. A wallet read for the first time has no window at all,
and neither does the part of a scan below a wallet's window: those blocks are
settled history, and a provider that stops serving old history must not be
able to erase settled records. The candidates come from one indexed range
query with the per-wallet test in SQL (`Database.prunable_event_keys`).

Pruning runs **only after the transfers of every wallet were fetched.** If any
wallet's fetch fails, the others are cancelled, the cycle records the error,
leaves the watermark where it was, and deletes nothing.

The scan covers active wallets only, so only events with an active end are
candidates. An event of a removed wallet is not in the scan because nobody
looked, not because the chain dropped it, and it is kept.

### Removed wallets

Removing a wallet — from the page, or from `wallets.yml` — archives the account
and keeps every event it was part of. Nothing is deleted. What changes is one
invariant: **the account columns of an activity only ever name active
accounts.** `relink_accounts()` maintains it, and every write that changes an
account runs it — adding, removing, applying the config file — while
`store_transfers()` links new rows against the accounts active at that moment.

Everything on the read side follows from that, without a second rule:

* An event shows when at least one end is active. One between two removed
  wallets is in the table and nowhere on the page, nor in the inspector.
* An event with one active end reads from that end. `Main → Temp` becomes an
  ordinary OUT from Main once Temp is removed, with Temp's address — not its
  old name — as the counterparty; a pair inside the scope unfolds the same way.
* IN/OUT, the scopes, the asset filter and "sent by one of your wallets" only
  ever see active accounts.
* Re-adding the address, from the page or the file and under any name,
  attributes its history again at once, and indexing resumes from the account's
  own watermark instead of backfilling.

A restart, where nothing changed, makes `relink_accounts()` touch no rows: it
releases what inactive accounts hold and attributes what active ones lack,
rather than rewriting every row.

### Balances

A balance cannot change without a transfer, and the cycle has just read every
transfer — so only the wallets that moved are re-read. Asking the provider about
the other four every thirty seconds bought nothing but rate-limit headroom
spent, and rate limits are what a free plan actually runs out of. A quiet cycle
now makes no balance call at all.

The argument is sound but not airtight, so a full sweep every
`BALANCE_REFRESH_SECONDS` remains the safety net.

Balances are read **after** the transfers are stored and the watermark has
moved, and they fail on their own. A wallet whose read fails keeps its last
known balances — never zeros — and is read again next cycle whether it moved or
not; the other wallets are stored as usual. The failure is recorded apart from
indexing (`balances_error` in `sync_state`, `balances` in `/api/status`) and
shown in amber beside a status that stays green: the index is current, the
balances are not. Before, one balance error failed the whole cycle and sent
the next one back over the same blocks. This is also why the provider
retries treat HTTP 403 as throttling rather than as a permissions problem: it
is what Alchemy answers a burst with, and reading it literally sends you looking
for a key that is fine.

### Reaching further back

The backfill window is a setting, not a fact about the chain, and raising it
used to do nothing because a watermark only ever moved forward. Accounts now
carry a floor as well as a ceiling — `indexed_from_block` alongside
`synced_to_block` — so the feed can be asked to go further back than it has,
and the end of the feed says where indexing stops rather than stopping without
explanation.

"Load N more days" is a request, not a fetch. The page records it and wakes
the indexer, and gets an answer at once; the background indexer does the work
in its next cycle, which it starts straight away. The end of the feed shows the
request as queued, then running, and polls until it is completed — at which
point the rows it uncovered take its place — or failed, with the reason. The
rule for repeated requests lives in one place (`Database.request_history`):
while one is queued or running, asking again changes nothing, so ten clicks
are one request; after a failure, asking again retries the same block; only
after it completed does a request reach one window deeper. There is no limit
on how deep.

The block asked for is stored as `requested_from_block`, a floor every cycle
honours from then on, and the request's state is stored beside it. A request
that was running when the process stopped is queued again at startup. Closing
the page changes nothing.

### What the status bar claims

The status bar is a fragment the server renders and the page polls every five
seconds. It shows four different things, and keeps them apart:

* **The sync state**, decided by the server: synced, lagging, stale, failed
  (the provider's error, in red), or starting.
* **Balances**, also the server's: when indexing is current but a balance
  read failed, the dot turns amber and a line says the balances are the last
  known ones — not "fully healthy", not an indexing error.
* **Older history**, while a request for it is queued or running, or after
  it failed.
* **The connection**, which only the browser can know. When a poll gets no
  answer, or a 5xx, the page marks the connection lost: the dot goes hollow,
  the bar dims, and a short line says "Connection lost · last reply 40s ago".
  The data on the page stays as it was. The first poll that is answered again
  clears it; nothing reloads.

The age ("12s ago") is counted in the browser every second, from the time the
server says the last sync succeeded, corrected by the server's clock — so it
keeps growing while the server is unreachable, and an old status can never
look fresh. State changes (connection lost or restored, a change of sync
state) are announced once, in a live region outside the polled fragment; the
ticking age is not announced. One listener and one timer watch the fragment,
wired once on the page rather than on the fragment that is replaced every
poll, and the feed's own refresh pauses while the connection is lost.

### One cycle at a time

A cycle is single-flight. `Indexer.run_once()` starts one, or joins the one
already running and returns its result — never a second cycle beside it, and
never a queue of them. Only the scheduler loop starts cycles; the web layer
asks for one with `wake()`, an event that any number of requests set once, so
a burst of them costs one extra cycle at most. Nothing is held while the loop
waits for its interval or a wake. A cycle that raises leaves nothing locked:
the next call starts a fresh one. On shutdown the loop and the cycle in flight
are cancelled, and the app waits for the cycle to unwind before closing the
provider and the database.

---

## Valuation

Prices are not on-chain data, so they sit behind their own protocol
(`PriceSource`) rather than being added to `ChainDataProvider`. One
implementation exists, over the Alchemy Prices API, which reuses the same key.

Three rules keep valuation honest:

* **A missing price is unknown, never zero.** An asset the source cannot quote
  is excluded from the total and counted in "N assets unpriced" instead of
  quietly dragging the total down.
* **Valuation never blocks indexing.** A pricing outage is logged and the last
  known prices are kept; the sync cycle still succeeds, because on-chain
  activity is the product and valuation is a convenience.
* **Age is always visible.** `asset_prices.updated_at` drives a "prices — 29s
  ago" reading that turns amber once stale, so an old number never passes for a
  current one.

Prices refresh on their own cadence (`PRICE_REFRESH_SECONDS`, default 300)
rather than once per sync, and only assets some account actually holds are
quoted.

Two guards exist because live mainnet data demanded them, not by anticipation:

* **Lookups are bounded.** Every address tested — including Vitalik's and the
  Ethereum Foundation's — holds more than 200 ERC-20s, nearly all airdropped.
  Each needs a metadata call, so they are fetched concurrently and capped
  (`MAX_TOKEN_LOOKUPS`). Resolving them one at a time never finished.
* **One asset cannot swallow the total.** A scam token with a real DEX quote and
  a supply of 10^17 made the first live portfolio read $4.9 quadrillion. An
  asset that would be more than `VALUE_MAX_SHARE_PERCENT` of the total on its own
  is excluded from it and named in the panel, rather than silently believed or
  silently dropped.

  Only assets nothing vouches for are candidates. The first version of this
  guard excluded a wallet's USDC because it was 99% of that wallet — which is
  what a stablecoin wallet looks like, and the cure was worse than the disease.
  A token that is known, vouched for in the config, or has been sent by one of
  your own wallets is never called implausible. Its quote is not printed
  anywhere — not in the holdings, not on a feed row, not in the inspector — and
  neither is the quote of a forged ticker: printing either would hand back the
  number the total refused.

* **One valuation, judged once per group.** Which assets count — hidden as
  dust or as unverified, excluded as implausible — is decided once over all of
  my wallets (and separately over the watched ones), then applied to every
  view: the panel, one wallet, the sidebar rows. So the wallets add up to the
  total above them, and one wallet never carries two different totals. What
  is hidden or excluded is never summed; revealing hidden tokens changes what
  is listed, not the total.

Where valuation stops is deliberate: there is no cost basis, no profit and loss,
no 24-hour change and no price history. Each of those needs a methodology —
which lot, which timestamp, which venue — that this tool does not have and
should not guess at.

---

## What is worth showing

A monitored address receives things nobody asked for. Live mainnet data on two
ordinary wallets produced 382 tokens and 572 transfers that the owner has no
relationship with. None of it is deleted — it is indexed and one click away —
but none of it is shown by default, and the app says how much it is holding
back and why.

Three different judgements, kept apart because conflating them made the
interface lie:

* **Dust** is small. A transfer worth less than `DUST_TRANSFER_USD_CENTS` is
  hidden. Small is not the same as fake: `0.000346 ETH` at 87 cents is an
  ordinary payment and stays.
* **Unverified** is unknown. A token that is unpriced, unknown to this chain,
  never vouched for in the config and never sent by one of your own wallets has
  nothing behind it but a `Transfer` log anyone can emit.
* **Impostor** is a name chosen to deceive, and is stronger than either — it
  overrides "you sent it", because an ERC-20 contract can emit a transfer with
  any `from` it likes, including yours.

The counts are reported separately (`N dust · M unverified`) because "small"
and "unknown" are different accusations. The two are combined only in the one
word the affordance needs — *noisy* — and only on a phone, where the breakdown
crowds the number it explains.

Detecting an impostor by listing lookalike characters is a race that cannot be
won: live data turned up Cyrillic, Greek, Armenian, Lisu, Canadian syllabics
and mathematical symbols, all rendering as plain Latin. So the test is
inverted. A ticker on this chain is ASCII, short and unspaced. One that is not
— from a contract nobody knows — is trying to look like something. That catches
`ꓴꓢꓓ⊤` and `U឵S឵DΤ`, and also `Tether USDT`, which is entirely Latin and still
an impersonation. `YRISE` and `Duhash.games` are worthless and honest about it;
they stay unverified rather than being called forgeries.

### Two lists that pull in opposite directions

"Which tokens are real" and "which tickers are worth forging" were one list of
thirty addresses answering both questions. They want opposite things, and the
file now keeps them apart.

**The allowlist wants to be wide.** It exists so a real holding is never called
dust because its price lookup failed or its balance is small, and a token
missing from it costs the owner a holding they have to go looking for. It comes
from a vendored token list (`app/chains/ethereum/tokenlist.json`, following the
[token-list standard](https://tokenlists.org)) unioned with a curated floor —
408 tokens, from 403 listed plus 30 chosen by hand.

A union, not a replacement, because upstream lists are not supersets: Uniswap's
default carries neither stETH nor wstETH, and losing Lido staked ETH would be a
regression dressed as an upgrade. A test pins the five that only the floor has.

**The forgery-target set wants to stay narrow**, and is hand-picked: twenty-one
tickers that are money, or are about to be spent as though they were. An
impostor is *excluded* — the harshest thing this app does to an asset — and
hundreds of real tokens share a ticker with some other real token. Deriving
these from four hundred listed ones would brand honest projects as frauds by
coincidence. Growing the allowlist is safe; growing this set is not, and a test
asserts one is not derived from the other.

### Why the list is vendored and not fetched

It decides which tokens to believe, which makes it a trust input, and a trust
input downloaded at startup is one nobody has read. Vendored, every change
arrives as a diff a person reviews before it ships, the app behaves identically
with no network at all, and a list server having a bad day cannot silently
reclassify a wallet's holdings as unverified.

`scripts/refresh-token-list.py` updates it — by hand, never by the app. It
refuses to write a list with no mainnet tokens in it, and records which sources
at which versions produced the file, because a trust input nobody can audit is
a trust input nobody should use.

### The chain's own currency is not a forgery of itself

Adding `ETH` to the forgery targets classified the native asset as an
impersonation: it has no contract, so it matched nothing in the allowlist, and
every ETH transfer vanished from the feed. Thirty-two tests said so at once.
`is_native()` guards it explicitly — the asset with no contract is the thing
being impersonated, never the impersonator.

## Names for well-known addresses

About fifty addresses — routers, bridges, lending pools, exchange wallets —
have built-in names, because a feed reading `TW1 → 0x4b74…d72A` makes the reader
do the lookup the app exists to save them.

A wrong name is worse than none: `0x4b74…d72A` is honest about what it does not
tell you, and a confident label on the wrong address is a claim the reader has
no way to doubt. Two checks enforce that. Every entry is written in EIP-55 form
and a test verifies it, so an altered character fails the checksum with
overwhelming probability. Every entry also declares whether it is a contract or
an externally owned account, which `tests/test_known_addresses_live.py` checks
against the chain — a contract must have code, an exchange wallet must not and
must have sent a great many transactions. That check rejected an entry during
development; it was removed rather than guessed at again.

These are a fallback. A monitored wallet's name, then a label in `wallets.yml`,
then this list, then the shortened address — the reader's word about their own
counterparties beats ours.

---

## HTTP security

The web layer has four writes (add, edit and remove a wallet; ask for older
history) and nothing else that changes state. One middleware decides where a
request may come from — a write only from this page, by `Sec-Fetch-Site` or,
without it, `HX-Request` — and the outermost one puts the same headers on
every answer: a CSP with no inline script, no inline style and no eval, no
framing, `no-store`. The page's script is two static files, `boot.js` and
`app.js`; controls name an action (`data-action`) instead of carrying an
`onclick`. No session, cookie or token exists. See
[docs/security.md](docs/security.md).

## Chain neutrality

Only Ethereum is implemented, and no plugin framework exists. What is deliberate
is the placement of Ethereum-specific knowledge:

* `Chain` and `ChainDataProvider` (`app/chains/__init__.py`) are small protocols;
  `get_chain()` is a dict lookup, not a registry framework.
* `0x`-prefixed addresses, EIP-55 checksums, Etherscan URLs and hash truncation
  exist only under `app/chains/ethereum/`. Nothing above that layer assumes an
  address starts with `0x` — configuration validates through
  `chain.normalize_address()`, which rejects a Bitcoin address as readily as a
  typo.
* Persistence, the JSON API and the view models speak `chain`, `account`,
  `asset`, `activity`, `counterparty`.
* `Chain.known_label()` is part of the protocol, not a lookup the view layer
  does for itself: an address means nothing without the chain it is on, and the
  same twenty bytes name something different elsewhere.
* `wallets.yml` accepts a per-wallet `chain:` key; `ethereum` is the only
  accepted value and the error message lists what is supported.

The UI does assume Ethereum — the top bar names the chain and every link goes to
Etherscan — which is exactly where the spec permits the assumption.

One instance indexes one chain. `build_context()` rejects a config naming more
than one, with an explicit message rather than silent misbehaviour.
