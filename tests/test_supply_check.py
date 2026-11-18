"""Tests voor de controle op de aanvoertemperatuursensor.

Aanleiding (18-11-2026): de PT1000 van een niet-gekalibreerde OpenQuatt las
0,65–0,81 K boven de uitgang van HP2. Elk aanvoeradvies leek daardoor 0,77 K te
laag, en de foutsensoren toonden dat als modelfout.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from custom_components.quatt_stooklijn import supply_check as sc
from custom_components.quatt_stooklijn.supply_check import (
    MIN_SAMPLES,
    SAMPLE_SPACING_S,
    STABLE_S,
    OffsetTracker,
    Reading,
    SupplySensorCheck,
)

MIN = 60


def _r(offset=0.66, heat=3000.0, boiler=0.0, flow=800.0, running=(True, False)):
    return Reading(
        supply=30.0 + offset, reference=30.0, flow=flow,
        heat_w=heat, boiler_w=boiler, running=running,
    )


def _feed(tracker, minutes, start=0, **kw):
    """Elke minuut een meting, zoals de echte tik."""
    added = 0
    for m in range(minutes):
        added += tracker.observe(start + m * MIN, _r(**kw))
    return added


class TestStabiliteit:

    def test_eerste_kwartier_telt_niet(self):
        t = OffsetTracker()
        assert _feed(t, STABLE_S // MIN) == 0

    def test_daarna_om_de_vijf_minuten(self):
        t = OffsetTracker()
        added = _feed(t, STABLE_S // MIN + 60)
        # minuut 15, 20, …, 70
        assert added == 12

    def test_ontdooien_zet_de_klok_terug(self):
        t = OffsetTracker()
        _feed(t, 30)
        n = len(t.samples)
        t.observe(30 * MIN, _r(heat=-500.0))   # ontdooicyclus
        assert _feed(t, 14, start=31 * MIN) == 0
        assert len(t.samples) == n

    def test_tweede_unit_erbij_zet_de_klok_terug(self):
        t = OffsetTracker()
        _feed(t, 30)
        assert _feed(t, 14, start=30 * MIN, running=(True, True)) == 0
        assert _feed(t, 5, start=45 * MIN, running=(True, True)) > 0

    def test_gasketel_stookt_mee(self):
        t = OffsetTracker()
        assert _feed(t, 120, boiler=4000.0) == 0

    def test_geen_debiet(self):
        t = OffsetTracker()
        assert _feed(t, 120, flow=100.0) == 0

    def test_compressoren_onbekend(self):
        t = OffsetTracker()
        assert _feed(t, 120, running=None) == 0

    def test_ontbrekende_referentie(self):
        t = OffsetTracker()
        _feed(t, 20)
        assert not t.observe(30 * MIN, Reading(31.0, None, 800.0, 3000.0, 0.0, (True,)))


class TestOordeel:

    def _vol(self, offset=0.66):
        t = OffsetTracker()
        _feed(t, STABLE_S // MIN + MIN_SAMPLES * SAMPLE_SPACING_S // MIN, offset=offset)
        return t

    def test_te_weinig_monsters_geen_oordeel(self):
        t = OffsetTracker()
        _feed(t, STABLE_S // MIN + 60)
        assert t.offset() is None

    def test_mediaan(self):
        assert self._vol(0.66).offset() == pytest.approx(0.66)

    def test_uitschieter_verandert_niets(self):
        t = self._vol(0.66)
        last = t.samples[-1][0]
        t.observe(last + SAMPLE_SPACING_S, _r(offset=5.0))
        assert t.offset() == pytest.approx(0.66)

    def test_kalibratie_wist_de_oude_monsters(self):
        """Na een sprong tellen alleen de nieuwe monsters, anders duurt het een dag."""
        t = OffsetTracker()
        minuten = STABLE_S // MIN + MIN_SAMPLES * SAMPLE_SPACING_S // MIN
        _feed(t, minuten, offset=0.66)
        # Doorlopend stoken, alleen de sensor verspringt.
        _feed(t, sc.STEP_RECENT * SAMPLE_SPACING_S // MIN, start=minuten * MIN, offset=0.02)
        assert len(t.samples) == sc.STEP_RECENT
        assert all(abs(v - 0.02) < 1e-9 for _, v in t.samples)
        assert t.offset() is None   # opnieuw verzamelen tot MIN_SAMPLES

    def test_oude_monsters_vervallen(self):
        t = self._vol(0.66)
        later = t.samples[-1][0] + sc.MAX_AGE_S + 3600
        _feed(t, STABLE_S // MIN + 1, start=later)
        assert len(t.samples) == 1

    def test_bewaren_en_terugladen(self):
        t = self._vol(0.66)
        terug = OffsetTracker(t.as_list())
        assert terug.offset() == pytest.approx(0.66, abs=0.001)


@pytest.fixture
def issues(monkeypatch):
    ir = ModuleType("homeassistant.helpers.issue_registry")
    ir.async_create_issue = MagicMock()
    ir.async_delete_issue = MagicMock()
    ir.IssueSeverity = MagicMock(WARNING="warning")
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", ir)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "issue_registry", ir, raising=False)
    return ir


def _check(offset):
    check = SupplySensorCheck.__new__(SupplySensorCheck)
    check._hass = MagicMock()
    check._entry_id = "e1"
    check._pair = ("sensor.aanvoer", "sensor.hp2_uit")
    check._reported = None
    check.tracker = MagicMock()
    check.tracker.offset.return_value = offset
    check.tracker.samples = [None] * MIN_SAMPLES
    return check


class TestMelding:

    def test_afwijking_geeft_melding(self, issues):
        check = _check(0.66)
        check._async_evaluate()
        issues.async_create_issue.assert_called_once()
        kw = issues.async_create_issue.call_args.kwargs
        assert kw["translation_key"] == "supply_sensor_offset"
        assert kw["translation_placeholders"]["offset"] == "+0.66"
        assert kw["translation_placeholders"]["supply_entity"] == "sensor.aanvoer"
        assert kw["is_fixable"] is False

    def test_negatieve_afwijking_ook(self, issues):
        _check(-0.5)._async_evaluate()
        issues.async_create_issue.assert_called_once()

    def test_niet_elke_minuut_opnieuw(self, issues):
        check = _check(0.66)
        check._async_evaluate()
        check.tracker.offset.return_value = 0.68
        check._async_evaluate()
        assert issues.async_create_issue.call_count == 1

    def test_klein_verschil_geen_melding(self, issues):
        _check(0.08)._async_evaluate()
        issues.async_create_issue.assert_not_called()

    def test_hysterese(self, issues):
        check = _check(0.66)
        check._async_evaluate()
        check.tracker.offset.return_value = 0.25    # tussen de grenzen: blijft staan
        check._async_evaluate()
        issues.async_delete_issue.assert_not_called()
        check.tracker.offset.return_value = 0.05    # na kalibratie
        check._async_evaluate()
        issues.async_delete_issue.assert_called_once()

    def test_zonder_oordeel_niets(self, issues):
        _check(None)._async_evaluate()
        issues.async_create_issue.assert_not_called()
        issues.async_delete_issue.assert_not_called()
