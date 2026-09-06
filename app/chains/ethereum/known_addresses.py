"""Names for the addresses that turn up in almost everyone's history.

A feed reading ``TW1 → 0x4b74…d72A`` makes the reader do the lookup that the
app exists to save them. Half the counterparties on a typical Ethereum wallet
are a few dozen well-known contracts and exchange wallets, and naming those is
the single change that makes a history readable without touching the layout.

Two rules govern what goes in here.

**A wrong name is worse than no name.** ``0x4b74…d72A`` is honest about what it
does not tell you; "Uniswap V3: Router" against the wrong address is a claim,
and one the reader has no way to doubt. So every entry is written in its EIP-55
checksummed form, which a test verifies — a transposed or altered character
fails the checksum with overwhelming probability — and every entry declares
whether it is a contract or an externally owned account, which a second test
verifies against the chain itself when a provider is available.

**Your names win.** These are a fallback for addresses nobody has named. A
label in ``wallets.yml``, or a monitored wallet's own name, always takes
precedence: the reader's word about their own counterparties beats ours.
"""

from __future__ import annotations

from enum import StrEnum


class AddressKind(StrEnum):
    CONTRACT = "contract"   # has code on chain
    EXCHANGE = "exchange"   # a custodial hot wallet: an EOA, not a contract


