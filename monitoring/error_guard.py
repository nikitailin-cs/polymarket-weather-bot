"""
Garde-fou global d'erreurs runtime avec alerte Telegram.

Objectif: capter les exceptions non gérées (main thread + threads) et envoyer
une alerte explicite, même si le script plante tôt.
"""
from __future__ import annotations

import logging
import sys
import threading
import traceback
from typing import Callable, Optional

from monitoring.telegram_alert import alert_error

_LOG = logging.getLogger("error_guard")


def _format_exception(component: str, exc_type: type[BaseException], exc_value: BaseException, tb) -> str:
    tb_text = "".join(traceback.format_exception(exc_type, exc_value, tb))
    # Telegram a une limite de taille: on garde un contexte utile.
    return f"[{component}] Unhandled exception:\n{tb_text[-3500:]}"


def install_global_exception_hooks(component: str) -> None:
    """
    Installe un hook global sur les exceptions non gérées.
    """

    def _sys_hook(exc_type, exc_value, exc_tb) -> None:
        try:
            msg = _format_exception(component, exc_type, exc_value, exc_tb)
            alert_error(msg)
        except Exception:
            _LOG.debug("alert_error failed in sys.excepthook", exc_info=True)
        # Préserve le comportement standard (traceback stderr).
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        try:
            msg = _format_exception(component, args.exc_type, args.exc_value, args.exc_traceback)
            alert_error(msg)
        except Exception:
            _LOG.debug("alert_error failed in threading.excepthook", exc_info=True)
        try:
            threading.__excepthook__(args)
        except Exception:
            pass

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


def run_guarded(component: str, fn: Callable[[], int]) -> int:
    """
    Exécute `fn` avec hook global + alerte Telegram sur exception fatale.
    """
    install_global_exception_hooks(component)
    try:
        return int(fn())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        try:
            alert_error(f"[{component}] Fatal error: {exc}")
        except Exception:
            _LOG.debug("alert_error failed in run_guarded", exc_info=True)
        raise

