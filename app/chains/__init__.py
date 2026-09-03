"""Chain registry and the two seams that keep the rest of the app chain-neutral.

Only Ethereum mainnet is implemented. The point of these protocols is not to
support other chains today -- it is to keep chain-specific assumptions (address
shape, explorer URLs, hash formatting) from leaking into persistence, sync and
the HTTP layer, so that adding a chain later is additive rather than a rewrite.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models import Balance, Transfer


class UnknownChainError(ValueError):
    pass


@runtime_checkable
class Chain(Protocol):
    """Pure, offline knowledge about a chain: identity, addresses, display."""

    chain_id: str
    display_name: str
    native_symbol: str
    native_decimals: int

    def normalize_address(self, raw: str) -> str:
        """Return the canonical storage form, or raise ``ValueError``."""

    def is_valid_address(self, raw: str) -> bool: ...

    def display_address(self, address: str) -> str:
        """Full address in the chain's preferred display form."""

    def shorten_address(self, address: str) -> str: ...

    def shorten_tx_hash(self, tx_hash: str) -> str: ...

    def explorer_address_url(self, address: str) -> str: ...

    def explorer_tx_url(self, tx_hash: str) -> str: ...

    def explorer_block_url(self, block_number: int) -> str: ...


@runtime_checkable
class ChainDataProvider(Protocol):
    """Network access to a chain. One implementation exists: Alchemy/Ethereum."""

    chain_id: str
    name: str

    async def get_head_block(self) -> int: ...

    async def get_native_balance(self, address: str) -> int:
        """Balance in the native asset's smallest unit."""

    async def get_token_balances(self, address: str) -> list[Balance]:
        """Non-zero token balances held by ``address``."""

    async def get_transfers(
        self,
        address: str,
        *,
        outgoing: bool,
        from_block: int,
        to_block: int,
    ) -> list[Transfer]:
        """All transfers in ``[from_block, to_block]`` where ``address`` is the
        sender (``outgoing``) or the recipient. Providers that expose separate
        endpoints per transfer type merge them here."""

    async def close(self) -> None: ...


_CHAINS: dict[str, Chain] = {}


def register_chain(chain: Chain) -> None:
    _CHAINS[chain.chain_id] = chain


def get_chain(chain_id: str) -> Chain:
    try:
        return _CHAINS[chain_id]
    except KeyError:
        raise UnknownChainError(
            f"unsupported chain {chain_id!r}; supported: {', '.join(sorted(_CHAINS)) or 'none'}"
        ) from None


def supported_chains() -> list[str]:
    return sorted(_CHAINS)


from app.chains.ethereum.chain import EthereumChain  # noqa: E402

register_chain(EthereumChain())
