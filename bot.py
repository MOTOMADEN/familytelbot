import os
import sqlite3
import logging
from datetime import time as dtime, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    ChatMemberHandler,
    filters,
)

# ----------------------------------------------------------------------
# تنظیمات
# ----------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "scores.db")

TIMEZONE = ZoneInfo("Asia/Tehran")

# جمعه ساعت 20:00
WEEKLY_ANNOUNCE_WEEKDAY = 4
WEEKLY_ANNOUNCE_TIME = dtime(hour=20, minute=0, tzinfo=TIMEZONE)

# هر شب ساعت 00:00
NIGHTLY_THANK_TIME = dtime(hour=0, minute=0, tzinfo=TIMEZONE)

KEYWORD_ADD_POINT = "امتیاز"
KEYWORD_LEADERBOARD = "تابلو امتیازات"

TITLE_TOP = "بسوزون برتر"
TITLE_BOTTOM = "خۥنۥک گروه"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# دیتابیس
# ----------------------------------------------------------------------

def db_connect():
    conn = sqlite3.connect(DB_PATH)

    # جدول امتیازات فعلی
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
        """
    )

    # لیست گروه‌هایی که ربات داخل آن‌هاست
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            chat_title TEXT
        )
        """
    )

    # تاریخچه امتیازها
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS score_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            amount INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    return conn


def register_chat(chat_id: int, chat_title: str = ""):
    conn = db_connect()

    conn.execute(
        """
        INSERT INTO chats (chat_id, chat_title)
        VALUES (?, ?)
        ON CONFLICT(chat_id)
        DO UPDATE SET chat_title = excluded.chat_title
        """,
        (chat_id, chat_title),
    )

    conn.commit()
    conn.close()


def all_chat_ids():
    conn = db_connect()

    rows = conn.execute(
        "SELECT chat_id FROM chats"
    ).fetchall()

    conn.close()

    return [row[0] for row in rows]


def add_point(
    chat_id: int,
    user_id: int,
    display_name: str,
    amount: int = 1
):
    conn = db_connect()

    # امتیاز فعلی
    conn.execute(
        """
        INSERT INTO scores (chat_id, user_id, display_name, score)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET
            score = score + excluded.score,
            display_name = excluded.display_name
        """,
        (chat_id, user_id, display_name, amount),
    )

    # ثبت رویداد برای بررسی 24 ساعت گذشته
    now_utc = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT INTO score_events
        (chat_id, user_id, display_name, amount, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            user_id,
            display_name,
            amount,
            now_utc,
        ),
    )

    conn.commit()
    conn.close()


def get_scores(chat_id: int):
    conn = db_connect()

    rows = conn.execute(
        """
        SELECT user_id, display_name, score
        FROM scores
        WHERE chat_id = ?
        ORDER BY score DESC
        """,
        (chat_id,),
    ).fetchall()

    conn.close()

    return rows


def reset_scores(chat_id: int):
    conn = db_connect()

    conn.execute(
        "DELETE FROM scores WHERE chat_id = ?",
        (chat_id,),
    )

    conn.commit()
    conn.close()


def get_users_scored_last_24_hours(chat_id: int):
    conn = db_connect()

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=24)
    ).isoformat()

    rows = conn.execute(
        """
        SELECT DISTINCT user_id, display_name
        FROM score_events
        WHERE chat_id = ?
          AND created_at >= ?
        ORDER BY display_name
        """,
        (chat_id, cutoff),
    ).fetchall()

    conn.close()

    return rows


# ----------------------------------------------------------------------
# کمکی
# ----------------------------------------------------------------------

