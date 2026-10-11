"""Signing, relayer response classification, breaker and frontend parsing."""

from __future__ import annotations

import pytest
from eth_account import Account
from eth_utils import keccak

from fakes import FakeClock

from ptfarmer.guards import RelayerBreaker, _typed_fields
from ptfarmer.protocol import CONTRACTS, INTENT_ID_TAG, TYPES
from ptfarmer.relayer import SubmitStatus, classify_response
from ptfarmer.signing import IntentSigner, compute_intent_id
from ptfarmer.store import Store


@pytest.fixture
def signer():
    acct = Account.create()
    return IntentSigner(acct.key.hex(), "0x" + "ab" * 20, CONTRACTS["exchange"], CONTRACTS["paperStaking"])


def recover(si) -> str:
    sig = bytes.fromhex(si.signed["r"][2:]) + bytes.fromhex(si.signed["s"][2:]) + bytes([int(si.signed["v"]) - 27])
    return Account._recover_hash(si.digest, signature=sig).lower()


def test_open_position_wire_format_and_recovery(signer):
    si = signer.open_position(0, True, 250 * 10**18, 40, 1234 * 256, 1_791_680_400)
    assert recover(si) == signer.address
    assert si.signed == {
        "user": "0x" + "ab" * 20, "instrumentId": "0", "isLong": True, "size": str(250 * 10**18), "leverage": "40",
        "useDebt": False, "nonce": str(1234 * 256), "deadline": "1791680400",
        "v": si.signed["v"], "r": si.signed["r"], "s": si.signed["s"],
    }
    assert si.signed["v"] in ("27", "28")


def test_close_cancel_stake_sign(signer):
    close = signer.close([102, 101], 7, 100)
    assert close.signed["positionIds"] == ["102", "101"] and recover(close) == signer.address
    cancel = signer.cancel(close.intent_id, 100)
    assert cancel.signed["intentId"] == close.intent_id and recover(cancel) == signer.address
    assert recover(signer.stake(10**21, 8, 100)) == signer.address
    assert cancel.digest != signer.sign("CancelIntent", {"user": signer.user, "intentId": close.intent_id, "deadline": 100}).digest, \
        "cancel uses its own EIP-712 domain"


def test_intent_id_derivation(signer):
    si = signer.close([1], 1, 1)
    expected = "0x" + keccak(keccak(INTENT_ID_TAG) + si.digest + bytes.fromhex(signer.address[2:])).hex()
    assert si.intent_id == compute_intent_id(si.digest, signer.address) == expected


def test_signing_rejects_missing_fields(signer):
    with pytest.raises(ValueError):
        signer.sign("Close", {"user": signer.user})


@pytest.mark.parametrize("status,body,expected,transport", [
    (200, {"intentId": "0xaa", "state": "queued"}, SubmitStatus.ACCEPTED, False),
    (202, {}, SubmitStatus.ACCEPTED, False),
    (400, {"code": "invalid_intent"}, SubmitStatus.REJECTED, False),
    (409, {"code": "admission_rejected", "reason": "noOpenPositions"}, SubmitStatus.REJECTED, False),
    (429, {}, SubmitStatus.REJECTED, True),
    (500, {}, SubmitStatus.UNCONFIRMED, True),
    (502, {"code": "upstream"}, SubmitStatus.UNCONFIRMED, True),
    (200, {"ok": False, "error": "weird"}, SubmitStatus.UNCONFIRMED, True),
])
def test_classify_response(status, body, expected, transport):
    result = classify_response(status, body, None, "0xaa")
    assert result.status is expected and result.transport_failure is transport


def test_relayer_intent_id_is_authoritative():
    result = classify_response(200, {"intentId": "0xBB"}, None, "0xaa")
    assert (result.status, result.intent_id, result.code) == (SubmitStatus.ACCEPTED, "0xbb", "intent_id_mismatch")


def test_breaker_backoff_retry_after_and_halt():
    clock = FakeClock()
    b = RelayerBreaker(3, 600, clock)
    b.record_failure()
    assert not b.can_submit()
    clock.advance(5)
    assert b.can_submit()
    b.record_failure(retry_after_s=60)
    clock.advance(30)
    assert not b.can_submit(), "Retry-After is honoured"
    b.record_failure()
    assert b.is_degraded and not b.should_halt
    clock.advance(600)
    assert b.should_halt
    b.record_success()
    assert not b.is_degraded and b.can_submit()


def test_typed_field_parser_on_minified_shape():
    chunk = ("const t={OpenPosition:[{name:`user`,type:`address`},{name:`size`,type:`uint128`}],"
             "Close:[{name:`user`,type:`address`},{name:`positionIds`,type:`uint256[]`}]},n=1")
    assert _typed_fields(chunk, "OpenPosition") == [("user", "address"), ("size", "uint128")]
    assert _typed_fields(chunk, "Close") == [("user", "address"), ("positionIds", "uint256[]")]
    assert _typed_fields(chunk, "Stake") is None
    assert [f["name"] for f in TYPES["Close"]] == ["user", "positionIds", "nonce", "deadline"]


def test_nonces_are_monotonic_and_persisted(tmp_path):
    store = Store(tmp_path)
    first = store.next_nonce("session:x", 512)
    assert (first, store.next_nonce("session:x", 999)) == (512, 513)
    assert Store(tmp_path).next_nonce("session:x", 1) == 514, "survives restart"
    assert store.next_nonce("paper:x", 256) == 256, "scopes are independent"
