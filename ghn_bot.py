"""
GHN Telegram Bot - Phân tích lỗi GHN từ group Telegram
Chạy: python3 ghn_bot.py
"""
import httpx
import asyncio
import json

BOT_TOKEN = "8858662524:AAH2wABUPxqqcu3z2y-P2CJ5ldCmemSSMu8"
API_BASE  = "https://my-pos-tool.vercel.app"
TG_API    = f"https://api.telegram.org/bot{BOT_TOKEN}"

ERROR_KEYWORDS = [
    "error", "lỗi", "failed", "invalid", "not found", "unauthorized",
    "400", "401", "403", "404", "500", "timeout", "required", "missing",
    "shopid", "ordercode", "ward", "district", "service", "cod", "weight"
]

async def send_message(chat_id, text, reply_to=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{TG_API}/sendMessage", json=payload)

async def analyze_error(text):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{API_BASE}/api/analyze", json={"error_text": text})
            return r.json()
    except Exception as e:
        return {"found": False, "solution": f"Lỗi kết nối: {e}"}

def format_result(data):
    if not data.get("found"):
        return f"🔴 <b>Không tìm thấy trong KB</b>\n\n{data.get('solution', 'Thử paste đúng error message từ JSON response GHN.')}"
    
    conf = data.get("confidence", 0)
    emoji = "🟢" if conf >= 80 else "🟡" if conf >= 50 else "🔴"
    
    lines = [
        f"{emoji} <b>Độ tin cậy: {conf}%</b>",
        f"\n📍 <b>Endpoint:</b> <code>{data.get('endpoint', '?')}</code>",
        f"\n❓ <b>Nguyên nhân:</b>\n{data.get('root_cause', '?')}",
        f"\n✅ <b>Cách sửa:</b>\n{data.get('solution', '?')}",
    ]
    if data.get("code_right"):
        lines.append(f"\n✅ <b>Code đúng:</b>\n<code>{data['code_right']}</code>")
    return "\n".join(lines)

def is_error_message(text):
    t = text.lower()
    return any(k in t for k in ERROR_KEYWORDS) and len(text) > 10

async def process_update(update):
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return

    text = msg.get("text", "")
    chat_id = msg["chat"]["id"]
    msg_id  = msg["message_id"]

    # Bỏ qua tin từ bot
    if msg.get("from", {}).get("is_bot"):
        return

    # Bỏ prefix [TOOL] nếu có
    if text.startswith("[TOOL]"):
        text = text[6:].strip()

    # Chỉ xử lý khi có lỗi GHN
    if not is_error_message(text):
        return
    
    print(f"[BOT] Phân tích lỗi: {text[:80]}...")
    await send_message(chat_id, "⏳ Đang phân tích lỗi GHN...", reply_to=msg_id)
    
    result = await analyze_error(text)
    reply  = format_result(result)
    await send_message(chat_id, reply, reply_to=msg_id)
    print(f"[BOT] Đã reply: confidence={result.get('confidence', 0)}%")

async def main():
    print("🤖 GHN Bot đang chạy...")
    offset = 0
    
    # Xóa webhook nếu có
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{TG_API}/deleteWebhook")
    print("✅ Webhook đã xóa, dùng polling")
    
    while True:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{TG_API}/getUpdates", params={
                    "offset": offset, "timeout": 20, "limit": 10
                })
            data = r.json()
            
            if data.get("ok"):
                for update in data["result"]:
                    offset = update["update_id"] + 1
                    await process_update(update)
        except Exception as e:
            print(f"[ERROR] {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
