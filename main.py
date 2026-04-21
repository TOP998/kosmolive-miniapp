import asyncio
import json
import os
import re
from typing import Dict, Any, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, Update
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

# ----------------------- CONFIG -----------------------
TOKEN = os.getenv("TOKEN", "").strip()
PRIMARY_ADMIN = int(os.getenv("PRIMARY_ADMIN", "0"))
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
SECOND_ADMIN = int(os.getenv("SECOND_ADMIN", "0"))
TEST_USER_IDS = {PRIMARY_ADMIN, SECOND_ADMIN}
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@Kosmolive")
APP_URL = os.getenv("APP_URL", "").strip()  # Пример: https://your-project.railway.app
WEBHOOK_PATH = "/webhook"

# ----------------------- HELPERS -----------------------
DATA_DIR = "data"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, "w", encoding="utf-8") as f: json.dump({}, f, ensure_ascii=False, indent=2)

def load_users() -> Dict[str, Any]:
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: return {}

def save_users(data: Dict[str, Any]):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)

def get_user_record(uid: int) -> Dict[str, Any]:
    data = load_users()
    key = str(uid)
    if key not in data:
        data[key] = {"user_id": uid, "state": "new", "submission": None, "correction": None, 
                     "awaiting_correction": False, "submissions_count": 0, "corrections_count": 0}
        save_users(data)
    return data[key]

def update_user_record(uid: int, patch: Dict[str, Any]):
    data = load_users()
    key = str(uid)
    if key not in data: data[key] = {"user_id": uid}
    data[key].update(patch)
    save_users(data)

def normalize_phone(text: str) -> Optional[str]:
    s = re.sub(r"[^\d\+]", "", text or "")
    if s.startswith("8") and len(s)==11: s = "+7"+s[1:]
    elif s.startswith("9") and len(s)==10: s = "+7"+s
    return s if re.match(r"^\+\d{10,14}$", s) else None

def validate_username(text: str) -> Optional[str]:
    if not text.startswith("@"): text = "@"+text
    return text if re.match(r"^@[\w\d_]{4,31}$", text) else None

async def send_to_primary(text: str) -> bool:
    global BOT
    try: await BOT.send_message(GROUP_CHAT_ID, text, parse_mode="HTML"); return True
    except: return False

# ----------------------- FSM & KB -----------------------
class Wizard(StatesGroup):
    sphere = State(); description = State(); hosting = State(); contact = State()

def sphere_kb():
    items = [
        [("🛒 Интернет-магазин","eshop"), ("🧰 Услуги/Мастера","services")],
        [("🏢 Строительство","build"), ("📲 Продажи в соцсети","social")],
        [("🎛 Автоматизация","auto"), ("✍️ Другое","other")],
        [("🤖 Ассистент","assistant"), ("🛠 Сопровождение","support")],
        [("💬 Поддержка",None)]
    ]
    kb = []
    for row in items:
        kb_row = []
        for txt, cb in row:
            if cb: kb_row.append({"text": txt, "callback_data": f"sphere_{cb}"})
            else: kb_row.append({"text": txt, "url": f"https://t.me/{SUPPORT_CONTACT.lstrip('@')}"})
        kb.append(kb_row)
    kb.append([{"text": "🔄 Начать заново", "callback_data": "restart"}])
    return {"inline_keyboard": kb}

# ----------------------- HANDLERS (Telegram) -----------------------
BOT: Optional[Bot] = None
dp: Optional[Dispatcher] = None

async def cmd_start(message: Message, state: FSMContext):
    uid = message.from_user.id; rec = get_user_record(uid)
    st = rec.get("state","new")
    await state.clear()
    if st in ("new","in_progress"):
        update_user_record(uid, {"state":"in_progress"})
        await message.answer("Выберите сферу деятельности:", reply_markup=InlineKeyboardMarkup(**sphere_kb()))
        await state.set_state(Wizard.sphere)
    elif st=="submitted":
        await message.answer("Заявка уже отправлена. Используйте «Поправить заявку» в Mini App или напишите нам.")
    else:
        await message.answer("Вы уже отправили заявку и правку. Спасибо!")

async def sphere_sel(cb: CallbackQuery, state: FSMContext):
    uid=cb.from_user.id; sphere=cb.data.replace("sphere_","")
    update_user_record(uid, {"state":"in_progress","sphere":sphere})
    await cb.message.edit_text("Опишите задачу подробнее:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[{"text":"🔄 Назад","callback_data":"restart"}]]))
    await state.set_state(Wizard.description)
    await cb.answer()

async def desc_input(msg: Message, state: FSMContext):
    uid=msg.from_user.id; txt=(msg.text or "").strip()
    if len(txt)<20: await msg.answer("Минимум 20 символов"); return
    update_user_record(uid, {"submission":{"description":txt}, "state":"in_progress"})
    await msg.answer("Хостинг и сопровождение нужны?\n✅ Да с поддержкой\n❌ Только бот", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [{"text":"✅ Да","callback_data":"host_yes"},{"text":"❌ Нет","callback_data":"host_no"}]
    ]))
    await state.set_state(Wizard.hosting)

async def hosting_sel(cb: CallbackQuery, state: FSMContext):
    uid=cb.from_user.id; hosting = cb.data=="host_yes"
    rec=get_user_record(uid); sub=rec.get("submission",{}) or {}; sub["hosting"]=hosting
    update_user_record(uid, {"submission":sub})
    await cb.message.edit_text("Оставьте контакт (телефон или @username):")
    await state.set_state(Wizard.contact)
    await cb.answer()

