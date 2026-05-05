"""Unit tests for crash detection and auto-regen logic in ExecutionService."""
import pytest

from ritesmith.core.execution import _is_contract_crash


@pytest.mark.parametrize("msg,expected", [
    ("Runtime error: attempt to index nil value", True),
    ("Syntax/load error: unexpected symbol near '}'", True),
    ("Script execution timed out", False),
    ("", False),
    (None, False),
])
def test_is_contract_crash(msg, expected):
    error_json = {"message": msg} if msg is not None else None
    assert _is_contract_crash(error_json) is expected


def test_is_contract_crash_policy_deny_not_crash():
    # Policy denies use a "reason" key, not "message"
    assert _is_contract_crash({"reason": "policy deny"}) is False


def test_is_contract_crash_empty_dict():
    assert _is_contract_crash({}) is False
