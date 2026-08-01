import pytest

from saferefund import ids


def test_identifier_prefixes_share_a_deterministic_sequence() -> None:
    ids.reset_counter_for_tests()

    assert ids.case_id() == "case_1"
    assert ids.refund_id() == "rfnd_2"
    assert ids.event_id() == "evt_3"
    assert ids.ticket_id() == "tkt_4"
    assert ids.verification_token() == "vtok_5"


def test_counter_can_start_at_a_specific_positive_value() -> None:
    ids.reset_counter_for_tests(start_at=42)

    assert ids.case_id() == "case_42"


def test_counter_rejects_non_positive_start() -> None:
    with pytest.raises(ValueError, match="one or greater"):
        ids.reset_counter_for_tests(start_at=0)
