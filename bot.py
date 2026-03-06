import os
import io
import asyncio
import httpx
import replicate
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN        = os.getenv("TELEGRAM_TOKEN")
REPLICATE_API_TOKEN   = os.getenv("REPLICATE_API_TOKEN")
DID_API_KEY           = os.getenv("DID_API_KEY")
CHORUS_AUDIO_URL      = os.getenv("CHORUS_AUDIO_URL")
TRACK_URL             = os.getenv("TRACK_URL", "https://band.link/vcvotivsyanashalubov")


async def transform_to_ai(image_bytes: bytes) -> str:
    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as f:
            # lucataco/instantid — лучшее сохранение лица + красивый стиль
            output = await asyncio.to_thread(
                replicate.run,
                "zsxkib/instant-id:c98b2e7a196828d00955767813b81fc05c5c9b294c670c6d147d545fed4ceecf",
                input={
                    "image": f,
                    "prompt": "a beautiful woman, same person, preserve facial features, cyberpunk style, neon purple and blue lights, futuristic city, glowing skin, ultra detailed, 8k, cinematic, masterpiece",
                    "negative_prompt": "ugly, deformed, blurry, low quality, different person, changed face, different identity, nsfw",
                    "sdxl_weights": "protovision-xl-high-fidel",
                    "width": 640,
                    "height": 640,
                    "guidance_scale": 5,
                    "ip_adapter_scale": 0.8,
                    "controlnet_conditioning_scale": 0.8,
                    "num_inference_steps": 30,
                    "disable_safety_checker": True,
                }
            )
    finally:
        os.unlink(tmp_path)

    if isinstance(output, list):
        item = output[0]
    else:
        item = output

    if hasattr(item, 'url'):
        return item.url
    else:
        return str(item)


async def create_lipsync(image_url: str) -> bytes:
    headers = {
        "Authorization": f"Basic {DID_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "source_url": image_url,
        "script": {
            "type": "audio",
            "audio_url": CHORUS_AUDIO_URL,
        },
        "config": {
            "fluent": True,
            "pad_audio": 0.0,
            "stitch": True,
        },
        "result_format": "mp4",
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.d-id.com/talks",
            headers=headers,
            json=payload
        )
        resp.raise_for_status()
        talk_id = resp.json()["id"]

        for attempt in range(30):
            await asyncio.sleep(3)
            status_resp = await client.get(
                f"https://api.d-id.com/talks/{talk_id}",
                headers=headers
            )
            data = status_resp.json()
            status = data.get("status")

            if status == "done":
                video_url = data["result_url"]
                video_resp = await client.get(video_url, timeout=60)
                return video_resp.content

            elif status == "error":
                raise RuntimeError(f"D-ID error: {data.get('error', 'unknown')}")

        raise TimeoutError("D-ID не ответил за 90 секунд")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🎵 Слушать трек", url=TRACK_URL)]]
    await update.message.reply_text(
        "Он в сети — общается с ИИ.\n"
        "Ну и ладно.\n\n"
        "Отправь своё фото — получи себя в виде ИИ-девушки "
        "которая поёт про него 🤖🎵",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "⏳ Шаг 1/2 — создаю твою ИИ-версию..."
    )

    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()

        ai_image_url = await transform_to_ai(bytes(image_bytes))
        print(f"[INFO] AI image URL: {ai_image_url}")

        await msg.edit_text("⏳ Шаг 2/2 — добавляю голос и движение... (~30с)")

        video_bytes = await create_lipsync(ai_image_url)

        keyboard = [[InlineKeyboardButton("🎵 Слушать трек", url=TRACK_URL)]]

        await update.message.reply_video(
            video=io.BytesIO(video_bytes),
            caption=(
                "Твоя ИИ-версия поёт про него 💀\n\n"
                "Поделись и отметь *@veeka\\_chered*\n"
                "#яИИдевушка"
            ),
            parse_mode="Markdown",
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


import time
import requests as req

def main():
    # Сбрасываем webhook и ждём освобождения соединения
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
