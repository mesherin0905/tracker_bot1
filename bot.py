import asyncio
import aiohttp
from aiohttp import web
import os
import json
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import (
    Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters, CallbackQueryHandler
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

CHECK_INTERVAL = 30
HL_API = "https://api.hyperliquid.xyz/info"
WALLETS_FILE = "wallets.json"
STATS_FILE = "stats.json"
STATE_FILE = "fills_state.json"
FILTERS_FILE = "filters.json"

last_known_fills = {}
bot_running = True
monitoring_paused = False

# Глобальная HTTP-сессия
http_session: aiohttp.ClientSession = None

# ======================== КЛАВИАТУРА ========================
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📋 Кошельки", "📊 Позиции"],
            ["📈 Статистика", "➕ Добавить"],
            ["➖ Удалить", "⏸ Пауза / ▶️ Старт"],
            ["❓ Помощь", "🔄 Обновить"],
        ],
        resize_keyboard=True
    )

# =================== РАБОТА С ФАЙЛАМИ =======================
def load_wallets() -> dict:
    if not os.path.exists(WALLETS_FILE):
        return {}
    try:
        with open(WALLETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data.get("wallets"), list):
                return {w: "" for w in data["wallets"]}
            return data.get("wallets", {})
    except Exception as e:
        log.error(f"Ошибка чтения кошельков: {e}")
        return {}

def save_wallets(wallets: dict):
    try:
        with open(WALLETS_FILE, "w", encoding="utf-8") as f:
            json.dump({"wallets": wallets}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Ошибка сохранения кошельков: {e}")

def load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_stats(stats: dict):
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        log.error(f"Ошибка сохранения статистики: {e}")

def load_fills_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return {addr: set(ids) for addr, ids in json.load(f).items()}
        except:
            pass
    return {}

def save_fills_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({addr: list(ids) for addr, ids in state.items()}, f, indent=2)
    except Exception as e:
        log.error(f"Ошибка сохранения fills_state: {e}")

def load_filters() -> dict:
    default = {"min_volume_usd": 0, "coins": [], "notify_opening": True, "notify_closing": True}
    if os.path.exists(FILTERS_FILE):
        try:
            with open(FILTERS_FILE, "r") as f:
                loaded = json.load(f)
                default.update(loaded)
        except:
            pass
    return default

# ====================== СТАТИСТИКА ===========================
def update_stats(address: str, fill: dict):
    stats = load_stats()
    addr = address.lower()
    if addr not in stats:
        stats[addr] = {"total_trades": 0, "wins": 0, "losses": 0,
                       "total_pnl": 0.0, "total_volume": 0.0,
                       "best_trade": 0.0, "worst_trade": 0.0}
    s = stats[addr]
    pnl = float(fill.get("closedPnl", 0))
    sz = float(fill.get("sz", 0))
    px = float(fill.get("px", 0))
    volume = sz * px

    s["total_trades"] += 1
    s["total_pnl"] = round(s["total_pnl"] + pnl, 4)
    s["total_volume"] = round(s["total_volume"] + volume, 4)
    if pnl > 0:
        s["wins"] += 1
        if pnl > s["best_trade"]:
            s["best_trade"] = round(pnl, 4)
    elif pnl < 0:
        s["losses"] += 1
        if pnl < s["worst_trade"]:
            s["worst_trade"] = round(pnl, 4)
    stats[addr] = s
    save_stats(stats)

# ======================== ФИЛЬТРЫ ============================
def should_notify(fill: dict) -> bool:
    filters = load_filters()
    coin = fill.get("coin", "")
    pnl = float(fill.get("closedPnl", 0))
    size = float(fill.get("sz", 0))
    price = float(fill.get("px", 0))
    volume = size * price
    if filters["coins"] and coin not in filters["coins"]:
        return False
    if volume < filters["min_volume_usd"]:
        return False
    if pnl == 0 and not filters["notify_opening"]:
        return False
    if pnl != 0 and not filters["notify_closing"]:
        return False
    return True

# ==================== HYPERLIQUID API ========================
async def hl_request(payload: dict):
    if http_session is None:
        log.error("HTTP-сессия не создана")
        return {}
    try:
        async with http_session.post(HL_API, json=payload,
                                     timeout=aiohttp.ClientTimeout(total=15),
                                     headers={"Content-Type": "application/json"}) as resp:
            if resp.status == 200:
                return await resp.json()
            log.error(f"HL API {resp.status}: {await resp.text()[:200]}")
    except Exception as e:
        log.error(f"Ошибка API: {e}")
    return {}

async def get_fills(address: str) -> list:
    data = await hl_request({"type": "userFills", "user": address.lower(), "limit": 1000})
    return data if isinstance(data, list) else []

async def get_positions(address: str) -> list:
    data = await hl_request({"type": "clearinghouseState", "user": address.lower()})
    if isinstance(data, dict):
        return [p["position"] for p in data.get("assetPositions", [])
                if float(p.get("position", {}).get("szi", 0)) != 0]
    return []

async def get_account_value(address: str) -> float:
    data = await hl_request({"type": "clearinghouseState", "user": address.lower()})
    if isinstance(data, dict):
        return float(data.get("marginSummary", {}).get("accountValue", 0))
    return 0.0

# ================== ФОРМАТИРОВАНИЕ ===========================
def short_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}"

