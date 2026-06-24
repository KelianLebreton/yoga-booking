"""
Synchronisation Google Calendar — vue lecture seule pour la professeure.

Un événement récurrent par créneau, mis à jour avec la liste des participants
à chaque appel à sync_creneau() ou sync_tous_les_creneaux().

La professeure ne doit JAMAIS modifier Calendar manuellement : tout sera
écrasé à la prochaine synchronisation.

Variables d'environnement requises :
  GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_SERVICE_ACCOUNT_INFO  (même compte que Sheets)
  GOOGLE_CALENDAR_ID  – ID du calendrier à synchroniser (ex: abc123@group.calendar.google.com)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from booking_logic import Creneau, Reservation, TypeCours
from sheets_client import SheetsClient, get_sheets_client

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
]

# Champ utilisé pour retrouver nos événements : extendedProperties.private.id_creneau
_PROP_KEY = "id_creneau"

_JOURS_FR = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
    "vendredi": 4, "samedi": 5, "dimanche": 6,
}


def _prochaine_occurrence(jour_semaine: str, heure: str) -> datetime:
    """Retourne la prochaine occurrence (future) du jour et de l'heure donnés."""
    now = datetime.now()
    cible = _JOURS_FR.get(jour_semaine.lower(), 0)
    h, m = (int(x) for x in heure.split(":")) if ":" in heure else (9, 0)
    jours = (cible - now.weekday()) % 7
    if jours == 0 and now.hour >= h:
        jours = 7  # déjà passé aujourd'hui → semaine prochaine
    return now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=jours)


# ---------------------------------------------------------------------------
# Client Calendar
# ---------------------------------------------------------------------------

class CalendarClient:
    def __init__(self, calendar_id: str, credentials: Credentials) -> None:
        self._calendar_id = calendar_id
        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def sync_tous_les_creneaux(self, sheets: SheetsClient) -> None:
        """
        Synchronise tous les créneaux actifs depuis Sheets vers Calendar.
        À appeler manuellement ou via un cron après chaque réservation/annulation
        si on veut garder Calendar à jour en temps réel.
        """
        creneaux = sheets.get_creneaux()
        for creneau in creneaux:
            try:
                self.sync_creneau(creneau, sheets)
            except Exception as exc:
                logger.error("Erreur sync créneau %s : %s", creneau.id_creneau, exc)

    def sync_creneau(self, creneau: Creneau, sheets: SheetsClient) -> None:
        """
        Met à jour (ou crée) l'événement Calendar pour un créneau donné.
        La description de l'événement contient la liste des participants confirmés.
        """
        resas = self._get_resas_creneau(creneau.id_creneau, sheets)
        participants = self._liste_participants(resas, sheets)

        event_body = self._build_event(creneau, participants)
        existing_event_id = self._find_event(creneau.id_creneau)

        if existing_event_id:
            self._service.events().update(
                calendarId=self._calendar_id,
                eventId=existing_event_id,
                body=event_body,
            ).execute()
            logger.info("Événement mis à jour : créneau %s", creneau.id_creneau)
        else:
            self._service.events().insert(
                calendarId=self._calendar_id,
                body=event_body,
            ).execute()
            logger.info("Événement créé : créneau %s", creneau.id_creneau)

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------

    def _get_resas_creneau(self, id_creneau: str, sheets: SheetsClient) -> list[Reservation]:
        """Retourne toutes les réservations confirmées pour un créneau donné."""
        ws = sheets._tab("Réservations")
        rows = ws.get_all_records()
        resas = []
        for row in rows:
            if str(row["id_créneau"]) == id_creneau and row["statut"] == "confirmé":
                resas.append(Reservation(
                    id_resa=str(row["id_résa"]),
                    email_eleve=row["email_élève"],
                    id_creneau=str(row["id_créneau"]),
                    date_seance=datetime.strptime(str(row["date_séance"]), "%Y-%m-%dT%H:%M"),
                    statut=row["statut"],
                ))
        return resas

    def _liste_participants(self, resas: list[Reservation], sheets: SheetsClient) -> list[str]:
        """Retourne la liste 'Prénom NOM (email)' des participants."""
        participants = []
        for r in resas:
            eleve = sheets.get_eleve(r.email_eleve)
            if eleve:
                nom = eleve.get("nom", r.email_eleve)
                participants.append(f"{nom} ({r.email_eleve}) — {r.date_seance.strftime('%d/%m à %H:%M')}")
            else:
                participants.append(f"{r.email_eleve} — {r.date_seance.strftime('%d/%m à %H:%M')}")
        return participants

    def _build_event(self, creneau: Creneau, participants: list[str]) -> dict:
        """Construit le corps d'un événement Google Calendar."""
        start = _prochaine_occurrence(creneau.jour_semaine, creneau.heure)
        end = start + timedelta(hours=1, minutes=30)

        description_lines = [
            f"Cours : {creneau.type_cours.value}",
            f"Capacité : {creneau.places_prises}/{creneau.capacite} places",
            "",
            f"Participants ({len(participants)}) :",
        ] + (participants if participants else ["— Aucun participant —"])

        return {
            "summary": f"{creneau.type_cours.value} ({creneau.places_prises}/{creneau.capacite})",
            "description": "\n".join(description_lines),
            "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Paris"},
            "end":   {"dateTime": end.isoformat(),   "timeZone": "Europe/Paris"},
            "extendedProperties": {
                "private": {_PROP_KEY: creneau.id_creneau}
            },
        }

    def _find_event(self, id_creneau: str) -> str | None:
        """Cherche un événement existant par id_creneau dans les propriétés privées."""
        try:
            result = self._service.events().list(
                calendarId=self._calendar_id,
                privateExtendedProperty=f"{_PROP_KEY}={id_creneau}",
                maxResults=1,
                singleEvents=True,
            ).execute()
            items = result.get("items", [])
            return items[0]["id"] if items else None
        except HttpError as e:
            logger.error("Erreur recherche événement Calendar : %s", e)
            return None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_client: CalendarClient | None = None


def get_calendar_client() -> CalendarClient:
    global _client
    if _client is None:
        _client = _build_client()
    return _client


def _build_client() -> CalendarClient:
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]

    info_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_INFO")
    if info_env:
        info = json.loads(info_env)
        credentials = Credentials.from_service_account_info(info, scopes=_SCOPES)
    else:
        key_path = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
        credentials = Credentials.from_service_account_file(key_path, scopes=_SCOPES)

    return CalendarClient(calendar_id, credentials)
