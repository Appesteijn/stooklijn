"""Tests voor QuattGasActiveSensor (binary_sensor.gasketel_actief)."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

from custom_components.quatt_stooklijn.binary_sensor import (
    QuattGasActiveSensor,
    async_setup_entry,
)


def _entry(**options) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {}
    entry.options = options
    return entry


class TestSetupIsNietAanGeluidGekoppeld:
    """De sensor hoort er te zijn ongeacht de geluidscompensatie.

    Hij hing achter ``sound_level_enabled`` omdat de compensatie zijn eerste
    afnemer was. Gevolg: wie de compensatie uitzette, raakte ook "draait de
    gasketel mee" kwijt — terwijl dat een eigenschap van de installatie is.
    """

    def _setup(self, entry) -> list:
        toegevoegd: list = []
        asyncio.run(
            async_setup_entry(MagicMock(), entry, lambda e: toegevoegd.extend(e))
        )
        return toegevoegd

    def test_zonder_geluidscompensatie_toch_aangemaakt(self):
        entities = self._setup(_entry(sound_level_enabled=False))
        assert len(entities) == 1
        assert isinstance(entities[0], QuattGasActiveSensor)

    def test_met_geluidscompensatie_ook(self):
        entities = self._setup(_entry(sound_level_enabled=True))
        assert len(entities) == 1

    def test_zonder_enige_optie_ook(self):
        assert len(self._setup(_entry())) == 1


class TestEntityId:
    """De entity-ID moet vastgepind zijn, niet door HA afgeleid.

    HA bouwt de ID voor een *nieuwe* entity op uit het gebied van het device.
    Staat het device in de bijkeuken, dan wordt het
    binary_sensor.bijkeuken_quatt_warmteanalyse_... en breekt de
    dashboardverwijzing. Voor iedereen met de geluidscompensatie uit is dit
    per definitie een nieuwe entity, dus die vlieger gaat hier echt op.
    """

    def test_init_pint_de_entity_id(self):
        src = inspect.getsource(QuattGasActiveSensor.__init__)
        assert "async_generate_entity_id" in src
        assert "gasketel_actief" in src

    def test_entity_id_krijgt_het_vaste_voorvoegsel(self):
        sensor = QuattGasActiveSensor(MagicMock(), _entry())
        assert sensor.entity_id == "binary_sensor.quatt_warmteanalyse_gasketel_actief"