# Written checksummed so the test can verify them; looked up in lower case.
_KNOWN: dict[str, tuple[str, AddressKind]] = {
    # -- decentralised exchanges ------------------------------------------
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D": ("Uniswap V2: Router", AddressKind.CONTRACT),
    "0xf164fC0Ec4E93095b804a4795bBe1e041497b92a": ("Uniswap V2: Router 1", AddressKind.CONTRACT),
    "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f": ("Uniswap V2: Factory", AddressKind.CONTRACT),
    "0xE592427A0AEce92De3Edee1F18E0157C05861564": ("Uniswap V3: Router", AddressKind.CONTRACT),
    "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45": ("Uniswap V3: Router 2", AddressKind.CONTRACT),
    "0x1F98431c8aD98523631AE4a59f267346ea31F984": ("Uniswap V3: Factory", AddressKind.CONTRACT),
    "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD": ("Uniswap: Universal Router", AddressKind.CONTRACT),
    "0x66a9893cC07D91D95644AEDD05D03f95e1dBA8Af": ("Uniswap: Universal Router 2", AddressKind.CONTRACT),
    "0x000000000022D473030F116dDEE9F6B43aC78BA3": ("Uniswap: Permit2", AddressKind.CONTRACT),
    "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F": ("SushiSwap: Router", AddressKind.CONTRACT),
    "0x1111111254fb6c44bAC0beD2854e76F90643097d": ("1inch: Router V4", AddressKind.CONTRACT),
    "0x1111111254EEB25477B68fb85Ed929f73A960582": ("1inch: Router V5", AddressKind.CONTRACT),
    "0xDef1C0ded9bec7F1a1670819833240f027b25EfF": ("0x: Exchange Proxy", AddressKind.CONTRACT),
    "0xDEF171Fe48CF0115B1d80b88dc8eAB59176FEe57": ("ParaSwap: Augustus V5", AddressKind.CONTRACT),
    "0x9008D19f58AAbD9eD0D60971565AA8510560ab41": ("CoW Protocol: Settlement", AddressKind.CONTRACT),
    "0xBA12222222228d8Ba445958a75a0704d566BF2C8": ("Balancer: Vault", AddressKind.CONTRACT),
    "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7": ("Curve: 3pool", AddressKind.CONTRACT),
    "0x881D40237659C251811CEC9c364ef91dC08D300C": ("MetaMask: Swap Router", AddressKind.CONTRACT),

    # -- lending and staking ----------------------------------------------
    "0x7d2768dE32b0b80b7a3454c06BdAc94A69DDc7A9": ("Aave V2: Lending Pool", AddressKind.CONTRACT),
    "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2": ("Aave V3: Pool", AddressKind.CONTRACT),
    "0x3d9819210A31b4961b30EF54bE2aeD79B9c9Cd3B": ("Compound: Comptroller", AddressKind.CONTRACT),
    "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84": ("Lido: stETH", AddressKind.CONTRACT),
    "0x889edC2eDab5f40e902b864aD4d7AdE8E412F9B1": ("Lido: Withdrawal Queue", AddressKind.CONTRACT),
    "0xae78736Cd615f374D3085123A210448E74Fc6393": ("Rocket Pool: rETH", AddressKind.CONTRACT),
    "0x00000000219ab540356cBB839Cbe05303d7705Fa": ("Beacon Deposit Contract", AddressKind.CONTRACT),

    # -- bridges -----------------------------------------------------------
    "0x4Dbd4fc535Ac27206064B68FfCf827b0A60BAB3f": ("Arbitrum: Delayed Inbox", AddressKind.CONTRACT),
    "0x99C9fc46f92E8a1c0deC1b1747d010903E884bE1": ("Optimism: L1 Bridge", AddressKind.CONTRACT),
    "0x3154Cf16ccdb4C6d922629664174b904d80F2C35": ("Base: L1 Bridge", AddressKind.CONTRACT),
    "0x40ec5B33f54e0E8A33A975908C5BA1c14e5BbbDf": ("Polygon: PoS Bridge", AddressKind.CONTRACT),
    "0x401F6c983eA34274ec46f84D70b31C151321188b": ("Polygon: Plasma Bridge", AddressKind.CONTRACT),
    "0x32400084C286CF3E17e7B677ea9583e60a000324": ("zkSync Era: Diamond Proxy", AddressKind.CONTRACT),
    "0x3ee18B2214AFF97000D974cf647E7C347E8fa585": ("Wormhole: Token Bridge", AddressKind.CONTRACT),

    # -- marketplaces and utilities ---------------------------------------
    "0x00000000006c3852cbEf3e08E8dF289169EdE581": ("OpenSea: Seaport 1.1", AddressKind.CONTRACT),
    "0x00000000000000ADc04C56Bf30aC9d3c0aAF14dC": ("OpenSea: Seaport 1.5", AddressKind.CONTRACT),
    "0x000000000000Ad05Ccc4F10045630fb830B95127": ("Blur: Marketplace", AddressKind.CONTRACT),
    "0xcA11bde05977b3631167028862bE2a173976CA11": ("Multicall3", AddressKind.CONTRACT),
    "0xa6B71E26C5e0845f74c812102Ca7114b6a896AB2": ("Safe: Proxy Factory", AddressKind.CONTRACT),

    # -- exchange wallets (custodial, and externally owned) ----------------
    "0x28C6c06298d514Db089934071355E5743bf21d60": ("Binance 14", AddressKind.EXCHANGE),
    "0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549": ("Binance 15", AddressKind.EXCHANGE),
    "0xDFd5293D8e347dFe59E90eFd55b2956a1343963d": ("Binance 16", AddressKind.EXCHANGE),
    "0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8": ("Binance 7", AddressKind.EXCHANGE),
    "0xF977814e90dA44bFA03b6295A0616a897441aceC": ("Binance 8", AddressKind.EXCHANGE),
    "0x71660c4005BA85c37ccec55d0C4493E66Fe775d3": ("Coinbase 10", AddressKind.EXCHANGE),
    "0x503828976D22510aad0201ac7EC88293211D23Da": ("Coinbase 4", AddressKind.EXCHANGE),
    "0xddfAbCdc4D8FfC6d5beaf154f18B778f892A0740": ("Coinbase 6", AddressKind.EXCHANGE),
    "0xAe2D4617c862309A3d75A0fFB358c7a5009c673F": ("Kraken 4", AddressKind.EXCHANGE),
    "0xA83B11093c858c86321FBc4c20FE82cdbd58E09E": ("Kraken 13", AddressKind.EXCHANGE),
    "0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b": ("OKX", AddressKind.EXCHANGE),
    "0x77134cbC06cB00b66F4c7e623D5fdBF6777635EC": ("Bitfinex", AddressKind.EXCHANGE),
    "0x0D0707963952f2fBA59dD06f2b425ace40b492Fe": ("Gate.io", AddressKind.EXCHANGE),
    "0xf89d7b9c864f589bbF53a82105107622B35EaA40": ("Bybit", AddressKind.EXCHANGE),
    "0x6262998Ced04146fA42253a5C0AF90CA02dfd2A3": ("Crypto.com", AddressKind.EXCHANGE),
}

KNOWN_ADDRESSES: dict[str, tuple[str, AddressKind]] = {
    address.lower(): entry for address, entry in _KNOWN.items()
}

# The checksummed spellings, for the test that verifies them.
CHECKSUMMED_SOURCE: tuple[str, ...] = tuple(_KNOWN)


def label_for(address: str) -> str | None:
    """A name for a well-known address, or ``None`` when we have nothing to add."""
    entry = KNOWN_ADDRESSES.get(address.lower())
    return None if entry is None else entry[0]


def kind_of(address: str) -> AddressKind | None:
    entry = KNOWN_ADDRESSES.get(address.lower())
    return None if entry is None else entry[1]
