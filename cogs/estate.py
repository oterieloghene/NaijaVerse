"""
cogs/estate.py

The "Estate" app on !phone (the slot that used to be the unused "Smart" app):

  Apply for Pass   Owner only, must be standing in their own house. Names a
                   visitor, which of their own threads to grant, and a
                   requested duration. Posts a request to that state's
                   staff-office for a Housing Officer to decide.
  Redeem Code      Visitor enters the single-use 4-digit code an officer
                   relayed to them. Grants the class visitor role (if the
                   house type has one — low-cost housing doesn't) and tags
                   the visitor into exactly the threads the owner picked.
                   The clock starts here, not at approval.
  End Visit        Visitor ends an active pass early — access is stripped
                   immediately, no cooldown.

Housing Officers approve/deny/revoke from buttons on the staff-office
message itself (guest_pass_database.py tracks the request through
pending -> ready -> redeemed/expired/ended, or denied/revoked).

If the bot auto-expires a pass (visitor didn't end it themselves), that
owner can't issue a new pass to that same visitor for
guest_pass_config.COOLDOWN_MINUTES.

Note: ApprovalView/PostApprovalView use timeout=None so they don't expire
while an officer is slow to respond, but like the rest of the phone UI
they aren't re-registered as persistent views on restart — a request left
pending across a bot restart needs re-posting (!housinginfo-style command
isn't provided for that here; flag if you want one).
"""

import datetime as dt
import logging
import random
import re

import discord
from discord.ext import commands, tasks

