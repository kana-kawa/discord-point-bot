"""Discordコミュニティ向け ポイント制度Bot（試作品）。

できること
- 投稿に一定数のリアクションがつくと、運営チャンネルに「承認／却下」ボタンつきで通知
- メッセージの右クリックメニュー「回答として推薦」で、良い回答を運営に推薦
- 承認するとポイントを付与し、本人にDMで通知
- /points（残高と履歴）、/ranking（月間ランキング）、/gifts（交換できるギフト）
- /exchange（交換申請 → ポイントを仮確保 → 運営が承認・却下、却下ならポイント返却）
- /admin ...（運営だけ：手動の付与・減算、ギフトの登録・削除、操作ログの確認）
"""

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from db import PointsDB, PointsError

load_dotenv()


def env_int(name: str, default: int | None = None) -> int | None:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = env_int("DISCORD_GUILD_ID")
ADMIN_CHANNEL_ID = env_int("ADMIN_CHANNEL_ID")
ADMIN_ROLE_ID = env_int("ADMIN_ROLE_ID")  # 空なら「サーバー管理」権限を持つ人が運営
WATCH_CHANNEL_IDS = {
    int(x) for x in os.environ.get("WATCH_CHANNEL_IDS", "").split(",") if x.strip()
}  # 空なら全チャンネルを見る
REACTION_THRESHOLD = env_int("REACTION_THRESHOLD", 3)
POINTS_PER_POST = env_int("POINTS_PER_POST", 10)
POINTS_PER_ANSWER = env_int("POINTS_PER_ANSWER", 20)
DB_PATH = os.environ.get("DB_PATH", "points.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points_bot")

db = PointsDB(DB_PATH)

intents = discord.Intents.default()  # リアクションの検知はこれで足りる（メッセージ本文は読まない）
bot = commands.Bot(command_prefix="!", intents=intents)
GUILD = discord.Object(id=GUILD_ID)

COLOR_PENDING = discord.Color.gold()
COLOR_OK = discord.Color.green()
COLOR_NG = discord.Color.red()


# ---------- 共通の部品 ----------
def is_admin(user: discord.abc.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if ADMIN_ROLE_ID and any(role.id == ADMIN_ROLE_ID for role in user.roles):
        return True
    return user.guild_permissions.manage_guild


async def get_admin_channel() -> discord.abc.Messageable:
    return bot.get_channel(ADMIN_CHANNEL_ID) or await bot.fetch_channel(ADMIN_CHANNEL_ID)


def decision_buttons(kind: str, item_id: int) -> discord.ui.View:
    """承認・却下ボタン。押されたときの処理は on_button が受け持つので、Botを再起動しても効く。"""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="承認", style=discord.ButtonStyle.success,
                                    custom_id=f"pts:{kind}:approve:{item_id}"))
    view.add_item(discord.ui.Button(label="却下", style=discord.ButtonStyle.danger,
                                    custom_id=f"pts:{kind}:reject:{item_id}"))
    return view


async def notify_user(user_id: int, text: str, fallback_channel_id: int | None = None) -> None:
    """本人にDMで知らせる。DMを受け取らない設定の人には、元のチャンネルでメンションする。"""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(text)
        return
    except discord.HTTPException:
        logger.info("DMを送れませんでした: %s", user_id)
    if fallback_channel_id:
        channel = bot.get_channel(fallback_channel_id) or await bot.fetch_channel(fallback_channel_id)
        await channel.send(f"<@{user_id}> {text}",
                           allowed_mentions=discord.AllowedMentions(users=True))


def candidate_embed(row, status_text: str | None = None) -> discord.Embed:
    source = "リアクション" if row["source"] == "reaction" else "回答の推薦"
    embed = discord.Embed(
        title=f"ポイント付与の候補 #{row['id']}",
        description=f"[投稿を見る]({row['jump_url']})",
        color=COLOR_PENDING if status_text is None else
        (COLOR_OK if row["status"] == "approved" else COLOR_NG),
    )
    embed.add_field(name="投稿者", value=f"<@{row['author_id']}>")
    embed.add_field(name="ポイント", value=f"{row['points']}pt")
    embed.add_field(name="きっかけ", value=source)
    if status_text:
        embed.add_field(name="結果", value=status_text, inline=False)
    return embed


