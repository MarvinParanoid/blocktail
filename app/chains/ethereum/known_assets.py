"""Which Ethereum mainnet tokens are known to be real, and which tickers are
worth forging. These are two different questions and the file keeps them apart.

**Known** is an allowlist, and wider is better: it exists so a holding of a real
asset is never called dust because its price lookup failed or its balance is
small. Most of it comes from a vendored token list (`tokenlist.json`, refreshed
by `scripts/refresh-token-list.py`), with a curated floor below it for the few
majors no upstream list carries — stETH among them, which is not a footnote.

**Forgery targets** is the opposite: a small, hand-picked set of the tickers a
scam actually bothers to impersonate. It must stay small. An impostor is
*excluded* — the harshest thing this app does to an asset — and hundreds of real
tokens share a ticker with some other real token, so deriving this set from the
allowlist would brand honest projects as frauds by coincidence. Growing the
allowlist is safe; growing this set is not.

Anything in neither can still be shown by being worth something, by having been
sent by one of your own wallets, or by being named in `trusted_assets`.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

# The few majors no upstream list carries, plus the ones worth stating outright.
CURATED: dict[str, str] = {
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "WBTC",
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": "stETH",
    "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0": "wstETH",
    "0xae78736cd615f374d3085123a210448e74fc6393": "rETH",
    "0x514910771af9ca656af840dff83e8264ecf986ca": "LINK",
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": "UNI",
    "0x7d1afa7b718fb893db30a3abc0cfc608aacfebb0": "MATIC",
    "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2": "MKR",
    "0x0d8775f648430679a709e98d2b0cb6250d2887ef": "BAT",
    "0x4fabb145d64652a948d72533023f6e7a623c7c53": "BUSD",
    "0x853d955acef822db058eb8505911ed77f175b99e": "FRAX",
    "0x5f98805a4e8be255a32880fdec7f6728c6568ba0": "LUSD",
    "0xd533a949740bb3306d119cc777fa900ba034cd52": "CRV",
    "0xba100000625a3754423978a60c9317c58a424e3d": "BAL",
    "0xc00e94cb662c3520282e6f5717214004a7f26888": "COMP",
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": "AAVE",
    "0x6982508145454ce325ddbe47a25d4ec3d2311933": "PEPE",
    "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce": "SHIB",
    "0x4d224452801aced8b2f0aebe155379bb5d594381": "APE",
    "0x5a98fcbea516cf06857215779fd812ca3bef1b32": "LDO",
    "0x111111111117dc0aa78b770fa6a738034120c302": "1INCH",
    "0x0000000000085d4780b73119b644ae5ecd22b376": "TUSD",
    "0x8e870d67f660d95d5be530380d0ec0bd388289e1": "USDP",
    "0x83f20f44975d03b1b09e64809b757c47f942beea": "sDAI",
    "0xdd974d5c2e2928dea5f71b9825b8b646686bd200": "KNC",
    "0xe41d2489571d322189246dafa5ebde1f4699f498": "ZRX",
}

_LIST_PATH = Path(__file__).with_name("tokenlist.json")


def _vendored_tokens() -> dict[str, str]:
    """The token list as shipped. Read once, at import.

    A missing or unreadable file is not fatal: the curated floor still covers
    the majors, and a monitor that refuses to start because a convenience is
    absent is worse than one that recognises fewer tokens.
    """
    try:
        document = json.loads(_LIST_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return {
        address.lower(): entry["symbol"]
        for address, entry in document.get("tokens", {}).items()
        if isinstance(entry, dict) and entry.get("symbol")
    }


# Curated last: a name we have chosen deliberately wins over a list's.
KNOWN_TOKENS: dict[str, str] = {**_vendored_tokens(), **CURATED}


def is_native(contract_address: str) -> bool:
    """The chain's own currency, which has no contract.

    It cannot impersonate anything: it is the thing being impersonated. Adding
    "ETH" to the forgery targets without this guard classified the native asset
    as a forgery of itself, which emptied the feed of every ETH transfer — the
    suite caught it immediately, which is the only reason it is a comment and
    not an incident.
    """
    return not contract_address


def is_known(contract_address: str) -> bool:
    return contract_address.lower() in KNOWN_TOKENS


# Digits and punctuation that stand in for letters in a spoofed ticker.
_LOOKALIKES = str.maketrans({"0": "O", "1": "I", "5": "S", "$": "S", "8": "B", "|": "I"})

# Cyrillic and Greek letters that are indistinguishable from Latin ones in every
# font a browser will pick. Decomposition does not touch these — they are
# separate letters, not accented Latin — so they have to be mapped by hand, or
# they get dropped and `ÚЅDТ` quietly reduces to `UD` instead of `USDT`.
_CONFUSABLES = str.maketrans(
    {
        "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "І": "I", "Ј": "J",
        "К": "K", "М": "M", "О": "O", "Р": "P", "Ѕ": "S", "Т": "T", "Х": "X",
        "У": "Y", "Ү": "Y", "а": "A", "е": "E", "о": "O", "р": "P", "с": "C",
        "х": "X", "ѕ": "S", "і": "I",
        "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
        "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
        "ο": "O", "ν": "V",
    }
)
# Deliberately not derived from KNOWN_TOKENS. See the module docstring: an
# impostor is excluded outright, and hundreds of real tokens share a ticker with
# some other real token. These are the ones a scam actually forges — every one
# of them is money, or is about to be spent as though it were.
_FORGERY_TARGETS = {
    "USDT", "USDC", "DAI", "USDE", "USDS", "USDP", "TUSD", "BUSD", "FRAX",
    "LUSD", "PYUSD", "ETH", "WETH", "STETH", "WSTETH", "RETH", "CBETH",
    "BTC", "WBTC", "TBTC", "CBBTC",
}


def normalise_symbol(symbol: str) -> str:
    """Reduce a ticker to what it looks like at a glance.

    Strips combining marks and accents, folds the digits and punctuation that
    stand in for letters, and drops everything else. `Ụ᠋5DT` and `U$DT` both
    come out as `USDT`.
    """
    # Confusables first: NFKD leaves them alone, so mapping afterwards is too
    # late — the non-ASCII filter would already have thrown them away.
    return "".join(
        c for c in _visible(symbol).upper().translate(_LOOKALIKES)
        if c.isascii() and c.isalnum()
    )


def _visible(symbol: str) -> str:
    """The symbol with accents, invisible marks and formatting characters gone."""
    folded = unicodedata.normalize("NFKD", symbol.translate(_CONFUSABLES))
    return "".join(
        c for c in folded
        if not unicodedata.combining(c) and unicodedata.category(c) not in {"Mn", "Cf"}
    )


def impersonates_known(symbol: str, contract_address: str) -> bool:
    """A ticker that reads as a token this chain is known for, from a contract
    that is not it — `Ụ᠋5DT` beside the real USDT."""
    if is_native(contract_address) or contract_address.lower() in KNOWN_TOKENS:
        return False
    return normalise_symbol(symbol) in _FORGERY_TARGETS


# Long enough for the longest honest ticker anyone actually uses, short enough
# that a sentence cannot hide under it.
MAX_TICKER_LENGTH = 12


def looks_forged(symbol: str, contract_address: str) -> bool:
    """Whether a ticker is built to be mistaken for something else.

    Enumerating lookalike characters is a race that cannot be won: live data
    turned up Cyrillic, Greek, Armenian, Lisu, Canadian syllabics and
    mathematical symbols, all rendering as plain Latin letters. So the test is
    inverted — a ticker on this chain is ASCII, and one that is not, from a
    contract nobody knows, is trying to look like something.

    Plain ASCII is not proof of honesty, so two more things a ticker is not:
    a sentence, and an advertisement. Live data held a contract calling itself
    "Tether USDT" — every character Latin, and the whole point of the name is
    to be read as the real one — beside "! bimarket.io - World Cup binary
    markets". Real tickers are short and unspaced, which is what makes both
    tests safe: no legitimate ERC-20 symbol on this chain has a space in it,
    and none runs to forty characters.

    Deliberately not a judgement about worth. A token can be worthless and
    honest; this is about a name chosen to deceive.
    """
    if is_native(contract_address) or contract_address.lower() in KNOWN_TOKENS:
        return False
    if impersonates_known(symbol, contract_address):
        return True

    visible = _visible(symbol)
    if any(not c.isascii() for c in visible):
        return True
    return any(c.isspace() for c in visible) or len(visible.strip()) > MAX_TICKER_LENGTH
