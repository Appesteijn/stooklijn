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


class TestKlok:
    def test_terugspringende_klok_blokkeert_het_logboek_niet(self):
        """NTP-correctie na een herstart zonder RTC. Zonder deze uitzondering
        ligt het logboek stil tot de echte tijd het oude laatste tijdstip
        heeft ingehaald — bij een sprong van een half uur dus een half uur."""
        log = HighResLog()
        log.record(T0, 20.0, 4.0, 0.0, 0.0)
        assert log.record(T0 - timedelta(minutes=30), 20.0, 4.0, 0.0, 0.0) is True
        n = sum(log.record(T0 - timedelta(minutes=30) + SAMPLE_INTERVAL * i,
                           20.0, 4.0, 0.0, 0.0) for i in range(1, 5))
        assert n == 4
        assert len(log) == 6

    def test_span_klopt_als_de_reeks_niet_op_volgorde_staat(self):
        log = HighResLog()
        log.record(T0 + timedelta(hours=2), 20.0, 4.0, 0.0, 0.0)
        log.record(T0, 20.0, 4.0, 0.0, 0.0)
        assert log.span_hours == pytest.approx(2.0)


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


class TestStore:
    """De store mag nooit een winter aan data wegvagen na een leesfout."""

    def _store(self, load_result):
        from unittest.mock import AsyncMock, MagicMock, patch
        from custom_components.quatt_stooklijn.highres_log import HighResLogStore
        with patch("custom_components.quatt_stooklijn.highres_log.Store") as St:
            inst = St.return_value
            if isinstance(load_result, BaseException):
                inst.async_load = AsyncMock(side_effect=load_result)
            else:
                inst.async_load = AsyncMock(return_value=load_result)
            inst.async_save = AsyncMock()
            return HighResLogStore(MagicMock()), inst

    def test_schrijft_niet_weg_na_een_mislukte_lezing(self):
        """Het geval dat telt: HA's Store gooit zelf, bijvoorbeeld op ongeldige
        JSON of een I/O-fout. Zou de store daarna gewoon opslaan, dan wist de
        eerstvolgende uurtick een heel stookseizoen."""
        import asyncio
        store, inner = self._store(OSError("schijf weg"))
        with pytest.raises(OSError):
            asyncio.run(store.async_load())
        store.record(T0, 20.0, 4.0, 0.0, 0.0)
        asyncio.run(store.async_save())
        inner.async_save.assert_not_awaited()

    def test_rommelige_rijen_zijn_geen_leesfout(self):
        """from_dict slaat onbruikbare rijen over in plaats van te gooien, dus
        dit telt als een geslaagde lezing en opslaan mag gewoon."""
        import asyncio
        store, inner = self._store({"samples": ["onzin", None, [1, 2]]})
        asyncio.run(store.async_load())
        assert len(store.log) == 0
        store.record(T0, 20.0, 4.0, 0.0, 0.0)
        asyncio.run(store.async_save())
        inner.async_save.assert_awaited_once()

    def test_schrijft_wel_na_een_goede_lezing(self):
        import asyncio
        store, inner = self._store(None)
        asyncio.run(store.async_load())
        store.record(T0, 20.0, 4.0, 0.0, 0.0)
        asyncio.run(store.async_save())
        inner.async_save.assert_awaited_once()

    def test_schrijft_niets_zonder_nieuwe_monsters(self):
        import asyncio
        store, inner = self._store(None)
        asyncio.run(store.async_load())
        asyncio.run(store.async_save())
        inner.async_save.assert_not_awaited()
