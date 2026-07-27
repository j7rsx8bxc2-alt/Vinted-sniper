"""
Zugriffskontrolle – zentrale Helfer, welche Rolle welche Befehle nutzen darf.

Regeln:
  - Snipe-Bot-Befehle (!add, !remove, !list, !proxy) in vinted_bot.py:
    nur Server-Admins (verändern die geteilte Monitor-Konfiguration für alle).
  - Alle Premium-Befehle (Buchhaltung, Listing, Coach, Preis-Check, TryOn,
    Verkauft-Erkennung, Trend-Radar, Content-Ideen): nur VIP-Rolle oder Admins.
  - Bei fehlender Berechtigung: stille Ablehnung, keine Fehlermeldung im Chat
    (siehe on_command_error in vinted_bot.py).
  - Funktioniert auch per Direktnachricht (DM) an den Bot: dort ist ctx.author
    sonst nur ein discord.User ohne Rolleninfos (kein echtes Server-Mitglied),
    is_vip_or_admin_ctx() schaut dafür im Hintergrund selbst im Server nach,
    welche Rollen die Person dort hat. Damit können VIPs den Bot einfach
    privat anschreiben, statt in einem für alle sichtbaren Kanal zu schreiben.

Konfiguration:
  VIP_ROLE_NAME – exakter Name der VIP-Rolle (Standard: "VIP")
"""

import os

import discord
from discord.ext import commands

VIP_ROLE_NAME = os.getenv("VIP_ROLE_NAME", "VIP")


def is_admin(member: discord.Member) -> bool:
    return isinstance(member, discord.Member) and member.guild_permissions.administrator


def is_vip(member: discord.Member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return any(role.name.lower() == VIP_ROLE_NAME.lower() for role in member.roles)


def is_vip_or_admin(member: discord.Member) -> bool:
    return is_vip(member) or is_admin(member)


async def resolve_member(ctx: commands.Context) -> discord.Member | None:
    """Löst ctx.author zu einem echten Server-Mitglied auf (inkl. Rollen) –
    auch wenn der Befehl per Direktnachricht (DM) an den Bot kommt. In einer
    DM ist ctx.author nur ein discord.User ohne Rolleninfos, deshalb schauen
    wir dann im Server nach, auf dem der Bot läuft, ob die Person dort
    Mitglied ist und welche Rollen sie hat. Geht davon aus, dass der Bot nur
    auf einem Server läuft (wie hier der Fall)."""
    if isinstance(ctx.author, discord.Member):
        return ctx.author
    if not ctx.bot.guilds:
        return None
    guild = ctx.bot.guilds[0]
    member = guild.get_member(ctx.author.id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(ctx.author.id)
    except (discord.NotFound, discord.HTTPException):
        return None


async def is_vip_or_admin_ctx(ctx: commands.Context) -> bool:
    """Wie is_vip_or_admin(), aber nimmt den ganzen Befehls-Kontext statt nur
    ctx.author entgegen – damit die Prüfung auch per Direktnachricht (DM)
    funktioniert. Das hier sollten alle Cogs in ihrem cog_check nutzen."""
    member = await resolve_member(ctx)
    if member is None:
        return False
    return is_vip_or_admin(member)


def admin_only():
    """Decorator für einzelne Commands (z.B. die Snipe-Bot-Commands in vinted_bot.py).
    Bleibt bewusst server-only (kein DM-Support) – diese Befehle beziehen sich
    auf konkrete Server-Kanäle und ergeben per DM ohnehin keinen Sinn."""
    async def predicate(ctx: commands.Context) -> bool:
        return is_admin(ctx.author)
    return commands.check(predicate)


def vip_or_admin_only():
    """Decorator für einzelne Commands – funktioniert auch per DM."""
    async def predicate(ctx: commands.Context) -> bool:
        return await is_vip_or_admin_ctx(ctx)
    return commands.check(predicate)
