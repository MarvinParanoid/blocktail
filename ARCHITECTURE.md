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
rather than a card.

Below 860px the layout is not the same one compressed: the sidebar becomes a
docked sheet reached through a scope selector, the summary panel defaults to
collapsed, and feed rows are laid out as a log with the hash and block moved
into a per-row detail sheet (`GET /activity/{id}`). The goal is that the first
screen on a phone is activity, not navigation.

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
7. **Token metadata can be missing.** When a token reports no symbol, the feed
   shows a truncated contract address and `decimals = 0`, so the raw integer
   amount is displayed rather than a wrong one.
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

Each cycle re-reads the last `REORG_DEPTH` blocks (default 64, ~13 minutes).
Within that window the fresh scan is authoritative for every monitored wallet,
so any stored event the chain no longer reports is deleted. Below the window
nothing is ever pruned — a provider that stops serving old history must not be
able to erase settled records.

Pruning runs **only after a fully successful cycle**. If any wallet's fetch
fails, the cycle records the error, leaves the watermark where it was, and
deletes nothing.

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
  your own wallets is never called implausible. The sidebar and the summary run
  the same rule, so one wallet never carries two different totals.

Where valuation stops is deliberate: there is no cost basis, no profit and loss,
no 24-hour change and no price history. Each of those needs a methodology —
which lot, which timestamp, which venue — that this tool does not have and
should not guess at.

---

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
* `wallets.yml` accepts a per-wallet `chain:` key; `ethereum` is the only
  accepted value and the error message lists what is supported.

The UI does assume Ethereum — the top bar names the chain and every link goes to
Etherscan — which is exactly where the spec permits the assumption.

One instance indexes one chain. `build_context()` rejects a config naming more
than one, with an explicit message rather than silent misbehaviour.