def format_fill_msg(fill: dict, address: str, label: str = "") -> str:
    coin = fill.get("coin", "UNKNOWN")
    side = fill.get("side", "")
    size = float(fill.get("sz", 0))
    price = float(fill.get("px", 0))
    fee = float(fill.get("fee", 0))
    pnl = float(fill.get("closedPnl", 0))
    volume = size * price
    ts = fill.get("time", 0)
    dt = datetime.utcfromtimestamp(ts/1000).strftime('%d.%m.%Y %H:%M') if ts else "N/A"

    direction = "🟢 LONG" if side == "B" else "🔴 SHORT"
    action = "ОТКРЫТИЕ" if pnl == 0 else "ЗАКРЫТИЕ"
    wallet_display = f"{label} (<code>{short_addr(address)}</code>)" if label else f"<code>{short_addr(address)}</code>"

    pnl_line = ""
    if pnl != 0:
        emoji = "💚" if pnl >= 0 else "💔"
        pnl_line = f"\n{emoji} PnL: <b>{pnl:+.2f}$</b>"

    stats = load_stats().get(address.lower(), {})
    stat_block = ""
    if stats:
        total = stats.get("total_trades", 0)
        wins = stats.get("wins", 0)
        winrate = (wins / total * 100) if total > 0 else 0
        total_pnl = stats.get("total_pnl", 0)
        stat_block = (
            f"\n📈 <b>Трейдер:</b> сделок {total}, винрейт {winrate:.1f}%, "
            f"общ. PnL <b>{total_pnl:+.2f}$</b>"
        )

    return (
        f"{'🆕' if pnl==0 else '📤'} <b>{action} | {coin}</b>\n"
        f"👛 {wallet_display} {direction}\n"
        f"📦 {size} {coin}  💱 ${price:,.4f}\n"
        f"💵 Объём ${volume:,.2f}  💸 ком. ${fee:,.4f}"
        f"{pnl_line}\n{dt} UTC{stat_block}\n"
        f"<a href='https://app.hyperliquid.xyz/vaults/{address}'>🔗 Профиль</a>"
    )

def format_position_msg(pos: dict, address: str, label: str = "") -> str:
    coin = pos.get("coin", "UNKNOWN")
    szi = float(pos.get("szi", 0))
    entry = float(pos.get("entryPx", 0))
    pnl = float(pos.get("unrealizedPnl", 0))
    lev = pos.get("leverage", {})
    lev_val = lev.get("value", 1) if isinstance(lev, dict) else 1
    margin = float(pos.get("marginUsed", 0))
    liq = pos.get("liquidationPx")

    direction = "🟢 LONG" if szi > 0 else "🔴 SHORT"
    pnl_emoji = "💚" if pnl >= 0 else "💔"
    pnl_pct = (pnl / margin * 100) if margin > 0 else 0
    liq_str = f" ⚠️ Ликв: ${float(liq):,.4f}" if liq else ""
    wallet_display = f"{label} (<code>{short_addr(address)}</code>)" if label else f"<code>{short_addr(address)}</code>"

    return (
        f"📊 <b>{coin}</b> {direction} {lev_val}x\n"
        f"👛 {wallet_display}\n"
        f"📍 Вход ${entry:,.4f} | 📦 {abs(szi)} {coin}\n"
        f"💵 Маржа ${margin:,.2f} | {pnl_emoji} PnL {pnl:+.2f}$ ({pnl_pct:+.1f}%){liq_str}"
    )

