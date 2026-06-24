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
from booking_logic import credits_apres_achat, parse_product_name
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

    _verifier_signature(body, x_mollie_signature)

    form = await request.form()
    payment_id: str = form.get("id", "")
    if not payment_id:
        raise HTTPException(status_code=400, detail="Champ 'id' manquant dans le webhook.")

    paiement = await _fetch_paiement(payment_id)

    if paiement["status"] != "paid":
        # Mollie envoie le webhook pour chaque transition de statut ;
        # on ignore les statuts intermédiaires (open, pending, failed…)
        return {"detail": "statut ignoré", "status": paiement["status"]}

    await _traiter_paiement(paiement)
    return {"detail": "ok"}


# ---------------------------------------------------------------------------
# Traitement d'un paiement confirmé
# ---------------------------------------------------------------------------

async def _traiter_paiement(paiement: dict) -> None:
    """
    Parse la référence produit, crée/met à jour l'élève, ajoute la ligne
    Crédits et envoie le lien espace élève.
    """
    # Métadonnées attendues dans le paiement Mollie
    metadata = paiement.get("metadata", {}) or {}
    email: str = (metadata.get("email") or "").strip().lower()
    nom: str = (metadata.get("nom") or "").strip()
    telephone: str = (metadata.get("telephone") or "").strip()

    if not email:
        logger.error("Paiement %s sans email dans metadata", paiement.get("id"))
        return

    # Référence produit : on la cherche dans les lignes de commande Mollie
    reference = _extraire_reference(paiement)
    if not reference:
        logger.error(
            "Paiement %s : impossible d'extraire la référence produit", paiement.get("id")
        )
        return

    try:
        type_cours, formule = parse_product_name(reference)
    except Exception as exc:
        logger.error("Paiement %s : référence invalide '%s' — %s", paiement.get("id"), reference, exc)
        return

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


def _extraire_reference(paiement: dict) -> str | None:
    """
    Cherche la référence produit dans les lignes de commande Mollie
    (champ `lines[].sku` ou `lines[].description` selon la configuration).

    L'intégration Webador doit placer la référence dans le SKU de chaque ligne.
    Si plusieurs lignes sont présentes (panier), on prend la première — les paniers
    multi-produits Mollie/Webador doivent être évités pour ce projet (un achat = un produit).
    """
    lines = (paiement.get("lines") or [])
    if lines:
        sku = (lines[0].get("sku") or "").strip()
        if sku:
            return sku
        # Fallback : description si le SKU n'est pas rempli
        return (lines[0].get("description") or "").strip() or None

    # Pour les paiements simples (sans order lines), on regarde la description
    return (paiement.get("description") or "").strip() or None


# ---------------------------------------------------------------------------
# Appel API Mollie
# ---------------------------------------------------------------------------

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