def exchange_embed(row, status_text: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"ギフト交換の申請 #{row['id']}",
        color=COLOR_PENDING if status_text is None else
        (COLOR_OK if row["status"] == "approved" else COLOR_NG),
    )
    embed.add_field(name="申請者", value=f"<@{row['user_id']}>")
    embed.add_field(name="ギフト", value=row["gift_name"])
    embed.add_field(name="ポイント", value=f"{row['cost']}pt")
    if status_text:
        embed.add_field(name="結果", value=status_text, inline=False)
    return embed


async def post_candidate(row) -> None:
    channel = await get_admin_channel()
    await channel.send(embed=candidate_embed(row), view=decision_buttons("cand", row["id"]),
                       allowed_mentions=discord.AllowedMentions.none())


# ---------- リアクションの検知 ----------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if payload.guild_id != GUILD_ID or payload.user_id == bot.user.id:
        return
    if WATCH_CHANNEL_IDS and payload.channel_id not in WATCH_CHANNEL_IDS:
        return
    if payload.channel_id == ADMIN_CHANNEL_ID:
        return

    channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)
    if message.author.bot:
        return

    # 投稿者本人とBotを除いた「リアクションした人数」を数える（1人で何個押しても1人）
    reactors: set[int] = set()
    for reaction in message.reactions:
        async for user in reaction.users():
            if not user.bot and user.id != message.author.id:
                reactors.add(user.id)
    if len(reactors) < REACTION_THRESHOLD:
        return

    row = db.add_candidate(message.id, channel.id, message.author.id,
                           message.jump_url, POINTS_PER_POST, "reaction")
    if row:  # すでに候補になっている投稿は二重に通知しない
        await post_candidate(row)


# ---------- 回答の推薦（メッセージの右クリックメニュー） ----------
@bot.tree.context_menu(name="回答として推薦")
async def recommend_answer(interaction: discord.Interaction, message: discord.Message) -> None:
    if message.author.bot:
        await interaction.response.send_message("Botの投稿は推薦できません。", ephemeral=True)
        return
    if message.author.id == interaction.user.id:
        await interaction.response.send_message("自分の投稿は推薦できません。", ephemeral=True)
        return
    row = db.add_candidate(message.id, message.channel.id, message.author.id,
                           message.jump_url, POINTS_PER_ANSWER, "recommend")
    if row is None:
        await interaction.response.send_message("この投稿はすでに運営の確認待ちです。", ephemeral=True)
        return
    await post_candidate(row)
    await interaction.response.send_message("運営に推薦しました。ありがとうございます！", ephemeral=True)


# ---------- メンバー向けコマンド ----------
@bot.tree.command(name="points", description="自分のポイント残高と最近の履歴を見る")
async def points_cmd(interaction: discord.Interaction) -> None:
    uid = interaction.user.id
    lines = [f"{r['created_at'][5:16].replace('T', ' ')}　{r['amount']:+}pt　{r['reason']}"
             for r in db.history(uid)]
    embed = discord.Embed(title="あなたのポイント", color=COLOR_OK)
    embed.add_field(name="残高", value=f"**{db.balance(uid)}pt**", inline=False)
    embed.add_field(name="最近の履歴", value="\n".join(lines) or "まだありません", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ranking", description="今月のポイントランキングを見る")
async def ranking_cmd(interaction: discord.Interaction) -> None:
    rows = db.monthly_ranking(limit=10)
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"{medals.get(i, f'{i}位')}　<@{r['user_id']}>　{r['total']}pt"
             for i, r in enumerate(rows, start=1)]
    embed = discord.Embed(title="今月のポイントランキング",
                          description="\n".join(lines) or "今月はまだ獲得者がいません",
                          color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed,
                                            allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="gifts", description="ポイントと交換できるギフトの一覧")
