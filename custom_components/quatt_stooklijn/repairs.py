"""Reparatiestroom voor het dashboard.

Wordt aangesproken wanneer er een nieuwe versie van het meegeleverde dashboard
is, maar het dashboard van de gebruiker afwijkt van wat wij er zelf op gezet
hebben. Dan overschrijven we niet uit onszelf — zie de toelichting in
``dashboard.py`` — maar vragen we het.

De stap is bewust een bevestiging met een waarschuwing erin: bevestigen kost de
gebruiker zijn eigen aanpassingen aan dit dashboard.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .dashboard import DashboardManager


class DashboardUpdateRepairFlow(RepairsFlow):
    """Vraagt of het meegeleverde dashboard teruggezet mag worden."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        if user_input is not None:
            await DashboardManager(self.hass).async_force_update()
            return self.async_create_entry(data={})

        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Bouw de reparatiestroom voor een melding van deze integratie."""
    return DashboardUpdateRepairFlow()
