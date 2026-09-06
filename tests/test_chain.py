"""Ethereum chain behaviour, and the chain-neutrality of the layer above it."""

from __future__ import annotations

import pytest

from app.chains import ChainDataProvider, get_chain, supported_chains, UnknownChainError
from app.chains.ethereum.chain import to_checksum_address
from app.chains.ethereum.keccak import keccak256

chain = get_chain("ethereum")

# The four addresses from EIP-55 itself.
EIP55 = [
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
]


@pytest.mark.parametrize(
    "data,digest",
    [
        (b"", "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"),
        (b"abc", "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
        (b"testing", "5f16f4c7f149ac4f9510d9cf8cf384038ad348b3bcdc01915f95de12df9d1b02"),
        # longer than the 136-byte rate, so the sponge absorbs several blocks
        (b"a" * 200, "96ea54061def936c4be90b518992fdc6f12f535068a256229aca54267b4d084d"),
    ],
)
def test_keccak256_matches_known_vectors(data, digest):
    assert keccak256(data).hex() == digest


@pytest.mark.parametrize("address", EIP55)
def test_eip55_checksums(address):
    assert to_checksum_address(address.lower()) == address
    assert chain.normalize_address(address) == address.lower()


def test_registry_only_knows_ethereum():
    assert supported_chains() == ["ethereum"]
    with pytest.raises(UnknownChainError):
        get_chain("bitcoin")


def test_address_validation_lives_in_the_chain():
    assert chain.is_valid_address(EIP55[0])
    assert chain.is_valid_address(EIP55[0].lower())
    assert not chain.is_valid_address("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh")
    assert not chain.is_valid_address("0x" + "g" * 40)
    assert not chain.is_valid_address(None)


def test_shortening():
    assert chain.shorten_address(EIP55[0].lower()) == "0x5aAe…eAed"
    assert chain.shorten_tx_hash("0x" + "ab" * 32) == "0xababab…ababab"
    assert chain.shorten_address("0x12") == "0x12"


def test_explorer_urls():
    assert chain.explorer_tx_url("0xdead") == "https://etherscan.io/tx/0xdead"
    assert chain.explorer_address_url(EIP55[0].lower()).endswith(EIP55[0])
    assert chain.explorer_block_url(21_000_000) == "https://etherscan.io/block/21000000"
    assert "/token/" in chain.explorer_token_url(EIP55[1].lower())


def test_native_asset_description():
    assert chain.chain_id == "ethereum"
    assert chain.native_symbol == "ETH"
    assert chain.native_decimals == 18


def test_alchemy_provider_satisfies_the_generic_protocol():
    from app.chains.ethereum.alchemy import AlchemyProvider

    assert isinstance(AlchemyProvider("https://example.invalid"), ChainDataProvider)


# ------------------------------------------------------------- impostors


@pytest.mark.parametrize(
    "symbol",
    [
        "USDT",          # plain
        "U$DT", "U5DT",  # digits and punctuation standing in for letters
        "UṢDT",          # a combining dot below
        "Ụ᠋5DT",          # precomposed dot plus an invisible Mongolian selector
        "ÚЅDТ",          # Cyrillic Ѕ and Т
        "U឵S឵DΤ",         # Khmer inherent vowels and a Greek Tau
    ],
)
def test_tickers_that_ape_a_known_one_are_recognised(symbol):
    """Every one of these was found on a live mainnet address, all reading as
    USDT in any font a browser will pick."""
    from app.chains.ethereum.known_assets import impersonates_known, normalise_symbol

    assert normalise_symbol(symbol) == "USDT"
    assert impersonates_known(symbol, "0x" + "1" * 40)


def test_the_genuine_contract_is_not_an_impostor():
    from app.chains.ethereum.known_assets import impersonates_known

    assert not impersonates_known("USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7")
    assert not impersonates_known("USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")


@pytest.mark.parametrize("symbol", ["YRISE", "Duhash.games", "CAT", "DOG", "HEX", "GG"])
def test_an_ordinary_junk_ticker_is_not_an_impostor(symbol):
    """Being worthless and being a forgery are different accusations, and only
    the second one is about deception."""
    from app.chains.ethereum.known_assets import impersonates_known

    assert not impersonates_known(symbol, "0x" + "2" * 40)


def test_a_ticker_that_is_not_a_ticker_is_forged():
    """Plain ASCII is not proof of honesty. Live data held a contract calling
    itself "Tether USDT" — every character Latin, and the whole point of the
    name is to be read as the real one."""
    from app.chains.ethereum.known_assets import looks_forged

    scam = "0xb8949fa32aff11b0c92d0fa148e15b7a8cbd461f"
    assert looks_forged("Tether USDT", scam), "no real ticker has a space in it"
    assert looks_forged("! bimarket.io - World Cup binary markets", "0x" + "9" * 40)


def test_an_honest_ticker_survives_both_tests():
    """The cost of getting this wrong is a real holding vanishing from the
    portfolio, so the tests have to be ones no legitimate ticker can fail."""
    from app.chains.ethereum.known_assets import looks_forged

    assert not looks_forged("USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7")
    assert not looks_forged("WETH", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2")
    # A derivative from a contract we do not know is still not an impostor.
    assert not looks_forged("aEthUSDC", "0x" + "a" * 40)
    # Worthless, and honest about it.
    assert not looks_forged("YRISE", "0x6051c1354ccc51b4d561e43b02735deae64768b8")
    assert not looks_forged("Duhash.games", "0x" + "7" * 40)


# ------------------------------------------------------- known addresses


def test_every_known_address_is_written_checksummed():
    """A wrong name is worse than none: "0x4b74…d72A" is honest about what it
    does not tell you, and a confident label on the wrong address is not.
    EIP-55 catches an altered character with overwhelming probability."""
    from app.chains.ethereum.chain import to_checksum_address
    from app.chains.ethereum.known_addresses import CHECKSUMMED_SOURCE

    wrong = [a for a in CHECKSUMMED_SOURCE if to_checksum_address(a) != a]
    assert wrong == [], "these would not survive a transposed character"


def test_no_address_is_named_twice():
    from app.chains.ethereum.known_addresses import CHECKSUMMED_SOURCE, KNOWN_ADDRESSES

    assert len(KNOWN_ADDRESSES) == len(CHECKSUMMED_SOURCE)


def test_every_entry_declares_what_kind_of_address_it_is():
    """The declaration is what a live check against the chain can test: a
    contract must have code, an exchange wallet must not."""
    from app.chains.ethereum.known_addresses import KNOWN_ADDRESSES, AddressKind

    for address, (label, kind) in KNOWN_ADDRESSES.items():
        assert label.strip(), address
        assert kind in (AddressKind.CONTRACT, AddressKind.EXCHANGE), address


def test_a_known_name_is_only_a_fallback():
    """These are for counterparties nobody has named. A label in wallets.yml,
    or a monitored wallet's own name, is the reader's word about their own
    counterparties and beats ours."""
    from app.chains.ethereum.chain import EthereumChain
    from app.web.format import _party

    chain = EthereumChain()
    binance = "0x28C6c06298d514Db089934071355E5743bf21d60"

    assert _party(chain, binance, None, None).display == "Binance 14"
    assert _party(chain, binance, None, "Work exchange").display == "Work exchange"
    assert _party(chain, binance, "Cold", None).display == "Cold"


def test_an_address_nobody_knows_is_still_shortened():
    from app.chains.ethereum.chain import EthereumChain
    from app.web.format import _party

    party = _party(EthereumChain(), "0x" + "9" * 40, None, None)
    assert party.display == party.short
    assert not party.is_labelled
