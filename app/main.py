"""
Point d'entrée FastAPI.
Démarrage : uvicorn main:app --reload  (depuis le répertoire app/)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)
from dotenv import load_dotenv
load_dotenv()
from typing import Annotated

from dataclasses import replace

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import get_email_from_token, lien_espace, creer_token_eleve
from booking_logic import (
    BookingError,
    Formule,
    TypeCours,
    credit_compatible_avec_creneau,
    creneau_datetime,
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
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Dépendance réutilisable
EmailDep = Annotated[str, Depends(get_email_from_token)]


# ---------------------------------------------------------------------------
# Espace élève — dashboard
# ---------------------------------------------------------------------------

@app.get("/espace", response_class=HTMLResponse)
async def espace(request: Request, email: EmailDep):
    logger.warning("espace: début pour %s", email)
    sheets = get_sheets_client()
    eleve = sheets.get_eleve(email)
    logger.warning("espace: get_eleve OK")
    credits = sheets.get_credits_eleve(email)
    logger.warning("espace: get_credits OK (%d)", len(credits))
    reservations = sheets.get_reservations_eleve(email, statut="confirmé")
    logger.warning("espace: get_reservations OK (%d)", len(reservations))
    creneaux = sheets.get_creneaux()
    logger.warning("espace: get_creneaux OK (%d)", len(creneaux))
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

    # Séances disponibles, groupées par type de cours : futures et non complètes.
    # Chaque entrée porte sa date (YYYY-MM-DD) pour être filtrée côté client par
    # le jour choisi dans le calendrier ; le label n'affiche que heure + lieu + places.
    counts = sheets.count_reservations_par_session()
    logger.warning("espace: count_reservations_par_session OK")
    sessions_par_type: dict[str, list[dict]] = {}
    for c in creneaux:
        dt = creneau_datetime(c)
        if dt is None or dt <= maintenant:
            continue
        iso = dt.strftime("%Y-%m-%dT%H:%M")
        restantes = c.capacite - counts.get((c.id_creneau, iso), 0)
        if restantes <= 0:
            continue
        s = "s" if restantes > 1 else ""
        label = dt.strftime("%H:%M")
        if c.lieu:
            label += f" — {c.lieu}"
        label += f" ({restantes} place{s} restante{s})"
        sessions_par_type.setdefault(c.type_cours.value, []).append({
            "value": f"{c.id_creneau}|{iso}",
            "date": dt.strftime("%Y-%m-%d"),
            "heure": dt.strftime("%H:%M"),
            "label": label,
        })
    for liste in sessions_par_type.values():
        liste.sort(key=lambda x: (x["date"], x["heure"]))

    # Pour chaque crédit actif, les types de cours réservables (compatibilité +
    # disponibilité). Clé = index dans `credits` (= valeur du <select> Formule).
    types_par_credit: dict[int, list[str]] = {}
    for i, c in enumerate(credits):
        if c.statut != "actif":
            continue
        types_par_credit[i] = sorted(
            t for t in sessions_par_type
            if credit_compatible_avec_creneau(c.type_cours, TypeCours(t))
        )

    logger.warning("espace: sessions construites (%d types), rendu template", len(sessions_par_type))
    token = request.query_params.get("token", "")
    return templates.TemplateResponse(request, "espace.html", {
        "eleve": eleve,
        "nom": eleve["nom"],
        "email": email,
        "credits": credits,
        "resas": resas_enrichies,
        "sessions_par_type": json.dumps(sessions_par_type),
        "types_par_credit": json.dumps(types_par_credit),
        "today": maintenant.strftime("%Y-%m-%d"),
        "token": token,
    })


# ---------------------------------------------------------------------------
# Réserver
# ---------------------------------------------------------------------------

@app.post("/espace/reserver", response_class=HTMLResponse)
async def reserver(
    request: Request,
    email: EmailDep,
    session: str = Form(...),   # "id_creneau|YYYY-MM-DDTHH:MM"
    id_credit_index: int = Form(...),  # index dans la liste des crédits de l'élève
    token: str = Form(...),
):
    sheets = get_sheets_client()

    credits_eleve = sheets.get_credits_eleve(email)
    if id_credit_index < 0 or id_credit_index >= len(credits_eleve):
        raise HTTPException(status_code=400, detail="Crédit sélectionné invalide.")
    credit = credits_eleve[id_credit_index]

    id_creneau, _, date_seance = session.partition("|")
    if not id_creneau or not date_seance:
        return templates.TemplateResponse(request, "erreur.html", {
            "message": "Séance invalide. Merci de refaire votre sélection.",
            "token": token,
        }, status_code=400)

    try:
        date_seance_dt = datetime.fromisoformat(date_seance)
    except ValueError:
        return templates.TemplateResponse(request, "erreur.html", {
            "message": "Date invalide. Merci de refaire votre sélection.",
            "token": token,
        }, status_code=400)

    # Charge les créneaux une seule fois : sert à valider la séance ET à connaître
    # le type de cours de chaque réservation existante.
    creneaux = sheets.get_creneaux()

    # La séance doit correspondre à une ligne réellement programmée (id + date + heure)
    creneau = next(
        (c for c in creneaux
         if c.id_creneau == id_creneau and creneau_datetime(c) == date_seance_dt),
        None,
    )
    if not creneau:
        return templates.TemplateResponse(request, "erreur.html", {
            "message": "Ce créneau n'existe pas à cette date. Merci de refaire votre réservation.",
            "token": token,
        }, status_code=400)

    maintenant = datetime.now()

    # Réservations confirmées portant sur un type de cours couvert par ce crédit.
    # Utile pour la règle ABO (1 réservation/semaine/type) : on ne compare qu'avec
    # les réservations du même type. (ESSAI/UNITE s'appuient sur credits_restants.)
    types_par_id = {c.id_creneau: c.type_cours for c in creneaux}
    reservations_pertinentes = [
        r for r in sheets.get_reservations_eleve(email, statut="confirmé")
        if r.id_creneau in types_par_id
        and credit_compatible_avec_creneau(credit.type_cours, types_par_id[r.id_creneau])
    ]

    places_ce_jour = sheets.count_reservations_date(id_creneau, date_seance_dt)
    creneau_ce_jour = replace(creneau, places_prises=places_ce_jour)

    try:
        valider_reservation(credit, creneau_ce_jour, date_seance_dt, maintenant, reservations_pertinentes)
    except BookingError as e:
        return templates.TemplateResponse(request, "erreur.html", {
            "message": str(e),
            "token": token,
        }, status_code=400)

    # Persistance : réservation + débit crédit
    id_resa = sheets.add_reservation(email, id_creneau, date_seance_dt)
    try:
        get_calendar_client().sync_session(creneau, date_seance_dt, sheets)
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
    creneau_annule = sheets.get_creneau(reservation.id_creneau)
    try:
        if creneau_annule:
            get_calendar_client().sync_session(creneau_annule, reservation.date_seance, sheets)
    except Exception as e:
        logger.warning("Sync Calendar échoué (non bloquant) : %s", e)

    # Recréditer si la formule le permet. On cherche un crédit COMPATIBLE avec le
    # créneau (même logique qu'à la réservation : les types combinés couvrent
    # plusieurs cours), pas une égalité stricte de type.
    credits_eleve = sheets.get_credits_eleve(email)
    creneau = sheets.get_creneau(reservation.id_creneau)
    if creneau:
        for credit in credits_eleve:
            if (
                credit_compatible_avec_creneau(credit.type_cours, creneau.type_cours)
                and credits_a_restituer(credit.formule) > 0
            ):
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


# ---------------------------------------------------------------------------
# Resynchronisation complète du calendrier Google (manuel / cron)
# Reconstruit un événement par séance ayant une réservation confirmée.
# ?key=<ADMIN_KEY>
# ---------------------------------------------------------------------------

@app.get("/tasks/sync-calendar")
async def sync_calendar(request: Request):
    admin_key = os.environ.get("ADMIN_KEY", "")
    if not admin_key or request.query_params.get("key") != admin_key:
        raise HTTPException(status_code=403, detail="Clé invalide.")
    try:
        get_calendar_client().sync_toutes_les_sessions(get_sheets_client())
    except Exception as e:
        logger.error("Sync calendrier complète échouée : %s", e)
        raise HTTPException(status_code=500, detail=f"Erreur sync : {e}")
    return {"detail": "calendrier synchronisé"}
