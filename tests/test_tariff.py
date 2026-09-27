"""Tests voor de uurprijzen: normaal/dal en dynamisch uit sensorattributen.

De valkuilen zitten in de tijd: een DST-nacht, het weekend, kwartierprijzen, en
een reeks die halverwege ophoudt omdat de prijzen van morgen er nog niet zijn.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.quatt_stooklijn.analysis.tariff import (
    MIN_PRICED_HOURS,
    hourly_prices_from_attributes,
    is_dal,
    normal_dal_prices,
)

AMS = ZoneInfo("Europe/Amsterdam")


class TestIsDal:
    # 2026-10-07 is een woensdag, 2026-10-10 een zaterdag.
    @pytest.mark.parametrize(
        ("uur", "verwacht"),
        [(22, False), (23, True), (0, True), (6, True), (7, False), (12, False)],
    )
    def test_doordeweeks_over_middernacht(self, uur, verwacht):
        assert is_dal(datetime(2026, 10, 7, uur, tzinfo=AMS)) is verwacht

    def test_weekend_is_de_hele_dag_dal(self):
        assert is_dal(datetime(2026, 10, 10, 12, tzinfo=AMS))

    def test_weekend_uit(self):
        assert not is_dal(datetime(2026, 10, 10, 12, tzinfo=AMS), weekend_dal=False)

    def test_21_tot_7(self):
        assert is_dal(datetime(2026, 10, 7, 21, tzinfo=AMS), dal_start_hour=21)

    def test_periode_binnen_de_dag(self):
        assert is_dal(datetime(2026, 10, 7, 13, tzinfo=AMS), 12, 16)
        assert not is_dal(datetime(2026, 10, 7, 16, tzinfo=AMS), 12, 16)

    def test_gelijke_uren_betekent_geen_dal(self):
        assert not is_dal(datetime(2026, 10, 7, 3, tzinfo=AMS), 7, 7)


class TestNormaalDal:
    def test_een_doordeweekse_dag(self):
        start = datetime(2026, 10, 7, 0, tzinfo=AMS)
        p = normal_dal_prices(start, 24, 0.25, 0.23, tz=AMS)
        assert len(p) == 24
        assert p[:7] == [0.23] * 7
        assert p[7:23] == [0.25] * 16
        assert p[23] == 0.23

    def test_wintertijdnacht_telt_25_lokale_uren(self):
        """In de nacht van 25 oktober 2026 gaat de klok terug.

        24 uur vanaf middernacht eindigt dan om 23:00 lokale tijd, en 02:00
        komt twee keer voor. Beide keren dal, en het laatste uur (23:00) ook.
        Het is een zondag, dus alles is dal; met weekend uit is het 7 + 1 dal.
        """
        start = datetime(2026, 10, 25, 0, tzinfo=AMS)
        p = normal_dal_prices(start, 24, 0.25, 0.23, weekend_dal=False, tz=AMS)
        assert len(p) == 24
        # 00, 01, 02, 02, 03, 04, 05, 06 = acht uren dal voor 07:00
        assert p[:8] == [0.23] * 8
        assert p[8] == 0.25
        # Het laatste uur van de reeks is 22:00 lokaal: nog normaal.
        assert p[-1] == 0.25

    def test_zonder_tijdzone_wordt_die_aangenomen(self):
        p = normal_dal_prices(datetime(2026, 10, 7, 0), 3, 0.25, 0.23, tz=AMS)
        assert p == [0.23] * 3


def _nordpool(start: datetime, waarden: list[float], stap_min: int = 60):
    """Zoals de Nord Pool-integratie ``raw_today`` aanlevert."""
    return [
        {
            "start": start + timedelta(minutes=stap_min * i),
            "end": start + timedelta(minutes=stap_min * (i + 1)),
            "value": v,
        }
        for i, v in enumerate(waarden)
    ]


class TestDynamisch:
    START = datetime(2026, 10, 7, 0, tzinfo=AMS)

    def test_nordpool_uurprijzen(self):
        waarden = [0.20 + 0.01 * i for i in range(24)]
        attrs = {"raw_today": _nordpool(self.START, waarden), "today": waarden}
        p = hourly_prices_from_attributes(attrs, self.START, 24, tz=AMS)
        assert p == pytest.approx(waarden)

    def test_start_halverwege_een_uur(self):
        waarden = [0.20 + 0.01 * i for i in range(24)]
        attrs = {"raw_today": _nordpool(self.START, waarden)}
        p = hourly_prices_from_attributes(
            attrs, self.START + timedelta(hours=2, minutes=5), 12, tz=AMS
        )
        assert p[0] == pytest.approx(0.22)

    def test_kwartierprijzen_worden_per_uur_gemiddeld(self):
        waarden = [0.10, 0.20, 0.30, 0.40] * 8
        attrs = {"raw_today": _nordpool(self.START, waarden, stap_min=15)}
        p = hourly_prices_from_attributes(attrs, self.START, 8, tz=AMS)
        assert p == pytest.approx([0.25] * 8)

    def test_iso_strings_met_z(self):
        # 2026-10-06T22:00Z is 00:00 lokaal op de 7e.
        attrs = {
            "prices": [
                {
                    "time": (datetime(2026, 10, 6, 22, tzinfo=timezone.utc)
                             + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
                    "price": 0.3 + i / 100,
                }
                for i in range(8)
            ]
        }
        p = hourly_prices_from_attributes(attrs, self.START, 8, tz=AMS)
        assert p == pytest.approx([0.3 + i / 100 for i in range(8)])

    def test_eenheid_cent(self):
        attrs = {"raw_today": _nordpool(self.START, [25.0] * 24)}
        p = hourly_prices_from_attributes(
            attrs, self.START, 24, unit="ct/kWh", tz=AMS
        )
        assert p == pytest.approx([0.25] * 24)

    def test_eenheid_mwh(self):
        attrs = {"raw_today": _nordpool(self.START, [250.0] * 24)}
        p = hourly_prices_from_attributes(
            attrs, self.START, 24, unit="EUR/MWh", tz=AMS
        )
        assert p == pytest.approx([0.25] * 24)

    def test_stopt_bij_het_eerste_gat(self):
        """Morgen nog niet bekend: inkorten, niet opvullen."""
        attrs = {"raw_today": _nordpool(self.START, [0.2] * 10)}
        p = hourly_prices_from_attributes(attrs, self.START, 24, tz=AMS)
        assert len(p) == 10

    def test_te_kort_geeft_niets(self):
        attrs = {"raw_today": _nordpool(self.START, [0.2] * (MIN_PRICED_HOURS - 1))}
        assert hourly_prices_from_attributes(attrs, self.START, 24, tz=AMS) == []

    def test_vandaag_en_morgen_samen(self):
        vandaag = _nordpool(self.START, [0.2] * 24)
        morgen = _nordpool(self.START + timedelta(days=1), [0.3] * 24)
        attrs = {"raw_today": vandaag, "raw_tomorrow": morgen}
        p = hourly_prices_from_attributes(
            attrs, self.START + timedelta(hours=14), 24, tz=AMS
        )
        assert p[:10] == pytest.approx([0.2] * 10)
        assert p[10:] == pytest.approx([0.3] * 14)

    def test_negatieve_prijs_blijft_negatief(self):
        """De weging klemt, de reeks zelf niet — de kosten horen te kloppen."""
        attrs = {"raw_today": _nordpool(self.START, [-0.05] + [0.2] * 23)}
        p = hourly_prices_from_attributes(attrs, self.START, 24, tz=AMS)
        assert p[0] == pytest.approx(-0.05)

    def test_onzin_attributen_worden_genegeerd(self):
        attrs = {
            "friendly_name": "Prijs",
            "raw_today": _nordpool(self.START, [0.2] * 24),
            "extra": [{"foo": 1}, "bar", 3],
        }
        assert len(hourly_prices_from_attributes(attrs, self.START, 24, tz=AMS)) == 24
