"""
Auth élève : JWT signé longue durée dans l'URL (/espace?token=...).
Pas de session, pas de mot de passe.

Variables d'environnement requises :
  JWT_SECRET          – secret de signature (générer avec: python -c "import secrets; print(secrets.token_hex(32))")
  JWT_EXPIRE_DAYS     – durée de vie en jours (défaut : 365)
  BASE_URL            – URL publique de l'app (ex: https://yoga.example.com)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Query, status
from jose import JWTError, jwt

_ALGORITHM = "HS256"


def _secret() -> str:
    return os.environ["JWT_SECRET"]


def _expire_days() -> int:
    return int(os.environ.get("JWT_EXPIRE_DAYS", "365"))


def creer_token_eleve(email: str) -> str:
    """Génère un JWT signé identifiant l'élève. Durée configurable via JWT_EXPIRE_DAYS."""
    expire = datetime.now(timezone.utc) + timedelta(days=_expire_days())
    payload = {"sub": email, "exp": expire}
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def verifier_token(token: str) -> str:
    """
    Vérifie et décode le token. Retourne l'email de l'élève.
    Lève HTTP 401 si invalide ou expiré.
    """
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
        email: str = payload["sub"]
        return email
    except (JWTError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Lien invalide ou expiré. Contactez la professeure pour recevoir un nouveau lien.",
        )


def get_email_from_token(token: str = Query(...)) -> str:
    """Dépendance FastAPI : extrait l'email depuis ?token= dans l'URL."""
    return verifier_token(token)


def lien_espace(token: str) -> str:
    """Construit l'URL complète de l'espace élève."""
    base = os.environ["BASE_URL"].rstrip("/")
    return f"{base}/espace?token={token}"
