"""
Point d'entrée FastAPI.
Démarrage : uvicorn main:app --reload  (depuis le répertoire app/)
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)
from dotenv import load_dotenv
load_dotenv()
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import get_email_from_token, lien_espace, creer_token_eleve
from booking_logic import (
    BookingError,
    Formule,
    credits_a_restituer,
    peut_annuler_en_self_service,
    valider_annulation,
    valider_reservation,
)
from email_client import envoyer_confirmation_reservation
from mollie_webhook import router as mollie_router
from sheets_client import get_sheets_client
from calendar_client import get_calendar_client

app = FastAPI(title="Yoga Booking")
app.include_router(mollie_router)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Dépendance réutilisable
EmailDep = Annotated[str, Depends(get_email_from_token)]


# ---------------------------------------------------------------------------
# Espace élève — dashboard
# ---------------------------------------------------------------------------

@app.get("/espace", response_class=HTMLResponse)
async def espace(request: Request, email: EmailDep):
    sheets = get_sheets_client()
    eleve = sheets.get_eleve(email)
    credits = sheets.get_credits_eleve(email)
    reservations = sheets.get_reservations_eleve(email, statut="confirmé")
    creneaux = sheets.get_creneaux()
    creneaux_par_id = {c.id_creneau: c for c in creneaux}

    maintenant = datetime.now()
    resas_enrichies = []
    for r in reservations:
        creneau = creneaux_par_id.get(r.id_creneau)
        resas_enrichies.append({
            "resa": r,
            "creneau": creneau,
            "peut_annuler": peut_annuler_en_self_service(r.date_seance, maintenant),
        })

    # Trier par date de séance croissante
    resas_enrichies.sort(key=lambda x: x["resa"].date_seance)

    token = request.query_params.get("token", "")
    return templates.TemplateResponse(request, "espace.html", {
        "eleve": eleve,
        "email": email,
        "credits": credits,
        "resas": resas_enrichies,
        "creneaux": creneaux,
        "maintenant": maintenant,
        "token": token,
    })


# ---------------------------------------------------------------------------
# Réserver
# ---------------------------------------------------------------------------

@app.post("/espace/reserver", response_class=HTMLResponse)
async def reserver(
    request: Request,
    email: EmailDep,
    id_creneau: str = Form(...),
    date_seance: str = Form(...),   # format ISO : "2025-10-20T09:00"
    id_credit_index: int = Form(...),  # index dans la liste des crédits de l'élève
    token: str = Form(...),
):
    sheets = get_sheets_client()

    credits_eleve = sheets.get_credits_eleve(email)
    if id_credit_index < 0 or id_credit_index >= len(credits_eleve):
        raise HTTPException(status_code=400, detail="Crédit sélectionné invalide.")
    credit = credits_eleve[id_credit_index]

    creneau = sheets.get_creneau(id_creneau)
    if not creneau:
        raise HTTPException(status_code=404, detail="Créneau introuvable.")

    try:
        date_seance_dt = datetime.fromisoformat(date_seance)
    except ValueError:
        raise HTTPException(status_code=400, detail="Format de date invalide.")

    maintenant = datetime.now()
    reservations_eleve = sheets.get_reservations_eleve(email, statut="confirmé")

    try:
        valider_reservation(credit, creneau, date_seance_dt, maintenant, reservations_eleve)
    except BookingError as e:
        return templates.TemplateResponse(request, "erreur.html", {
            "message": str(e),
            "token": token,
        }, status_code=400)

    # Persistance atomique : réservation + débit crédit + place
    id_resa = sheets.add_reservation(email, id_creneau, date_seance_dt)
    sheets.incrementer_places_prises(id_creneau)
    creneau_maj = sheets.get_creneau(id_creneau)  # relit pour avoir places_prises à jour
    try:
        get_calendar_client().sync_creneau(creneau_maj, sheets)
    except Exception as e:
        logger.warning("Sync Calendar échoué (non bloquant) : %s", e)

    if credit.formule not in (Formule.ABO,):
        sheets.decrementer_credit(email, credit.type_cours, credit.formule)

    # Email de confirmation (best-effort — pas de rollback si l'email échoue)
    eleve = sheets.get_eleve(email)
    nom = eleve["nom"] if eleve else ""
    try:
        await envoyer_confirmation_reservation(
            email=email,
            nom=nom,
            token=token,
            type_cours=credit.type_cours.value,
            date_seance=date_seance_dt.strftime("%A %d %B %Y à %H:%M"),
            lieu=creneau.type_cours.value,  # le sheets_client ne stocke pas lieu ici — voir note
        )
    except Exception:
        pass  # l'email est un bonus ; la réservation est déjà enregistrée

    return RedirectResponse(url=f"/espace?token={token}&ok=reserver", status_code=303)


# ---------------------------------------------------------------------------
# Annuler
# ---------------------------------------------------------------------------

@app.post("/espace/annuler", response_class=HTMLResponse)
async def annuler(
    request: Request,
    email: EmailDep,
    id_resa: str = Form(...),
    token: str = Form(...),
):
    sheets = get_sheets_client()

    reservation = sheets.get_reservation(id_resa)
    if not reservation or reservation.email_eleve.lower() != email.lower():
        raise HTTPException(status_code=404, detail="Réservation introuvable.")

    try:
        valider_annulation(reservation, datetime.now())
    except BookingError as e:
        return templates.TemplateResponse(request, "erreur.html", {
            "message": str(e),
            "token": token,
        }, status_code=400)

    sheets.update_statut_reservation(id_resa, "annulé_à_temps")
    sheets.decrementer_places_prises(reservation.id_creneau)
    creneau_maj = sheets.get_creneau(reservation.id_creneau)
    try:
        get_calendar_client().sync_creneau(creneau_maj, sheets)
    except Exception as e:
        logger.warning("Sync Calendar échoué (non bloquant) : %s", e)

    # Recréditer si la formule le permet
    credits_eleve = sheets.get_credits_eleve(email)
    creneau = sheets.get_creneau(reservation.id_creneau)
    if creneau:
        for credit in credits_eleve:
            if credit.type_cours == creneau.type_cours and credits_a_restituer(credit.formule) > 0:
                sheets.incrementer_credit(email, credit.type_cours, credit.formule)
                break

    return RedirectResponse(url=f"/espace?token={token}&ok=annuler", status_code=303)


# ---------------------------------------------------------------------------
# Signaler (annulation tardive)
# ---------------------------------------------------------------------------

@app.post("/espace/signaler", response_class=HTMLResponse)
async def signaler(
    request: Request,
    email: EmailDep,
    id_creneau: str = Form(...),
    motif: str = Form(...),
    token: str = Form(...),
):
    motif = motif.strip()
    if not motif:
        raise HTTPException(status_code=400, detail="Le motif ne peut pas être vide.")

    sheets = get_sheets_client()
    sheets.add_signalement(email=email, id_creneau=id_creneau, motif=motif)

    return RedirectResponse(url=f"/espace?token={token}&ok=signaler", status_code=303)
