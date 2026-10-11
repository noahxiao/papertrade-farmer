"""Static Papertrade protocol facts.

Every value here was read from the live frontend bundle (exchange.papertrade.xyz,
build 7976669c6bab) or the docs on 2026-10-10. None of it is a documented,
versioned API; the rule watcher re-checks the parts that matter at runtime.
"""

from __future__ import annotations

EXCHANGE_ORIGIN = "https://exchange.papertrade.xyz"
HYPEREVM_RPC = "https://rpc.hyperliquid.xyz/evm"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
CHAIN_ID = 999

CONTRACTS = {
    "exchange": "0x6cd5661646289fb6e65ea5c032310fded797d0a2",
    "paper": "0xe40f17915daa230324030003a197cdaef2261c0e",
    "paperStaking": "0xaad6c7b0cc3014fc80ffedae0ed7ce5967b0f016",
    "depositProxyFactory": "0x1629b7d46bef6e13ff754174fc9f32dea309b38f",
}

# EIP-1967 storage slots used to detect proxy upgrades.
EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"

WAD = 10**18

INSTRUMENTS = {"BTC": 0, "ETH": 1}

EXCHANGE_DOMAIN = {"name": "String", "version": "1"}
PAPER_STAKING_DOMAIN = {"name": "PaperStaking", "version": "1"}
CANCEL_DOMAIN = {"name": "PaperTrade Intent Cancel", "version": "2"}

INTENT_ID_TAG = b"papertrade:intent-id:v2"

INTENT_LIFETIME_S = 3600
SESSION_KEY_TTL_S = 30 * 24 * 3600
MAX_CLOSE_IDS = 12

TYPES = {
    "RegisterSessionKey": [
        {"name": "user", "type": "address"},
        {"name": "key", "type": "address"},
        {"name": "expiry", "type": "uint64"},
        {"name": "feeAddress", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint64"},
    ],
    "OpenPosition": [
        {"name": "user", "type": "address"},
        {"name": "instrumentId", "type": "uint32"},
        {"name": "isLong", "type": "bool"},
        {"name": "size", "type": "uint128"},
        {"name": "leverage", "type": "uint32"},
        {"name": "useDebt", "type": "bool"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint64"},
    ],
    "Close": [
        {"name": "user", "type": "address"},
        {"name": "positionIds", "type": "uint256[]"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint64"},
    ],
    "Stake": [
        {"name": "user", "type": "address"},
        {"name": "amount", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint64"},
    ],
    "CancelIntent": [
        {"name": "user", "type": "address"},
        {"name": "intentId", "type": "bytes32"},
        {"name": "deadline", "type": "uint256"},
    ],
}

# Relayer error codes the frontend treats as "definitely not queued".
DEFINITE_REJECTION_CODES = frozenset(
    {
        "invalid_intent",
        "deadline_expired",
        "unauthorized_intent",
        "intent_conflict",
        "admission_rejected",
        "below_minimum_paper_stake",
        "below_minimum_withdrawal",
        "invalid_onward",
        "intake_action_paused",
        "rate_limited",
    }
)
