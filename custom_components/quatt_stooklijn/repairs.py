"""Reparatiestroom voor het dashboard.

Wordt aangesproken wanneer er een nieuwe versie van het meegeleverde dashboard
is, maar het dashboard van de gebruiker afwijkt van wat wij er zelf op gezet
hebben. Dan overschrijven we niet uit onszelf — zie de toelichting in
``dashboard.py`` — maar vragen we het.

Bewust een menu en geen bevestigingsformulier. Een reparatiemelding met
``is_fixable`` opent meteen de flow, en een formulier met een leeg schema geeft
de gebruiker maar één knop: Submit. Wie zijn eigen dashboard wil houden kan het
dialoog dan alleen wegklikken, waarna de melding bij de volgende herstart
terugkomt — "nee" is dan geen antwoord maar uitstel. Met een menu zijn beide
uitkomsten een echte keuze, en beide sluiten de melding af.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .dashboard import DashboardManager


class DashboardUpdateRepairFlow(RepairsFlow):
    """Laat de gebruiker kiezen: de nieuwe versie, of zijn eigen dashboard."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        return self.async_show_menu(menu_options=["update", "keep"])

    async def async_step_update(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Neem de meegeleverde versie over; eigen aanpassingen gaan verloren."""
        await DashboardManager(self.hass).async_force_update()
        return self.async_create_entry(data={})

    async def async_step_keep(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Houd het eigen dashboard en bied deze versie niet opnieuw aan."""
        await DashboardManager(self.hass).async_decline_update()
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Bouw de reparatiestroom voor een melding van deze integratie."""
    return DashboardUpdateRepairFlow()
