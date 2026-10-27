import os
import json
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fall-tiers-bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
TESTER_ROLE_ID = int(os.getenv("TESTER_ROLE_ID", "0"))
ANNOUNCEMENT_CHANNEL_ID = int(os.getenv("ANNOUNCEMENT_CHANNEL_ID", "0"))
APPLICATION_CHANNEL_ID = int(os.getenv("APPLICATION_CHANNEL_ID", "0"))
WEBSITE_WEBHOOK_URL = os.getenv("WEBSITE_WEBHOOK_URL", "")
WEBSITE_WEBHOOK_SECRET = os.getenv("WEBSITE_WEBHOOK_SECRET", "")

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Simple local persistence for applications. For production/multiple bot instances,
# replace this JSON file with a database.
DATA_FILE = "applications.json"

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("verified_users", {})
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"applications": {}, "verified_users": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_tester(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == TESTER_ROLE_ID for role in interaction.user.roles) or interaction.user.guild_permissions.administrator

async def sync_result_to_website(payload: dict):
    """Optional secure webhook. Configure URL/secret only after the website backend supports it."""
    if not WEBSITE_WEBHOOK_URL:
        return
    headers = {"Content-Type": "application/json"}
    if WEBSITE_WEBHOOK_SECRET:
        headers["Authorization"] = f"Bearer {WEBSITE_WEBHOOK_SECRET}"
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(WEBSITE_WEBHOOK_URL, json=payload, headers=headers) as response:
            body = await response.text()
            if response.status >= 300:
                raise RuntimeError(f"Website webhook returned {response.status}: {body[:300]}")

