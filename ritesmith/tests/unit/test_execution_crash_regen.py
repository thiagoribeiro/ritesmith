"""Unit tests for crash detection and auto-regen logic in ExecutionService."""

import pytest

from ritesmith.core.execution import _is_contract_crash, _is_contract_violation


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("Runtime error: attempt to index nil value", True),
        ("Syntax/load error: unexpected symbol near '}'", True),
        ("Script execution timed out", False),
        ("", False),
        (None, False),
    ],
)
def test_is_contract_crash(msg, expected):
    error_json = {"message": msg} if msg is not None else None
    assert _is_contract_crash(error_json) is expected


def test_is_contract_crash_policy_deny_not_crash():
    # Policy denies use a "reason" key, not "message"
    assert _is_contract_crash({"reason": "policy deny"}) is False


def test_is_contract_crash_empty_dict():
    assert _is_contract_crash({}) is False


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("Output schema validation failed: -5 is less than the minimum of 0", True),
        ("Output schema validation failed: 'foo' is not of type 'number'", True),
        ("Runtime error: attempt to index nil value", False),
        ("Syntax/load error: unexpected symbol near '}'", False),
        ("Script execution timed out", False),
        ("", False),
        (None, False),
    ],
)
def test_is_contract_violation(msg, expected):
    error_json = {"message": msg} if msg is not None else None
    assert _is_contract_violation(error_json) is expected


def test_is_contract_violation_policy_deny_not_violation():
    assert _is_contract_violation({"reason": "policy deny"}) is False


def test_is_contract_violation_empty_dict():
    assert _is_contract_violation({}) is False


def test_contract_crash_and_contract_violation_are_mutually_exclusive():
    # A message should never satisfy both classifiers — _run_lua branches on
    # them with if/elif and relies on that being true.
    messages = [
        "Runtime error: boom",
        "Syntax/load error: boom",
        "Output schema validation failed: boom",
        "Script execution timed out",
    ]
    for msg in messages:
        error_json = {"message": msg}
        assert not (_is_contract_crash(error_json) and _is_contract_violation(error_json))