async def is_group_owner(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> bool:

    chat = update.effective_chat
    user = update.effective_user

    if chat is None or user is None:
        return False

    try:
        member = await context.bot.get_chat_member(
            chat.id,
            user.id
        )

        return member.status == ChatMemberStatus.OWNER

    except Exception as e:
        logger.warning(
            f"خطا در بررسی مالک بودن: {e}"
        )
        return False


def build_leaderboard_text(
    rows,
    title="🏆 تابلو امتیازات"
):
    if not rows:
        return f"{title}\n\nهنوز امتیازی ثبت نشده."

    lines = [title, ""]

    medals = ["🥇", "🥈", "🥉"]

    for i, (user_id, name, score) in enumerate(rows):
        prefix = (
            medals[i]
            if i < 3
            else f"{i + 1}."
        )

        lines.append(
            f"{prefix} {name} — {score} امتیاز"
        )

    return "\n".join(lines)


def mention(user_id: int, name: str) -> str:
    return (
        f'<a href="tg://user?id={user_id}">'
        f'{name}'
        f'</a>'
    )


# ----------------------------------------------------------------------
# وقتی ربات وارد گروه می‌شود
# ----------------------------------------------------------------------

async def handle_bot_added_to_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_member_update = update.my_chat_member

    if chat_member_update is None:
        return

    chat = chat_member_update.chat

    if chat.type not in ("group", "supergroup"):
        return

    old_status = chat_member_update.old_chat_member.status
    new_status = chat_member_update.new_chat_member.status

    # ثبت گروه در دیتابیس
    register_chat(
        chat.id,
        chat.title or ""
    )

    # ربات وارد گروه شده
    if (
        old_status in (
            ChatMemberStatus.LEFT,
            ChatMemberStatus.BANNED,
        )
        and new_status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
        )
    ):

        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                " سلام به خاندان پورنصر!🗿\n\n"
                "من ربات امتیازدهی گروهم.\n"
                "از این به بعد هر وقت خواستید به کسی امتیاز بدید، "
                "روی پیامش ریپلای کنید و بنویسید:\n\n"
                "«امتیاز»\n\n"
                "📊 برای دیدن تابلو امتیازات هم بنویسید:\n"
                "«تابلو امتیازات»\n\n"
                "فقط مالک گروه می‌تونه امتیاز ثبت کنه و تابلو رو ببینه.\n\n"
                "🔥 آخر هفته هم اعلام می‌کنم کی بیشتر سوزونده!"
            )
        )


# ----------------------------------------------------------------------
# پیام‌های گروه
# ----------------------------------------------------------------------

async def handle_group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    if (
        message is None
        or message.chat.type not in ("group", "supergroup")
    ):
        return

    # مطمئن شو گروه ثبت شده
    register_chat(
        message.chat_id,
        message.chat.title or ""
    )

    text = (
        message.text
        or message.caption
        or ""
    ).strip()

    if not text:
        return

    chat_id = message.chat_id

    # --------------------------------------------------------------
    # تابلو
    # --------------------------------------------------------------

    if text == KEYWORD_LEADERBOARD:

        if not await is_group_owner(update, context):
            return

        rows = get_scores(chat_id)

        await message.reply_html(
            build_leaderboard_text(rows)
        )

        return

    # --------------------------------------------------------------
    # امتیاز
    # --------------------------------------------------------------

    if text == KEYWORD_ADD_POINT:

        if not message.reply_to_message:

            await message.reply_text(
                "برای ثبت امتیاز باید روی پیام یا عکس همون کاربر "
                "ریپلای بزنی و کلمه «امتیاز» رو بفرستی."
            )

            return

        if not await is_group_owner(update, context):
            return

        target_user = (
            message.reply_to_message.from_user
        )

        if (
            target_user is None
            or target_user.is_bot
        ):
            await message.reply_text(
                "نمی‌شه به این پیام امتیاز داد."
            )

            return

        display_name = (
            target_user.full_name
            or target_user.username
            or str(target_user.id)
        )

        add_point(
            chat_id=chat_id,
            user_id=target_user.id,
            display_name=display_name,
            amount=1,
        )

        await message.reply_html(
            f"✅ یک امتیاز برای "
            f"{mention(target_user.id, display_name)} "
            f"ثبت شد."
        )

        return


# ----------------------------------------------------------------------
# اعلام هفتگی
# ----------------------------------------------------------------------

