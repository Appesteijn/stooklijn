"""Tests voor het hoge-resolutie meetlogboek."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.quatt_stooklijn.highres_log import (
    MAX_SAMPLES,
    MIN_SPACING,
    SAMPLE_INTERVAL,
    HighResLog,
)

T0 = datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc)


def _fill(log: HighResLog, n: int, step: timedelta = SAMPLE_INTERVAL) -> HighResLog:
    for i in range(n):
        log.record(T0 + step * i, 20.0 + i * 0.01, 4.0, 2500.0, 0.0)
    return log


class TestRecord:
    def test_legt_monster_vast(self):
        log = HighResLog()
        assert log.record(T0, 20.4, 3.2, 2500.0, 120.0) is True
        assert len(log) == 1

    def test_rondt_af_voor_compactheid(self):
        """Ongeafgerond schrijft een float zeventien cijfers weg; over 25.920
        monsters is dat het verschil tussen een megabyte en het meervoud daarvan."""
        log = HighResLog()
        log.record(T0, 20.123456789, 3.987654321, 2500.6789, 120.456789)
        assert log.to_dict()["samples"][0][1:] == [20.12, 3.99, 2501, 120.5]

    def test_slaat_te_dichte_monsters_over(self):
        """Een herstart-storm of handmatige aanroep mag het venster niet
        verkorten door de ringbuffer vol te pompen met bijna-duplicaten."""
        log = HighResLog()
        assert log.record(T0, 20.0, 4.0, 0.0, 0.0) is True
        assert log.record(T0 + MIN_SPACING / 2, 20.0, 4.0, 0.0, 0.0) is False
        assert log.record(T0 + MIN_SPACING, 20.0, 4.0, 0.0, 0.0) is True
        assert len(log) == 2


class TestRingbuffer:
    def test_houdt_maximum_aan(self):
        log = _fill(HighResLog(max_samples=10), 25)
        assert len(log) == 10

    def test_gooit_de_oudste_weg(self):
        log = _fill(HighResLog(max_samples=5), 12)
        eerste_ts = log.to_dict()["samples"][0][0]
        assert eerste_ts == int((T0 + SAMPLE_INTERVAL * 7).timestamp())

    def test_venster_overspant_een_stookseizoen(self):
        """De hele reden voor dit logboek: in maart nog bij januari kunnen."""
        assert MAX_SAMPLES * SAMPLE_INTERVAL >= timedelta(days=88)


class TestSpan:
    def test_span_is_none_bij_te_weinig_monsters(self):
        assert HighResLog().span_hours is None
        assert _fill(HighResLog(), 1).span_hours is None

    def test_span_in_uren(self):
        log = _fill(HighResLog(), 13)  # 12 intervallen van 5 min = 1 uur
        assert log.span_hours == pytest.approx(1.0)


class TestSerialisatie:
    def test_roundtrip(self):
        log = _fill(HighResLog(), 20)
        terug = HighResLog.from_dict(log.to_dict())
        assert len(terug) == 20
        assert terug.to_dict() == log.to_dict()

    def test_kolomnamen_staan_erbij(self):
        """Zonder kolomnamen is een kale lijst getallen over een half jaar
        niet meer terug te lezen."""
        assert HighResLog().to_dict()["columns"] == [
            "ts", "t_indoor", "t_outdoor", "q_hp_w", "q_solar_wm2",
        ]

    def test_corrupte_rijen_worden_overgeslagen(self):
        """Een half geschreven bestand mag niet het hele venster kosten."""
        goed = [int(T0.timestamp()), 20.0, 4.0, 2500.0, 0.0]
        data = {"samples": [goed, [1, 2, 3], "onzin", None,
                            [int(T0.timestamp()) + 300, "x", 4.0, 0.0, 0.0], goed]}
        assert len(HighResLog.from_dict(data)) == 2

    def test_leeg_bestand(self):
        assert len(HighResLog.from_dict({})) == 0

    def test_maximum_wordt_gerespecteerd_bij_inlezen(self):
        log = _fill(HighResLog(), 50)
        assert len(HighResLog.from_dict(log.to_dict(), max_samples=10)) == 10