async def gifts_cmd(interaction: discord.Interaction) -> None:
    lines = [f"・{g['name']}　{g['cost']}pt" for g in db.list_gifts()]
    await interaction.response.send_message(
        "\n".join(["**交換できるギフト**", *lines]) if lines else "交換できるギフトはまだありません。",
        ephemeral=True,
    )


async def gift_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=f"{g['name']}（{g['cost']}pt）", value=str(g["id"]))
            for g in db.list_gifts() if current in g["name"]][:25]


@bot.tree.command(name="exchange", description="ポイントをギフトと交換する（運営の承認後に送付）")
@app_commands.describe(gift="交換するギフト")
@app_commands.autocomplete(gift=gift_autocomplete)
async def exchange_cmd(interaction: discord.Interaction, gift: str) -> None:
    try:
        row = db.request_exchange(interaction.user.id, int(gift))
    except (PointsError, ValueError) as e:
        msg = str(e) if isinstance(e, PointsError) else "一覧からギフトを選んでください。"
        await interaction.response.send_message(msg, ephemeral=True)
        return
    channel = await get_admin_channel()
    await channel.send(embed=exchange_embed(row), view=decision_buttons("exch", row["id"]),
                       allowed_mentions=discord.AllowedMentions.none())
    await interaction.response.send_message(
        f"{row['gift_name']}の交換を申請しました。{row['cost']}ptを確保しています"
        f"（残高 {db.balance(interaction.user.id)}pt）。運営の承認をお待ちください。",
        ephemeral=True,
    )


# ---------- 運営向けコマンド（/admin ...） ----------
admin_group = app_commands.Group(
    name="admin", description="運営メンバー用のコマンド",
    default_permissions=discord.Permissions(manage_guild=True),  # 一般メンバーには表示しない
    guild_only=True,
)


async def admin_only(interaction: discord.Interaction) -> bool:
    if not is_admin(interaction.user):
        raise app_commands.CheckFailure("運営メンバーだけが使えます。")
    return True


admin_group.interaction_check = admin_only  # type: ignore[method-assign]


@admin_group.command(name="give", description="ポイントを付与する")
@app_commands.describe(member="対象のメンバー", amount="付与するポイント", reason="理由")
async def admin_give(interaction: discord.Interaction, member: discord.Member,
                     amount: app_commands.Range[int, 1, 100000], reason: str) -> None:
    balance = db.adjust(member.id, amount, f"運営による付与：{reason}", interaction.user.id)
    await notify_user(member.id, f"🎉 運営から {amount}pt が付与されました（{reason}）。残高 {balance}pt")
    await interaction.response.send_message(
        f"{member.mention} に {amount}pt を付与しました（残高 {balance}pt）。", ephemeral=True)


@admin_group.command(name="take", description="ポイントを減らす・取り消す")
@app_commands.describe(member="対象のメンバー", amount="減らすポイント", reason="理由")
async def admin_take(interaction: discord.Interaction, member: discord.Member,
                     amount: app_commands.Range[int, 1, 100000], reason: str) -> None:
    try:
        balance = db.adjust(member.id, -amount, f"運営による減算：{reason}", interaction.user.id)
    except PointsError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    await interaction.response.send_message(
        f"{member.mention} から {amount}pt を減らしました（残高 {balance}pt）。", ephemeral=True)


@admin_group.command(name="check", description="メンバーのポイントと履歴を確認する")
async def admin_check(interaction: discord.Interaction, member: discord.Member) -> None:
    lines = [f"{r['created_at'][5:16].replace('T', ' ')}　{r['amount']:+}pt　{r['reason']}"
             for r in db.history(member.id, limit=15)]
    await interaction.response.send_message(
        f"**{member.display_name}** 残高 {db.balance(member.id)}pt\n" + ("\n".join(lines) or "履歴なし"),
        ephemeral=True)


