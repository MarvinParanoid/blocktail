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
