"""EIP-712 signing of Papertrade intents.

Wire format (decoded from the frontend): every typed field is serialised as a
string (uints in decimal, addresses lower-case, uint256[] as a list of decimal
strings, bools as JSON bools) plus `v` ("27"/"28"), `r` and `s`.

intentId = keccak256(keccak256("papertrade:intent-id:v2") || digest || signer)
is inferred from the minified bundle; the relayer's returned intentId is
treated as authoritative and any mismatch is logged.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

from .protocol import CANCEL_DOMAIN, CHAIN_ID, EXCHANGE_DOMAIN, INTENT_ID_TAG, PAPER_STAKING_DOMAIN, TYPES

_TAG_HASH = keccak(INTENT_ID_TAG)


@dataclass(frozen=True)
class SignedIntent:
    primary_type: str
    signed: dict
    digest: bytes
    signer: str
    intent_id: str


def _wire_value(type_name: str, value):
    if type_name == "uint256[]":
        return [str(int(v)) for v in value]
    if type_name.startswith("uint"):
        return str(int(value))
    if type_name == "address" or type_name.startswith("bytes"):
        return str(value).lower()
    return value


def _typed_value(type_name: str, value):
    if type_name == "uint256[]":
        return [int(v) for v in value]
    if type_name.startswith("uint"):
        return int(value)
    if type_name == "address":
        return to_checksum_address(value)
    if type_name == "bytes32":
        return bytes.fromhex(str(value)[2:])
    return value


def compute_intent_id(digest: bytes, signer: str) -> str:
    return "0x" + keccak(_TAG_HASH + digest + bytes.fromhex(signer[2:])).hex()


class IntentSigner:
    """Signs intents with one private key for one Papertrade account (`user`)."""

    def __init__(self, private_key: str, user: str, exchange: str, paper_staking: str, chain_id: int = CHAIN_ID):
        self._account = Account.from_key(private_key)
        self.user = user.lower()
        self.exchange = exchange.lower()
        self.paper_staking = paper_staking.lower()
        self.chain_id = chain_id

    @property
    def address(self) -> str:
        return self._account.address.lower()

    def _domain(self, which: str) -> dict:
        base = {"exchange": EXCHANGE_DOMAIN, "paperStaking": PAPER_STAKING_DOMAIN, "cancel": CANCEL_DOMAIN}[which]
        contract = self.paper_staking if which == "paperStaking" else self.exchange
        return {**base, "chainId": self.chain_id, "verifyingContract": to_checksum_address(contract)}

    def sign(self, primary_type: str, message: dict, domain: str = "exchange") -> SignedIntent:
        fields = TYPES[primary_type]
        missing = [f["name"] for f in fields if f["name"] not in message]
        if missing:
            raise ValueError(f"{primary_type} missing fields {missing}")
        typed_message = {f["name"]: _typed_value(f["type"], message[f["name"]]) for f in fields}
        domain_data = self._domain(domain)
        domain_types = [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ]
        full = {
            "types": {"EIP712Domain": domain_types, primary_type: fields},
            "primaryType": primary_type,
            "domain": domain_data,
            "message": typed_message,
        }
        signable = encode_typed_data(full_message=full)
        digest = keccak(b"\x19" + signable.version + signable.header + signable.body)
        sig = self._account.unsafe_sign_hash(digest)
        signed = {f["name"]: _wire_value(f["type"], message[f["name"]]) for f in fields}
        signed.update({"v": str(sig.v), "r": "0x" + sig.r.to_bytes(32, "big").hex(), "s": "0x" + sig.s.to_bytes(32, "big").hex()})
        return SignedIntent(
            primary_type=primary_type,
            signed=signed,
            digest=digest,
            signer=self.address,
            intent_id=compute_intent_id(digest, self.address),
        )

    def open_position(self, instrument_id: int, is_long: bool, margin_wad: int, leverage: int, nonce: int, deadline: int) -> SignedIntent:
        return self.sign(
            "OpenPosition",
            {
                "user": self.user,
                "instrumentId": instrument_id,
                "isLong": is_long,
                "size": margin_wad,
                "leverage": leverage,
                "useDebt": False,
                "nonce": nonce,
                "deadline": deadline,
            },
        )

    def close(self, position_ids: list[int], nonce: int, deadline: int) -> SignedIntent:
        return self.sign("Close", {"user": self.user, "positionIds": position_ids, "nonce": nonce, "deadline": deadline})

    def cancel(self, intent_id: str, deadline: int) -> SignedIntent:
        return self.sign("CancelIntent", {"user": self.user, "intentId": intent_id, "deadline": deadline}, domain="cancel")

    def stake(self, amount_wad: int, nonce: int, deadline: int) -> SignedIntent:
        return self.sign("Stake", {"user": self.user, "amount": amount_wad, "nonce": nonce, "deadline": deadline}, domain="paperStaking")
