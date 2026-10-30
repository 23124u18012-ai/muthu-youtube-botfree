import asyncio
import logging
import os
import tempfile
import secrets
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.request import HTTPXRequest
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ==================== CONFIG ====================
# Paste your BotFather token between the quotes. Do not add a comma.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()

MAX_DOWNLOAD_MB = 45
MAX_DURATION_SECONDS = 0  # 0 means no duration limit
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("muthu_downloader")

FORMATS = {
    "mp3_320": ("MP3 320 kbps", "audio", "320"),
    "mp3_256": ("MP3 256 kbps", "audio", "256"),
    "mp3_192": ("MP3 192 kbps", "audio", "192"),
    "bass_320": ("MP3 320 kbps + Bass", "bass", "320"),
    "mp4_1080": ("MP4 up to 1080p", "video", "1080"),
    "mp4_720": ("MP4 up to 720p", "video", "720"),
    "mp4_480": ("MP4 up to 480p", "video", "480"),
    "mp4_360": ("MP4 up to 360p", "video", "360"),
}

MEDIA_EXTENSIONS = {
    ".mp3", ".mp4", ".m4a", ".webm", ".mkv", ".opus", ".aac", ".wav"
}


def valid_youtube_url(url: str) -> bool:
    """Accept only HTTP(S) URLs hosted by YouTube or youtu.be."""
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        allowed = (
            host == "youtu.be"
            or host.endswith(".youtu.be")
            or host == "youtube.com"
            or host.endswith(".youtube.com")
            or host == "youtube-nocookie.com"
            or host.endswith(".youtube-nocookie.com")
        )
        return parsed.scheme in ("http", "https") and allowed
    except ValueError:
        return False


def format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎧 MP3 320", callback_data="mp3_320"),
            InlineKeyboardButton("🎵 MP3 256", callback_data="mp3_256"),
        ],
        [
            InlineKeyboardButton("🎵 MP3 192", callback_data="mp3_192"),
            InlineKeyboardButton("🔊 MP3 + Bass", callback_data="bass_320"),
        ],
        [
            InlineKeyboardButton("🎬 MP4 1080p", callback_data="mp4_1080"),
            InlineKeyboardButton("🎬 MP4 720p", callback_data="mp4_720"),
        ],
        [
            InlineKeyboardButton("📹 MP4 480p", callback_data="mp4_480"),
            InlineKeyboardButton("📱 MP4 360p", callback_data="mp4_360"),
        ],
    ])


def find_media_file(folder: Path, info: dict, prepared_path: Path) -> Path:
    """Find final media after download, merge, or audio post-processing."""
    candidates = []

    # yt-dlp may report the final path here after post-processing.
    for item in info.get("requested_downloads") or []:
        for key in ("filepath", "_filename", "filename"):
            value = item.get(key)
            if value:
                p = Path(value)
                if p.is_file() and p.stat().st_size > 0:
                    candidates.append(p)

    # Check the prepared path and likely FFmpeg output extensions.
    for p in (
        prepared_path,
        prepared_path.with_suffix(".mp4"),
        prepared_path.with_suffix(".mp3"),
        prepared_path.with_suffix(".m4a"),
        prepared_path.with_suffix(".webm"),
        prepared_path.with_suffix(".mkv"),
    ):
        if p.is_file() and p.stat().st_size > 0:
            candidates.append(p)

    # Last resort: search all files inside this download's private temp folder.
    for p in folder.rglob("*"):
        if (
            p.is_file()
            and p.stat().st_size > 0
            and p.suffix.lower() in MEDIA_EXTENSIONS
            and not p.name.endswith((".part", ".ytdl"))
        ):
            candidates.append(p)

    # Remove duplicate paths and choose the newest candidate.
    unique = {str(p.resolve()): p for p in candidates}
    if not unique:
        existing = [str(p) for p in folder.rglob("*")]
        raise FileNotFoundError(
            f"No media file found in {folder}. Files present: {existing}"
        )
    return max(unique.values(), key=lambda p: p.stat().st_mtime)


