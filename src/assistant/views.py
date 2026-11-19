"""Owner-only ✅/❌ buttons: tool approvals and /delete confirmation."""

from __future__ import annotations

from dataclasses import dataclass

import discord

APPROVAL_TIMEOUT_SECONDS = 10 * 60


@dataclass(frozen=True)
class Wording:
    approve: str = "Approve"
    deny: str = "Deny"
    approved: str = "✅ approved"
    denied: str = "❌ denied"


class ApprovalView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float = APPROVAL_TIMEOUT_SECONDS,
                 wording: Wording = Wording()) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.approved: bool | None = None
        self.wording = wording
        self.approve.label, self.deny.label = wording.approve, wording.deny

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Approve", emoji="✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button[ApprovalView]) -> None:
        await self._finish(interaction, True)

    @discord.ui.button(label="Deny", emoji="❌", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button[ApprovalView]) -> None:
        await self._finish(interaction, False)

    async def _finish(self, interaction: discord.Interaction, approved: bool) -> None:
        self.approved = approved
        self.stop()
        content = interaction.message.content if interaction.message else ""
        outcome = self.wording.approved if approved else self.wording.denied
        await interaction.response.edit_message(content=f"{content}\n→ {outcome}", view=None)
