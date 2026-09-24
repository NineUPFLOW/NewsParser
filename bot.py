"""
Telegram-бот для скачивания видео через yt-dlp.
Обходит блокировку YouTube через Deno (JS runtime) + cookies.
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, Message

# ═══════════════════════════════════════════════════════════════════════
# Логирование
# ═══════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("download_bot")

# ═══════════════════════════════════════════════════════════════════════
# Конфигурация
# ═══════════════════════════════════════════════════════════════════════
BOT_TOKEN = os.environ["BOT_TOKEN"]

MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_RUN_TIME = 5 * 3600
DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOADS_DELAY = 3

# Путь к cookies-файлу (добавляется в репозиторий или секреты)
COOKIES_FILE = "youtube_cookies.txt"

SUPPORTED_DOMAINS = (
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "twitter.com", "x.com", "reddit.com", "vimeo.com",
    "soundcloud.com", "twitch.tv", "vk.com", "dailymotion.com",
)

bot = Bot(token=BOT_TOKEN)


# ═══════════════════════════════════════════════════════════════════════
# Утилиты
# ═══════════════════════════════════════════════════════════════════════

def _is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


def _detect_platform(url: str) -> str:
    url_lower = url.lower()
    for d in SUPPORTED_DOMAINS:
        if d in url_lower:
            return d.split(".")[0].capitalize()
    return "Сайт"


def _clean_dir():
    if DOWNLOAD_DIR.exists():
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ═══════════════════════════════════════════════════════════════════════
# Скачивание через yt-dlp
# ═══════════════════════════════════════════════════════════════════════

async def download_media(url: str) -> Path | None:
    """
    Скачивает видео/аудио через yt-dlp с обходом блокировки YouTube.
    """
    _clean_dir()
    output_template = str(DOWNLOAD_DIR / "%(title).100s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--max-filesize", "50M",
        # Формат
        "--format", "bestvideo[ext=mp4][filesize<50M]+bestaudio[ext=m4a]/best[ext=mp4][filesize<50M]/best[filesize<50M]",
        "--merge-output-format", "mp4",
        # JavaScript runtime (ОБЯЗАТЕЛЬНО для YouTube в 2026)
        "--js-runtimes", "deno",
        # Обход n challenge
        "--extractor-args", "youtube:player_client=default,-web_safari",
        # Логи
        "--no-warnings",
        "--quiet",
        "--no-progress",
        "-o", output_template,
    ]

    # Cookies (если есть)
    if Path(COOKIES_FILE).exists():
        cmd.extend(["--cookies", COOKIES_FILE])
        logger.info("Использую cookies: %s", COOKIES_FILE)

    cmd.append(url)

    logger.info("yt-dlp: %s", url)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=600
            )
        except asyncio.TimeoutError:
            proc.kill()
            logger.error("yt-dlp timeout")
            return None

        if proc.returncode != 0:
            err_text = stderr.decode(errors="ignore")[:800]
            logger.error("yt-dlp error (code %s): %s", proc.returncode, err_text)
            return None

        files = [
            f for f in DOWNLOAD_DIR.glob("*")
            if f.is_file() and not f.name.endswith((".part", ".ytdl"))
        ]
        if not files:
            return None

        return max(files, key=lambda f: f.stat().st_size)
    except FileNotFoundError:
        logger.error("yt-dlp не установлен")
        return None
    except Exception as e:
        logger.error("Ошибка скачивания: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════
# Обработка сообщений
# ═══════════════════════════════════════════════════════════════════════

async def safe_send(chat_id: int, text: str) -> int | None:
    try:
        msg = await bot.send_message(chat_id, text)
        return msg.message_id
    except Exception as e:
        logger.error("send failed: %s", e)
        return None


async def safe_edit(chat_id: int, message_id: int, text: str):
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def handle_message(msg: Message):
    if not msg.text:
        return

    chat_id = msg.chat.id
    text = msg.text.strip()

    if text == "/start":
        await bot.send_message(
            chat_id,
            "📥 <b>Бот для скачивания видео</b>\n\n"
            "Отправь ссылку на видео с:\n"
            "• YouTube, TikTok, Instagram, Twitter/X, Reddit\n"
            "• Vimeo, SoundCloud, Twitch, VK, Dailymotion\n\n"
            "⚠️ Максимальный размер — 50 MB",
            parse_mode="HTML",
        )
        return

    if text == "/help":
        await bot.send_message(chat_id, "Отправь ссылку — пришлю файл.")
        return

    if not _is_url(text):
        await bot.send_message(chat_id, "❌ Это не похоже на ссылку")
        return

    platform = _detect_platform(text)
    status_id = await safe_send(chat_id, f"⏳ Скачиваю с {platform}...")
    if status_id is None:
        return

    started = time.time()
    file = await download_media(text)

    if not file or not file.exists():
        await safe_edit(
            chat_id, status_id,
            "❌ Не удалось скачать.\n"
            "Возможно: видео удалено, приватное или превышает 50 MB.\n\n"
            "Для YouTube: возможно, нужно обновить cookies."
        )
        return

    size = file.stat().st_size
    if size > MAX_FILE_SIZE:
        await safe_edit(
            chat_id, status_id,
            f"❌ Файл слишком большой: {_human_size(size)} (лимит 50 MB)"
        )
        try:
            file.unlink()
        except Exception:
            pass
        return

    elapsed = int(time.time() - started)
    await safe_edit(
        chat_id, status_id,
        f"📤 Отправляю... ({_human_size(size)}, скачано за {elapsed} с)"
    )

    try:
        await bot.send_video(
            chat_id=chat_id,
            video=FSInputFile(file),
            caption=f"📥 {file.name}\n{_human_size(size)}",
            supports_streaming=True,
            read_timeout=300,
            write_timeout=300,
        )
        try:
            await bot.delete_message(chat_id, status_id)
        except Exception:
            pass
    except Exception as e:
        logger.error("send_video failed: %s", e)
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=FSInputFile(file),
                caption=f"📥 {file.name}",
            )
            try:
                await bot.delete_message(chat_id, status_id)
            except Exception:
                pass
        except Exception as e2:
            await safe_edit(chat_id, status_id, f"❌ Ошибка отправки: {e2}")
    finally:
        try:
            file.unlink()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
# Главный цикл
# ═══════════════════════════════════════════════════════════════════════

async def main_loop():
    logger.info("🚀 Бот запущен (long-polling)")
    start_time = time.time()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning("delete_webhook: %s", e)

    while True:
        if time.time() - start_time > MAX_RUN_TIME:
            logger.info("⏰ Достигнут лимит времени — перезапуск по cron")
            break

        try:
            updates = await bot.get_updates(timeout=30, allowed_updates=["message"])
            for update in updates:
                try:
                    if update.message:
                        asyncio.create_task(handle_message(update.message))
                except Exception as e:
                    logger.error("update processing: %s", e)

            if updates:
                await asyncio.sleep(DOWNLOADS_DELAY)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("get_updates error: %s", e)
            await asyncio.sleep(5)

    await bot.session.close()
    logger.info("👋 Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main_loop())
