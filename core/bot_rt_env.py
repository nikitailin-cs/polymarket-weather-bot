"""
Contexte d’environnement **exclusif** au pipeline temps réel (signature CLOB).

Définit des valeurs par défaut pour que le client d’ordres accepte les contrats
Polymarket en mode attendu (sans écraser une configuration déjà présente dans
l’environnement ou le ``.env`` chargé avant l’appel).

À appeler au démarrage des processus qui envoient des ordres RT (ex.
``finance_engine_rt``), après ``load_dotenv`` si besoin.
"""
from __future__ import annotations

import os


def apply_rt_signing_env() -> None:
    """``POLYMARKET_IS_CONTRACT=true``, ``TYPE=1`` pour le chemin de signature RT."""
    os.environ.setdefault("POLYMARKET_IS_CONTRACT", "true")
    os.environ.setdefault("TYPE", "1")
