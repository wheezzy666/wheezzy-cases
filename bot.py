import logging
import os
import random
import re
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL = os.getenv("CHANNEL_USERNAME", "@TheWheezzy")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/TheWheezzy")
DB = os.getenv("DATABASE_PATH", "bot.db")
START_BALANCE = 500
# Each tuple is (name, resale value, weight). Weights are relative and sum to 100.
ITEMS = {
    "common": [("🐰 Jelly Bunny", 40, 55), ("🎂 Homemade Cake", 100, 30), ("🎩 Santa Hat", 250, 12), ("🧢 Durov’s Cap", 800, 3)],
    "rare": [("🎂 Homemade Cake", 100, 50), ("🎩 Santa Hat", 250, 32), ("🧢 Durov’s Cap", 800, 15), ("🐸 Plush Pepe", 2500, 3)],
    "free": [("🐰 Jelly Bunny", 40, 50), ("🎂 Homemade Cake", 100, 32), ("🎩 Santa Hat", 250, 15), ("🧢 Durov’s Cap", 800, 3)],
}
CASES = {"common": ("📦 Обычный", 100), "rare": ("🚀 Космический", 350)}
UPGRADES = {"x2": (2, 45), "x3": (3, 30), "x5": (5, 18)}

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()

class PostgresConnection:
    def __init__(self, url):
        import psycopg
        self.db = psycopg.connect(url)
    def __enter__(self): return self
    def __exit__(self, typ, val, tb):
        try:
            if typ: self.db.rollback()
            else: self.db.commit()
        finally: self.db.close()
    def execute(self, sql, params=()):
        if sql == "BEGIN IMMEDIATE": return self.db.execute("SELECT pg_advisory_xact_lock(7272026)")
        sql = re.sub(r"\b(users|inventory|processed)\b", lambda m: "wcase_" + m.group(1), sql)
        return self.db.execute(sql.replace("?", "%s"), params)
    def executescript(self, sql):
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        sql = sql.replace("id INTEGER PRIMARY KEY", "id BIGINT PRIMARY KEY")
        sql = sql.replace("user_id INTEGER", "user_id BIGINT")
        for statement in sql.split(";"):
            if statement.strip(): self.execute(statement)

def connect():
    if os.getenv("DATABASE_URL"): return PostgresConnection(os.environ["DATABASE_URL"])
    db = sqlite3.connect(DB, timeout=10, factory=ClosingConnection)
    db.execute("PRAGMA foreign_keys=ON")
    return db

