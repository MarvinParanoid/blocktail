"""Check the address registry against the chain itself.

Skipped unless a provider is configured, because it needs the network — but it
is the check that matters. EIP-55 catches an altered character; only the chain
can say whether the address we named is the kind of thing we claim it is. A
contract we named must have code, and an exchange hot wallet must be an account
that has sent a great many transactions. A transposed character lands on an
address that is neither.

    ALCHEMY_API_KEY=... pytest tests/test_known_addresses_live.py
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

from app.chains.ethereum.known_addresses import KNOWN_ADDRESSES, AddressKind

URL = os.environ.get("ALCHEMY_URL") or (
    f"https://eth-mainnet.g.alchemy.com/v2/{os.environ['ALCHEMY_API_KEY']}"
    if os.environ.get("ALCHEMY_API_KEY")
    else None
)

pytestmark = pytest.mark.skipif(URL is None, reason="no provider configured")


def _rpc(method: str, params: list, _id=[0]) -> object:
    _id[0] += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params})
    request = urllib.request.Request(
        URL, data=body.encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response).get("result")


@pytest.mark.parametrize(
    ("address", "label", "kind"),
    [(a, label, kind) for a, (label, kind) in KNOWN_ADDRESSES.items()],
    ids=[label for label, _ in KNOWN_ADDRESSES.values()],
)
def test_the_address_is_what_we_say_it_is(address: str, label: str, kind: AddressKind):
    code = _rpc("eth_getCode", [address, "latest"]) or "0x"
    has_code = code != "0x"

    if kind is AddressKind.CONTRACT:
        assert has_code, f"{label}: named a contract, but nothing is deployed there"
        return

    assert not has_code, f"{label}: named an exchange wallet, but it has code"
    nonce = int(_rpc("eth_getTransactionCount", [address, "latest"]) or "0x0", 16)
    assert nonce > 100, f"{label}: named an exchange wallet, but it has sent {nonce} txs"
