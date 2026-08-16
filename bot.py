import asyncio
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Optional, Sequence, Tuple, Union

import discord
from discord import app_commands
from discord.ext import commands

SNOWFLAKE_RE = re.compile(r"\d{15,20}")


def parse_snowflake(raw: str, label: str) -> int:
    """Accept raw IDs, <@&id> role mentions, <#id> channels, or Discord links."""
    value = raw.strip().strip('"').strip("'")
    match = SNOWFLAKE_RE.search(value)
    if not match:
        raise ValueError(
            f"{label} must be a Discord ID (numbers only). Got: {raw!r}"
        )
    return int(match.group(0))


def require_snowflake(label: str, *env_names: str) -> int:
    for name in env_names:
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            continue
        try:
            return parse_snowflake(str(raw), name)
        except ValueError as exc:
            print(f"CONFIG ERROR: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    print(
        f"CONFIG ERROR: Missing {label}. Set one of: {', '.join(env_names)}",
        file=sys.stderr,
    )
    raise SystemExit(1)


def optional_snowflake(*env_names: str) -> Optional[int]:
    """Optional ID override. Invalid values are ignored so name lookup can win."""
    for name in env_names:
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            continue
        try:
            return parse_snowflake(str(raw), name)
        except ValueError as exc:
            print(f"CONFIG WARNING: ignoring {name}: {exc}", file=sys.stderr)
    return None


# ================== CONFIG (from Railway Variables) ==================
TOKEN = os.environ.get("DISCORD_TOKEN", "").strip().strip('"').strip("'")
if not TOKEN:
    print("CONFIG ERROR: DISCORD_TOKEN is missing.", file=sys.stderr)
    raise SystemExit(1)

GUILD_ID = require_snowflake("GUILD_ID", "GUILD_ID")
# Optional / unused by runtime logic — kept so old Railway envs don't break.
PANEL_CHANNEL_ID = optional_snowflake("PANEL_CHANNEL_ID")
TICKET_CATEGORY_ID = optional_snowflake("TICKET_CATEGORY_ID")
# Optional overrides — by default the bot finds roles named "mods" and "admin".
MOD_ROLE_ID = optional_snowflake("MOD_ROLE_ID", "SUPPORT_ROLE_ID", "MODS_ROLE_ID")
ADMIN_ROLE_ID = optional_snowflake("ADMIN_ROLE_ID", "ADMINISTRATOR_ROLE_ID")
MOD_ROLE_NAMES = ("mods", "mod")
ADMIN_ROLE_NAMES = ("admin", "admins")
HELP_EMOJI = "🆘"
# =====================================================================

# No privileged intents — avoids Discord PrivilegedIntentsRequired crash-loops
# when Message Content / Server Members are disabled in the developer portal.
intents = discord.Intents.default()
intents.guilds = True
intents.guild_messages = True
intents.guild_reactions = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    # Role pings for @mods / @admin must be explicitly allowed.
    allowed_mentions=discord.AllowedMentions(
        everyone=False, users=True, roles=True, replied_user=False
    ),
)
guild_obj = discord.Object(id=GUILD_ID)

DATA_FILE = Path("tickets.json")
_creating_tickets: set[int] = set()


def load_data():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
            data.setdefault("next_ticket", 1)
            data.setdefault("open_tickets", {})
            data.setdefault("panel_message_ids", [])
            return data
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: could not read {DATA_FILE}: {exc}")
    return {"next_ticket": 1, "open_tickets": {}, "panel_message_ids": []}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)


def find_role_by_names(guild: discord.Guild, names: Sequence[str]) -> Optional[discord.Role]:
    wanted = {name.casefold() for name in names}
    for role in guild.roles:
        if role.is_default():
            continue
        if role.name.casefold() in wanted:
            return role
    return None


