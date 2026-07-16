"""
Zugriffskontrolle – zentrale Helfer, welche Rolle welche Befehle nutzen darf.

Regeln:
  - Snipe-Bot-Befehle (!add, !remove, !list, !proxy) in vinted_bot.py:
    nur Server-Admins (verändern die geteilte Monitor-Konfiguration für alle).
  - Alle Premium-Befehle (Buchhaltung, Listing, Coach, Preis-Check, TryOn,
    Verkauft-Erkennung): nur VIP-Rolle oder Admins.
  - Bei fehlender Berechtigung: stille Ablehnung, keine Fehlermeldung im Chat
    (siehe on_command_error in vinted_bot.py).

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


def admin_only():
    """Decorator für einzelne Commands (z.B. die Snipe-Bot-Commands in vinted_bot.py)."""
    async def predicate(ctx: commands.Context) -> bool:
        return is_admin(ctx.author)
    return commands.check(predicate)


def vip_or_admin_only():
    """Decorator für einzelne Commands."""
    async def predicate(ctx: commands.Context) -> bool:
        return is_vip_or_admin(ctx.author)
    return commands.check(predicate)