import database
import document_config as cfg
import guest_pass_config as gcfg
import guest_pass_database as gdb
import housing_config as hcfg
import housing_database as hdb
from location_permissions import state_location_channels
from player_rules import find_roles

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
STATES = ["Lagos", "Delta", "Abuja"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _show(interaction, embed, view):
    """Same trick as cogs/phone.py's _show — kept local to avoid a circular import."""
    embed.set_image(url="attachment://phone.png")
    if interaction.message is not None:
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


def _room_options(house_type):
    """{key: label} for every thread this house type has — private rooms + shared ones."""
    rooms = hcfg.HOUSE_ROOMS[house_type]
    options = {key: label for key, label in rooms["private"]}
    for shared_key in rooms["shared"]:
        options[shared_key] = hcfg.shared_thread_name(shared_key)
    return options


def _parse_visitor(guild, text):
    text = text.strip()
    match = re.fullmatch(r"<@!?(\d+)>", text) or re.fullmatch(r"\d{15,25}", text)
    if match:
        user_id = int(match.group(1) if match.groups() else match.group(0))
        return guild.get_member(user_id)
    lowered = text.casefold()
    for member in guild.members:
        if member.name.casefold() == lowered or member.display_name.casefold() == lowered:
            return member
    return None


def _parse_thread_keys(text, options):
    text = text.strip().casefold()
    if text in ("all", "everything"):
        return list(options), None
    label_lookup = {label.casefold(): key for key, label in options.items()}
    keys = []
    for piece in (p.strip().casefold() for p in text.split(",")):
        if not piece:
            continue
        if piece in options:
            keys.append(piece)
        elif piece in label_lookup:
            keys.append(label_lookup[piece])
        else:
            valid = ", ".join(options.values())
            return None, f"`{piece}` isn't one of your threads. Choose from: {valid} (or `all`)."
    if not keys:
        return None, "Pick at least one thread."
    return keys, None


async def _resolve_thread(guild, request):
    """{key: Thread} for every key in the request, skipping any that can't be found."""
    threads = {}
    private_rows = {row["room_key"]: row["thread_id"] for row in await hdb.get_private_threads(
        request["assignment_id"])}
    for key in request["thread_keys"]:
        thread_id = private_rows.get(key)
        if thread_id is None:
            thread_id = await hdb.get_shared_thread(request["state"], request["house_type"], key)
        if thread_id is None:
            continue
        thread = guild.get_thread(thread_id)
        if thread is None:
            try:
                thread = await guild.fetch_channel(thread_id)
            except discord.HTTPException:
                continue
        threads[key] = thread
    return threads


async def _grant_access(guild, request, visitor_member):
    role_name = gcfg.VISITOR_ROLE_BY_HOUSE_TYPE.get(request["house_type"])
    if role_name:
        roles, _ = find_roles(guild.roles, [role_name])
        if roles:
            try:
                await visitor_member.add_roles(*roles, reason="Guest pass redeemed")
            except discord.HTTPException:
                log.exception("estate: couldn't grant %s to %s", role_name, visitor_member)
    for thread in (await _resolve_thread(guild, request)).values():
        try:
            await thread.add_user(visitor_member)
        except discord.HTTPException:
            log.exception("estate: couldn't add %s to thread %s", visitor_member, thread.id)


async def _revoke_access(guild, request, visitor_member):
    role_name = gcfg.VISITOR_ROLE_BY_HOUSE_TYPE.get(request["house_type"])
    if role_name and visitor_member is not None:
        roles, _ = find_roles(guild.roles, [role_name])
        if roles:
            try:
                await visitor_member.remove_roles(*roles, reason="Guest pass ended")
            except discord.HTTPException:
                log.exception("estate: couldn't remove %s from %s", role_name, visitor_member)
    for thread in (await _resolve_thread(guild, request)).values():
        try:
            if visitor_member is not None:
                await thread.remove_user(visitor_member)
        except discord.HTTPException:
            log.exception("estate: couldn't remove visitor from thread %s", thread.id)


def _is_officer(member, state):
    if member.guild_permissions.administrator:
        return True
    role_names = {r.name.casefold() for r in member.roles}
    return "housing officer" in role_names and f"{state} employee".casefold() in role_names


async def _generate_code():
    for _ in range(20):
        code = f"{random.randint(0, 9999):04d}"
        if not await gdb.code_in_use(code):
            return code
    raise RuntimeError("Couldn't find a free guest pass code — is every 4-digit code in use?")


# ---------------------------------------------------------------------------
# Phone entry point (called from cogs/phone.py)
# ---------------------------------------------------------------------------

def _estate_menu_embed():
    return discord.Embed(title="🏘️ Estate", description="What would you like to do?", colour=0x2E7D32)


class EstateMenuView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=900)
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your phone.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Apply for Pass", emoji="📝", style=discord.ButtonStyle.primary, row=0)
    async def apply(self, interaction, button):
        await interaction.response.send_modal(ApplyModal(self.owner_id))

    @discord.ui.button(label="Redeem Code", emoji="🔑", style=discord.ButtonStyle.secondary, row=0)
    async def redeem(self, interaction, button):
        await interaction.response.send_modal(RedeemModal(self.owner_id))

    @discord.ui.button(label="End Visit", emoji="🚪", style=discord.ButtonStyle.secondary, row=1)
    async def end_visit(self, interaction, button):
        rows = await gdb.get_active_for_visitor(interaction.user.id)
        if not rows:
            await interaction.response.send_message("You don't have any active passes.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Which pass do you want to end?", view=EndVisitView(self.owner_id, rows), ephemeral=True)

    @discord.ui.button(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        from cogs.phone import show_home
        await show_home(interaction)


async def open_estate(interaction):
    await interaction.response.defer()
    await _show(interaction, _estate_menu_embed(), EstateMenuView(interaction.user.id))


# ---------------------------------------------------------------------------
# Apply for Pass
# ---------------------------------------------------------------------------

class ApplyModal(discord.ui.Modal, title="Apply for a Guest Pass"):
    def __init__(self, owner_id):
        super().__init__()
        self.owner_id = owner_id
        self.visitor = discord.ui.TextInput(label="Visitor (mention, ID, or username)", max_length=100)
        self.threads = discord.ui.TextInput(
            label="Threads to grant (comma-separated, or 'all')", max_length=200,
            placeholder="e.g. parlour, bathroom  —  or  all")
        self.minutes = discord.ui.TextInput(label="Requested duration (minutes)", max_length=6, placeholder="60")
        for item in (self.visitor, self.threads, self.minutes):
            self.add_item(item)

    async def on_submit(self, interaction):
        owner = await database.get_player_by_discord_id(interaction.user.id)
        if not owner:
            await interaction.response.send_message("Something went wrong finding your character.",
                                                      ephemeral=True)
            return

        state, house_type = owner["current_state"], owner["current_sub_location"]
        assignment = await hdb.get_assignment(owner["player_id"], state) if state else None
        if not assignment or assignment["house_type"] != house_type:
            await interaction.response.send_message(
                "You need to be standing in your own house to apply for a pass.", ephemeral=True)
            return

        visitor_member = _parse_visitor(interaction.guild, self.visitor.value)
        if visitor_member is None or visitor_member.id == interaction.user.id:
            await interaction.response.send_message("Couldn't find that visitor.", ephemeral=True)
            return
        visitor_player = await database.get_player_by_discord_id(visitor_member.id)
        if not visitor_player:
            await interaction.response.send_message(f"{visitor_member.display_name} hasn't arrived yet.",
                                                      ephemeral=True)
            return

        until = await gdb.get_cooldown_until(owner["player_id"], visitor_player["player_id"])
        now = dt.datetime.now(dt.timezone.utc)
        if until and until > now:
            minutes_left = int((until - now).total_seconds() // 60) + 1
            await interaction.response.send_message(
                f"You can't invite {visitor_member.display_name} again for another {minutes_left} minute(s).",
                ephemeral=True)
            return

        options = _room_options(house_type)
        keys, error = _parse_thread_keys(self.threads.value, options)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        try:
            requested_minutes = int(self.minutes.value.strip())
        except ValueError:
            requested_minutes = -1
        if not (0 < requested_minutes <= gcfg.MAX_REQUESTED_MINUTES):
            await interaction.response.send_message(
                f"Duration must be a whole number of minutes, up to {gcfg.MAX_REQUESTED_MINUTES}.",
                ephemeral=True)
            return

        request_id = await gdb.create_request(
            state, house_type, assignment["assignment_id"], owner["player_id"], visitor_player["player_id"],
            visitor_member.id, keys, requested_minutes)

        staff_office = state_location_channels(interaction.guild, state).get("staff-office")
        if staff_office is not None:
            house_label = cfg.HOUSE_TYPES[house_type][1]
            embed = discord.Embed(
                title="🔔 Guest Pass Request",
                description=(
                    f"**Owner:** {interaction.user.mention}\n"
                    f"**Visitor:** {visitor_member.mention}\n"
                    f"**House:** {house_label} ({state})\n"
                    f"**Threads:** {', '.join(options[k] for k in keys)}\n"
                    f"**Requested:** {requested_minutes} minute(s)"
                ),
                colour=0x2E7D32,
            )
            message = await staff_office.send(embed=embed, view=ApprovalView(request_id, state),
                                               allowed_mentions=NO_PINGS)
            await gdb.set_message_ref(request_id, staff_office.id, message.id)

        await interaction.response.send_message(
            "Your guest pass request has been sent to the Housing Officers.", ephemeral=True)


# ---------------------------------------------------------------------------
# Officer approval
# ---------------------------------------------------------------------------

class DurationModal(discord.ui.Modal, title="Set Pass Duration"):
    def __init__(self, request_id, state, requested_minutes):
        super().__init__()
        self.request_id, self.state = request_id, state
        self.minutes = discord.ui.TextInput(label="Granted duration (minutes)", default=str(requested_minutes),
                                            max_length=6)
        self.add_item(self.minutes)

    async def on_submit(self, interaction):
        try:
            granted = int(self.minutes.value.strip())
        except ValueError:
            granted = -1
        if not (0 < granted <= gcfg.MAX_REQUESTED_MINUTES):
            await interaction.response.send_message("Enter a whole number of minutes.", ephemeral=True)
            return
        code = await _generate_code()
        await gdb.approve(self.request_id, granted, code, interaction.user.id)
        request = await gdb.get_request(self.request_id)

        embed = interaction.message.embeds[0]
        embed.colour = 0x1565C0
        embed.add_field(name="✅ Approved", value=(
            f"By {interaction.user.mention} — granted {granted} minute(s).\n"
            f"**Code:** `{code}` — relay this to the visitor yourself."
        ), inline=False)
        await interaction.response.edit_message(embed=embed, view=PostApprovalView(request["request_id"]))


class ApprovalView(discord.ui.View):
    def __init__(self, request_id, state):
        super().__init__(timeout=None)
        self.request_id, self.state = request_id, state

    async def interaction_check(self, interaction):
        if not _is_officer(interaction.user, self.state):
            await interaction.response.send_message("Only Housing Officers can decide this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Approve", emoji="✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction, button):
        request = await gdb.get_request(self.request_id)
        if request is None or request["status"] != "pending":
            await interaction.response.send_message("This request has already been decided.", ephemeral=True)
            return
        await interaction.response.send_modal(
            DurationModal(self.request_id, self.state, request["requested_minutes"]))

    @discord.ui.button(label="Deny", emoji="❌", style=discord.ButtonStyle.danger)
    async def deny(self, interaction, button):
        request = await gdb.get_request(self.request_id)
        if request is None or request["status"] != "pending":
            await interaction.response.send_message("This request has already been decided.", ephemeral=True)
            return
        await gdb.deny(self.request_id, interaction.user.id)
        embed = interaction.message.embeds[0]
        embed.colour = 0xB71C1C
        embed.add_field(name="❌ Denied", value=f"By {interaction.user.mention}", inline=False)
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)


class PostApprovalView(discord.ui.View):
    """Shown on an approved-but-not-yet-redeemed request: lets an officer cancel the code."""

    def __init__(self, request_id):
        super().__init__(timeout=None)
        self.request_id = request_id

    @discord.ui.button(label="Revoke (unredeemed)", emoji="🚫", style=discord.ButtonStyle.danger)
    async def revoke(self, interaction, button):
        request = await gdb.get_request(self.request_id)
        if request is None or request["status"] != "ready":
            await interaction.response.send_message(
                "This code has already been used or is no longer live.", ephemeral=True)
            return
        if not _is_officer(interaction.user, request["state"]):
            await interaction.response.send_message("Only Housing Officers can do that.", ephemeral=True)
            return
        await gdb.revoke_ready(self.request_id, interaction.user.id)
        embed = interaction.message.embeds[0]
        embed.colour = 0x757575
        embed.add_field(name="🚫 Revoked", value=f"By {interaction.user.mention} before redemption.", inline=False)
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)


# ---------------------------------------------------------------------------
# Redeem Code
# ---------------------------------------------------------------------------

class RedeemModal(discord.ui.Modal, title="Redeem Guest Pass Code"):
    def __init__(self, owner_id):
        super().__init__()
        self.owner_id = owner_id
        self.code = discord.ui.TextInput(label="4-digit code", min_length=gcfg.CODE_LENGTH,
                                         max_length=gcfg.CODE_LENGTH)
        self.add_item(self.code)

    async def on_submit(self, interaction):
        code = self.code.value.strip()
        request = await gdb.find_ready_by_code(interaction.user.id, code)
        if request is None:
            await interaction.response.send_message("Invalid or expired code.", ephemeral=True)
            return
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=request["granted_minutes"])
        await gdb.redeem(request["request_id"], expires_at)
        await _grant_access(interaction.guild, request, interaction.user)
        await interaction.response.send_message(
            f"✅ Pass redeemed — you have access until <t:{int(expires_at.timestamp())}:t>.", ephemeral=True)