def download_media(url: str, choice: str, folder_name: str):
    """Synchronous yt-dlp work; called through asyncio.to_thread."""
    label, mode, quality = FORMATS[choice]
    folder = Path(folder_name)
    folder.mkdir(parents=True, exist_ok=True)
    output_template = str(folder / "%(title).80s [%(id)s].%(ext)s")

    options = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
        "restrictfilenames": True,
        "overwrites": True,
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
        "progress": True,
    }

    if mode in ("audio", "bass"):
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": quality,
        }]
        if mode == "bass":
            options["postprocessor_args"] = {
                "ExtractAudio": [
                    "-af", "bass=g=3:f=100:w=0.6,alimiter=limit=0.95"
                ]
            }
    else:
        height = int(quality)
        options["format"] = (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        )
        options["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("yt-dlp did not return video information.")
        title = info.get("title") or "YouTube media"
        prepared_name = ydl.prepare_filename(info)

    output_file = find_media_file(folder, info, Path(prepared_name))

    if output_file.stat().st_size > MAX_DOWNLOAD_MB * 1024 * 1024:
        raise ValueError(
            f"The finished file exceeds the {MAX_DOWNLOAD_MB} MB limit. "
            "Choose a lower quality or a shorter video."
        )

    return title, label, output_file


async def safe_send_message(bot, chat_id: int, text: str) -> None:
    """Best-effort error/status message; don't crash if Telegram is unreachable."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            connect_timeout=60,
            read_timeout=60,
            write_timeout=60,
            pool_timeout=60,
        )
    except (NetworkError, TimedOut) as exc:
        logger.error("Could not send Telegram status message: %s", exc)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_url", None)
    await update.effective_message.reply_text(
        "🎬 Muthu YouTube Downloader\n\n"
        "Send a YouTube video URL, then choose a format.\n\n"
        "🎧 MP3: 320 / 256 / 192 kbps\n"
        "🔊 MP3 with bass boost\n"
        "📹 MP4: up to 1080p / 720p / 480p / 360p\n\n"
        f"Maximum size: {MAX_DOWNLOAD_MB} MB\n"
        "Duration limit: none (file-size and Telegram limits still apply).\n\n"
        "Download only content you have permission to save."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Send a YouTube URL and choose a format.\n\n"
        "/start - Start\n/help - Help\n/cancel - Cancel current selection"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_url", None)
    await update.effective_message.reply_text("Cancelled. Send another URL when ready.")


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    url = (message.text or "").strip()
    if not valid_youtube_url(url):
        await message.reply_text(
            "Please send a valid YouTube URL from youtube.com or youtu.be."
        )
        return

    context.user_data["pending_url"] = url
    await message.reply_text("Choose your format:", reply_markup=format_keyboard())


async def handle_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    url = context.user_data.get("pending_url")
    choice = query.data

    if not url:
        await query.edit_message_text("Selection expired. Send the video URL again.")
        return
    if choice not in FORMATS:
        await query.edit_message_text("Invalid format. Send /start and try again.")
        return

    label = FORMATS[choice][0]
    await query.edit_message_text(f"Selected: {label}\nDownloading and processing...")
    chat_id = query.message.chat_id

    async with DOWNLOAD_SEMAPHORE:
        try:
            with tempfile.TemporaryDirectory(prefix="muthu_yt_") as temp_dir:
                title, actual_label, media_path = await asyncio.to_thread(
                    download_media, url, choice, temp_dir
                )

                size_mb = media_path.stat().st_size / (1024 * 1024)
                if size_mb > MAX_DOWNLOAD_MB:
                    raise ValueError(
                        f"The finished file is {size_mb:.1f} MB, over the limit. "
                        "Choose a lower quality or shorter video."
                    )

                # Chat actions are optional. A temporary Telegram timeout here
                # should not cancel an otherwise successful download/upload.
                try:
                    await context.bot.send_chat_action(
                        chat_id=chat_id,
                        action=ChatAction.UPLOAD_DOCUMENT,
                        connect_timeout=60,
                        read_timeout=60,
                        write_timeout=60,
                        pool_timeout=60,
                    )
                except (NetworkError, TimedOut) as exc:
                    logger.warning("Could not send chat action; continuing: %s", exc)

                caption = f"{title[:700]}\nFormat: {actual_label}"

                # Retry interrupted uploads; reopen the file for each attempt.
                upload_succeeded = False
                last_upload_error = None
                for attempt in range(1, 4):
                    try:
                        with media_path.open("rb") as media:
                            if media_path.suffix.lower() == ".mp3":
                                await context.bot.send_audio(
                                    chat_id=chat_id,
                                    audio=media,
                                    filename=media_path.name,
                                    title=title[:64],
                                    caption=caption[:1024],
                                    read_timeout=300,
                                    write_timeout=300,
                                    connect_timeout=60,
                                    pool_timeout=60,
                                )
                            else:
                                await context.bot.send_document(
                                    chat_id=chat_id,
                                    document=media,
                                    filename=media_path.name,
                                    caption=caption[:1024],
                                    read_timeout=300,
                                    write_timeout=300,
                                    connect_timeout=60,
                                    pool_timeout=60,
                                )
                        upload_succeeded = True
                        break
                    except (NetworkError, TimedOut) as exc:
                        last_upload_error = exc
                        logger.warning(
                            "Telegram upload attempt %s/3 failed: %s",
                            attempt, exc,
                        )
                        if attempt < 3:
                            await asyncio.sleep(attempt * 3)
                if not upload_succeeded:
                    raise RuntimeError(
                        "Telegram upload failed after 3 attempts. Check internet stability."
                    ) from last_upload_error

            await safe_send_message(context.bot, chat_id, "✅ Download complete!")

        except yt_dlp.utils.DownloadError as exc:
            logger.exception("yt-dlp download error: %s", exc)
            await safe_send_message(
                context.bot, chat_id,
                "Download failed. The video may be unavailable, restricted, or too large. "
                "Check the Termux terminal for details."
            )
        except (FileNotFoundError, RuntimeError) as exc:
            logger.exception("Media output error: %s", exc)
            await safe_send_message(
                context.bot, chat_id,
                "Processing or Telegram delivery failed. Check the Termux terminal log."
            )
        except ValueError as exc:
            logger.warning("Download limit: %s", exc)
            await safe_send_message(context.bot, chat_id, str(exc))
        except Exception as exc:
            logger.exception("Unexpected bot error: %s", exc)
            await safe_send_message(
                context.bot, chat_id,
                "Processing or delivery failed. Check the Termux terminal log."
            )
        finally:
            context.user_data.pop("pending_url", None)


def main():
    token = BOT_TOKEN
    if not token or ":" not in token:
        raise SystemExit("Set the BOT_TOKEN environment variable in Render.")
    if not RENDER_EXTERNAL_HOSTNAME:
        raise SystemExit("RENDER_EXTERNAL_HOSTNAME is missing. Deploy this as a Render Web Service.")

    # Telegram webhooks require a public HTTPS URL. Render supplies the hostname.
    webhook_path = secrets.token_urlsafe(24)
    webhook_secret = secrets.token_urlsafe(32).replace("-", "_")
    webhook_url = f"https://{RENDER_EXTERNAL_HOSTNAME}/{webhook_path}"

    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=60,
        read_timeout=300,
        write_timeout=300,
        pool_timeout=60,
    )
    updates_request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=60,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=60,
    )
    app = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(updates_request)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(
        handle_format,
        pattern=r"^(mp3_320|mp3_256|mp3_192|bass_320|mp4_1080|mp4_720|mp4_480|mp4_360)$",
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))

    logger.info("Starting Muthu YouTube Downloader webhook on port %s", PORT)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=webhook_url,
        secret_token=webhook_secret,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
