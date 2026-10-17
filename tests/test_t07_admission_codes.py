"""Stable HTTP admission error codes for database-trigger rejections."""

from types import SimpleNamespace

from gateway.contracts import AdmissionLimitError
from gateway.mysql_repository import MySQLGatewayRepository


def test_mysql_user_active_limit_trigger_maps_to_public_turn_limit_code():
    error = SimpleNamespace(
        orig=SimpleNamespace(args=(1644, "user_turn_limit_reached"))
    )

    assert MySQLGatewayRepository._admission_limit_code(error) == "user_turn_limit_reached"


def test_unrelated_integrity_signal_is_not_misreported_as_admission_limit():
    error = SimpleNamespace(
        orig=SimpleNamespace(args=(1644, "model_quota_reservation_underflow"))
    )

    assert MySQLGatewayRepository._admission_limit_code(error) is None


def test_model_quota_limit_uses_the_stable_public_problem_code():
    assert AdmissionLimitError.MODEL_QUOTA_EXCEEDED == "quota_admission_denied"