# ---------------------------------------------------------------------------
# End Visit
# ---------------------------------------------------------------------------

class EndVisitSelect(discord.ui.Select):
    def __init__(self, rows):
        options = [
            discord.SelectOption(label=f"{cfg.HOUSE_TYPES[row['house_type']][1]} ({row['state']})",
                                 value=str(row["request_id"]))
            for row in rows
        ]
        super().__init__(placeholder="Choose a pass to end", options=options)

    async def callback(self, interaction):
        request_id = int(self.values[0])
        request = await gdb.get_request(request_id)
        if request is None or request["status"] != "redeemed":
            await interaction.response.send_message("That pass is no longer active.", ephemeral=True)
            return
        await gdb.end_early(request_id)
        await _revoke_access(interaction.guild, request, interaction.user)
        await interaction.response.edit_message(content="Visit ended — access removed, no cooldown.", view=None)


class EndVisitView(discord.ui.View):
    def __init__(self, owner_id, rows):
        super().__init__(timeout=120)
        self.add_item(EndVisitSelect(rows))


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Estate(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._expiry_ticker.start()

    async def cog_load(self):
        await gdb.init_tables()

    def cog_unload(self):
        self._expiry_ticker.cancel()

    @tasks.loop(seconds=gcfg.AUTO_EXPIRE_POLL_SECONDS)
    async def _expiry_ticker(self):
        for request in await gdb.get_due_for_expiry():
            guild = next((g for g in self.bot.guilds if g.get_member(request["visitor_discord_id"])), None)
            guild = guild or (self.bot.guilds[0] if self.bot.guilds else None)
            if guild is None:
                continue
            visitor_member = guild.get_member(request["visitor_discord_id"])
            try:
                await _revoke_access(guild, request, visitor_member)
            except Exception:
                log.exception("estate: auto-expiry cleanup failed for request %s", request["request_id"])
            await gdb.expire(request["request_id"])
            until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=gcfg.COOLDOWN_MINUTES)
            await gdb.set_cooldown(request["owner_player_id"], request["visitor_player_id"], until)

    @_expiry_ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Estate(bot))
