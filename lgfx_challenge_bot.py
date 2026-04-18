import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

# =========================
# LGFX CHALLENGE BOT
# =========================
# Single-file Discord bot for a 5-day alliance duo challenge.
# Features:
# - Team registration
# - Daily challenge management
# - Submission tracking
# - Leadership approval workflow
# - Automatic points on approval
# - Bonus points
# - Leaderboard
# - Persistent SQLite storage
#
# Requirements:
#   pip install -U discord.py
#
# Environment variables:
#   DISCORD_TOKEN=your_bot_token
#   GUILD_ID=your_discord_server_id
#   LEADER_ROLE_NAME=Leadership
#
# Optional:
#   DB_PATH=lgfx_challenge.db
#
# Notes:
# - Best used as a semi-automated bot: players submit, leadership approves.
# - Restrict slash commands in Discord Server Settings > Integrations > Command Permissions,
#   or use the leadership role gate in this code.
#

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
LEADER_ROLE_NAME = os.getenv("LEADER_ROLE_NAME", "Leadership")
DB_PATH = os.getenv("DB_PATH", "lgfx_challenge.db")

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
if not GUILD_ID:
    raise RuntimeError("Missing or invalid GUILD_ID environment variable.")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
message_content = False
intents.message_content = message_content

bot = commands.Bot(command_prefix="!", intents=intents)
GUILD_OBJECT = discord.Object(id=GUILD_ID)


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS teams (
                team_name TEXT PRIMARY KEY,
                member1_id INTEGER NOT NULL,
                member2_id INTEGER,
                member3_id INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS challenges (
                day INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                base_points INTEGER NOT NULL DEFAULT 100,
                is_open INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day INTEGER NOT NULL,
                team_name TEXT NOT NULL,
                submitter_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                attachment_url TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                awarded_points INTEGER NOT NULL DEFAULT 0,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(day, team_name)
            );

            CREATE TABLE IF NOT EXISTS bonuses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name TEXT NOT NULL,
                points INTEGER NOT NULL,
                reason TEXT NOT NULL,
                granted_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

        # Seed 5 challenge days if empty
        count = conn.execute("SELECT COUNT(*) AS c FROM challenges").fetchone()["c"]
        if count == 0:
            seed = [
                (1, "Find Your Partner", "Post one screenshot together on the map, both coordinates, and 3 facts about your teammate.", 100, 0),
                (2, "LGFX Quiz War", "Answer the alliance quiz together. One submission per duo.", 100, 0),
                (3, "Barbarian Hunters", "Hunt barbarians together and submit proof screenshot.", 100, 0),
                (4, "Map Detectives", "Find the mystery location from the screenshot and submit exact coordinates.", 100, 0),
                (5, "Trust Mission", "Complete one coordinated action together and write what you learned about your teammate.", 100, 0),
            ]
            now = utc_now()
            conn.executemany(
                "INSERT INTO challenges(day, title, description, base_points, is_open, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(d, t, desc, pts, open_, now) for d, t, desc, pts, open_ in seed],
            )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_leader(member: discord.Member) -> bool:
    return any(role.name == LEADER_ROLE_NAME for role in member.roles) or member.guild_permissions.administrator


def leader_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used inside the server.", ephemeral=True)
            return False
        if not is_leader(interaction.user):
            await interaction.response.send_message(
                f"You need the **{LEADER_ROLE_NAME}** role (or admin) to use this command.",
                ephemeral=True,
            )
            return False
        return True

    return app_commands.check(predicate)


def get_team(team_name: str) -> Optional[sqlite3.Row]:
    with db_conn() as conn:
        return conn.execute("SELECT * FROM teams WHERE team_name = ?", (team_name,)).fetchone()


def get_challenge(day: int) -> Optional[sqlite3.Row]:
    with db_conn() as conn:
        return conn.execute("SELECT * FROM challenges WHERE day = ?", (day,)).fetchone()


def team_total_points(team_name: str) -> int:
    with db_conn() as conn:
        approved = conn.execute(
            "SELECT COALESCE(SUM(awarded_points), 0) AS total FROM submissions WHERE team_name = ? AND status = 'approved'",
            (team_name,),
        ).fetchone()["total"]
        bonuses = conn.execute(
            "SELECT COALESCE(SUM(points), 0) AS total FROM bonuses WHERE team_name = ?",
            (team_name,),
        ).fetchone()["total"]
        return int(approved) + int(bonuses)


def all_team_scores():
    with db_conn() as conn:
        teams = conn.execute("SELECT team_name FROM teams ORDER BY team_name ASC").fetchall()
    results = []
    for row in teams:
        results.append((row["team_name"], team_total_points(row["team_name"])))
    results.sort(key=lambda x: (-x[1], x[0]))
    return results


async def build_leaderboard_embed(guild: discord.Guild) -> discord.Embed:
    scores = all_team_scores()
    embed = discord.Embed(title="🏆 LGFX Challenge Leaderboard", color=discord.Color.gold())
    if not scores:
        embed.description = "No teams registered yet."
        return embed

    lines = []
    for idx, (team_name, score) in enumerate(scores[:25], start=1):
        team = get_team(team_name)
        members = []
        if team:
            for key in ("member1_id", "member2_id", "member3_id"):
                uid = team[key]
                if uid:
                    member = guild.get_member(uid)
                    members.append(member.display_name if member else f"User {uid}")
        member_text = " + ".join(members) if members else "Unknown members"
        medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
        lines.append(f"{medal} **{team_name}** — {score} pts\n↳ {member_text}")

    embed.description = "\n\n".join(lines)
    embed.set_footer(text=f"Leadership role required for approvals: {LEADER_ROLE_NAME}")
    return embed


@bot.event
async def on_ready():
    init_db()
    try:
        synced = await bot.tree.sync(guild=GUILD_OBJECT)
        print(f"Logged in as {bot.user} | Synced {len(synced)} guild commands")
    except Exception as exc:
        print(f"Failed to sync commands: {exc}")


@bot.tree.command(name="register_team", description="Register a duo or trio for the LGFX challenge.", guild=GUILD_OBJECT)
@leader_only()
@app_commands.describe(
    team_name="Example: Duo-01",
    member1="First member",
    member2="Second member",
    member3="Optional third member if needed",
)
async def register_team(
    interaction: discord.Interaction,
    team_name: str,
    member1: discord.Member,
    member2: discord.Member,
    member3: Optional[discord.Member] = None,
):
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO teams(team_name, member1_id, member2_id, member3_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(team_name) DO UPDATE SET
                member1_id = excluded.member1_id,
                member2_id = excluded.member2_id,
                member3_id = excluded.member3_id
            """,
            (team_name, member1.id, member2.id, member3.id if member3 else None, utc_now()),
        )

    members = [member1.mention, member2.mention]
    if member3:
        members.append(member3.mention)
    await interaction.response.send_message(
        f"✅ Team **{team_name}** registered: {' + '.join(members)}",
        ephemeral=False,
    )


@bot.tree.command(name="list_teams", description="List all registered teams.", guild=GUILD_OBJECT)
@leader_only()
async def list_teams(interaction: discord.Interaction):
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM teams ORDER BY team_name ASC").fetchall()

    if not rows:
        await interaction.response.send_message("No teams registered yet.", ephemeral=True)
        return

    embed = discord.Embed(title="LGFX Registered Teams", color=discord.Color.blurple())
    for row in rows[:25]:
        members = []
        for key in ("member1_id", "member2_id", "member3_id"):
            uid = row[key]
            if uid:
                member = interaction.guild.get_member(uid)
                members.append(member.display_name if member else f"User {uid}")
        embed.add_field(name=row["team_name"], value=" + ".join(members), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="open_day", description="Open a challenge day for submissions.", guild=GUILD_OBJECT)
@leader_only()
@app_commands.describe(day="Challenge day number: 1 to 5")
async def open_day(interaction: discord.Interaction, day: app_commands.Range[int, 1, 5]):
    with db_conn() as conn:
        updated = conn.execute("UPDATE challenges SET is_open = 1 WHERE day = ?", (day,)).rowcount
    if not updated:
        await interaction.response.send_message(f"Day {day} does not exist.", ephemeral=True)
        return

    challenge = get_challenge(day)
    embed = discord.Embed(
        title=f"📣 Day {day} Open — {challenge['title']}",
        description=challenge["description"],
        color=discord.Color.green(),
    )
    embed.add_field(name="Base Points", value=str(challenge["base_points"]))
    embed.set_footer(text="Teams can now submit using /submit")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="close_day", description="Close a challenge day.", guild=GUILD_OBJECT)
@leader_only()
@app_commands.describe(day="Challenge day number: 1 to 5")
async def close_day(interaction: discord.Interaction, day: app_commands.Range[int, 1, 5]):
    with db_conn() as conn:
        updated = conn.execute("UPDATE challenges SET is_open = 0 WHERE day = ?", (day,)).rowcount
    if not updated:
        await interaction.response.send_message(f"Day {day} does not exist.", ephemeral=True)
        return
    await interaction.response.send_message(f"🔒 Day {day} is now closed.")


@bot.tree.command(name="day_info", description="View a challenge day description and status.", guild=GUILD_OBJECT)
@app_commands.describe(day="Challenge day number: 1 to 5")
async def day_info(interaction: discord.Interaction, day: app_commands.Range[int, 1, 5]):
    challenge = get_challenge(day)
    if not challenge:
        await interaction.response.send_message(f"Day {day} does not exist.", ephemeral=True)
        return

    status = "Open" if challenge["is_open"] else "Closed"
    embed = discord.Embed(title=f"Day {day} — {challenge['title']}", description=challenge["description"], color=discord.Color.blurple())
    embed.add_field(name="Status", value=status)
    embed.add_field(name="Base Points", value=str(challenge["base_points"]))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="submit", description="Submit a challenge entry for your team.", guild=GUILD_OBJECT)
@app_commands.describe(
    day="Challenge day number: 1 to 5",
    team_name="Your registered team name",
    content="Your answer, notes, coordinates, or proof summary",
    attachment_url="Optional screenshot or image link",
)
async def submit(
    interaction: discord.Interaction,
    day: app_commands.Range[int, 1, 5],
    team_name: str,
    content: str,
    attachment_url: Optional[str] = None,
):
    team = get_team(team_name)
    if not team:
        await interaction.response.send_message(f"Team **{team_name}** is not registered.", ephemeral=True)
        return

    member_ids = {team["member1_id"], team["member2_id"], team["member3_id"]}
    member_ids.discard(None)
    if interaction.user.id not in member_ids:
        await interaction.response.send_message("You are not a member of that team.", ephemeral=True)
        return

    challenge = get_challenge(day)
    if not challenge:
        await interaction.response.send_message(f"Day {day} does not exist.", ephemeral=True)
        return
    if not challenge["is_open"]:
        await interaction.response.send_message(f"Day {day} is currently closed.", ephemeral=True)
        return

    try:
        with db_conn() as conn:
            conn.execute(
                """
                INSERT INTO submissions(day, team_name, submitter_id, content, attachment_url, status, awarded_points, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)
                """,
                (day, team_name, interaction.user.id, content, attachment_url, utc_now()),
            )
    except sqlite3.IntegrityError:
        await interaction.response.send_message(
            f"Team **{team_name}** already submitted for Day {day}. Use leadership to reject/reset if needed.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"📝 Submission received for **{team_name}** on Day {day}. Status: **pending approval**.",
        ephemeral=False,
    )


@bot.tree.command(name="pending", description="List pending submissions.", guild=GUILD_OBJECT)
@leader_only()
async def pending(interaction: discord.Interaction):
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, day, team_name, submitter_id, created_at FROM submissions WHERE status = 'pending' ORDER BY day ASC, created_at ASC"
        ).fetchall()

    if not rows:
        await interaction.response.send_message("No pending submissions right now.", ephemeral=True)
        return

    embed = discord.Embed(title="Pending LGFX Submissions", color=discord.Color.orange())
    for row in rows[:25]:
        submitter = interaction.guild.get_member(row["submitter_id"])
        who = submitter.display_name if submitter else f"User {row['submitter_id']}"
        embed.add_field(
            name=f"ID {row['id']} — {row['team_name']} / Day {row['day']}",
            value=f"Submitted by: {who}\nAt: {row['created_at']}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="view_submission", description="View a submission by ID.", guild=GUILD_OBJECT)
@leader_only()
@app_commands.describe(submission_id="Submission ID from /pending")
async def view_submission(interaction: discord.Interaction, submission_id: int):
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()

    if not row:
        await interaction.response.send_message("Submission not found.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Submission {row['id']} — {row['team_name']} / Day {row['day']}",
        description=row["content"],
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Status", value=row["status"])
    embed.add_field(name="Awarded Points", value=str(row["awarded_points"]))
    if row["attachment_url"]:
        embed.add_field(name="Attachment URL", value=row["attachment_url"], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="approve", description="Approve a pending submission and award points automatically.", guild=GUILD_OBJECT)
@leader_only()
@app_commands.describe(submission_id="Submission ID from /pending", extra_points="Optional extra points on top of base points")
async def approve(interaction: discord.Interaction, submission_id: int, extra_points: Optional[int] = 0):
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
        if not row:
            await interaction.response.send_message("Submission not found.", ephemeral=True)
            return
        if row["status"] != "pending":
            await interaction.response.send_message(f"Submission already reviewed: {row['status']}", ephemeral=True)
            return

        challenge = conn.execute("SELECT * FROM challenges WHERE day = ?", (row["day"],)).fetchone()
        points = int(challenge["base_points"]) + int(extra_points or 0)
        conn.execute(
            """
            UPDATE submissions
            SET status = 'approved', awarded_points = ?, reviewed_by = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (points, interaction.user.id, utc_now(), submission_id),
        )

    total = team_total_points(row["team_name"])
    await interaction.response.send_message(
        f"✅ Approved submission **#{submission_id}** for **{row['team_name']}**.\n"
        f"Awarded: **{points} pts**\n"
        f"Team total: **{total} pts**"
    )


@bot.tree.command(name="reject", description="Reject a pending submission.", guild=GUILD_OBJECT)
@leader_only()
@app_commands.describe(submission_id="Submission ID from /pending", reason="Short reason for rejection")
async def reject(interaction: discord.Interaction, submission_id: int, reason: str):
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
        if not row:
            await interaction.response.send_message("Submission not found.", ephemeral=True)
            return
        if row["status"] != "pending":
            await interaction.response.send_message(f"Submission already reviewed: {row['status']}", ephemeral=True)
            return
        conn.execute(
            "UPDATE submissions SET status = 'rejected', reviewed_by = ?, reviewed_at = ?, awarded_points = 0 WHERE id = ?",
            (interaction.user.id, utc_now(), submission_id),
        )

    await interaction.response.send_message(
        f"❌ Rejected submission **#{submission_id}** for **{row['team_name']}**. Reason: {reason}"
    )


@bot.tree.command(name="bonus", description="Grant bonus points to a team.", guild=GUILD_OBJECT)
@leader_only()
@app_commands.describe(team_name="Registered team name", points="Bonus points to add", reason="Reason for the bonus")
async def bonus(interaction: discord.Interaction, team_name: str, points: int, reason: str):
    if not get_team(team_name):
        await interaction.response.send_message("Unknown team name.", ephemeral=True)
        return

    with db_conn() as conn:
        conn.execute(
            "INSERT INTO bonuses(team_name, points, reason, granted_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (team_name, points, reason, interaction.user.id, utc_now()),
        )

    total = team_total_points(team_name)
    await interaction.response.send_message(
        f"✨ Bonus applied to **{team_name}**: **{points} pts**\nReason: {reason}\nTeam total: **{total} pts**"
    )


@bot.tree.command(name="team_score", description="See the score of one team.", guild=GUILD_OBJECT)
@app_commands.describe(team_name="Registered team name")
async def team_score(interaction: discord.Interaction, team_name: str):
    if not get_team(team_name):
        await interaction.response.send_message("Unknown team name.", ephemeral=True)
        return
    total = team_total_points(team_name)
    await interaction.response.send_message(f"📊 **{team_name}** has **{total} points**.")


@bot.tree.command(name="leaderboard", description="Show the current leaderboard.", guild=GUILD_OBJECT)
async def leaderboard(interaction: discord.Interaction):
    embed = await build_leaderboard_embed(interaction.guild)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="remove_team", description="Remove a team if you need to rebuild pairings.", guild=GUILD_OBJECT)
