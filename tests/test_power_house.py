"""Tests voor de kalibratie van het Power House-huismodel.

De getallen die hier uitkomen gaan rechtstreeks naar de regelaar, dus de
belangrijkste eigenschap is niet nauwkeurigheid maar terughoudendheid: bij
twijfel liever None dan een plausibel ogend getal.
"""

from __future__ import annotations

import pytest

from custom_components.quatt_stooklijn.power_house import (
    COLD_TEMP_MIN,
    SOURCE_CAPABILITY_CURVE,
    SOURCE_KNEE,
    T0_FROM_CONTROLLER,
    TEMP_GUARD_DELTA,
    ZERO_POWER_TEMP_MAX,
    calc_power_house_calibration,
)

# Marks eigen meting, augustus 2026: 418 dagen, R² 0,86.
HLC = 284.76284706922564
BALANCE = 16.66437805890929
CAP_SLOPE = 337.5
CAP_INTERCEPT = 6599.144024493845
KNEE_POWER = 5052.22


def _cal(**kwargs):
    base = dict(
        capability_slope=CAP_SLOPE,
        capability_intercept=CAP_INTERCEPT,
        knee_power=KNEE_POWER,
    )
    base.update(kwargs)
    return calc_power_house_calibration(HLC, BALANCE, **base)


class TestSnijpunt:
    """Tc is het snijpunt van vraaglijn en capaciteitslijn, geen instelling."""

    def test_gebruikt_de_capaciteitscurve_als_die_er_is(self):
        cal = _cal()
        assert cal.capacity_source == SOURCE_CAPABILITY_CURVE
        assert cal.cold_temp == -3.0

    def test_vraag_en_capaciteit_komen_samen_op_tc(self):
        """De hele afleiding staat of valt hiermee, dus expliciet nagerekend."""
        cal = _cal()
        vraag = HLC * (cal.zero_power_temp - cal.cold_temp)
        capaciteit = CAP_SLOPE * cal.cold_temp + CAP_INTERCEPT
        assert vraag == pytest.approx(capaciteit, abs=60)

    def test_pr_hoort_bij_de_afgeronde_t0_en_tc(self):
        """Anders klopt het drietal onderling niet met wat de firmware krijgt."""
        cal = _cal()
        assert cal.rated_power == pytest.approx(
            HLC * (cal.zero_power_temp - cal.cold_temp), abs=10
        )

    def test_valt_terug_op_de_knie_zonder_capaciteitscurve(self):
        cal = _cal(capability_slope=None, capability_intercept=None)
        assert cal.capacity_source == SOURCE_KNEE
        assert cal.cold_temp == -1.0

    def test_platte_knie_geeft_een_warmere_tc_dan_de_curve(self):
        """De knie negeert dat de capaciteit zelf met de kou meezakt."""
        met_curve = _cal().cold_temp
        met_knie = _cal(capability_slope=None, capability_intercept=None).cold_temp
        assert met_knie > met_curve


class TestAfronding:
    def test_temperaturen_op_halve_graden(self):
        cal = _cal()
        assert (cal.zero_power_temp * 2) % 1 == 0
        assert (cal.cold_temp * 2) % 1 == 0

    def test_vermogen_op_stappen_van_tien(self):
        assert _cal().rated_power % 10 == 0

    def test_t0_blijft_binnen_het_bereik_van_de_knop(self):
        cal = calc_power_house_calibration(
            HLC, 40.0, capability_slope=CAP_SLOPE, capability_intercept=CAP_INTERCEPT
        )
        assert cal.zero_power_temp == ZERO_POWER_TEMP_MAX

    def test_randgeval_wordt_wel_afgekapt(self):
        """Net buiten het bereik is een randgeval, geen onzin — dat mag naar -25."""
        # Capaciteit zo gekozen dat het snijpunt een paar graden onder -25 valt.
        cal = calc_power_house_calibration(
            HLC, BALANCE, capability_slope=0.0, capability_intercept=12500.0
        )
        assert cal is not None
        assert cal.cold_temp == COLD_TEMP_MIN


