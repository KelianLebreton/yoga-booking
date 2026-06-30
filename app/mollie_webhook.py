"""
Crédit des achats Mollie.

Webador crée des *orders* Mollie mais ne déclenche aucun webhook exploitable
vers notre app. On procède donc par **réconciliation via l'API Mollie** :
l'endpoint /tasks/reconcile-mollie interroge l'API, repère les paiements payés
non encore traités, crédite l'élève dans Sheets et envoie le lien espace.

Le endpoint /webhook/mollie reste présent (accuse réception du hook.ping de test)
mais n'est plus la source de vérité.

Variables d'environnement :
  MOLLIE_API_KEY   – clé Mollie (live_... ou test_...)
  ADMIN_KEY        – protège les endpoints /debug et /tasks (?key=...)
  MOLLIE_WEBHOOK_SECRET – secret optionnel de signature (si configuré)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from datetime import date

from fastapi import APIRouter, Header, HTTPException, Request, status

from auth import creer_token_eleve
from booking_logic import TypeCours, Formule, credits_apres_achat
from email_client import envoyer_lien_espace
from sheets_client import get_sheets_client

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Webhook (conservé pour le hook.ping ; Webador n'envoie pas de vrai événement)
# ---------------------------------------------------------------------------

@router.post("/webhook/mollie", status_code=status.HTTP_200_OK)
async def webhook_mollie(
    request: Request,
    x_mollie_signature: str | None = Header(default=None),
) -> dict:
    body = await request.body()
    _verifier_signature(body, x_mollie_signature)

    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        return {"detail": "corps non-JSON"}

    event_type = payload.get("type", "")
    if event_type == "hook.ping":
        return {"detail": "pong"}

    logger.warning("Webhook Mollie — type=%r reçu (non traité, voir réconciliation)", event_type)
    return {"detail": "reçu", "type": event_type}


# ---------------------------------------------------------------------------
# Réconciliation Mollie : crédite les paiements payés non encore traités.
# ?key=<ADMIN_KEY>  &  ?dry_run=1 pour un aperçu sans rien écrire.
# ---------------------------------------------------------------------------

@router.get("/tasks/reconcile-mollie")
async def reconcile_mollie(request: Request) -> dict:
    import httpx

    admin_key = os.environ.get("ADMIN_KEY", "")
    if not admin_key or request.query_params.get("key") != admin_key:
        raise HTTPException(status_code=403, detail="Clé invalide.")
    dry_run = request.query_params.get("dry_run") == "1"
    only_pid = request.query_params.get("payment_id")  # optionnel : ne traiter qu'un paiement

    api_key = os.environ["MOLLIE_API_KEY"].strip()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.mollie.com/v2/payments?limit=50",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20.0,
        )
    payments = (resp.json().get("_embedded", {}) or {}).get("payments", [])

    sheets = get_sheets_client()
    deja = sheets.paiements_traites_ids()

    resultats = []
    for p in payments:
        if p.get("status") != "paid":
            continue
        pid = p.get("id", "")
        if only_pid and pid != only_pid:
            continue
        if pid in deja:
            resultats.append({"payment_id": pid, "statut": "déjà_traité"})
            continue
        order = await _fetch_order(p["orderId"]) if p.get("orderId") else None
        resultats.append(await _reconcilier_paiement(p, order, sheets, dry_run))

    return {
        "dry_run": dry_run,
        "paiements_payes": sum(1 for p in payments if p.get("status") == "paid"),
        "resultats": resultats,
    }


async def _reconcilier_paiement(paiement: dict, order: dict | None, sheets, dry_run: bool) -> dict:
    """
    Traite un paiement payé : identifie le produit, crédite l'élève et envoie le
    lien espace (sauf dry_run). Retourne un compte-rendu. La déduplication est à la
    charge de l'appelant (vérifier paiements_traites_ids avant d'appeler).
    """
    pid = paiement.get("id", "")
    src = order or paiement
    nom_produit = _nom_produit(src)
    billing = (order or {}).get("billingAddress", {}) or {}
    email = (billing.get("email") or "").strip().lower()
    prenom = (billing.get("givenName") or "").strip()
    famille = (billing.get("familyName") or "").strip()
    nom = f"{prenom} {famille}".strip()

    produit = _identifier_produit(src)
    if produit is None:
        return {"payment_id": pid, "statut": "produit_non_reconnu", "produit": nom_produit, "email": email}
    if not email:
        return {"payment_id": pid, "statut": "sans_email", "produit": nom_produit}

    type_cours, formule = produit
    if dry_run:
        return {"payment_id": pid, "statut": "à_créditer", "produit": nom_produit,
                "email": email, "credit": f"{type_cours.value}/{formule.value}"}

    credits_initiaux, date_expiration = credits_apres_achat(formule, date.today())
    sheets.upsert_eleve(email, nom, "", contact_urgence="")
    sheets.add_credit(
        email=email, type_cours=type_cours, formule=formule,
        credits_restants=credits_initiaux, date_expiration=date_expiration, statut="actif",
    )
    try:
        token = creer_token_eleve(email)
        await envoyer_lien_espace(email=email, nom=nom, token=token)
        mail = "envoyé"
    except Exception as e:
        logger.warning("Réconciliation : mail échoué pour %s : %s", email, e)
        mail = "échec"
    sheets.marquer_paiement_traite(pid, (order or {}).get("id", ""), email, f"{type_cours.value}/{formule.value}")
    return {"payment_id": pid, "statut": "crédité", "produit": nom_produit,
            "email": email, "credit": f"{type_cours.value}/{formule.value}", "mail": mail}


# ---------------------------------------------------------------------------
# Catalogue produits Webador → (TypeCours, Formule)
# Webador ne renseigne PAS le SKU : on matche sur le NOM d'affichage de la ligne.
# La comparaison passe par _normaliser() (apostrophes, espaces, slashs) pour
# tolérer les variantes typographiques de Webador.
# ---------------------------------------------------------------------------

_CATALOGUE: dict[str, tuple[TypeCours, Formule]] = {
    "yoga aérien/ashtanga/vinyasa ; séance découverte": (TypeCours.AERIEN_ASHTANGA_VINYASA, Formule.ESSAI),
    # "yoga sonore et yoga aérien": (TypeCours.AERIEN, Formule.STAGE),
    "stage yoga aérien": (TypeCours.AERIEN, Formule.STAGE),
    "3 séances offre special": (TypeCours.AERIEN_ASHTANGA_VINYASA, Formule.C3),
    "séances d'été ashtanga/vinyasa": (TypeCours.ASHTANGA_VINYASA, Formule.C5),
    "séances d'été yoga aérien": (TypeCours.AERIEN, Formule.C5),
    "séance personnalisée": (TypeCours.PERSO, Formule.UNITE),
    "ashtanga & flow yoga carte de 5": (TypeCours.ASHTANGA_VINYASA, Formule.C5),
    "yoga aérien carte de 5": (TypeCours.AERIEN, Formule.C5),
    "ashtanga & flow yoga carte de 10": (TypeCours.ASHTANGA_VINYASA, Formule.C10),
    "yoga aérien carte de 10": (TypeCours.AERIEN, Formule.C10),
}


def _normaliser(s: str) -> str:
    """Normalise un nom de produit : minuscules, apostrophes droites, espaces
    autour des slashs supprimés, espaces multiples réduits."""
    s = (s or "").strip().lower()
    s = s.replace("’", "'").replace("‘", "'").replace("`", "'")
    s = re.sub(r"\s*/\s*", "/", s)
    s = re.sub(r"\s+", " ", s)
    return s


_CATALOGUE_NORM: dict[str, tuple[TypeCours, Formule]] = {
    _normaliser(k): v for k, v in _CATALOGUE.items()
}


def _nom_produit(data: dict) -> str:
    """Nom de la première ligne de commande (ou description en repli)."""
    lines = data.get("lines") or []
    nom = (lines[0].get("name") or "") if lines else ""
    return nom or (data.get("description") or "")


def _identifier_produit(data: dict) -> tuple[TypeCours, Formule] | None:
    """Identifie le produit via le nom de la première ligne (matching normalisé)."""
    return _CATALOGUE_NORM.get(_normaliser(_nom_produit(data)))


# ---------------------------------------------------------------------------
# Appels API Mollie
# ---------------------------------------------------------------------------

async def _fetch_order(order_id: str) -> dict:
    """Récupère un order Mollie avec ses lignes (email client + produit)."""
    import httpx

    api_key = os.environ["MOLLIE_API_KEY"].strip()
    url = f"https://api.mollie.com/v2/orders/{order_id}?embed=lines"
    async with httpx.AsyncClient() as client:
        response = await client.get(
            url, headers={"Authorization": f"Bearer {api_key}"}, timeout=10.0
        )
    if response.status_code != 200:
        logger.error("Mollie Orders API %s pour order %s", response.status_code, order_id)
        return {}
    return response.json()


async def _fetch_paiement(payment_id: str) -> dict:
    """Récupère les détails d'un paiement via l'API Mollie REST."""
    import httpx

    api_key = os.environ["MOLLIE_API_KEY"].strip()
    url = f"https://api.mollie.com/v2/payments/{payment_id}"
    async with httpx.AsyncClient() as client:
        response = await client.get(
            url, headers={"Authorization": f"Bearer {api_key}"}, timeout=10.0
        )
    if response.status_code != 200:
        logger.error("Mollie API %s pour payment %s", response.status_code, payment_id)
        raise HTTPException(status_code=502, detail=f"Erreur Mollie API : {response.status_code}")
    return response.json()


# ---------------------------------------------------------------------------
# Vérification de signature HMAC (optionnelle)
# ---------------------------------------------------------------------------

def _verifier_signature(body: bytes, signature: str | None) -> None:
    """Vérifie la signature HMAC-SHA256 si MOLLIE_WEBHOOK_SECRET est défini."""
    secret = os.environ.get("MOLLIE_WEBHOOK_SECRET", "")
    if not secret:
        return
    if not signature:
        raise HTTPException(status_code=401, detail="Signature manquante.")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Signature invalide.")