# ====================== МОНИТОРИНГ ===========================
async def check_address(bot: Bot, address: str, label: str = ""):
    addr = address.lower()
    fills = await get_fills(addr)
    if not fills:
        return
    if addr not in last_known_fills:
        last_known_fills[addr] = {
            f"{f.get('time')}_{f.get('coin')}_{f.get('sz')}_{f.get('px')}" for f in fills
        }
        save_fills_state(last_known_fills)
        return

    known = last_known_fills[addr]
    for fill in fills:
        fid = f"{fill.get('time')}_{fill.get('coin')}_{fill.get('sz')}_{fill.get('px')}"
        if fid not in known:
            if not should_notify(fill):
                known.add(fid)
                continue
            update_stats(addr, fill)
            msg = format_fill_msg(fill, addr, label)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Позиции", callback_data=f"pos_{addr}"),
                 InlineKeyboardButton("📈 Статистика", callback_data=f"stat_{addr}")]
            ])
            try:
                await bot.send_message(CHAT_ID, msg, parse_mode='HTML',
                                       reply_markup=keyboard, disable_web_page_preview=True)
                log.info(f"Уведомление: {addr[:10]} {fill.get('coin')}")
            except Exception as e:
                log.error(f"Ошибка отправки: {e}")
            known.add(fid)
            await asyncio.sleep(0.5)
    last_known_fills[addr] = known
    save_fills_state(last_known_fills)

async def monitoring_loop(bot: Bot):
    log.info("Мониторинг запущен!")
    while bot_running:
        if monitoring_paused:
            await asyncio.sleep(CHECK_INTERVAL)
            continue
        wallets = load_wallets()
        for addr, label in wallets.items():
            try:
                await check_address(bot, addr, label)
            except Exception as e:
                log.error(f"Ошибка мониторинга {addr[:10]}: {e}")
            await asyncio.sleep(2)
        await asyncio.sleep(CHECK_INTERVAL)

