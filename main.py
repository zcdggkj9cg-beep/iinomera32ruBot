import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from openai import OpenAI

from storage import init_db, save_message, get_history

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("ai-photo-studio")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TELEGRAM_WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-2.5-flare")
EDIT_MODEL = os.getenv("EDIT_MODEL", "gpt-image-2.5-sunburst")
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-5.5")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")
MAX_IMAGE_BYTES = 15 * 1024 * 1024

ai = OpenAI(api_key=OPENAI_API_KEY)
telegram_api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
pending_images: dict[int, bytes] = {}


async def tg_call(method: str, data: dict[str, Any] | None = None, files=None):
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            f"{telegram_api}/{method}",
            data=data,
            files=files,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload.get("result")


async def send_text(chat_id: int, text: str):
    return await tg_call("sendMessage", {"chat_id": chat_id, "text": text})


async def send_photo(chat_id: int, image_bytes: bytes, caption: str = "Готово!"):
    return await tg_call(
        "sendPhoto",
        {"chat_id": chat_id, "caption": caption},
        files={"photo": ("ai-photo-studio.png", image_bytes, "image/png")},
    )


async def download_telegram_file(file_id: str) -> bytes:
    meta = await tg_call("getFile", {"file_id": file_id})
    file_path = meta["file_path"]
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        )
        response.raise_for_status()
        content = response.content
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("Файл слишком большой. Отправь изображение до 15 МБ.")
    return content


def generate_image(prompt: str) -> bytes:
    result = ai.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size="1024x1024",
        quality="medium",
    )
    return base64.b64decode(result.data[0].b64_json)


def edit_image(image_bytes: bytes, prompt: str) -> bytes:
    result = ai.images.edit(
        model=EDIT_MODEL,
        image=("input.jpg", image_bytes),
        prompt=prompt,
        size="1024x1024",
        quality="medium",
    )
    return base64.b64decode(result.data[0].b64_json)


def describe_image(image_bytes: bytes) -> str:
    data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
    response = ai.responses.create(
        model=VISION_MODEL,
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Опиши фотографию по-русски: основные объекты, обстановка, стиль и заметные детали. Не делай предположений о личности людей."},
                {"type": "input_image", "image_url": data_url},
            ],
        }],
    )
    return response.output_text


async def handle_message(message: dict[str, Any]):
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    user = message.get("from", {})
    user_id = int(user.get("id", chat_id or 0))
    if not chat_id:
        return

    text = (message.get("text") or message.get("caption") or "").strip()
    save_message(user_id, "user", text or "[photo]")

    if text.startswith("/start"):
        await send_text(
            chat_id,
            "Привет! Я AI Photo Studio 🎨\n\n"
            "• Напиши описание — создам изображение.\n"
            "• Отправь фото с подписью — отредактирую его.\n"
            "• Отправь фото без подписи, затем напиши /describe для анализа "
            "или текст с инструкцией для редактирования.\n\n"
            "Команды: /help, /history, /describe.\n"
            "Сейчас бот работает в тестовом режиме.",
        )
        return

    if text.startswith("/help"):
        await send_text(
            chat_id,
            "Как пользоваться:\n"
            "1. Для новой картинки отправь текстовое описание.\n"
            "2. Для редактирования отправь фотографию с подписью-инструкцией.\n"
            "3. Для анализа фото отправь фото, затем /describe.\n"
            "4. Чтобы продолжить редактирование, отправь фото и новую инструкцию.\n\n"
            "Не отправляй пароли, документы и конфиденциальные данные."
        )
        return

    if text.startswith("/history"):
        history = get_history(user_id, limit=10)
        if not history:
            await send_text(chat_id, "История пока пуста.")
        else:
            lines = [f"{role}: {content[:250]}" for role, content in history]
            await send_text(chat_id, "Последние сообщения:\n\n" + "\n".join(lines))
        return

    photos = message.get("photo") or []
    if photos:
        try:
            photo = await download_telegram_file(photos[-1]["file_id"])
            pending_images[user_id] = photo
            if text:
                await send_text(chat_id, "Редактирую фотографию. Это может занять до пары минут…")
                result = await __import__("asyncio").to_thread(edit_image, photo, text)
                await send_photo(chat_id, result)
                save_message(user_id, "assistant", "[edited image]")
                pending_images.pop(user_id, None)
            else:
                await send_text(chat_id, "Фото получил! Напиши, что изменить, или отправь /describe, чтобы я описал фотографию.")
        except Exception as exc:
            log.exception("Photo processing failed")
            await send_text(chat_id, f"Не удалось обработать фото: {str(exc)[:250]}")
        return

    if text.startswith("/describe"):
        photo = pending_images.get(user_id)
        if not photo:
            await send_text(chat_id, "Сначала отправь фотографию, затем команду /describe.")
            return
        await send_text(chat_id, "Анализирую фотографию…")
        try:
            description = await __import__("asyncio").to_thread(describe_image, photo)
            save_message(user_id, "assistant", description)
            await send_text(chat_id, description[:4000])
        except Exception:
            log.exception("Vision request failed")
            await send_text(chat_id, "Не получилось проанализировать фото. Попробуй ещё раз позже.")
        return

    if text and not text.startswith("/"):
        photo = pending_images.get(user_id)
        try:
            if photo:
                await send_text(chat_id, "Принял инструкцию. Редактирую фото…")
                result = await __import__("asyncio").to_thread(edit_image, photo, text)
                await send_photo(chat_id, result)
                save_message(user_id, "assistant", "[edited image]")
                pending_images.pop(user_id, None)
            else:
                await send_text(chat_id, "Создаю изображение. Это может занять до пары минут…")
                result = await __import__("asyncio").to_thread(generate_image, text)
                await send_photo(chat_id, result)
                save_message(user_id, "assistant", "[generated image]")
        except Exception as exc:
            log.exception("AI request failed")
            await send_text(chat_id, f"Не удалось выполнить запрос. Проверь API-баланс или попробуй другой запрос. Детали: {str(exc)[:180]}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if PUBLIC_BASE_URL:
        try:
            await tg_call("setWebhook", {
                "url": f"{PUBLIC_BASE_URL}/telegram/webhook",
                "secret_token": TELEGRAM_WEBHOOK_SECRET,
                "allowed_updates": '["message","pre_checkout_query"]',
            })
            log.info("Telegram webhook configured")
        except Exception:
            log.exception("Could not set Telegram webhook yet")
    yield


app = FastAPI(title="AI Photo Studio", lifespan=lifespan)


@app.get("/")
async def root():
    return {"service": "AI Photo Studio", "status": "ok"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if not x_telegram_bot_api_secret_token or not secrets.compare_digest(
        x_telegram_bot_api_secret_token, TELEGRAM_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    update = await request.json()
    message = update.get("message")
    if message:
        await handle_message(message)
    return {"ok": True}
