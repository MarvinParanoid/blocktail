"""Ethereum-specific knowledge: address shape, checksums, explorer URLs.

Everything that assumes a `0x`-prefixed 20-byte hex address lives here, and
nowhere else.
"""

from __future__ import annotations

import re

from app.chains.ethereum.keccak import keccak256
from app.chains.ethereum.known_addresses import label_for

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

ZERO_ADDRESS = "0x" + "0" * 40


def to_checksum_address(address: str) -> str:
    """EIP-55 mixed-case checksum encoding."""
    body = address.lower().removeprefix("0x")
    digest = keccak256(body.encode("ascii")).hex()
    return "0x" + "".join(
        char.upper() if char.isalpha() and int(digest[i], 16) >= 8 else char
        for i, char in enumerate(body)
    )


class EthereumChain:
    chain_id = "ethereum"
    display_name = "Ethereum"
    native_symbol = "ETH"
    native_decimals = 18
    explorer_name = "Etherscan"
    explorer_base = "https://etherscan.io"

    def normalize_address(self, raw: str) -> str:
        candidate = (raw or "").strip()
        if not _ADDRESS_RE.match(candidate):
            raise ValueError(
                f"{candidate!r} is not a valid Ethereum address "
                "(expected 0x followed by 40 hex characters)"
            )
        # A mixed-case address carries an EIP-55 checksum; verify it so that a
        # typo in the config is caught at startup rather than silently watching
        # an address that does not exist.
        body = candidate.removeprefix("0x")
        if body != body.lower() and body != body.upper():
            if to_checksum_address(candidate) != candidate:
                raise ValueError(
                    f"{candidate} has an invalid EIP-55 checksum "
                    "(mixed-case address does not match its checksum)"
                )
        return candidate.lower()

    def is_valid_address(self, raw: str) -> bool:
        try:
            self.normalize_address(raw)
        except ValueError:
            return False
        return True

    def is_valid_tx_hash(self, raw: str) -> bool:
        return bool(_TX_HASH_RE.match((raw or "").strip()))

    def display_address(self, address: str) -> str:
        return to_checksum_address(address) if _ADDRESS_RE.match(address) else address

    def shorten_address(self, address: str) -> str:
        shown = self.display_address(address)
        if len(shown) < 12:
            return shown
        return f"{shown[:6]}…{shown[-4:]}"

    def shorten_tx_hash(self, tx_hash: str) -> str:
        if len(tx_hash) < 14:
            return tx_hash
        return f"{tx_hash[:8]}…{tx_hash[-6:]}"

    def explorer_address_url(self, address: str) -> str:
        return f"{self.explorer_base}/address/{self.display_address(address)}"

    def explorer_tx_url(self, tx_hash: str) -> str:
        return f"{self.explorer_base}/tx/{tx_hash}"

    def explorer_block_url(self, block_number: int) -> str:
        return f"{self.explorer_base}/block/{block_number}"

    def known_label(self, address: str) -> str | None:
        return label_for(address)

    def explorer_token_url(self, contract_address: str) -> str:
        return f"{self.explorer_base}/token/{self.display_address(contract_address)}"