def resolve_staff_roles(guild: discord.Guild) -> Tuple[discord.Role, discord.Role]:
    """
    Resolve the @mods and @admin roles.
    Prefers optional env IDs when set and valid; otherwise looks up by name.
    Bad env IDs (missing / @everyone) fall back to name lookup.
    """
    mod_role = guild.get_role(MOD_ROLE_ID) if MOD_ROLE_ID else None
    admin_role = guild.get_role(ADMIN_ROLE_ID) if ADMIN_ROLE_ID else None

    if mod_role is not None and mod_role.is_default():
        print("CONFIG WARNING: mod role env points at @everyone; using name lookup")
        mod_role = None
    if admin_role is not None and admin_role.is_default():
        print("CONFIG WARNING: admin role env points at @everyone; using name lookup")
        admin_role = None

    if mod_role is None:
        mod_role = find_role_by_names(guild, MOD_ROLE_NAMES)
    if admin_role is None:
        admin_role = find_role_by_names(guild, ADMIN_ROLE_NAMES)

    problems = []
    if mod_role is None:
        problems.append(
            'could not find a role named "mods" (or set MOD_ROLE_ID / SUPPORT_ROLE_ID)'
        )
    if admin_role is None:
        problems.append(
            'could not find a role named "admin" (or set ADMIN_ROLE_ID)'
        )

    if problems:
        raise ValueError("; ".join(problems))

    return mod_role, admin_role


def bot_member(guild: discord.Guild) -> discord.abc.Snowflake:
    me = guild.me
    if me is not None:
        return me
    if bot.user is not None:
        return discord.Object(id=bot.user.id)
    raise RuntimeError("Bot user is not available yet")


def build_ticket_overwrites(
    guild: discord.Guild,
    opener: discord.abc.Snowflake,
    mod_role: discord.Role,
    admin_role: discord.Role,
):
    """
    Private ticket access:
    - @everyone: cannot see
    - ticket opener: can see + send
    - mod role: can see + send
    - admin role: can see + send + manage channel
    - bot: can see + send + manage channel
    """
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False,
            read_message_history=False,
            send_messages=False,
        ),
        opener: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
        bot_member(guild): discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
        ),
    }

    mod_perms = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        attach_files=True,
        embed_links=True,
    )
    admin_perms = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        attach_files=True,
        embed_links=True,
        manage_channels=True,
        manage_messages=True,
    )

    if mod_role.id != guild.default_role.id and mod_role not in overwrites:
        overwrites[mod_role] = mod_perms

    if admin_role.id != guild.default_role.id:
        # Same role for mod+admin → keep the stronger admin overwrite.
        overwrites[admin_role] = admin_perms

    return overwrites


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    try:
        mod_role, admin_role = resolve_staff_roles(member.guild)
    except ValueError:
        return False
    role_ids = {role.id for role in member.roles}
    return mod_role.id in role_ids or admin_role.id in role_ids


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="🆘 Need Help?",
        description=(
            "React with 🆘 below to open a **private help ticket**.\n\n"
            "Only you, **mods**, and **admins** can see your ticket.\n"
            "Staff will be pinged when your ticket opens.\n\n"
            "Click **Close Ticket** inside your ticket when you're done."
        ),
        color=discord.Color.green(),
    )


