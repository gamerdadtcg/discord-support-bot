import discord
from discord.ext import commands
import json
import os
from pathlib import Path

# ================== CONFIG (from Railway Variables) ==================
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
PANEL_CHANNEL_ID = int(os.environ["PANEL_CHANNEL_ID"])
TICKET_CATEGORY_ID = int(os.environ["TICKET_CATEGORY_ID"])
SUPPORT_ROLE_ID = int(os.environ["SUPPORT_ROLE_ID"])
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

class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Help", style=discord.ButtonStyle.primary, emoji="🆘", custom_id="create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        user_id = str(interaction.user.id)

        if user_id in data["open_tickets"]:
            channel = bot.get_channel(data["open_tickets"][user_id])
            if channel:
                await interaction.response.send_message(f"You already have an open ticket: {channel.mention}", ephemeral=True)
                return
            else:
                del data["open_tickets"][user_id]
                save_data(data)

        guild = interaction.guild
        category = guild.get_channel(TICKET_CATEGORY_ID)
        support_role = guild.get_role(SUPPORT_ROLE_ID)
        admin_role = guild.get_role(ADMIN_ROLE_ID)

        ticket_number = data["next_ticket"]
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            support_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            admin_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }

        channel = await guild.create_text_channel(
            name=f"help-{ticket_number}",
            category=category,
            overwrites=overwrites,
            topic=f"Ticket for {interaction.user} | ID: {ticket_number}"
        )

        data["next_ticket"] += 1
        data["open_tickets"][user_id] = channel.id
        save_data(data)

        embed = discord.Embed(
            title=f"Help Ticket #{ticket_number}",
            description=(
                f"Hello {interaction.user.mention}!\n\n"
                "Please describe your issue.\n"
                "Staff will help you shortly.\n\n"
                "Click **Close Ticket** when finished."
            ),
            color=discord.Color.blue()
        )

        await channel.send(
            content=f"{interaction.user.mention} {support_role.mention} {admin_role.mention}",
            embed=embed,
            view=CloseTicketView()
        )

        await interaction.response.send_message(f"Your private ticket is ready: {channel.mention}", ephemeral=True)


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        channel = interaction.channel
        user_id = None

        for uid, cid in data["open_tickets"].items():
            if cid == channel.id:
                user_id = uid
                break

        is_owner = str(interaction.user.id) == user_id
        is_staff = (
            interaction.user.get_role(SUPPORT_ROLE_ID) is not None
            or interaction.user.get_role(ADMIN_ROLE_ID) is not None
            or interaction.user.guild_permissions.administrator
        )

        if not (is_owner or is_staff):
            await interaction.response.send_message("Only the ticket creator or staff can close this.", ephemeral=True)
            return

        await interaction.response.send_message("Closing ticket in 3 seconds…")
        await discord.utils.sleep_until(discord.utils.utcnow() + discord.timedelta(seconds=3))

        if user_id and user_id in data["open_tickets"]:
            del data["open_tickets"][user_id]
            save_data(data)

        await channel.delete(reason=f"Closed by {interaction.user}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    bot.add_view(TicketPanel())
    bot.add_view(CloseTicketView())


@bot.command(name="setup")
@commands.has_permissions(administrator=True)
async def setup(ctx):
    embed = discord.Embed(
        title="🆘 Support Ticket System",
        description=(
            "Need help?\n\n"
            "Click the **Help** button below to open a private ticket with the mod & admin team.\n"
            "Tickets are named `help-1`, `help-2`, etc.\n"
            "You or staff can close the ticket when done."
        ),
        color=discord.Color.green()
    )
    await ctx.send(embed=embed, view=TicketPanel())
    await ctx.message.delete()


bot.run(TOKEN)
