"""Checklist Discord des donjons, stockée dans SQLite."""
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
    """Texte brut pour un bloc ANSI, sans séquence de contrôle ni clôture Markdown."""
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
    return result or ["Aucune donnée disponible."]


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

    @discord.ui.button(label="Précédent", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.index = max(0, self.index - 1)
        self.update_buttons()
        await interaction.response.edit_message(content=self.content(), view=self, allowed_mentions=NO_MENTIONS)

    @discord.ui.button(label="Suivant", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        self.index = min(len(self.contents) - 1, self.index + 1)
        self.update_buttons()
        await interaction.response.edit_message(content=self.content(), view=self, allowed_mentions=NO_MENTIONS)


async def private_list(interaction, lines):
    view = PrivatePages(pages(lines))
    await interaction.response.send_message(view.content(), view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def interaction_error(interaction):
    text = "Une erreur est survenue. Rouvre Mes choix pour vérifier tes sélections."
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
            LOG.exception("Échec d'une interaction donjon")
            await interaction_error(interaction)

    async def handle(self, interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message("Utilise ce tableau dans un serveur.", ephemeral=True)
            return
        bot = self.bot
        if self.action == "edit":
            await interaction.response.send_message(
                "**Mes choix**\nSélectionne les donjons terminés. Désélectionne un donjon pour le retirer. Chaque menu est enregistré séparément.",
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
            lines.append("**Tes donjons terminés**")
            lines.extend([safe(d['name']) for d in bot.catalogue.values() if d['id'] in mine] or ["Aucun"])
        await private_list(interaction, lines)


class DungeonSelect(discord.ui.Select):
    def __init__(self, bot, dungeons, index, mine):
        self.bot = bot
        self.scope = [d['id'] for d in dungeons]
        super().__init__(
            placeholder=f"Terminés ({index * 25 + 1}–{index * 25 + len(dungeons)})",
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
        message = "Choix enregistrés. Tu peux modifier les autres menus."
        if not success:
            message += " Certains tableaux n’ont pas pu être actualisés."
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
        LOG.error("Échec de la sélection des donjons", exc_info=(type(error), error, error.__traceback__))
        await interaction_error(interaction)


class SummaryView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.add_item(ActionButton(bot, "edit", "Mes choix"))
        self.add_item(ActionButton(bot, "personal", "Mes donjons terminés"))
        self.add_item(ActionButton(bot, "players", "Joueurs par donjon"))


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
            raise ValueError("Le catalogue dépasse la taille maximale d'un tableau Discord.")
        embed = discord.Embed(title="Donjons", description=description, color=discord.Color.gold())
        embed.set_footer(text="Mes choix : modifier mes donjons terminés · Joueurs par donjon : liste complète.")
        return embed

    async def refresh_board(self, guild, channel_id, *, create=False):
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if channel.guild.id != guild:
            raise ValueError("Salon associé au mauvais serveur")
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
                LOG.exception("Impossible d’actualiser le tableau %s", channel)
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
        LOG.info("Connecté en tant que %s", self.user)


@app_commands.command(name="checklist_donjons", description="Publie ou actualise le tableau compact des donjons")
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
        await interaction.followup.send("Tableau des donjons actualisé en un seul message.", ephemeral=True)
    except (ValueError, OSError, sqlite3.Error, discord.HTTPException):
        LOG.exception("Échec de publication du tableau")
        await interaction.followup.send("Publication incomplète. Vérifie la base SQLite et les permissions du bot, puis relance la commande.", ephemeral=True)


@checklist_donjons.error
async def checklist_error(interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        text = "Cette commande nécessite les droits administrateur dans un serveur."
    else:
        LOG.error("Échec de la commande", exc_info=(type(error), error, error.__traceback__))
        text = "Une erreur est survenue lors de la publication du tableau."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def main():
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Définis DISCORD_TOKEN avant de démarrer le bot.")
    MonBot().run(token)


if __name__ == "__main__":
    main()