async def create_ticket_for_member(
    guild: discord.Guild,
    member: Union[discord.Member, discord.abc.Snowflake],
) -> Tuple[Optional[discord.TextChannel], Optional[str]]:
    """
    Create a private help ticket for a member.
    Returns (channel, None) on success, or (None, error_message).
    """
    data = load_data()
    user_id = str(member.id)

    if user_id in data["open_tickets"]:
        channel = bot.get_channel(data["open_tickets"][user_id])
        if channel:
            return None, f"You already have an open ticket: {channel.mention}"
        del data["open_tickets"][user_id]
        save_data(data)

    category = None
    if TICKET_CATEGORY_ID:
        category = guild.get_channel(TICKET_CATEGORY_ID)
        if category is not None and not isinstance(category, discord.CategoryChannel):
            return None, "TICKET_CATEGORY_ID must be a category channel ID."

    try:
        mod_role, admin_role = resolve_staff_roles(guild)
    except ValueError as exc:
        return (
            None,
            f"Ticket system misconfigured: {exc}. "
            "Create roles named **mods** and **admin**.",
        )

    ticket_number = data["next_ticket"]
    overwrites = build_ticket_overwrites(guild, member, mod_role, admin_role)

    channel = await guild.create_text_channel(
        name=f"help-{ticket_number}",
        category=category,
        overwrites=overwrites,
        topic=f"Ticket for {member} | ID: {ticket_number}",
    )

    data["next_ticket"] += 1
    data["open_tickets"][user_id] = channel.id
    save_data(data)

    mention_user = getattr(member, "mention", f"<@{member.id}>")
    embed = discord.Embed(
        title=f"Help Ticket #{ticket_number}",
        description=(
            f"Hello {mention_user}!\n\n"
            "Please describe your issue.\n"
            "Mods and admins will help you shortly.\n\n"
            "Click **Close Ticket** when finished."
        ),
        color=discord.Color.blue(),
    )

    ping_roles = [mod_role]
    if admin_role.id != mod_role.id:
        ping_roles.append(admin_role)
    mentions = [mention_user, *(role.mention for role in ping_roles)]

    allowed_users = [member] if isinstance(member, discord.abc.User) else []
    await channel.send(
        content=" ".join(mentions),
        embed=embed,
        view=CloseTicketView(),
        allowed_mentions=discord.AllowedMentions(
            everyone=False,
            users=allowed_users or True,
            roles=ping_roles,
        ),
    )
    return channel, None


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="close_ticket",
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self._close_ticket(interaction)
        except Exception:
            traceback.print_exc()
            message = "Something went wrong closing this ticket."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    async def _close_ticket(self, interaction: discord.Interaction):
        data = load_data()
        channel = interaction.channel
        user_id = None

        for uid, cid in data["open_tickets"].items():
            if cid == channel.id:
                user_id = uid
                break

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Could not verify your roles in this server.",
                ephemeral=True,
            )
            return

        is_owner = str(member.id) == user_id
        if not (is_owner or is_staff(member)):
            await interaction.response.send_message(
                "Only the ticket creator, mods, or admins can close this.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message("Closing ticket in 3 seconds…")
        await asyncio.sleep(3)

        if user_id and user_id in data["open_tickets"]:
            del data["open_tickets"][user_id]
            save_data(data)

        if isinstance(channel, discord.TextChannel):
            await channel.delete(reason=f"Closed by {interaction.user}")


async def repair_open_ticket_permissions(guild: discord.Guild) -> int:
    """Re-apply private overwrites on tracked tickets (fixes bad env / leaked access)."""
    try:
        mod_role, admin_role = resolve_staff_roles(guild)
    except ValueError as exc:
        print(f"Skipping ticket permission repair: {exc}")
        return 0

    data = load_data()
    fixed = 0
    stale = []

    for user_id, channel_id in list(data["open_tickets"].items()):
        channel = guild.get_channel(channel_id)
        if channel is None:
            stale.append(user_id)
            continue
        if not isinstance(channel, discord.TextChannel):
            continue

        opener = guild.get_member(int(user_id)) or discord.Object(id=int(user_id))
        overwrites = build_ticket_overwrites(guild, opener, mod_role, admin_role)
        try:
            await channel.edit(overwrites=overwrites, reason="Repair private ticket access")
            fixed += 1
        except discord.HTTPException as exc:
            print(f"Could not repair #{channel.name}: {exc}")

    for user_id in stale:
        del data["open_tickets"][user_id]
    if stale:
        save_data(data)

    return fixed


async def start_health_server() -> None:
    """
    Railway web services expect something listening on $PORT.
    Without this, Discord bots often get restarted in a crash-loop.
    """
    port_raw = os.environ.get("PORT")
    if not port_raw:
        print("No PORT env set; skipping health server (fine for worker services).")
        return

    try:
        port = int(port_raw)
    except ValueError:
        print(f"CONFIG WARNING: invalid PORT={port_raw!r}; skipping health server")
        return

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await asyncio.wait_for(reader.read(1024), timeout=1.0)
        except (asyncio.TimeoutError, ConnectionResetError):
            pass
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok")
        try:
            await writer.drain()
        except ConnectionResetError:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            pass

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    print(f"Health check server listening on 0.0.0.0:{port}")
    # Keep reference so the server is not garbage-collected.
    bot.health_server = server  # type: ignore[attr-defined]


@bot.event
async def setup_hook() -> None:
    await start_health_server()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id if bot.user else '?'})")
    print(
        f"Using GUILD_ID={GUILD_ID}; "
        f"mod role via {'id ' + str(MOD_ROLE_ID) if MOD_ROLE_ID else 'name mods'}; "
        f"admin role via {'id ' + str(ADMIN_ROLE_ID) if ADMIN_ROLE_ID else 'name admin'}"
    )
    bot.add_view(CloseTicketView())

    try:
        synced = await bot.tree.sync(guild=guild_obj)
        print(f"Synced {len(synced)} slash command(s) to guild {GUILD_ID}")
    except Exception:
        traceback.print_exc()
        print("Slash command sync failed; bot will keep running.")

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print(
            f"Warning: guild {GUILD_ID} not in cache yet. "
            "Check GUILD_ID and that the bot is in that server."
        )
        return

    try:
        mod_role, admin_role = resolve_staff_roles(guild)
        print(
            f"Staff roles ready: @{mod_role.name} ({mod_role.id}), "
            f"@{admin_role.name} ({admin_role.id})"
        )
    except ValueError as exc:
        print(f"CONFIG ERROR: {exc}")
        return

    try:
        fixed = await repair_open_ticket_permissions(guild)
        if fixed:
            print(f"Repaired permissions on {fixed} open ticket channel(s).")
    except Exception:
        traceback.print_exc()
        print("Ticket permission repair failed; bot will keep running.")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Open a ticket when someone reacts with 🆘 on a panel message."""
    if payload.guild_id != GUILD_ID:
        return
    if bot.user and payload.user_id == bot.user.id:
        return
    if str(payload.emoji) != HELP_EMOJI:
        return

    data = load_data()
    if payload.message_id not in data.get("panel_message_ids", []):
        return

    if payload.user_id in _creating_tickets:
        return
    _creating_tickets.add(payload.user_id)

    try:
        guild = bot.get_guild(payload.guild_id) or await bot.fetch_guild(payload.guild_id)
        channel = bot.get_channel(payload.channel_id)
        if channel is None:
            channel = await bot.fetch_channel(payload.channel_id)

        # Remove their reaction so they can click 🆘 again later if needed.
        if isinstance(channel, discord.TextChannel):
            try:
                message = await channel.fetch_message(payload.message_id)
                user = discord.Object(id=payload.user_id)
                await message.remove_reaction(HELP_EMOJI, user)
            except discord.HTTPException:
                pass

        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                member = discord.Object(id=payload.user_id)

        ticket_channel, error = await create_ticket_for_member(guild, member)
        notice_channel = channel if isinstance(channel, discord.TextChannel) else None

        if error:
            if notice_channel is not None:
                msg = await notice_channel.send(
                    f"<@{payload.user_id}> {error}",
                    delete_after=15,
                )
                # delete_after handles cleanup
                _ = msg
            return

        if ticket_channel is not None and notice_channel is not None:
            await notice_channel.send(
                f"<@{payload.user_id}> Your private ticket is ready: {ticket_channel.mention}",
                delete_after=20,
            )
    except Exception:
        traceback.print_exc()
    finally:
        _creating_tickets.discard(payload.user_id)


@bot.tree.command(name="setup", description="Post the private ticket Help panel", guild=guild_obj)
@app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(interaction: discord.Interaction):
    if interaction.channel is None or not isinstance(
        interaction.channel, discord.TextChannel
    ):
        await interaction.response.send_message(
            "Run /setup in a text channel.", ephemeral=True
        )
        return

    message = await interaction.channel.send(embed=panel_embed())
    await message.add_reaction(HELP_EMOJI)

    data = load_data()
    panel_ids = data.setdefault("panel_message_ids", [])
    if message.id not in panel_ids:
        panel_ids.append(message.id)
        save_data(data)

    await interaction.response.send_message(
        f"Ticket panel posted. People can react with {HELP_EMOJI} to open a ticket.",
        ephemeral=True,
    )


@bot.tree.command(
    name="fixticks",
    description="Re-lock open tickets to opener + mods + admins only",
    guild=guild_obj,
)
@app_commands.checks.has_permissions(administrator=True)
async def fixticks_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Run this inside the server.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    fixed = await repair_open_ticket_permissions(interaction.guild)
    await interaction.followup.send(
        f"Repaired **{fixed}** open ticket(s). "
        "Only the sender, mods, and admins can view them now.",
        ephemeral=True,
    )


@setup_cmd.error
@fixticks_cmd.error
async def staff_cmd_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "Admin permission required."
    else:
        traceback.print_exc()
        message = "Command failed."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def main():
    print("Starting support ticket bot…")
    print(f"Python {sys.version}")
    try:
        bot.run(TOKEN, reconnect=True)
    except discord.errors.PrivilegedIntentsRequired:
        print(
            "CONFIG ERROR: Discord rejected privileged intents.\n"
            "This bot no longer needs Message Content or Server Members intents.\n"
            "Turn those OFF in the Discord Developer Portal → Bot.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except discord.LoginFailure:
        print("CONFIG ERROR: DISCORD_TOKEN is invalid.", file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
