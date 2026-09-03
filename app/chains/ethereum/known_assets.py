"""A short list of Ethereum mainnet tokens that are known to be real.

This is not an attempt at a token registry. It exists so that a holding of a
major asset is never classified as dust just because its price lookup failed or
its balance happens to be small. Anything not listed here can still be shown by
being worth something, by having been sent by one of your own wallets, or by
being named in `trusted_assets` in the config.
"""

from __future__ import annotations

KNOWN_TOKENS: dict[str, str] = {
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


def is_known(contract_address: str) -> bool:
    return contract_address.lower() in KNOWN_TOKENS
