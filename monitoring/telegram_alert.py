"""
Alertes Telegram — optionnel, le bot fonctionne sans.
Configure TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID dans .env
"""
import json
import os
import urllib.request

try:
    from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
except Exception:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def _html_escape(text: str, max_len: int = 480) -> str:
    s = (text or "")[:max_len]
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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


def alert_bot2_rt_scalp_success(
    *,
    station_icao: str,
    shares: float,
    outcome_side: str,
    price: float,
    reason_line: str,
    stake_usdc: float,
    market_id: str = "",
    order_id: str = "",
) -> None:
    """
    Bot 2 temps réel (METAR / scalping) — distinct du batch NOAA long-terme et du bot1 RT YES-only.
    Exemple : [SCALP RT] Achat de 50 parts YES sur KORD à 95c. Raison : … METAR.
    """
    st = _html_escape(station_icao or "—", 8)
    side = _html_escape((outcome_side or "?").upper(), 4)
    cents = float(price) * 100.0
    reason = _html_escape(reason_line, 420)
    mid = _html_escape(market_id[:18] + "…" if len(market_id) > 20 else market_id, 28)
    oid = _html_escape(order_id[:22] + "…" if len(order_id) > 24 else order_id, 48)
    lines = [
        "<b>[SCALP RT]</b> <i>bot2 · METAR</i>",
        f"Achat de <b>{shares:.2f}</b> parts <b>{side}</b> sur <b>{st}</b> à <b>{cents:.2f}c</b>.",
        f"Raison : {reason}",
        f"Notional ~ <b>${stake_usdc:.2f}</b> USDC",
    ]
    if mid:
        lines.append(f"Marché: <code>{mid}</code>")
    if oid:
        lines.append(f"Order: <code>{oid}</code>")
    send_alert("\n".join(lines), "⚡")


def alert_rt_scalp_success(
    *,
    station: str,
    shares: float,
    price: float,
    stake_usdc: float,
    market_id: str = "",
    order_id: str = "",
    condition_met: bool = True,
) -> None:
    """
    Alerte dédiée scalping temps réel (bot1 RT), distincte du batch NOAA.
    Exemple : Bought 500 YES on KJFK at 95.0c. Condition Met!
    """
    st = _html_escape(station or "—", 64)
    cents = float(price) * 100.0
    mid = _html_escape(market_id[:18] + "…" if len(market_id) > 20 else market_id, 28)
    oid = _html_escape(order_id[:22] + "…" if len(order_id) > 24 else order_id, 48)
    cond = "Condition Met!" if condition_met else ""
    lines = [
        "<b>SCALP SUCCESS</b>",
        f"Bought <b>{shares:.2f}</b> YES on <b>{st}</b> at <b>{cents:.1f}c</b>. {cond}",
        f"Notional ~ <b>${stake_usdc:.2f}</b> USDC",
    ]
    if mid:
        lines.append(f"Marché: <code>{mid}</code>")
    if oid:
        lines.append(f"Order: <code>{oid}</code>")
    send_alert("\n".join(lines), "🚀")


def alert_bot1_position_open(
    *,
    question: str,
    side: str,
    price: float,
    stake_usdc: float,
    shares: float,
    station: str,
    ev: float,
    market_id: str = "",
    order_id: str = "",
    note: str = "",
) -> None:
    q = _html_escape(question, 400)
    st = _html_escape(station or "—", 64)
    oid = _html_escape(order_id[:22] + "…" if len(order_id) > 24 else order_id, 48)
    mid = _html_escape(market_id[:14] + "…" if len(market_id) > 16 else market_id, 24)
    nt = _html_escape(note, 200) if note else ""
    lines = [
        "<b>Ouverture de position (bot1)</b>",
        q,
        f"Côté: <b>{_html_escape(side, 4)}</b> · Lieu: {st}",
        f"Prix d’achat (meilleur ask): <b>{price:.4f}</b>",
        f"Mise (USDC dépensés): <b>${stake_usdc:.2f}</b> · Taille: {shares:.4f} shares",
        f"EV (signal − ask): {ev:.4f}",
    ]
    if nt:
        lines.append(nt)
    if mid:
        lines.append(f"Marché: <code>{mid}</code>")
    if oid:
        lines.append(f"Order: <code>{oid}</code>")
    send_alert("\n".join(lines), "📥")


def alert_bot1_position_close(
    *,
    title: str,
    question: str,
    side: str,
    station: str,
    pnl_usdc: float,
    stake_usdc: float,
    detail: str = "",
    emoji: str = "📤",
    cashout_context_tag: str = "",
) -> None:
    """
    ``cashout_context_tag`` : ex. « 💰 [SCALP RT CASH-OUT] » quand le settler utilise
    ``portfolio_rt.sqlite`` (temps réel) pour distinguer les gains de scalping du long terme.
    """
    q = _html_escape(question, 400)
    st = _html_escape(station or "—", 64)
    det = _html_escape(detail, 200) if detail else ""
    pnl_s = f"{pnl_usdc:+.2f}"
    lines: list[str] = []
    tag = (cashout_context_tag or "").strip()
    if tag:
        lines.append(f"<b>{_html_escape(tag, 96)}</b>")
    lines.extend(
        [
            f"<b>{_html_escape(title, 80)}</b>",
            q,
            f"Côté joué: <b>{_html_escape(side, 4)}</b> · {st}",
            f"Mise (réf.): ${stake_usdc:.2f} USDC",
            f"<b>Résultat: {pnl_s} USDC</b>",
        ]
    )
    if det:
        lines.append(det)
    send_alert("\n".join(lines), emoji)


def alert_error(error: str) -> None:
    send_alert(f"<b>ERREUR</b>\n{error[:300]}", "🚨")