class TestWeigeren:
    """Bij twijfel geen advies — dit gaat naar de regelaar."""

    def test_zonder_warmteverlies_geen_advies(self):
        assert calc_power_house_calibration(None, BALANCE, knee_power=KNEE_POWER) is None

    def test_zonder_balanspunt_geen_advies(self):
        assert calc_power_house_calibration(HLC, None, knee_power=KNEE_POWER) is None

    def test_zonder_capaciteitsschatting_geen_advies(self):
        assert calc_power_house_calibration(HLC, BALANCE) is None

    def test_negatief_warmteverlies_geen_advies(self):
        assert calc_power_house_calibration(-100.0, BALANCE, knee_power=KNEE_POWER) is None

    def test_dalende_capaciteitslijn_valt_terug_op_de_knie(self):
        """Capaciteit die stijgt naarmate het kouder wordt is onfysisch."""
        cal = _cal(capability_slope=-400.0)
        assert cal.capacity_source == SOURCE_KNEE

    def test_onbereikbaar_vollastpunt_geeft_geen_advies(self):
        """Pompen die het altijd aankunnen hebben geen vollastpunt.

        Afkappen op -25 zou hier een plausibel ogend model opleveren dat overal
        te weinig vraagt — erger dan niets teruggeven.
        """
        assert (
            calc_power_house_calibration(
                HLC, BALANCE, capability_slope=10.0, capability_intercept=99000.0,
                knee_power=99000.0,
            )
            is None
        )


class TestFirmwareMarge:
    def test_tc_blijft_onder_t0_min_de_marge(self):
        """Zonder marge verwerpt de firmware het model en vraagt Power House niets."""
        cal = _cal()
        assert cal.cold_temp <= cal.zero_power_temp - TEMP_GUARD_DELTA


