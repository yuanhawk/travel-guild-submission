"""
test_visa_crosscheck_2026.py — locks in the 2026 visa cross-check corrections
(adversarially verified). Each was the "Vietnam pattern": a
missing visa-free row wrongly charging an exempt traveller, or a stale fee.
Deterministic; no LLM/network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.compliance_agent import check_eligibility  # noqa: E402

TODAY = "2026-06-25"


def _fee(nat, dest):
    v = check_eligibility(legs=[{"leg_id": "0", "dest_country": dest, "departure_date": "2026-10-01"}],
                          nationality=nat, today=TODAY)
    return sum(int(x["usd_cents"]) for x in v.get("line_items", []))


# (nationality, destination, expected_total_usd_cents) — visa-free pairs that used to be charged
import pytest  # noqa: E402


@pytest.mark.parametrize("nat,dest,cents", [
    ("SG", "CN", 0), ("TH", "CN", 0), ("MY", "CN", 0), ("JP", "CN", 0),   # China 2024 exemptions
    ("GB", "CN", 0), ("CA", "CN", 0),   # China unilateral exemption from 2026-02-17 (E1 restore)
    ("SG", "KR", 0), ("MY", "KR", 0),                                       # Korea waiver
    ("NP", "IN", 0), ("BT", "IN", 0), ("MV", "IN", 0),                      # India neighbours
    ("SA", "AE", 0), ("QA", "AE", 0), ("KW", "AE", 0),                      # GCC → UAE
    ("SA", "TR", 0), ("AE", "TR", 0),                                       # GCC → Türkiye
    ("IL", "FR", 0), ("IL", "ES", 0), ("CO", "FR", 0), ("PE", "ES", 0),     # Schengen Annex II
])
def test_visa_free_pairs_not_charged(nat, dest, cents):
    assert _fee(nat, dest) == cents, f"{nat}->{dest} should be visa-free (${cents/100})"


@pytest.mark.parametrize("nat,dest,cents", [
    ("US", "KE", 3000),    # Kenya eTA $30 (was $34)
    ("US", "BR", 8081),    # Brazil e-Visa reinstated 2025 (was untabled)
])
def test_corrected_fees(nat, dest, cents):
    assert _fee(nat, dest) == cents, f"{nat}->{dest} fee should be ${cents/100}"


def test_non_exempt_still_charged():
    # the corrections must NOT make everyone free — non-exempt pairs still pay
    # E1 fix (was WRONG): China's visa-free list never covered the US, Mexico,
    # or South Africa (only a 240-hour transit exemption, not general tourist
    # entry) — these still require the full China Visa (VISA_REQUIRED, $140).
    # GB and CA were briefly (and wrongly) removed alongside US/MX/ZA in an
    # earlier pass; they are genuinely visa-free to China (unilateral
    # exemption in force since 2026-02-17) and are restored — see
    # test_visa_free_pairs_not_charged above.
    assert _fee("US", "CN") == 14000  # US still needs a China visa (E1)
    assert _fee("MX", "CN") == 14000  # Mexico still needs a China visa (E1)
    assert _fee("ZA", "CN") == 14000  # South Africa still needs a China visa (E1)
    assert _fee("IN", "CN") == 14000  # India still needs a China visa
    assert _fee("US", "IN") == 2500   # US still needs the India e-visa
