"""
Couche de persistance Google Sheets (gspread).

Chaque méthode publique lit ou écrit un onglet précis du classeur.
Aucune logique métier ici — seules des opérations CRUD sur les données brutes.

Configuration attendue dans l'environnement :
  GOOGLE_SERVICE_ACCOUNT_JSON  – chemin vers le fichier de compte de service, OU
  GOOGLE_SERVICE_ACCOUNT_INFO  – contenu JSON en variable d'environnement (pour Render/Railway)
  SPREADSHEET_ID               – identifiant du classeur Google Sheets
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from booking_logic import (
    Credit,
    Creneau,
    Formule,
    Reservation,
    TypeCours,
)

# ---------------------------------------------------------------------------
# Scopes Google
# ---------------------------------------------------------------------------

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Noms des onglets (correspondent exactement aux noms dans le classeur)
_TAB_ELEVES = "Élèves"
_TAB_CREDITS = "Crédits"
_TAB_CRENEAUX = "Créneaux"
_TAB_RESERVATIONS = "Réservations"
_TAB_SIGNALEMENTS = "Signalements"

_DATE_FMT = "%Y-%m-%d"
_DATETIME_FMT = "%Y-%m-%dT%H:%M"


# ---------------------------------------------------------------------------
# Client principal
# ---------------------------------------------------------------------------

class SheetsClient:
    """
    Wraps toutes les opérations gspread.
    Instancier une seule fois au démarrage (via get_sheets_client()).
    """

    def __init__(self, spreadsheet_id: str, credentials: Credentials) -> None:
        gc = gspread.authorize(credentials)
        self._spreadsheet = gc.open_by_key(spreadsheet_id)

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------

    def _tab(self, name: str) -> gspread.Worksheet:
        return self._spreadsheet.worksheet(name)

    @staticmethod
    def _parse_date(s: str) -> Optional[date]:
        return datetime.strptime(s, _DATE_FMT).date() if s else None

    @staticmethod
    def _parse_datetime(s: str) -> datetime:
        return datetime.strptime(s, _DATETIME_FMT)

    @staticmethod
    def _fmt_date(d: Optional[date]) -> str:
        return d.strftime(_DATE_FMT) if d else ""

    @staticmethod
    def _fmt_datetime(dt: datetime) -> str:
        return dt.strftime(_DATETIME_FMT)

    # ------------------------------------------------------------------
    # Élèves
    # ------------------------------------------------------------------

    def get_eleve(self, email: str) -> Optional[dict]:
        """Retourne la ligne élève ou None si introuvable."""
        ws = self._tab(_TAB_ELEVES)
        rows = ws.get_all_records()
        for row in rows:
            if row["email"].strip().lower() == email.strip().lower():
                return row
        return None

    def upsert_eleve(self, email: str, nom: str, telephone: str, contact_urgence: str) -> None:
        """Crée l'élève s'il n'existe pas, sinon met à jour nom/téléphone/contact."""
        ws = self._tab(_TAB_ELEVES)
        records = ws.get_all_records()
        for i, row in enumerate(records, start=2):  # ligne 1 = en-tête
            if row["email"].strip().lower() == email.strip().lower():
                ws.update(f"A{i}:D{i}", [[email, nom, telephone, contact_urgence]])
                return
        ws.append_row([email, nom, telephone, contact_urgence])

    # ------------------------------------------------------------------
    # Crédits
    # ------------------------------------------------------------------

    def get_credits_eleve(self, email: str) -> list[Credit]:
        """Retourne toutes les lignes Crédits d'un élève (toutes formules)."""
        ws = self._tab(_TAB_CREDITS)
        rows = ws.get_all_records()
        credits: list[Credit] = []
        for row in rows:
            if row["email"].strip().lower() != email.strip().lower():
                continue
            credits.append(Credit(
                email=row["email"],
                type_cours=TypeCours(row["type_cours"]),
                formule=Formule(row["formule"]),
                credits_restants=int(row["credits_restants"]),
                date_expiration=self._parse_date(str(row["date_expiration"])),
                statut=row["statut"],
            ))
        return credits

    def add_credit(
        self,
        email: str,
        type_cours: TypeCours,
        formule: Formule,
        credits_restants: int,
        date_expiration: Optional[date],
        statut: str = "actif",
    ) -> None:
        """Ajoute une ligne dans l'onglet Crédits (après un achat Mollie)."""
        ws = self._tab(_TAB_CREDITS)
        ws.append_row([
            email,
            type_cours.value,
            formule.value,
            credits_restants,
            self._fmt_date(date_expiration),
            statut,
        ])

    def decrementer_credit(self, email: str, type_cours: TypeCours, formule: Formule) -> None:
        """Décrémente credits_restants de 1 sur la première ligne active correspondante."""
        ws = self._tab(_TAB_CREDITS)
        records = ws.get_all_records()
        for i, row in enumerate(records, start=2):
            if (
                row["email"].strip().lower() == email.strip().lower()
                and row["type_cours"] == type_cours.value
                and row["formule"] == formule.value
                and row["statut"] == "actif"
            ):
                new_val = int(row["credits_restants"]) - 1
                # Colonne D = credits_restants (index 4, lettre D)
                ws.update_cell(i, 4, new_val)
                if new_val <= 0:
                    ws.update_cell(i, 6, "épuisé")  # colonne F = statut
                return

    def incrementer_credit(self, email: str, type_cours: TypeCours, formule: Formule) -> None:
        """Recrédite de 1 (annulation à temps) sur la ligne correspondante."""
        ws = self._tab(_TAB_CREDITS)
        records = ws.get_all_records()
        for i, row in enumerate(records, start=2):
            if (
                row["email"].strip().lower() == email.strip().lower()
                and row["type_cours"] == type_cours.value
                and row["formule"] == formule.value
            ):
                new_val = int(row["credits_restants"]) + 1
                ws.update_cell(i, 4, new_val)
                ws.update_cell(i, 6, "actif")
                return

    # ------------------------------------------------------------------
    # Créneaux
    # ------------------------------------------------------------------

    def get_creneaux(self, type_cours: Optional[TypeCours] = None) -> list[Creneau]:
        """Retourne les créneaux, filtrés par type de cours si fourni."""
        ws = self._tab(_TAB_CRENEAUX)
        rows = ws.get_all_records()
        creneaux: list[Creneau] = []
        for row in rows:
            if type_cours and row["type_cours"] != type_cours.value:
                continue
            creneaux.append(Creneau(
                id_creneau=str(row["id_créneau"]),
                type_cours=TypeCours(row["type_cours"]),
                capacite=int(row["capacité"]),
                places_prises=int(row["places_prises"]),
                jour_semaine=str(row.get("jour_semaine", "")),
                heure=str(row.get("heure", "")),
                lieu=str(row.get("lieu", "")),
            ))
        return creneaux

    def get_creneau(self, id_creneau: str) -> Optional[Creneau]:
        """Retourne un créneau par son id, ou None."""
        for c in self.get_creneaux():
            if c.id_creneau == id_creneau:
                return c
        return None

    def incrementer_places_prises(self, id_creneau: str) -> None:
        """Incrémente places_prises de 1 pour le créneau donné."""
        self._update_places(id_creneau, delta=+1)

    def decrementer_places_prises(self, id_creneau: str) -> None:
        """Décrémente places_prises de 1 pour le créneau donné."""
        self._update_places(id_creneau, delta=-1)

    def _update_places(self, id_creneau: str, delta: int) -> None:
        ws = self._tab(_TAB_CRENEAUX)
        records = ws.get_all_records()
        for i, row in enumerate(records, start=2):
            if str(row["id_créneau"]) == id_creneau:
                new_val = int(row["places_prises"]) + delta
                # places_prises est la 7e colonne (G)
                ws.update_cell(i, 7, max(0, new_val))
                return

    # ------------------------------------------------------------------
    # Réservations
    # ------------------------------------------------------------------

    def get_reservations_eleve(
        self,
        email: str,
        statut: Optional[str] = None,
        type_cours: Optional[TypeCours] = None,
    ) -> list[Reservation]:
        """
        Retourne les réservations d'un élève.
        Filtres optionnels : statut exact et/ou type_cours (nécessite une jointure
        avec Créneaux — passez type_cours uniquement si vous avez déjà les créneaux
        en mémoire pour éviter un appel réseau supplémentaire).
        """
        ws = self._tab(_TAB_RESERVATIONS)
        rows = ws.get_all_records()
        resas: list[Reservation] = []
        for row in rows:
            if row["email_élève"].strip().lower() != email.strip().lower():
                continue
            if statut and row["statut"] != statut:
                continue
            resas.append(Reservation(
                id_resa=str(row["id_résa"]),
                email_eleve=row["email_élève"],
                id_creneau=str(row["id_créneau"]),
                date_seance=self._parse_datetime(str(row["date_séance"])),
                statut=row["statut"],
            ))
        return resas

    def get_reservation(self, id_resa: str) -> Optional[Reservation]:
        """Retourne une réservation par son id, ou None."""
        ws = self._tab(_TAB_RESERVATIONS)
        rows = ws.get_all_records()
        for row in rows:
            if str(row["id_résa"]) == id_resa:
                return Reservation(
                    id_resa=str(row["id_résa"]),
                    email_eleve=row["email_élève"],
                    id_creneau=str(row["id_créneau"]),
                    date_seance=self._parse_datetime(str(row["date_séance"])),
                    statut=row["statut"],
                )
        return None

    def add_reservation(
        self,
        email: str,
        id_creneau: str,
        date_seance: datetime,
    ) -> str:
        """Crée une réservation confirmée. Retourne l'id_resa généré."""
        ws = self._tab(_TAB_RESERVATIONS)
        id_resa = str(uuid.uuid4())[:8]
        ws.append_row([
            id_resa,
            email,
            id_creneau,
            self._fmt_datetime(date_seance),
            self._fmt_datetime(datetime.now()),
            "confirmé",
        ])
        return id_resa

    def update_statut_reservation(self, id_resa: str, nouveau_statut: str) -> None:
        """Met à jour le statut d'une réservation existante."""
        ws = self._tab(_TAB_RESERVATIONS)
        records = ws.get_all_records()
        for i, row in enumerate(records, start=2):
            if str(row["id_résa"]) == id_resa:
                # statut est la 6e colonne (F)
                ws.update_cell(i, 6, nouveau_statut)
                return

    # ------------------------------------------------------------------
    # Signalements
    # ------------------------------------------------------------------

    def add_signalement(
        self,
        email: str,
        id_creneau: str,
        motif: str,
    ) -> None:
        """Enregistre un signalement d'annulation tardive. Aucun autre effet."""
        ws = self._tab(_TAB_SIGNALEMENTS)
        ws.append_row([
            email,
            id_creneau,
            self._fmt_datetime(datetime.now()),
            motif,
        ])


# ---------------------------------------------------------------------------
# Singleton — à utiliser dans les routes FastAPI via Depends
# ---------------------------------------------------------------------------

_client: Optional[SheetsClient] = None


def get_sheets_client() -> SheetsClient:
    """
    Retourne l'instance unique de SheetsClient.
    Lit les credentials depuis l'environnement au premier appel.
    """
    global _client
    if _client is None:
        _client = _build_client()
    return _client


def _build_client() -> SheetsClient:
    spreadsheet_id = os.environ["SPREADSHEET_ID"]

    info_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_INFO")
    if info_env:
        info = json.loads(info_env)
        credentials = Credentials.from_service_account_info(info, scopes=_SCOPES)
    else:
        key_path = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
        credentials = Credentials.from_service_account_file(key_path, scopes=_SCOPES)

    return SheetsClient(spreadsheet_id, credentials)
