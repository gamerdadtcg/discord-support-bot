import asyncio
import json
import os
from pathlib import Path

import discord
from discord.ext import commands

# ================== CONFIG (from Railway Variables) ==================
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
PANEL_CHANNEL_ID = int(os.environ["PANEL_CHANNEL_ID"])
TICKET_CATEGORY_ID = int(os.environ["TICKET_CATEGORY_ID"])
# Prefer MOD_ROLE_ID; SUPPORT_ROLE_ID kept as a fallback for older env setups.
MOD_ROLE_ID = int(os.environ.get("MOD_ROLE_ID") or os.environ["SUPPORT_ROLE_ID"])
ADMIN_ROLE_ID = int(os.environ["ADMIN_ROLE_ID"])
# =====================================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

DATA_FILE = Path("tickets.json")


def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"next_ticket": 1, "open_tickets": {}}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)


def resolve_staff_roles(guild: discord.Guild):
    """Resolve mod/admin roles and refuse @everyone / missing IDs (common misconfig)."""
    mod_role = guild.get_role(MOD_ROLE_ID)
    admin_role = guild.get_role(ADMIN_ROLE_ID)

    problems = []
    if mod_role is None:
        problems.append(f"MOD/SUPPORT role id {MOD_ROLE_ID} not found")
    elif mod_role.is_default():
        problems.append("MOD/SUPPORT role is @everyone — that would open tickets to everyone")

    if admin_role is None:
        problems.append(f"ADMIN role id {ADMIN_ROLE_ID} not found")
    elif admin_role.is_default():
        problems.append("ADMIN role is @everyone — that would open tickets to everyone")

    if problems:
        raise ValueError("; ".join(problems))

    return mod_role, admin_role


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

    Roles are applied one target at a time so a wrong shared id cannot wipe the
    @everyone deny (dict key collision).
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
        guild.me: discord.PermissionOverwrite(
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

    # Never grant staff access via @everyone; that misconfig opens tickets to all.
    if mod_role.id != guild.default_role.id and mod_role not in overwrites:
        overwrites[mod_role] = mod_perms

    if admin_role.id != guild.default_role.id:
        # Same role for mod+admin → keep the stronger admin overwrite.
        overwrites[admin_role] = admin_perms

    return overwrites


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    role_ids = {role.id for role in member.roles}
    return MOD_ROLE_ID in role_ids or ADMIN_ROLE_ID in role_ids


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Help",
        style=discord.ButtonStyle.primary,
        emoji="🆘",
        custom_id="create_ticket",
    )
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        user_id = str(interaction.user.id)

        if user_id in data["open_tickets"]:
            channel = bot.get_channel(data["open_tickets"][user_id])
            if channel:
                await interaction.response.send_message(
                    f"You already have an open ticket: {channel.mention}",
                    ephemeral=True,
                )
                return
            del data["open_tickets"][user_id]
            save_data(data)

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Tickets can only be created in a server.",
                ephemeral=True,
            )
            return

        category = guild.get_channel(TICKET_CATEGORY_ID)
        try:
            mod_role, admin_role = resolve_staff_roles(guild)
        except ValueError as exc:
            await interaction.response.send_message(
                f"Ticket system misconfigured: {exc}. "
                "An admin must set MOD_ROLE_ID (or SUPPORT_ROLE_ID) and ADMIN_ROLE_ID "
                "to real mod/admin role IDs — not @everyone.",
                ephemeral=True,
            )
            return

        ticket_number = data["next_ticket"]
        overwrites = build_ticket_overwrites(
            guild, interaction.user, mod_role, admin_role
        )

        channel = await guild.create_text_channel(
            name=f"help-{ticket_number}",
            category=category,
            overwrites=overwrites,
            topic=f"Ticket for {interaction.user} | ID: {ticket_number}",
        )

        data["next_ticket"] += 1
        data["open_tickets"][user_id] = channel.id
        save_data(data)

        embed = discord.Embed(
            title=f"Help Ticket #{ticket_number}",
            description=(
                f"Hello {interaction.user.mention}!\n\n"
                "Please describe your issue.\n"
                "Mods and admins will help you shortly.\n\n"
                "Click **Close Ticket** when finished."
            ),
            color=discord.Color.blue(),
        )

        mentions = [interaction.user.mention, mod_role.mention]
        if admin_role.id != mod_role.id:
            mentions.append(admin_role.mention)

        await channel.send(
            content=" ".join(mentions),
            embed=embed,
            view=CloseTicketView(),
        )

        await interaction.response.send_message(
            f"Your private ticket is ready: {channel.mention}",
            ephemeral=True,
        )


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

        opener = guild.get_member(int(user_id))
        opener_target = opener or discord.Object(id=int(user_id))
        overwrites = build_ticket_overwrites(
            guild, opener_target, mod_role, admin_role
        )
        await channel.edit(overwrites=overwrites, reason="Repair private ticket access")
        fixed += 1

    for user_id in stale:
        del data["open_tickets"][user_id]
    if stale:
        save_data(data)

    return fixed


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    bot.add_view(TicketPanel())
    bot.add_view(CloseTicketView())

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print(f"Warning: guild {GUILD_ID} not found; cannot repair tickets yet.")
        return

    try:
        resolve_staff_roles(guild)
        print(
            f"Staff access locked to mod role {MOD_ROLE_ID} and admin role {ADMIN_ROLE_ID} "
            "(plus ticket opener)."
        )
    except ValueError as exc:
        print(f"CONFIG ERROR: {exc}")
        return

    fixed = await repair_open_ticket_permissions(guild)
    if fixed:
        print(f"Repaired permissions on {fixed} open ticket channel(s).")


@bot.command(name="setup")
@commands.has_permissions(administrator=True)
async def setup(ctx):
    embed = discord.Embed(
        title="🆘 Support Ticket System",
        description=(
            "Need help?\n\n"
            "Click the **Help** button below to open a private ticket with the mod & admin team.\n"
            "Only you, mods, and admins can see the ticket.\n"
            "Tickets are named `help-1`, `help-2`, etc.\n"
            "You or staff can close the ticket when done."
        ),
        color=discord.Color.green(),
    )
    await ctx.send(embed=embed, view=TicketPanel())
    await ctx.message.delete()


@bot.command(name="fixticks")
@commands.has_permissions(administrator=True)
async def fixticks(ctx):
    """Re-lock open tickets so only opener + mod + admin can access them."""
    fixed = await repair_open_ticket_permissions(ctx.guild)
    await ctx.send(
        f"Repaired **{fixed}** open ticket(s). "
        "Only the sender, mods, and admins can view them now.",
        delete_after=15,
    )


bot.run(TOKEN)