class ApplyModal(discord.ui.Modal, title="Minecraft Tier Test Application"):
    minecraft_ign = discord.ui.TextInput(
        label="Minecraft IGN",
        placeholder="Enter your exact Minecraft username",
        min_length=3, max_length=16
    )
    gamemode = discord.ui.TextInput(
        label="PvP gamemode",
        placeholder="e.g. Sword, Axe, Crystal, UHC",
        min_length=2, max_length=40
    )
    region = discord.ui.TextInput(
        label="Region / timezone (optional)",
        placeholder="e.g. EU",
        required=False, max_length=40
    )
    notes = discord.ui.TextInput(
        label="Extra information (optional)",
        style=discord.TextStyle.paragraph,
        required=False, max_length=300
    )

    async def on_submit(self, interaction: discord.Interaction):
        data = load_data()
        apps = data["applications"]
        uid = str(interaction.user.id)
        # One pending application per Discord account.
        if any(a["discord_id"] == uid and a["status"] == "pending" for a in apps.values()):
            await interaction.response.send_message(
                "You already have a pending application. Please wait for a tester.",
                ephemeral=True
            )
            return

        app_id = str(max([int(k) for k in apps.keys()] or [1000]) + 1)
        record = {
            "id": app_id,
            "discord_id": uid,
            "discord_name": str(interaction.user),
            "minecraft_ign": str(self.minecraft_ign).strip(),
            "gamemode": str(self.gamemode).strip(),
            "region": str(self.region).strip(),
            "notes": str(self.notes).strip(),
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "result_tier": None,
            "tester_id": None
        }
        apps[app_id] = record
        save_data(data)

        embed = discord.Embed(title=f"Tier Test Application #{app_id}", color=discord.Color.blurple())
        embed.add_field(name="Minecraft IGN", value=record["minecraft_ign"], inline=True)
        embed.add_field(name="Applicant", value=f"<@{uid}>", inline=True)
        embed.add_field(name="Gamemode", value=record["gamemode"], inline=True)
        embed.add_field(name="Region", value=record["region"] or "Not provided", inline=True)
        embed.add_field(name="Extra information", value=record["notes"] or "None", inline=False)
        embed.add_field(name="Status", value="Pending", inline=True)
        embed.set_footer(text=f"Application ID: {app_id}")

        channel = interaction.client.get_channel(APPLICATION_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            await channel.send(embed=embed, view=ReviewView(app_id))
        await interaction.response.send_message(
            f"Your application **#{app_id}** has been submitted. Please wait for a tester.",
            ephemeral=True
        )

class ReviewView(discord.ui.View):
    def __init__(self, app_id: str):
        super().__init__(timeout=None)
        self.app_id = app_id

    @discord.ui.button(label="Set Tier / Complete", style=discord.ButtonStyle.success, custom_id="falltiers:complete")
    async def complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_tester(interaction):
            await interaction.response.send_message("Only authorized testers can publish results.", ephemeral=True)
            return
        await interaction.response.send_modal(ResultModal(self.app_id))

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="falltiers:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_tester(interaction):
            await interaction.response.send_message("Only authorized testers can deny applications.", ephemeral=True)
            return
        data = load_data()
        record = data["applications"].get(self.app_id)
        if not record:
            await interaction.response.send_message("Application not found.", ephemeral=True)
            return
        record["status"] = "denied"
        record["tester_id"] = str(interaction.user.id)
        save_data(data)
        await interaction.response.send_message(f"Application #{self.app_id} denied.", ephemeral=True)
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass

class ResultModal(discord.ui.Modal, title="Publish Tier Test Result"):
    tier = discord.ui.TextInput(
        label="Assigned Tier",
        placeholder="e.g. HT1, LT2, HT3",
        min_length=2, max_length=20
    )
    notes = discord.ui.TextInput(
        label="Result notes (optional)",
        style=discord.TextStyle.paragraph,
        required=False, max_length=300
    )

    def __init__(self, app_id: str):
        super().__init__()
        self.app_id = app_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_tester(interaction):
            await interaction.response.send_message("Only authorized testers can publish results.", ephemeral=True)
            return
        data = load_data()
        record = data["applications"].get(self.app_id)
        if not record:
            await interaction.response.send_message("Application not found.", ephemeral=True)
            return

        record["status"] = "completed"
        record["result_tier"] = str(self.tier).strip()
        record["result_notes"] = str(self.notes).strip()
        record["tester_id"] = str(interaction.user.id)
        record["completed_at"] = datetime.now(timezone.utc).isoformat()
        save_data(data)

        embed = discord.Embed(
            title="Minecraft Tier Test Result",
            description=f"**{record['minecraft_ign']}** has received **{record['result_tier']}**.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Gamemode", value=record["gamemode"], inline=True)
        embed.add_field(name="Tester", value=interaction.user.mention, inline=True)
        embed.add_field(name="Discord", value=f"<@{record['discord_id']}>", inline=True)
        if record.get("result_notes"):
            embed.add_field(name="Notes", value=record["result_notes"], inline=False)
        embed.add_field(name="Leaderboard", value="https://falltiersss.lovable.app/leaderboard", inline=False)

        channel = interaction.client.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            await channel.send(embed=embed)

        payload = {
            "event": "tier_result_published",
            "application_id": record["id"],
            "discord_id": record["discord_id"],
            "minecraft_ign": record["minecraft_ign"],
            "gamemode": record["gamemode"],
            "tier": record["result_tier"],
            "tester_discord_id": record["tester_id"],
            "completed_at": record["completed_at"],
            "notes": record.get("result_notes", "")
        }
        try:
            await sync_result_to_website(payload)
        except Exception as exc:
            log.exception("Website sync failed: %s", exc)
            await interaction.followup.send(
                "Result saved and announced, but website sync failed. The website backend webhook must be configured.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Result published: **{record['minecraft_ign']} — {record['result_tier']}**.",
            ephemeral=True
        )
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass

class ApplyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Apply for Tier Test", style=discord.ButtonStyle.primary, emoji="⚔️", custom_id="falltiers:apply")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApplyModal())



class VerifyModal(discord.ui.Modal, title="Verify Minecraft Account"):
    minecraft_ign = discord.ui.TextInput(
        label="Minecraft IGN",
        placeholder="Enter your exact Minecraft username",
        min_length=3, max_length=16
    )
    region = discord.ui.TextInput(
        label="Region",
        placeholder="EU, NA, AS, etc.",
        min_length=2, max_length=20
    )

    async def on_submit(self, interaction: discord.Interaction):
        data = load_data()
        if "verified_users" not in data:
            data["verified_users"] = {}
        uid = str(interaction.user.id)
        data["verified_users"][uid] = {
            "discord_id": uid,
            "discord_name": str(interaction.user),
            "minecraft_ign": str(self.minecraft_ign).strip(),
            "region": str(self.region).strip().upper(),
            "verified_at": datetime.now(timezone.utc).isoformat()
        }
        save_data(data)
        await interaction.response.send_message(
            f"✅ Verified! IGN: **{str(self.minecraft_ign).strip()}** | Region: **{str(self.region).strip().upper()}**",
            ephemeral=True
        )

@bot.tree.command(name="verify", description="Verify your Minecraft IGN and region")
@app_commands.guild_only()
async def verify(interaction: discord.Interaction):
    await interaction.response.send_modal(VerifyModal())


@bot.event
async def on_ready():
    bot.add_view(ApplyView())
    bot.add_view(ReviewView("persistent-placeholder"))
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
    except Exception:
        log.exception("Command sync failed")
    log.info("Logged in as %s", bot.user)

@bot.tree.command(name="setup_tier_testing", description="Post the Tier Testing application panel")
@app_commands.guild_only()
async def setup_tier_testing(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only server administrators can run this command.", ephemeral=True)
        return
    embed = discord.Embed(
        title="Fall Tiers | Minecraft Tier Testing",
        description=(
            "Ready to test your PvP skills?\n\n"
            "1. Click **Apply for Tier Test**.\n"
            "2. Enter your Minecraft IGN and gamemode.\n"
            "3. Wait for an authorized tester to review your application.\n"
            "4. Completed results will be announced in the results channel."
        ),
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Fall Tiers • Tier Testing")
    await interaction.channel.send(embed=embed, view=ApplyView())
    await interaction.response.send_message("Tier Testing panel posted.", ephemeral=True)

@bot.tree.command(name="my_application", description="Check your latest Tier Testing application")
@app_commands.guild_only()
async def my_application(interaction: discord.Interaction):
    data = load_data()
    records = [a for a in data["applications"].values() if a["discord_id"] == str(interaction.user.id)]
    if not records:
        await interaction.response.send_message("You don't have an application yet. Use the Tier Testing panel.", ephemeral=True)
        return
    latest = sorted(records, key=lambda a: a["created_at"])[-1]
    await interaction.response.send_message(
        f"Application **#{latest['id']}** — **{latest['minecraft_ign']}** — Status: **{latest['status']}**"
        + (f" — Tier: **{latest['result_tier']}**" if latest.get("result_tier") else ""),
        ephemeral=True
    )

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and add your bot token.")

bot.run(DISCORD_TOKEN)
