"""Discord dungeon checklist, stored in SQLite."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import re
import sqlite3

import discord
from discord import app_commands

from database import Store

BASE = Path(__file__).resolve().parent
LOG = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()


def safe(value):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))


def player_name(value):
    """Raw text for an ANSI block, without control sequences or Markdown closing."""
    text = str(value).replace("\n", " ").replace("\r", " ")
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text).replace("`", "ˋ")


def green_names(text):
    if not text:
        return " "
    return f"```ansi\n\x1b[1;32m{text}\x1b[0m\n```"


def pages(lines, limit=1700):
    result, current = [], ""
    for line in lines:
        for start in range(0, max(1, len(line)), limit):
            part = line[start:start + limit]
            if current and len(current) + len(part) + 1 > limit:
                result.append(current)
                current = ""
            current += ("\n" if current else "") + part
    if current:
        result.append(current)
    return result or ["No data available."]


class PrivatePages(discord.ui.View):
    def __init__(self, contents):
        super().__init__(timeout=300)
        self.contents, self.index = contents, 0
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.index == 0
        self.next_page.disabled = self.index == len(self.contents) - 1

    def content(self):
        return self.contents[self.index] + f"\n\n*Page {self.index + 1}/{len(self.contents)}*"

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.index = max(0, self.index - 1)
        self.update_buttons()
        await interaction.response.edit_message(content=self.content(), view=self, allowed_mentions=NO_MENTIONS)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        self.index = min(len(self.contents) - 1, self.index + 1)
        self.update_buttons()
        await interaction.response.edit_message(content=self.content(), view=self, allowed_mentions=NO_MENTIONS)


async def private_list(interaction, lines):
    view = PrivatePages(pages(lines))
    await interaction.response.send_message(view.content(), view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def interaction_error(interaction):
    text = "An error occurred. Reopen My Choices to check your selections."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


class ActionButton(discord.ui.Button):
    def __init__(self, bot, action, label):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=f"donjon:v2:summary:{action}")
        self.bot, self.action = bot, action

    async def callback(self, interaction):
        try:
            await self.handle(interaction)
        except Exception:
            LOG.exception("Dungeon interaction failed")
            await interaction_error(interaction)

    async def handle(self, interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this board in a server.", ephemeral=True)
            return
        bot = self.bot
        if self.action == "edit":
            await interaction.response.send_message(
                "**My Choices**\nSelect completed dungeons. Deselect a dungeon to remove it. Each menu is saved separately.",
                view=VoteView(bot, interaction.guild_id, interaction.user.id),
                ephemeral=True, allowed_mentions=NO_MENTIONS,
            )
            return
        rows = bot.store.votes(interaction.guild_id)
        lines = []
        if self.action == "players":
            for d in bot.catalogue.values():
                lines.append(f"\n**{safe(d['name'])}**")
                lines.extend([green_names(player_name(r['name'])) for r in rows if r['dungeon'] == d['id'] and r['done']] or [green_names("")])
        elif self.action == "personal":
            mine = {r['dungeon'] for r in rows if r['user'] == interaction.user.id and r['done']}
            lines.append("**Your Completed Dungeons**")
            lines.extend([safe(d['name']) for d in bot.catalogue.values() if d['id'] in mine] or ["None"])
        await private_list(interaction, lines)


class DungeonSelect(discord.ui.Select):
    def __init__(self, bot, dungeons, index, mine):
        self.bot = bot
        self.scope = [d['id'] for d in dungeons]
        super().__init__(
            placeholder=f"Completed ({index * 25 + 1}–{index * 25 + len(dungeons)})",
            custom_id=f"donjon:v4:done:{index}", min_values=0, max_values=len(dungeons),
            options=[discord.SelectOption(label=d['name'], value=d['id'], default=d['id'] in mine) for d in dungeons],
        )

    async def callback(self, interaction):
        await interaction.response.defer()
        async with self.bot.board_lock:
            self.bot.store.set_selection(
                interaction.guild_id, interaction.user.id, interaction.user.display_name, self.scope, self.values,
            )
            success = await self.bot.refresh_guild(interaction.guild_id)
        for option in self.options:
            option.default = option.value in self.values
        message = "Choices saved. You can modify other menus."
        if not success:
            message += " Some boards could not be refreshed."
        await interaction.edit_original_response(content=message, view=self.view, allowed_mentions=NO_MENTIONS)


class VoteView(discord.ui.View):
    def __init__(self, bot, guild, user):
        super().__init__(timeout=600)
        self.guild, self.user = guild, user
        mine = {r['dungeon'] for r in bot.store.votes(guild) if r['user'] == user and r['done']}
        dungeons = list(bot.catalogue.values())
        for index, start in enumerate(range(0, len(dungeons), 25)):
            self.add_item(DungeonSelect(bot, dungeons[start:start + 25], index, mine))

    async def interaction_check(self, interaction):
        return interaction.guild_id == self.guild and interaction.user.id == self.user

    async def on_error(self, interaction, error, item):
        LOG.error("Dungeon selection failed", exc_info=(type(error), error, error.__traceback__))
        await interaction_error(interaction)


class SummaryView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.add_item(ActionButton(bot, "edit", "My Choices"))
        self.add_item(ActionButton(bot, "personal", "My Completed Dungeons"))
        self.add_item(ActionButton(bot, "players", "Players per Dungeon"))


class MonBot(discord.Client):
    def __init__(self, *, db_path=None):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=NO_MENTIONS)
        self.store = Store(db_path if db_path is not None else os.environ.get("DONJONS_DB", str(BASE / "donjons.sqlite3")))
        self.catalogue = self.store.catalogue()
        self.tree = app_commands.CommandTree(self)
        self.board_lock = asyncio.Lock()
        self.tree.add_command(checklist_donjons)

    def summary(self, guild):
        rows = self.store.votes(guild)
        entries = []
        for d in self.catalogue.values():
            names = [player_name(r['name']) for r in rows if r['dungeon'] == d['id'] and r['done']]
            entries.append((f"**{safe(d['name'])}** ({d['min_level']}–{d['max_level']})", names))

        def render(limit):
            lines = []
            for title, names in entries:
                kept = []
                for name in names:
                    candidate = ", ".join(kept + [name])
                    suffix = f" … +{len(names) - len(kept) - 1}"
                    if len(candidate) + len(suffix) > limit:
                        break
                    kept.append(name)
                text = ", ".join(kept)
                if len(kept) < len(names):
                    text += f" … +{len(names) - len(kept)}"
                lines.append(title + "\n" + green_names(text.strip()))
            return "\n".join(lines)

        limit = 250
        description = render(limit)
        while len(description) > 4096 and limit > 0:
            limit -= 1
            description = render(limit)
        if len(description) > 4096:
            raise ValueError("The catalogue exceeds Discord's maximum embed description size.")
        embed = discord.Embed(title="Dungeons", description=description, color=discord.Color.gold())
        embed.set_footer(text="My Choices: modify completed dungeons · Players per dungeon: complete list.")
        return embed

    async def refresh_board(self, guild, channel_id, *, create=False):
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if channel.guild.id != guild:
            raise ValueError("Channel associated with the wrong server")
        embed, view = self.summary(guild), SummaryView(self)
        message_id = self.store.message(guild, channel_id)
        if message_id:
            try:
                await channel.get_partial_message(message_id).edit(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
                return
            except discord.NotFound:
                pass
        if create:
            message = await channel.send(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
            self.store.set_message(guild, channel_id, message.id)

    async def refresh_guild(self, guild):
        success = True
        for g, channel in self.store.boards():
            if g != guild:
                continue
            try:
                await self.refresh_board(g, channel)
            except (discord.HTTPException, ValueError):
                LOG.exception("Failed to refresh board %s", channel)
                success = False
        return success

    async def setup_hook(self):
        self.add_view(SummaryView(self))
        await self.tree.sync()

    async def close(self):
        try:
            await super().close()
        finally:
            self.store.close()

    async def on_ready(self):
        LOG.info("Logged in as %s", self.user)


@app_commands.command(name="dungeons_checklist", description="Publish or update the compact dungeon board")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def checklist_donjons(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    bot = interaction.client
    try:
        async with bot.board_lock:
            bot.store.import_progression(interaction.guild_id)
            await bot.refresh_board(interaction.guild_id, interaction.channel_id, create=True)
        await interaction.followup.send("Dungeon board updated into a single message.", ephemeral=True)
    except (ValueError, OSError, sqlite3.Error, discord.HTTPException):
        LOG.exception("Failed to publish board")
        await interaction.followup.send("Incomplete publication. Check the SQLite database and bot permissions, then run the command again.", ephemeral=True)


@checklist_donjons.error
async def checklist_error(interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        text = "This command requires administrator permissions in a server."
    else:
        LOG.error("Command failed", exc_info=(type(error), error, error.__traceback__))
        text = "An error occurred while publishing the board."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def main():
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN before starting the bot.")
    MonBot().run(token)


if __name__ == "__main__":
    main()