def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, balance INTEGER NOT NULL DEFAULT 500 CHECK(balance >= 0), free_claimed INTEGER NOT NULL DEFAULT 0, daily_at INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS inventory (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, value INTEGER NOT NULL CHECK(value > 0));
        CREATE TABLE IF NOT EXISTS processed (callback_id TEXT PRIMARY KEY);
        """)

def ensure_user(user):
    with connect() as db:
        db.execute("INSERT INTO users(id,name) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name", (user.id, user.first_name[:80]))

def keyboard(rows): return M([[B(label, callback_data=data) for label, data in row] for row in rows])
def menu(): return keyboard([[('📦 Кейсы', 'cases'), ('🎒 Инвентарь', 'inventory')], [('🎁 Бесплатный кейс', 'free'), ('💰 Ежедневный бонус', 'daily')], [('🏆 Топ', 'top'), ('ℹ️ Правила', 'help')]])
def back(): return keyboard([[('🏠 Меню', 'menu')]])
def user_balance(uid):
    with connect() as db: return db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]
def roll(case):
    pool = ITEMS[case]
    return random.SystemRandom().choices(pool, weights=[x[2] for x in pool], k=1)[0]
def transaction(callback_id, fn):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM processed WHERE callback_id=?", (callback_id,)).fetchone(): return "Это действие уже обработано."
        db.execute("INSERT INTO processed VALUES (?)", (callback_id,))
        return fn(db)
def grant_item(db, uid, item):
    db.execute("INSERT INTO inventory(user_id,name,value) VALUES (?,?,?)", (uid, item[0], item[1]))
def open_case(uid, case, cb):
    def action(db):
        price=CASES[case][1]
        if db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0] < price: return "Недостаточно монет. Забери ежедневный бонус или продай предмет."
        item=roll(case)
        db.execute("UPDATE users SET balance=balance-? WHERE id=?", (price,uid)); grant_item(db,uid,item)
        return f"🎉 Выпало: {item[0]}\nСтоимость продажи: {item[1]} монет.\nПредмет уже в инвентаре."
    return transaction(cb,action)
def claim_free(uid, cb):
    def action(db):
        if db.execute("SELECT free_claimed FROM users WHERE id=?", (uid,)).fetchone()[0]: return "Бесплатный кейс уже получен."
        item=roll("free"); db.execute("UPDATE users SET free_claimed=1 WHERE id=?", (uid,)); grant_item(db,uid,item)
        return f"🎁 Бесплатный кейс открыт!\nТебе выпал {item[0]} стоимостью {item[1]} монет."
    return transaction(cb,action)
def sell(uid, item_id, cb):
    def action(db):
        item=db.execute("SELECT name,value FROM inventory WHERE id=? AND user_id=?",(item_id,uid)).fetchone()
        if not item: return "Предмет уже продан или использован."
        db.execute("DELETE FROM inventory WHERE id=? AND user_id=?",(item_id,uid)); db.execute("UPDATE users SET balance=balance+? WHERE id=?",(item[1],uid))
        return f"Продано: {item[0]} за {item[1]} монет."
    return transaction(cb,action)
def upgrade(uid, item_id, mode, cb):
    def action(db):
        item=db.execute("SELECT name,value FROM inventory WHERE id=? AND user_id=?",(item_id,uid)).fetchone()
        if not item: return "Предмет уже продан или использован."
        mult,chance=UPGRADES[mode]; success=random.SystemRandom().randrange(100)<chance
        db.execute("DELETE FROM inventory WHERE id=? AND user_id=?",(item_id,uid))
        if success:
            value=item[1]*mult; name=f"✨ Улучшенный предмет ×{mult}"; grant_item(db,uid,(name,value))
            return f"🚀 Успех! Получен {name} стоимостью {value} монет."
        return f"💥 Неудача. {item[0]} исчез. Попробуй снова с другим предметом."
    return transaction(cb,action)
def daily(uid,cb):
    def action(db):
        now=int(time.time()); last=db.execute("SELECT daily_at FROM users WHERE id=?",(uid,)).fetchone()[0]; remaining=86400-(now-last)
        if remaining>0: return f"Бонус уже получен. Возвращайся через {remaining//3600} ч {(remaining%3600)//60+1} мин."
        db.execute("UPDATE users SET balance=balance+150,daily_at=? WHERE id=?",(now,uid)); return "💰 Начислено 150 монет! Следующий бонус через 24 часа."
    return transaction(cb,action)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    ensure_user(update.effective_user)
    await update.message.reply_text(f"👋 Привет, {update.effective_user.first_name}!\nДобро пожаловать в Wheezzy Cases.\n\nБаланс: {user_balance(update.effective_user.id)} монет\nОткрывай кейсы, собирай коллекцию и пробуй апгрейды!", reply_markup=menu())

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if update.effective_chat.type != "private":
        await q.answer("Открой бота в личных сообщениях.",show_alert=True); return
    await q.answer(); uid=update.effective_user.id; ensure_user(update.effective_user); data=q.data or ""; text=""; markup=back()
    try:
        if data == "menu": text=f"🏠 Главное меню\nБаланс: {user_balance(uid)} монет"; markup=menu()
        elif data == "help": text="ℹ️ Предметы — игровые копии подарков Telegram, не настоящие NFT. Вывода и покупок за деньги нет.\n\nОткрывай кейсы, продавай предметы или улучшай их. При неудачном апгрейде предмет исчезает. Бесплатный кейс за подписку выдаётся один раз. Ежедневный бонус — раз в 24 часа.\n\nШансы каждого кейса указаны в его описании."
        elif data == "cases":
            text=f"📦 Выбери кейс. Баланс: {user_balance(uid)} монет"
            markup=keyboard([[(f"{name} — {price} 🪙",f"case:{key}")] for key,(name,price) in CASES.items()]+[[('🏠 Меню','menu')]])
        elif data.startswith("case:") and data.split(":")[1] in CASES:
            case=data.split(":")[1]; name,price=CASES[case]; text=f"{name} — {price} монет\n\nСодержимое и шансы:\n"+"\n".join(f"{n} — {v} 🪙 ({w}%)" for n,v,w in ITEMS[case]); markup=keyboard([[('🎲 Открыть',f'open:{case}')],[('◀️ К кейсам','cases')]])
        elif data.startswith("open:") and data.split(":")[1] in CASES: text=open_case(uid,data.split(":")[1],q.id)
        elif data == "free":
            text="🎁 Подпишись на канал и получи один бесплатный кейс!\n\nСодержимое и шансы:\n"+"\n".join(f"{n} — {v} 🪙 ({w}%)" for n,v,w in ITEMS['free'])
            markup=M([[B('📣 Открыть канал',url=CHANNEL_URL)],[B('✅ Проверить подписку',callback_data='check')],[B('🏠 Меню',callback_data='menu')]])
        elif data == "check":
            try:
                member=await context.bot.get_chat_member(CHANNEL,uid)
                subscribed=member.status in ('creator','administrator','member') or (member.status=='restricted' and member.is_member)
                text=claim_free(uid,q.id) if subscribed else "Подписка не найдена. Подпишись на канал и нажми «Проверить подписку» ещё раз."
                if not subscribed: markup=keyboard([[('🎁 К бесплатному кейсу','free')],[('🏠 Меню','menu')]])
            except TelegramError:
                logging.exception("Subscription check failed")
                text="Не удалось проверить подписку. Администратору нужно добавить бота в канал как администратора; попробуй позже."
        elif data == "daily": text=daily(uid,q.id)
        elif data == "inventory":
            with connect() as db: items=db.execute("SELECT id,name,value FROM inventory WHERE user_id=? ORDER BY id DESC LIMIT 30",(uid,)).fetchall(); total=db.execute("SELECT COUNT(*) FROM inventory WHERE user_id=?",(uid,)).fetchone()[0]
            text=f"🎒 Инвентарь: {total} предметов. Выбери предмет:" if items else "🎒 Инвентарь пока пуст. Открой кейс!"
            markup=keyboard([[(f'{name} · {value} 🪙',f'item:{iid}')] for iid,name,value in items]+[[('🏠 Меню','menu')]])
            if total>30: text+="\nПоказаны последние 30 предметов."
        elif data.startswith("item:"):
            iid=int(data.split(":")[1]);
            with connect() as db: item=db.execute("SELECT name,value FROM inventory WHERE id=? AND user_id=?",(iid,uid)).fetchone()
            if not item: text="Предмет не найден."
            else: text=f"{item[0]}\nСтоимость: {item[1]} монет\nВыбери действие:"; markup=keyboard([[('💰 Продать',f'sell:{iid}'),('🚀 Улучшить',f'up:{iid}')],[('◀️ Инвентарь','inventory')]])
        elif data.startswith("sell:"): text=sell(uid,int(data.split(":")[1]),q.id)
        elif data.startswith("up:"):
            iid=int(data.split(":")[1]);
            with connect() as db: item=db.execute("SELECT name,value FROM inventory WHERE id=? AND user_id=?",(iid,uid)).fetchone()
            if not item: text="Предмет не найден."
            else:
                text=f"🚀 Апгрейд: {item[0]} ({item[1]} 🪙)\nПри неудаче предмет исчезает. Выбери цель:"
                markup=keyboard([[(f'×{mult}: {item[1]*mult} 🪙 · шанс {chance}%',f'upgrade:{iid}:{key}')] for key,(mult,chance) in UPGRADES.items()]+[[('◀️ Инвентарь','inventory')]])
        elif data.startswith("upgrade:"):
            _,iid,mode=data.split(":"); text=upgrade(uid,int(iid),mode,q.id) if mode in UPGRADES else "Неизвестный апгрейд."
        elif data == "top":
            with connect() as db: rows=db.execute("SELECT u.name,u.balance+COALESCE(SUM(i.value),0) AS wealth FROM users u LEFT JOIN inventory i ON u.id=i.user_id GROUP BY u.id ORDER BY wealth DESC,u.id LIMIT 10").fetchall()
            text="🏆 Топ по состоянию\nМонеты + стоимость инвентаря\n\n"+"\n".join(f"{idx}. {name} — {wealth} 🪙" for idx,(name,wealth) in enumerate(rows,1))
        else: text="Кнопка устарела. Открой меню заново."
        await q.edit_message_text(text,reply_markup=markup)
    except (ValueError, IndexError): await q.edit_message_text("Некорректная кнопка. Открой меню заново.",reply_markup=back())

async def error_handler(update, context): logging.error("Unhandled error",exc_info=context.error)

def main():
    if not TOKEN or TOKEN == "paste_token_here": raise SystemExit("Укажите BOT_TOKEN в файле .env")
    init_db(); app=Application.builder().token(TOKEN).build(); app.add_handler(CommandHandler('start',start)); app.add_handler(CallbackQueryHandler(callback)); app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s'); logging.getLogger('httpx').setLevel(logging.WARNING); main()