async def contact_input(msg: Message, state: FSMContext):
    uid=msg.from_user.id; txt=(msg.text or "").strip()
    phone=normalize_phone(txt); user=validate_username(txt)
    contact = phone or (user if user else None)
    if not contact: await msg.answer("Введите телефон (+7...) или username (@...)"); return
    rec=get_user_record(uid); sub=rec.get("submission",{}) or {}; sub["contact"]=contact
    update_user_record(uid, {"submission":sub})
    
    if uid not in TEST_USER_IDS and rec.get("submissions_count",0)>=1:
        await msg.answer("Вы уже отправляли заявку. Для правки используйте Mini App."); return
        
    sphere=rec.get("sphere",""); desc=sub.get("description",""); host=sub.get("hosting",False)
    summary = (f"📩 <b>Заявка из чата</b> @{msg.from_user.username} (id:{uid})\n"
               f"🎯 Сфера: {sphere}\n📋 Задача: {desc}\n🛠 Сопровождение: {'Да' if host else 'Нет'}\n📞 Контакт: {contact}")
    if await send_to_primary(summary):
        update_user_record(uid, {"state":"submitted", "submissions_count":(rec.get("submissions_count",0) or 0)+1})
        await msg.answer("✅ Заявка принята! Свяжемся в течение 24ч.")
    else: await msg.answer("⚠️ Ошибка отправки админу.")
    await state.clear()

# ----------------------- WEBHOOK & MINI APP API -----------------------
async def handle_webhook(request):
    update_data = await request.json()
    await dp.feed_update(BOT, Update(**update_data))
    return web.Response(status=200)

async def serve_miniapp(request):
    with open("miniapp.html", "r", encoding="utf-8") as f: html = f.read()
    return web.Response(text=html, content_type="text/html")

async def api_state(request):
    data = await request.json(); uid=int(data.get("user_id",0))
    rec=get_user_record(uid)
    return web.json_response({"state":rec.get("state"),"submission":rec.get("submission"),
                              "can_correct":(rec.get("corrections_count",0) or 0)<1})

async def api_submit(request):
    data = await request.json(); uid=int(data.get("user_id",0))
    if uid not in TEST_USER_IDS and get_user_record(uid).get("submissions_count",0)>=1:
        return web.json_response({"error":"already_submitted"}, status=403)
    sphere=data.get("sphere",""); desc=data.get("description",""); host=data.get("hosting")=="yes"
    raw_c=data.get("contact","").strip()
    contact = normalize_phone(raw_c) or validate_username(raw_c)
    if not contact: return web.json_response({"error":"invalid_contact"}, status=400)
    
    summary = (f"📩 <b>Mini App заявка</b> @{data.get('username',uid)} (id:{uid})\n"
               f"🎯 {sphere}\n📋 {desc}\n🛠 {'✅ Да' if host else '❌ Нет'}\n📞 {contact}")
    if not await send_to_primary(summary): return web.json_response({"error":"admin_failed"}, status=500)
    update_user_record(uid, {"state":"submitted","submission":{"sphere":sphere,"description":desc,"hosting":host,"contact":contact},
                             "submissions_count":(get_user_record(uid).get("submissions_count",0) or 0)+1})
    return web.json_response({"status":"success"})

async def api_correct(request):
    data=await request.json(); uid=int(data.get("user_id",0)); txt=data.get("correction","").strip()
    rec=get_user_record(uid)
    if rec.get("state")!="submitted" or (rec.get("corrections_count",0) or 0)>=1:
        return web.json_response({"error":"limit_reached"}, status=403)
    if not await send_to_primary(f"✏️ <b>Правка (Mini App)</b> @{data.get('username',uid)} (id:{uid})\n{txt}"):
        return web.json_response({"error":"failed"}, status=500)
    update_user_record(uid, {"state":"corrected","correction":txt,"corrections_count":(rec.get("corrections_count",0) or 0)+1})
    return web.json_response({"status":"success"})

# ----------------------- MAIN -----------------------
async def main():
    global BOT, dp
    BOT = Bot(token=TOKEN); dp = Dispatcher(storage=MemoryStorage())
    
    dp.message.register(cmd_start, F.text=="/start")
    dp.message.register(desc_input, Wizard.description)
    dp.message.register(contact_input, Wizard.contact)
    dp.callback_query.register(sphere_sel, F.data.startswith("sphere_"))
    dp.callback_query.register(hosting_sel, F.data.startswith("host_"))
    dp.callback_query.register(cmd_start, F.data=="restart")
    
    app = web.Application()
    app.add_routes([web.post(WEBHOOK_PATH, handle_webhook)])
    app.add_routes([web.get("/miniapp", serve_miniapp)])
    app.add_routes([web.post("/api/state", api_state), web.post("/api/submit", api_submit), web.post("/api/correct", api_correct)])
    
    if APP_URL:
        await BOT.set_webhook(f"{APP_URL}{WEBHOOK_PATH}")
        print(f"✅ Webhook установлен: {APP_URL}{WEBHOOK_PATH}")
        
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8000)))
    await site.start()
    print("🚀 Сервер запущен. Жду запросы...")
    await asyncio.Event().wait()

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("✅ Остановлен")
