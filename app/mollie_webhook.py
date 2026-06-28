"""
Réception et traitement des webhooks Mollie.

Flux : Mollie POST /webhook/mollie → on récupère le paiement via l'API Mollie
→ on parse la référence produit → on crédite l'élève dans Sheets → on envoie
le lien permanent par email.

Variable d'environnement requise :
  MOLLIE_API_KEY        – clé Mollie (live_... ou test_...)
  MOLLIE_WEBHOOK_SECRET – secret optionnel pour valider la signature (si configuré)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import date

from fastapi import APIRouter, Header, HTTPException, Request, status

from auth import creer_token_eleve
from booking_logic import TypeCours, Formule, credits_apres_achat
from email_client import envoyer_lien_espace
from sheets_client import get_sheets_client

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Route webhook
# ---------------------------------------------------------------------------

@router.post("/webhook/mollie", status_code=status.HTTP_200_OK)
async def webhook_mollie(
    request: Request,
    x_mollie_signature: str | None = Header(default=None),
) -> dict:
    """
    Mollie appelle cette URL après chaque changement de statut de paiement.
    On ne traite que les paiements au statut "paid".
    """
    body = await request.body()

    # --- LOG TEMPORAIRE DE DIAGNOSTIC (à retirer une fois le format identifié) ---
    logger.warning(
        "Webhook Mollie reçu — content-type=%r — body=%r",
        request.headers.get("content-type"),
        body.decode("utf-8", "replace")[:2000],
    )
    # ---------------------------------------------------------------------------

    _verifier_signature(body, x_mollie_signature)

    form = await request.form()
    payment_id: str = form.get("id", "")
    if not payment_id:
        raise HTTPException(status_code=400, detail="Champ 'id' manquant dans le webhook.")

    paiement = await _fetch_paiement(payment_id)

    if paiement["status"] != "paid":
        return {"detail": "statut ignoré", "status": paiement["status"]}

    order = None
    if paiement.get("orderId"):
        order = await _fetch_order(paiement["orderId"])

    await _traiter_paiement(paiement, order)
    return {"detail": "ok"}


# ---------------------------------------------------------------------------
# Traitement d'un paiement confirmé
# ---------------------------------------------------------------------------

async def _traiter_paiement(paiement: dict, order: dict | None = None) -> None:
    """
    Parse la référence produit, crée/met à jour l'élève, ajoute la ligne
    Crédits et envoie le lien espace élève.

    Webador crée des orders Mollie : l'email vient de billingAddress et la
    référence produit du champ SKU des lignes (champ "référence" dans Webador).
    """
    metadata = paiement.get("metadata", {}) or {}
    email: str = (metadata.get("email") or "").strip().lower()
    nom: str = (metadata.get("nom") or "").strip()
    telephone: str = (metadata.get("telephone") or "").strip()

    if order:
        billing = order.get("billingAddress", {}) or {}
        if not email:
            email = (billing.get("email") or "").strip().lower()
        if not nom:
            prenom = (billing.get("givenName") or "").strip()
            famille = (billing.get("familyName") or "").strip()
            nom = f"{prenom} {famille}".strip()
        if not telephone:
            telephone = (billing.get("phone") or "").strip()

    if not email:
        logger.error("Paiement %s sans email (ni metadata ni billingAddress)", paiement.get("id"))
        return

    produit = _identifier_produit(order or paiement)
    if produit is None:
        logger.warning(
            "Paiement %s : produit non reconnu dans le catalogue — ignoré", paiement.get("id")
        )
        return
    type_cours, formule = produit

    credits_initiaux, date_expiration = credits_apres_achat(formule, date.today())

    sheets = get_sheets_client()
    sheets.upsert_eleve(email, nom, telephone, contact_urgence="")
    sheets.add_credit(
        email=email,
        type_cours=type_cours,
        formule=formule,
        credits_restants=credits_initiaux,
        date_expiration=date_expiration,
        statut="actif",
    )

    token = creer_token_eleve(email)
    await envoyer_lien_espace(email=email, nom=nom, token=token)

    logger.info("Paiement %s traité : %s / %s pour %s", paiement.get("id"), type_cours, formule, email)


# Mapping nom Webador (normalisé) → (TypeCours, Formule)
# Les noms sont en minuscules sans espaces superflus pour la comparaison.
_CATALOGUE: dict[str, tuple[TypeCours, Formule]] = {
    "yoga aérien/ashtanga/vinyasa ; séance découverte": (TypeCours.AERIEN_ASHTANGA_VINYASA, Formule.ESSAI),
    "yoga sonore et yoga aérien": (TypeCours.AERIEN, Formule.C10),
    "stage yoga aérien": (TypeCours.AERIEN, Formule.STAGE),
    "3 séances offre special"
    "séances d'été ashtanga/vinyasa"
    "séances d'été yoga aérien"
    "séance personnalisée"
    "ashtanga & flow yoga carte de 5"
    "yoga aérien carte de 5"
    "ashtanga & flow yoga carte de 10"
    "yoga aérien carte de 10":                    (TypeCours.AERIEN, Formule.C10),
    
    "yoga aérien à la carte de 5":                     (TypeCours.AERIEN, Formule.C5),
    "ashtanga & flow yoga carte de 10":                 (TypeCours.ASHTANGA_VINYASA, Formule.C10),
    "ashtanga & flow yoga carte de 5 sèances":          (TypeCours.ASHTANGA_VINYASA, Formule.C5),
    "ashtanga & flow yoga carte de 5 séances":          (TypeCours.ASHTANGA_VINYASA, Formule.C5),
    "séances d'été ashtanga / vinyasa":                 (TypeCours.ASHTANGA_VINYASA, Formule.C5),
    "séances d'été yoga aérien":                        (TypeCours.AERIEN_ETE, Formule.C5),
}


def _identifier_produit(data: dict) -> tuple[TypeCours, Formule] | None:
    """
    Identifie le produit acheté à partir du nom de la première ligne du panier Mollie.
    Retourne None si le produit n'est pas dans le catalogue (retreat, stage, etc.).
    """
    lines = data.get("lines") or []
    nom = (lines[0].get("name") or "").strip().lower() if lines else ""
    if not nom:
        nom = (data.get("description") or "").strip().lower()
    return _CATALOGUE.get(nom)


# ---------------------------------------------------------------------------
# Appels API Mollie
# ---------------------------------------------------------------------------

async def _fetch_order(order_id: str) -> dict:
    """Récupère un order Mollie avec ses lignes pour avoir le SKU et l'email client."""
    import httpx

    api_key = os.environ["MOLLIE_API_KEY"]
    url = f"https://api.mollie.com/v2/orders/{order_id}?embed=lines"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )

    if response.status_code != 200:
        logger.error("Mollie Orders API %s pour order %s", response.status_code, order_id)
        return {}

    return response.json()


async def _fetch_paiement(payment_id: str) -> dict:
    """Récupère les détails d'un paiement via l'API Mollie REST."""
    import httpx

    api_key = os.environ["MOLLIE_API_KEY"]
    url = f"https://api.mollie.com/v2/payments/{payment_id}"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )

    if response.status_code != 200:
        logger.error("Mollie API %s pour payment %s", response.status_code, payment_id)
        raise HTTPException(
            status_code=502,
            detail=f"Erreur Mollie API : {response.status_code}",
        )

    return response.json()


# ---------------------------------------------------------------------------
# Vérification de signature HMAC (optionnelle)
# ---------------------------------------------------------------------------

def _verifier_signature(body: bytes, signature: str | None) -> None:
    """
    Vérifie la signature HMAC-SHA256 si MOLLIE_WEBHOOK_SECRET est défini.
    Si la variable n'est pas présente, la vérification est sautée (dev local).
    """
    secret = os.environ.get("MOLLIE_WEBHOOK_SECRET", "")
    if not secret:
        return

    if not signature:
        raise HTTPException(status_code=401, detail="Signature manquante.")

    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Signature invalide.")