# ======================== КОМАНДЫ ============================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Hyperliquid Wallet Tracker</b>\n\n"
        "📋 /list – кошельки\n"
        "📊 /status – открытые позиции\n"
        "📈 /stats – статистика\n"
        "➕ /add 0x... Метка\n"
        "➖ /remove 0x... (или без аргументов – выбрать из списка)\n"
        "⏸ /pause | ▶️ /resume\n"
        "⚙️ Фильтры в filters.json",
        parse_mode='HTML',
        reply_markup=main_keyboard()
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Формат: /add 0xАдрес [Метка]", reply_markup=main_keyboard())
        return
    addr = context.args[0].lower().strip()
    if not addr.startswith("0x") or len(addr) != 42:
        await update.message.reply_text("❌ Неверный адрес!", reply_markup=main_keyboard())
        return
    label = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    wallets = load_wallets()
    if addr in wallets:
        await update.message.reply_text("⚠️ Уже добавлен!", reply_markup=main_keyboard())
        return
    wallets[addr] = label
    save_wallets(wallets)
    msg = f"✅ Добавлен: <code>{addr}</code>"
    if label:
        msg += f"\n🏷 {label}"
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=main_keyboard())

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = load_wallets()
    if not wallets:
        await update.message.reply_text("📭 Список пуст.", reply_markup=main_keyboard())
        return
    if context.args:
        addr = context.args[0].lower().strip()
        if addr in wallets:
            del wallets[addr]
            save_wallets(wallets)
            await update.message.reply_text(f"✅ Удалён: <code>{addr}</code>", parse_mode='HTML', reply_markup=main_keyboard())
        else:
            await update.message.reply_text("❌ Адрес не найден.", reply_markup=main_keyboard())
    else:
        buttons = []
        for a, l in wallets.items():
            display = f"❌ {l} {short_addr(a)}" if l else f"❌ {short_addr(a)}"
            buttons.append([InlineKeyboardButton(display, callback_data=f"del_{a}")])
        await update.message.reply_text("Выбери для удаления:", reply_markup=InlineKeyboardMarkup(buttons))

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    wallets = list(load_wallets().items())
    per_page = 5
    total_pages = max(1, (len(wallets) + per_page - 1) // per_page)
    if not wallets:
        await update.message.reply_text("📭 Кошельков нет.")
        return
    start = page * per_page
    end = start + per_page
    chunk = wallets[start:end]
    text = f"📋 <b>Кошельки (стр. {page+1}/{total_pages})</b>\n\n"
    for i, (addr, label) in enumerate(chunk, start+1):
        lab = f" — <b>{label}</b>" if label else ""
        text += f"{i}. <code>{addr}</code>{lab}\n"
    keyboard = []
    for addr, _ in chunk:
        keyboard.append([InlineKeyboardButton(f"❌ Удалить {short_addr(addr)}", callback_data=f"del_{addr}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data=f"list_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"list_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = load_wallets()
    if not wallets:
        await update.message.reply_text("📭 Нет кошельков.", reply_markup=main_keyboard())
        return
    wait = await update.message.reply_text("⏳ Загрузка позиций...")
    total_value = 0.0
    total_pnl = 0.0
    text = "📊 <b>Открытые позиции</b>\n\n"
    for addr, label in wallets.items():
        display = f"<b>{label}</b> {short_addr(addr)}" if label else short_addr(addr)
        try:
            positions = await get_positions(addr)
            acc_val = await get_account_value(addr)
            total_value += acc_val
            text += f"👛 {display}\n"
            if positions:
                for pos in positions:
                    pnl = float(pos.get("unrealizedPnl", 0))
                    total_pnl += pnl
                    text += format_position_msg(pos, addr, label) + "\n\n"
            else:
                text += "  💤 Нет позиций\n"
            text += f"  💼 Аккаунт: <b>${acc_val:,.2f}</b>\n\n"
        except Exception as e:
            log.error(f"Ошибка статуса для {addr}: {e}")
            text += "  ⚠️ Ошибка загрузки\n\n"
    text += f"{'═'*20}\n💼 Всего: ${total_value:,.2f}  Общ. PnL: {total_pnl:+.2f}$"
    await wait.edit_text(text, parse_mode='HTML')

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = load_wallets()
    all_stats = load_stats()
    if not wallets:
        await update.message.reply_text("📭 Нет кошельков.")
        return
    text = "📈 <b>Статистика сделок</b>\n\n"
    for addr, label in wallets.items():
        s = all_stats.get(addr.lower(), {})
        display = f"<b>{label}</b> ({short_addr(addr)})" if label else f"<code>{addr}</code>"
        if not s:
            text += f"👛 {display}\n  📭 Нет данных\n\n"
            continue
        total = s.get("total_trades", 0)
        wins = s.get("wins", 0)
        pnl = s.get("total_pnl", 0)
        wr = (wins/total*100) if total > 0 else 0
        text += (f"👛 {display}\n"
                 f"  Сделок: {total} | Винрейт: {wr:.1f}%\n"
                 f"  PnL: {pnl:+.2f}$ | Объём: ${s.get('total_volume',0):,.2f}\n"
                 f"  Лучшая: +${s.get('best_trade',0):,.2f} | Худшая: ${s.get('worst_trade',0):,.2f}\n\n")
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=main_keyboard())

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_paused
    monitoring_paused = True
    await update.message.reply_text("⏸ Мониторинг приостановлен.", reply_markup=main_keyboard())

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_paused
    monitoring_paused = False
    await update.message.reply_text("▶️ Мониторинг возобновлён.", reply_markup=main_keyboard())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ <b>Помощь</b>\n\n"
        "➕ /add 0x... Метка\n"
        "➖ /remove [адрес]\n"
        "📋 /list\n"
        "📊 /status\n"
        "📈 /stats\n"
        "⏸ /pause | ▶️ /resume\n"
        "🔄 Кнопка «Обновить» на клавиатуре\n\n"
        "⚙️ Фильтры уведомлений — файл filters.json",
        parse_mode='HTML',
        reply_markup=main_keyboard()
    )

# =================== ОБРАБОТЧИК КНОПОК =======================
async def keyboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📋 Кошельки":
        await cmd_list(update, context)
    elif text == "📊 Позиции":
        await cmd_status(update, context)
    elif text == "📈 Статистика":
        await cmd_stats(update, context)
    elif text == "➕ Добавить":
        await update.message.reply_text("Отправь /add 0xАдрес [Метка]", reply_markup=main_keyboard())
    elif text == "➖ Удалить":
        await cmd_remove(update, context)
    elif text == "⏸ Пауза / ▶️ Старт":
        global monitoring_paused
        monitoring_paused = not monitoring_paused
        st = "приостановлен" if monitoring_paused else "активен"
        await update.message.reply_text(f"⏸▶️ Мониторинг {st}.", reply_markup=main_keyboard())
    elif text == "❓ Помощь":
        await cmd_help(update, context)
    elif text == "🔄 Обновить":
        await update.message.reply_text("🔄 Обновляю данные...", reply_markup=main_keyboard())
        await cmd_status(update, context)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    wallets = load_wallets()

    if data.startswith("list_page_"):
        page = int(data.split("_")[-1])
        await query.message.delete()
        await cmd_list(update, context, page)
        return

    if data.startswith("del_"):
        addr = data[4:]
        if addr in wallets:
            label = wallets.pop(addr)
            save_wallets(wallets)
            if addr in last_known_fills:
                del last_known_fills[addr]
                save_fills_state(last_known_fills)
            lab = f" ({label})" if label else ""
            await query.edit_message_text(f"✅ Удалён{lab}: <code>{addr}</code>", parse_mode='HTML')
        else:
            await query.edit_message_text("❌ Не найден.")
        return

    if data.startswith("pos_"):
        addr = data[4:]
        label = wallets.get(addr, "")
        try:
            positions = await get_positions(addr)
            acc_val = await get_account_value(addr)
            if positions:
                txt = f"📊 <b>Позиции {label} {short_addr(addr)}</b>\n"
                for pos in positions:
                    txt += format_position_msg(pos, addr, label) + "\n\n"
                txt += f"💼 Аккаунт: ${acc_val:,.2f}"
            else:
                txt = f"💤 Нет позиций (баланс ${acc_val:,.2f})"
            await query.message.reply_text(txt, parse_mode='HTML')
        except Exception as e:
            await query.message.reply_text("⚠️ Ошибка загрузки позиций.")
        return

    if data.startswith("stat_"):
        addr = data[5:]
        label = wallets.get(addr, "")
        s = load_stats().get(addr.lower(), {})
        if not s:
            await query.message.reply_text("📭 Нет статистики.")
            return
        total = s.get("total_trades", 0)
        wins = s.get("wins", 0)
        wr = (wins/total*100) if total > 0 else 0
        await query.message.reply_text(
            f"📈 <b>{label} {short_addr(addr)}</b>\n"
            f"Сделок: {total} | Винрейт: {wr:.1f}%\n"
            f"PnL: {s['total_pnl']:+.2f}$ | Объём: ${s['total_volume']:,.2f}\n"
            f"Лучшая: +${s['best_trade']:,.2f} | Худшая: ${s['worst_trade']:,.2f}",
            parse_mode='HTML'
        )
        return

# ================== ВЕБ-СЕРВЕР ДЛЯ RENDER ===================
async def handle_health(request):
    return web.Response(text="OK")

async def run_web_server():
    port = int(os.getenv("PORT", 10000))
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Веб-сервер запущен на порту {port}")

# ==================== ЗАПУСК ================================
async def main():
    global http_session, last_known_fills
    if not TOKEN or not CHAT_ID:
        log.error("❌ TELEGRAM_TOKEN или CHAT_ID не заданы в .env")
        return

    timeout = aiohttp.ClientTimeout(total=60, connect=30)
    http_session = aiohttp.ClientSession(timeout=timeout)
    last_known_fills = load_fills_state()
    log.info(f"Загружено состояние fills для {len(last_known_fills)} кошельков")

    app = Application.builder().token(TOKEN).build()
    for attempt in range(5):
        try:
            await app.bot.set_my_commands([
                BotCommand("start", "Главное меню"),
                BotCommand("add", "Добавить кошелёк"),
                BotCommand("remove", "Удалить кошелёк"),
                BotCommand("list", "Список кошельков"),
                BotCommand("status", "Открытые позиции"),
                BotCommand("stats", "Статистика сделок"),
                BotCommand("pause", "Пауза мониторинга"),
                BotCommand("resume", "Возобновить мониторинг"),
                BotCommand("help", "Помощь"),
            ])
            break
        except Exception as e:
            log.warning(f"Попытка {attempt+1}: {e}")
            await asyncio.sleep(5)
    else:
        log.error("Не удалось подключиться к Telegram API")
        return

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", lambda u, c: cmd_list(u, c)))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, keyboard_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))

    await app.initialize()
    await app.start()
    poll = asyncio.create_task(app.updater.start_polling(drop_pending_updates=True))
    monitor = asyncio.create_task(monitoring_loop(app.bot))
    web_task = asyncio.create_task(run_web_server())

    log.info("✅ Бот запущен и мониторит кошельки.")
    await asyncio.gather(poll, monitor, web_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Бот остановлен")
    except Exception as e:
        log.error(f"Критическая ошибка: {e}")
