import os
import io
import asyncio
import httpx
import replicate
import requests as req
import time
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
CHORUS_AUDIO_URL    = os.getenv("CHORUS_AUDIO_URL")
TRACK_URL           = os.getenv("TRACK_URL", "https://band.link/vcvotivsyanashalubov")
TIKTOK_SOUND_URL    = "https://vt.tiktok.com/ZS9dkQxdcqNFN-RYvnX"


# ─── ШАГ 1: GFPGAN — AI-гламур на коже (~5с) ──────────────────────────────
async def enhance_face(image_bytes: bytes) -> bytes:
    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    buf.seek(0)

    output = await asyncio.to_thread(
        replicate.run,
        "tencentarc/gfpgan:0fbacf7afc6817f3606c37d0f1a2028c22b04c9b7c4a4b8d47c6e7e89c74b1",
        input={
            "img":     buf,
            "version": "v1.4",  # лучше сохраняет детали и идентичность
            "scale":   2,
        }
    )

    print(f"[INFO] GFPGAN output: {output}")

    url = output if isinstance(output, str) else (output.url if hasattr(output, "url") else str(output))

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


# ─── ШАГ 2: OmniHuman — фото + аудио → видео с пением ──────────────────────
async def create_singing_video(image_bytes: bytes) -> bytes:
    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    buf.seek(0)

    output = await asyncio.to_thread(
        replicate.run,
        "bytedance/omni-human",
        input={
            "image": buf,
            "audio": CHORUS_AUDIO_URL,
        }
    )

    print(f"[INFO] OmniHuman output: {output}")

    if isinstance(output, list):
        item = output[0]
    elif hasattr(output, "__iter__") and not isinstance(output, (str, bytes)):
        item = next(iter(output))
    else:
        item = output

    url = item.url if hasattr(item, "url") else str(item)

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


# ─── HANDLERS ───────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Он в сети — общается с ИИ.\nНу и ладно.\n\n"
        "Отправь своё фото — получи себя в виде ИИ-девушки "
        "которая поёт про него 🤖🎵"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Шаг 1/2 — делаю твою ИИ-версию... (~10с)")

    try:
        t0 = time.time()

        photo = update.message.photo[-1]
        file = await photo.get_file()
        image_bytes = bytes(await file.download_as_bytearray())
        print(f"[INFO] Got photo {len(image_bytes)} bytes")

        # Шаг 1 — GFPGAN: AI-кожа, гламур
        enhanced_bytes = await enhance_face(image_bytes)
        print(f"[INFO] GFPGAN done in {time.time()-t0:.1f}s")

        await msg.edit_text("⏳ Шаг 2/2 — записываю как она поёт... (~2-3 мин)")

        # Шаг 2 — OmniHuman: поёт под трек
        video_bytes = await create_singing_video(enhanced_bytes)
        print(f"[INFO] Total done in {time.time()-t0:.1f}s, video {len(video_bytes)} bytes")

        keyboard = [
            [InlineKeyboardButton("🎵 Слушать трек", url=TRACK_URL)],
            [InlineKeyboardButton("📱 Снять видео под этот звук в TikTok", url=TIKTOK_SOUND_URL)],
        ]

        await update.message.reply_video(
            video=io.BytesIO(video_bytes),
            caption=(
                "Твоя ИИ-версия поёт про него 💀\n\n"
                "Сохрани видео → нажми кнопку ниже → снимешь ролик под этот звук в TikTok 👇\n\n"
                "Отметь @veeka_chered и #яИИдевушка"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            supports_streaming=True,
        )

        await msg.delete()

    except Exception as e:
        await msg.edit_text(
            "Что-то пошло не так 😔\n"
            "Попробуй другое фото — лучше всего портрет анфас с чётким лицом."
        )
        print(f"[ERROR] {type(e).__name__}: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Отправь своё фото 📸 — сделаю ИИ-версию которая поёт трек VEÉKA"
    )


def main():
    try:
        req.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=10)
        time.sleep(3)
    except:
        pass

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("VEÉKA AI бот запущен ✅")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