@leader_only()
@app_commands.describe(team_name="Registered team name")
async def remove_team(interaction: discord.Interaction, team_name: str):
    with db_conn() as conn:
        conn.execute("DELETE FROM teams WHERE team_name = ?", (team_name,))
        conn.execute("DELETE FROM submissions WHERE team_name = ?", (team_name,))
        conn.execute("DELETE FROM bonuses WHERE team_name = ?", (team_name,))
    await interaction.response.send_message(f"🗑️ Removed team **{team_name}** and its related challenge data.")


@bot.tree.command(name="reset_submission", description="Delete a team's submission for a day so they can resubmit.", guild=GUILD_OBJECT)
@leader_only()
@app_commands.describe(team_name="Registered team name", day="Challenge day number")
async def reset_submission(interaction: discord.Interaction, team_name: str, day: app_commands.Range[int, 1, 5]):
    with db_conn() as conn:
        conn.execute("DELETE FROM submissions WHERE team_name = ? AND day = ?", (team_name, day))
    await interaction.response.send_message(f"♻️ Submission reset for **{team_name}** on Day {day}.")


@bot.tree.command(name="help_lgfx", description="Show bot setup help and key commands.", guild=GUILD_OBJECT)
async def help_lgfx(interaction: discord.Interaction):
    embed = discord.Embed(title="LGFX Challenge Bot Help", color=discord.Color.blurple())
    embed.description = (
        "**Player commands**\n"
        "• `/day_info`\n"
        "• `/submit`\n"
        "• `/team_score`\n"
        "• `/leaderboard`\n\n"
        "**Leadership commands**\n"
        "• `/register_team`\n"
        "• `/list_teams`\n"
        "• `/open_day` / `/close_day`\n"
        "• `/pending` / `/view_submission`\n"
        "• `/approve` / `/reject`\n"
        "• `/bonus`\n"
        "• `/reset_submission`\n"
        "• `/remove_team`"
    )
    embed.set_footer(text=f"Restricted leadership role: {LEADER_ROLE_NAME}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