class TestKalibratieSensor:
    """De sensorlaag: tellen, weigeren, en niets verzinnen over lege states."""

    def _sensor(self, current: dict, targets: dict | None = None, data=True):
        from custom_components.quatt_stooklijn.analysis.heat_loss import HeatLossResult
        from custom_components.quatt_stooklijn.analysis.stooklijn import StooklijnResult
        from custom_components.quatt_stooklijn.coordinator import QuattStooklijnData
        from custom_components.quatt_stooklijn.sensor import (
            QuattPowerHouseCalibrationSensor,
        )

        payload = None
        if data:
            payload = QuattStooklijnData(
                stooklijn=StooklijnResult(
                    balance_temp_optimal=BALANCE,
                    slope_local=CAP_SLOPE,
                    intercept_local=CAP_INTERCEPT,
                    knee_power=KNEE_POWER,
                ),
                heat_loss_hp=HeatLossResult(
                    slope=-HLC, intercept=HLC * BALANCE,
                    heat_loss_coefficient=HLC, balance_point=BALANCE,
                ),
            )

        sensor = QuattPowerHouseCalibrationSensor.__new__(
            QuattPowerHouseCalibrationSensor
        )
        # __init__ overgeslagen: die vraagt een echte hass voor de entity-ID.
        sensor.coordinator = type("C", (), {"data": payload})()
        if targets is None:
            targets = {
                "zero_power_temp": "number.oq_t0",
                "cold_temp": "number.oq_tc",
                "rated_power": "number.oq_pr",
            }
        sensor._targets = lambda: targets
        sensor._current = lambda entity_id: current.get(entity_id)
        return sensor

    # Marks stand vóór kalibratie; advies is 16,5 / -3,0 / 5550.
    HUIDIG_ONGEKALIBREERD = {
        "number.oq_t0": 16.0,
        "number.oq_tc": -10.0,
        "number.oq_pr": 7020.0,
    }

    def _advised(self, current):
        """Wat het advies wordt gegeven de T0 die in de regelaar staat."""
        return calc_power_house_calibration(
            HLC, BALANCE,
            capability_slope=CAP_SLOPE, capability_intercept=CAP_INTERCEPT,
            knee_power=KNEE_POWER,
            controller_zero_power_temp=current["number.oq_t0"],
        )

    def test_telt_ook_de_stookgrens_als_die_ver_weg_staat(self):
        """T0 telt mee zodra de meting er iets over te zeggen heeft.

        Bij 16,0 vraagt de feedforward 189 W per stookdag te weinig — meer dan
        één knopstap kan corrigeren, dus stil blijven is dan een keuze voor een
        bekende fout.
        """
        sensor = self._sensor(self.HUIDIG_ONGEKALIBREERD)
        assert sensor.native_value == "3 aanpassingen aanbevolen"

    def test_stookgrens_binnen_een_knopstap_telt_niet(self):
        """Binnen één stap valt er niets te corrigeren, dus wordt er gezwegen.

        Zo dempt het advies zichzelf: het duwt T0 naar het gemeten balanspunt
        en houdt daar op, in plaats van elke analyse opnieuw een halve graad
        te blijven vragen.
        """
        cal = self._advised({"number.oq_t0": 16.5})
        sensor = self._sensor({
            "number.oq_t0": 16.5,
            "number.oq_tc": cal.cold_temp,
            "number.oq_pr": cal.rated_power,
        })
        assert cal.zero_power_temp_advised is False
        assert sensor.native_value == "model is gekalibreerd"

    def test_enkelvoud_bij_een_afwijking(self):
        current = {"number.oq_t0": 16.5, "number.oq_tc": None, "number.oq_pr": 7020.0}
        cal = self._advised(current)
        current["number.oq_tc"] = cal.cold_temp
        sensor = self._sensor(current)
        assert sensor.native_value == "1 aanpassing aanbevolen"

    def test_gekalibreerd_model_meldt_niets(self):
        # 16,5 ligt binnen één knopstap van het gemeten balanspunt, dus over de
        # stookgrens valt niets meer te zeggen; Tc en Pr volgen het advies.
        current = {"number.oq_t0": 16.5, "number.oq_tc": None, "number.oq_pr": None}
        cal = self._advised(current)
        current["number.oq_tc"] = cal.cold_temp
        current["number.oq_pr"] = cal.rated_power
        sensor = self._sensor(current)
        assert sensor.native_value == "model is gekalibreerd"

    def test_lege_huidige_waarde_telt_niet_als_afwijking(self):
        """Zonder vergelijkingswaarde is 'aanpassing nodig' een gok."""
        assert self._sensor({}).native_value == "model is gekalibreerd"

    def test_zonder_analyse_geen_advies(self):
        sensor = self._sensor(self.HUIDIG_ONGEKALIBREERD, data=False)
        assert sensor.native_value == "onvoldoende data"
        assert sensor.extra_state_attributes is None

    def test_zonder_openquatt_geen_advies(self):
        sensor = self._sensor(
            self.HUIDIG_ONGEKALIBREERD,
            targets={"zero_power_temp": None, "cold_temp": None, "rated_power": None},
        )
        assert sensor.native_value == "OpenQuatt niet gevonden"

    def test_attributen_dragen_de_doel_entiteiten_mee(self):
        """Zodat een automation niet op firmware-naamgeving hoeft te gokken."""
        attrs = self._sensor(self.HUIDIG_ONGEKALIBREERD).extra_state_attributes
        assert attrs["zero_power_temp_entity"] == "number.oq_t0"
        assert attrs["rated_power"] == 5550.0
        assert attrs["capaciteitsbron"] == SOURCE_CAPABILITY_CURVE
        assert attrs["zero_power_temp_huidig"] == 16.0

    def test_t0_wordt_overgenomen_zolang_hij_dichtbij_staat(self):
        attrs = self._sensor({**self.HUIDIG_ONGEKALIBREERD,
                              "number.oq_t0": 16.5}).extra_state_attributes
        assert attrs["zero_power_temp"] == 16.5
        assert attrs["zero_power_temp_bron"] == T0_FROM_CONTROLLER
        assert attrs["zero_power_temp_geadviseerd"] is False
        assert attrs["stookgrens_afwijking_w"] == 47

    def test_t0_wordt_geadviseerd_als_de_afwijking_meetbaar_is(self):
        attrs = self._sensor(self.HUIDIG_ONGEKALIBREERD).extra_state_attributes
        assert attrs["zero_power_temp"] == 16.5
        assert attrs["zero_power_temp_huidig"] == 16.0
        assert attrs["zero_power_temp_geadviseerd"] is True
        # Positief: het huis vraagt meer dan de feedforward aanbiedt.
        assert attrs["stookgrens_afwijking_w"] == 189
        assert "189 W" in attrs["toelichting"]

    def test_de_afwijking_kan_ook_de_andere_kant_op(self):
        """Een te hoge stookgrens laat de feedforward juist te veel vragen."""
        attrs = self._sensor({**self.HUIDIG_ONGEKALIBREERD,
                              "number.oq_t0": 18.0}).extra_state_attributes
        assert attrs["stookgrens_afwijking_w"] == -380
        assert attrs["zero_power_temp_geadviseerd"] is True

    def test_gemeten_balanspunt_blijft_zichtbaar(self):
        """Informatief houden, ook al wordt er niet naar geschreven."""
        attrs = self._sensor(self.HUIDIG_ONGEKALIBREERD).extra_state_attributes
        assert attrs["balanspunt_gemeten"] == pytest.approx(16.66, abs=0.01)

    def test_tc_en_pr_volgen_de_t0_van_de_regelaar(self):
        """Tc en Pr hangen van T0 af, dus ze moeten tegen dezelfde T0 gerekend
        worden als er in de firmware staat — anders klopt het drietal niet.

        Dit geldt binnen de band waarin de stookgrens niet geadviseerd wordt;
        daarbuiten hoort het drietal juist bij het *geadviseerde* nulpunt.
        """
        laag = self._sensor({**self.HUIDIG_ONGEKALIBREERD, "number.oq_t0": 16.5})
        hoog = self._sensor({**self.HUIDIG_ONGEKALIBREERD, "number.oq_t0": 17.0})
        assert laag.extra_state_attributes["rated_power"] < (
            hoog.extra_state_attributes["rated_power"]
        )

    def test_buiten_de_band_horen_tc_en_pr_bij_het_geadviseerde_nulpunt(self):
        """Anders levert het advies een drietal op dat onderling niet klopt."""
        ver_weg = self._sensor({**self.HUIDIG_ONGEKALIBREERD, "number.oq_t0": 14.0})
        op_advies = self._sensor({**self.HUIDIG_ONGEKALIBREERD, "number.oq_t0": 16.5})
        a, b = ver_weg.extra_state_attributes, op_advies.extra_state_attributes
        assert a["zero_power_temp"] == b["zero_power_temp"]
        assert a["cold_temp"] == b["cold_temp"]
        assert a["rated_power"] == b["rated_power"]


class TestEntityId:
    """De entity-ID moet vastgepind zijn, niet door HA afgeleid.

    HA bouwt de ID voor een nieuwe entity op uit het *gebied* van het device.
    Staat het device in de bijkeuken, dan wordt het
    sensor.bijkeuken_quatt_warmteanalyse_... en breekt elke dashboardverwijzing.
    Dit is precies wat er bij de eerste release van deze sensor gebeurde.
    """

    def test_init_pint_de_entity_id(self):
        import inspect
        from custom_components.quatt_stooklijn.sensor import (
            QuattPowerHouseCalibrationSensor,
        )

        src = inspect.getsource(QuattPowerHouseCalibrationSensor.__init__)
        assert "async_generate_entity_id" in src
        assert "openquatt_power_house_kalibratie" in src