async def weekly_announcement(
    context: ContextTypes.DEFAULT_TYPE
):

    for chat_id in all_chat_ids():

        rows = get_scores(chat_id)

        if not rows:
            continue

        text_lines = [
            build_leaderboard_text(
                rows,
                title="📅 جمع‌بندی امتیازات این هفته"
            ),
            "",
        ]

        top_user_id, top_name, top_score = rows[0]

        bottom_user_id, bottom_name, bottom_score = rows[-1]

        # نفر اول
        text_lines.append(
            f"🔥 لقب «{TITLE_TOP}» این هفته به "
            f"{mention(top_user_id, top_name)} "
            f"می‌رسه! ({top_score} امتیاز)"
        )

        # نفر آخر
        if bottom_user_id != top_user_id:

            text_lines.append(
                f"🧊 لقب «{TITLE_BOTTOM}» این هفته به "
                f"{mention(bottom_user_id, bottom_name)} "
                f"می‌رسه. ({bottom_score} امتیاز)"
            )

        # پیام شروع هفته جدید
        text_lines.extend(
            [
                "",
                "♻️ امتیازات هفته قبل پاک شد.",
                "🔥 از الان هفته جدید شروع شده!",
            ]
        )

        try:

            await context.bot.send_message(
                chat_id=chat_id,
                text="\n".join(text_lines),
                parse_mode="HTML",
            )

            # بعد از اعلام، امتیازات صفر شوند
            reset_scores(chat_id)

        except Exception as e:

            logger.warning(
                f"ارسال اعلام هفتگی برای "
                f"{chat_id} ناموفق بود: {e}"
            )


# ----------------------------------------------------------------------
# تشکر شبانه
# ----------------------------------------------------------------------

async def nightly_thank_you(
    context: ContextTypes.DEFAULT_TYPE
):

    for chat_id in all_chat_ids():

        users = get_users_scored_last_24_hours(
            chat_id
        )

        # کسی امتیاز نگرفته
        if not users:

            try:

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "😐 دارین شل می‌شیناااا...\n\n"
                        "امروز کسی نسوزوند!"
                    ),
                )

            except Exception as e:

                logger.warning(
                    f"ارسال پیام شبانه برای "
                    f"{chat_id} ناموفق بود: {e}"
                )

            continue

        # یک یا چند نفر امتیاز گرفته‌اند
        mentions = []

        for user_id, name in users:
            mentions.append(
                mention(user_id, name)
            )

        if len(mentions) == 1:

            message_text = (
                "🔥 اووف چه شب سختی بود!\n\n"
                f"دمت گرم {mentions[0]} "
                "که امروز امتیاز گرفتی ❤️‍🔥\n\n"
                "مرسی که گروه رو سوزوندی!"
            )

        else:

            names_text = "، ".join(mentions)

            message_text = (
                "🔥 اووف چه شب سختی بود!\n\n"
                f"دمت گرم {names_text} "
                "که امروز امتیاز گرفتید ❤️‍🔥\n\n"
                "مرسی که گروه رو سوزوندید!"
            )

        try:

            await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode="HTML",
            )

        except Exception as e:

            logger.warning(
                f"ارسال پیام تشکر برای "
                f"{chat_id} ناموفق بود: {e}"
            )


# ----------------------------------------------------------------------
# اجرا
# ----------------------------------------------------------------------

def main():

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # تشخیص اضافه شدن / حذف شدن ربات از گروه
    app.add_handler(
        ChatMemberHandler(
            handle_bot_added_to_group,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    # پیام‌های گروه
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION)
            & filters.ChatType.GROUPS,
            handle_group_message,
        )
    )

    # جمعه ساعت 20
    app.job_queue.run_daily(
        weekly_announcement,
        time=WEEKLY_ANNOUNCE_TIME,
        days=(WEEKLY_ANNOUNCE_WEEKDAY,),
        name="weekly_announcement",
    )

    # هر شب ساعت 00:00
    app.job_queue.run_daily(
        nightly_thank_you,
        time=NIGHTLY_THANK_TIME,
        name="nightly_thank_you",
    )

    logger.info("ربات در حال اجراست...")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()