@admin_group.command(name="gift_set", description="ギフトを追加する（同じ名前なら必要ポイントを変更）")
async def admin_gift_set(interaction: discord.Interaction, name: str,
                         cost: app_commands.Range[int, 1, 1000000]) -> None:
    db.upsert_gift(name, cost, interaction.user.id)
    await interaction.response.send_message(f"ギフト「{name}」を {cost}pt で登録しました。", ephemeral=True)


@admin_group.command(name="gift_remove", description="ギフトを交換できないようにする")
async def admin_gift_remove(interaction: discord.Interaction, name: str) -> None:
    ok = db.remove_gift(name, interaction.user.id)
    await interaction.response.send_message(
        f"ギフト「{name}」を一覧から外しました。" if ok else f"ギフト「{name}」は見つかりません。",
        ephemeral=True)


@admin_group.command(name="log", description="最近の操作ログを見る")
async def admin_log(interaction: discord.Interaction) -> None:
    lines = [f"{r['created_at'][5:16].replace('T', ' ')}　"
             f"{'<@' + str(r['actor_id']) + '>' if r['actor_id'] else 'Bot'}　{r['action']}　{r['detail']}"
             for r in db.recent_audit()]
    await interaction.response.send_message("\n".join(lines) or "ログはまだありません。",
                                            ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


bot.tree.add_command(admin_group)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        text = str(error) or "この操作はできません。"
    else:
        logger.exception("コマンドでエラー", exc_info=error)
        text = "エラーが起きました。運営にお知らせください。"
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# ---------- 承認・却下ボタン ----------
@bot.listen("on_interaction")
async def on_button(interaction: discord.Interaction) -> None:
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith("pts:"):
        return
    if not is_admin(interaction.user):
        await interaction.response.send_message("運営メンバーだけが操作できます。", ephemeral=True)
        return

    _, kind, action, item_id = custom_id.split(":")
    approve = action == "approve"
    who = interaction.user.display_name

    if kind == "cand":
        row = db.decide_candidate(int(item_id), approve, interaction.user.id)
        if row is None:  # 別の運営が先に押した、または二重押し
            await interaction.response.send_message("この候補はすでに処理済みです。", ephemeral=True)
            return
        result = f"✅ {who} が承認（{row['points']}pt 付与）" if approve else f"❌ {who} が却下"
        await interaction.response.edit_message(embed=candidate_embed(row, result), view=None)
        if approve:
            await notify_user(
                row["author_id"],
                f"🎉 あなたの投稿に {row['points']}pt が付与されました！ {row['jump_url']}\n"
                f"残高 {db.balance(row['author_id'])}pt",
                fallback_channel_id=row["channel_id"],
            )

    elif kind == "exch":
        row = db.decide_exchange(int(item_id), approve, interaction.user.id)
        if row is None:
            await interaction.response.send_message("この申請はすでに処理済みです。", ephemeral=True)
            return
        result = f"✅ {who} が承認（ギフトを送付してください）" if approve else f"❌ {who} が却下（ポイント返却済み）"
        await interaction.response.edit_message(embed=exchange_embed(row, result), view=None)
        text = (f"🎁 {row['gift_name']}の交換が承認されました。運営からギフトをお送りします。"
                if approve else
                f"{row['gift_name']}の交換申請は承認されませんでした。{row['cost']}ptを返却しました"
                f"（残高 {db.balance(row['user_id'])}pt）。")
        await notify_user(row["user_id"], text)


# ---------- 起動 ----------
@bot.event
async def setup_hook() -> None:
    # サーバー限定でコマンドを登録すると、すぐに反映される
    bot.tree.copy_global_to(guild=GUILD)
    await bot.tree.sync(guild=GUILD)


@bot.event
async def on_ready() -> None:
    logger.info("ログインしました: %s", bot.user)


if __name__ == "__main__":
    bot.run(TOKEN)
