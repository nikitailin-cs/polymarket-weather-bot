"""
Alertes Telegram — optionnel, le bot fonctionne sans.
Configure TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID dans .env
"""
import urllib.request, json, os

try:
    from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
except Exception:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

def send_alert(message: str, emoji: str = "🤖") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"{emoji} {message}",
        "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False

def alert_start() -> None:
    send_alert("Bot démarré ✅", "🟢")

def alert_stop(reason: str = "") -> None:
    send_alert(f"Bot arrêté. {reason}", "🔴")

def alert_trade(side, question, stake, price, edge) -> None:
    send_alert(f"<b>TRADE</b>\n{side} | ${stake:.2f} | edge={edge*100:.1f}%\n{question[:80]}", "💰")

def alert_error(error: str) -> None:
    send_alert(f"<b>ERREUR</b>\n{error[:300]}", "🚨")