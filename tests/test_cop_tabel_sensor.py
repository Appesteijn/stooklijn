"""De sensor "geschatte actuele COP" rekent met de COP-tabel, niet met losse dagen."""

import pytest

from custom_components.quatt_stooklijn.analysis.heat_loss import cop_from_table

TABEL = {-10: {"heat": 7457, "cop": None}, -5: {"heat": 6060, "cop": 1.99},
         0: {"heat": 4662, "cop": 2.43}, 5: {"heat": 3265, "cop": 3.59},
         10: {"heat": 1868, "cop": None}, 15: {"heat": 470, "cop": 4.80}}


def test_tussen_twee_buren():
    # Testhuis 27-11-2026 bij −2 °C.
    assert cop_from_table(TABEL, -2.0) == pytest.approx(1.99 + 0.44 * 3 / 5)


def test_op_een_tabelpunt():
    assert cop_from_table(TABEL, 0.0) == 2.43


@pytest.mark.parametrize("temp", [-6.0, -12.0, 20.0])
def test_buiten_de_tabel_niets(temp):
    assert cop_from_table(TABEL, temp) is None


def test_niet_over_een_gat():
    # 5 → 15 °C slaat de lege 10 over: liever niets dan een gok.
    assert cop_from_table(TABEL, 8.0) is None


def test_sleutels_als_tekst():
    # Zo staan ze in de attributen van HA.
    assert cop_from_table({str(k): v for k, v in TABEL.items()}, -2.5) == pytest.approx(2.21)


@pytest.mark.parametrize("tabel", [None, {}, {0: {"cop": 0}}])
def test_leeg(tabel):
    assert cop_from_table(tabel, 0.0) is None
