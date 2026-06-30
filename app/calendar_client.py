"""
Synchronisation Google Calendar — vue lecture seule pour la professeure.

Modèle daté : **un événement par séance réservée** (id_créneau + date précise),
mis à jour avec la liste des participants confirmés de cette séance. Quand une
séance n'a plus aucun participant, son événement est supprimé.

La professeure ne doit JAMAIS modifier Calendar manuellement : tout sera
écrasé à la prochaine synchronisation.

Variables d'environnement requises :
  GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_SERVICE_ACCOUNT_INFO  (même compte que Sheets)
  GOOGLE_CALENDAR_ID  – ID du calendrier à synchroniser
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from booking_logic import Creneau, Reservation
from sheets_client import SheetsClient

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Clé d'identification de NOS événements : extendedProperties.private.session
_PROP_KEY = "session"

_DUREE_SEANCE = timedelta(hours=1, minutes=30)


class CalendarClient:
    def __init__(self, calendar_id: str, credentials: Credentials) -> None:
        self._calendar_id = calendar_id
        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def sync_session(self, creneau: Creneau, date_seance: datetime, sheets: SheetsClient) -> None:
        """
        Met à jour (ou crée/supprime) l'événement Calendar d'une séance datée.
        - participants présents  → crée/met à jour l'événement à la date de la séance
        - plus aucun participant → supprime l'événement s'il existe
        """
        resas = self._get_resas_session(creneau.id_creneau, date_seance, sheets)
        participants = self._liste_participants(resas, sheets)
        key = self._session_key(creneau.id_creneau, date_seance)
        existing_id = self._find_event(key)

        if not participants:
            if existing_id:
                self._service.events().delete(
                    calendarId=self._calendar_id, eventId=existing_id
                ).execute()
                logger.info("Événement supprimé (séance vide) : %s", key)
            return

        body = self._build_event(creneau, date_seance, participants)
        if existing_id:
            self._service.events().update(
                calendarId=self._calendar_id, eventId=existing_id, body=body
            ).execute()
            logger.info("Événement mis à jour : %s", key)
        else:
            self._service.events().insert(
                calendarId=self._calendar_id, body=body
            ).execute()
            logger.info("Événement créé : %s", key)

    def sync_toutes_les_sessions(self, sheets: SheetsClient) -> None:
        """
        Reconstruit toutes les séances ayant au moins une réservation confirmée.
        Utile pour une resynchronisation manuelle complète.
        """
        creneaux = {c.id_creneau: c for c in sheets.get_creneaux()}
        ws = sheets._tab("Réservations")
        vues: set[tuple[str, str]] = set()
        for row in ws.get_all_records():
            if row["statut"] != "confirmé":
                continue
            idc = str(row["id_créneau"])
            date_str = str(row["date_séance"])
            if (idc, date_str) in vues:
                continue
            vues.add((idc, date_str))
            creneau = creneaux.get(idc)
            if not creneau:
                continue
            try:
                date_seance = datetime.strptime(date_str, "%Y-%m-%dT%H:%M")
                self.sync_session(creneau, date_seance, sheets)
            except Exception as exc:
                logger.error("Erreur sync séance %s %s : %s", idc, date_str, exc)

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------

    @staticmethod
    def _session_key(id_creneau: str, date_seance: datetime) -> str:
        return f"{id_creneau}|{date_seance.strftime('%Y-%m-%dT%H:%M')}"

    def _get_resas_session(
        self, id_creneau: str, date_seance: datetime, sheets: SheetsClient
    ) -> list[Reservation]:
        """Réservations confirmées pour CETTE séance précise (id + date)."""
        ws = sheets._tab("Réservations")
        date_str = date_seance.strftime("%Y-%m-%dT%H:%M")
        resas = []
        for row in ws.get_all_records():
            if (
                str(row["id_créneau"]) == id_creneau
                and str(row["date_séance"]) == date_str
                and row["statut"] == "confirmé"
            ):
                resas.append(Reservation(
                    id_resa=str(row["id_résa"]),
                    email_eleve=row["email_élève"],
                    id_creneau=str(row["id_créneau"]),
                    date_seance=datetime.strptime(str(row["date_séance"]), "%Y-%m-%dT%H:%M"),
                    statut=row["statut"],
                ))
        return resas

    def _liste_participants(self, resas: list[Reservation], sheets: SheetsClient) -> list[str]:
        """Liste 'Prénom NOM (email)' des participants de la séance."""
        participants = []
        for r in resas:
            eleve = sheets.get_eleve(r.email_eleve)
            nom = eleve.get("nom", r.email_eleve) if eleve else r.email_eleve
            participants.append(f"{nom} ({r.email_eleve})")
        return participants

    def _build_event(self, creneau: Creneau, date_seance: datetime, participants: list[str]) -> dict:
        """Corps d'un événement Calendar pour une séance datée."""
        start = date_seance
        end = start + _DUREE_SEANCE
        n = len(participants)

        lignes = [f"Cours : {creneau.type_cours.value}"]
        if creneau.lieu:
            lignes.append(f"Lieu : {creneau.lieu}")
        lignes += [
            f"Places : {n}/{creneau.capacite}",
            "",
            f"Participants ({n}) :",
        ] + participants

        return {
            "summary": f"{creneau.type_cours.value} ({n}/{creneau.capacite})",
            "description": "\n".join(lignes),
            "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Paris"},
            "end": {"dateTime": end.isoformat(), "timeZone": "Europe/Paris"},
            "extendedProperties": {
                "private": {_PROP_KEY: self._session_key(creneau.id_creneau, date_seance)}
            },
        }

    def _find_event(self, session_key: str) -> str | None:
        """Cherche l'événement d'une séance par sa clé (id_créneau|date)."""
        try:
            result = self._service.events().list(
                calendarId=self._calendar_id,
                privateExtendedProperty=f"{_PROP_KEY}={session_key}",
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
