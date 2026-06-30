"""
Logique métier pure de réservation — aucune I/O réseau.
Toutes les fonctions reçoivent des objets Python et retournent un résultat
ou lèvent BookingError. Tester ici sans aucun mock réseau.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Domaine
# ---------------------------------------------------------------------------

class TypeCours(str, Enum):
    AERIEN = "AERIEN"
    VINYASA = "VINYASA"
    ASHTANGA = "ASHTANGA"
    PRENATAL = "PRENATAL"
    POSTNATAL = "POSTNATAL"
    AYURVEDA = "AYURVEDA"
    FACIAL = "FACIAL"
    PERSO = "PERSO"
    # Types combinés : un crédit valable pour plusieurs types de cours
    ASHTANGA_VINYASA = "ASHTANGA_VINYASA"
    AERIEN_ASHTANGA_VINYASA = "AERIEN_ASHTANGA_VINYASA"
    AERIEN_ETE = "AERIEN_ETE"
    ASHTANGA_VINYASA_ETE = "ASHTANGA_VINYASA_ETE"

# Quels types de cours un crédit combiné peut couvrir
_TYPES_COMPATIBLES: dict[TypeCours, frozenset[TypeCours]] = {
    TypeCours.ASHTANGA_VINYASA: frozenset({TypeCours.ASHTANGA, TypeCours.VINYASA}),
    TypeCours.AERIEN_ASHTANGA_VINYASA: frozenset({TypeCours.AERIEN, TypeCours.ASHTANGA, TypeCours.VINYASA}),
}


def credit_compatible_avec_creneau(credit_type: TypeCours, creneau_type: TypeCours) -> bool:
    """Un crédit est compatible si son type correspond exactement ou le couvre (type combiné)."""
    if credit_type == creneau_type:
        return True
    return creneau_type in _TYPES_COMPATIBLES.get(credit_type, frozenset())


class Formule(str, Enum):
    ESSAI = "ESSAI"
    UNITE = "UNITE"
    STAGE = "STAGE"
    C3 = "C3"
    C5 = "C5"
    C10 = "C10"
    C20 = "C20"
    ABO = "ABO"


CREDITS_PAR_FORMULE: dict[Formule, int] = {
    Formule.ESSAI: 1,
    Formule.UNITE: 1,
    Formule.STAGE : 1,
    Formule.C3: 3,
    Formule.C5: 5,
    Formule.C10: 10,
    Formule.C20: 20,
    Formule.ABO: 0,  # pas de décompte
}

DUREE_VALIDITE_MOIS: dict[Formule, Optional[int]] = {
    Formule.ESSAI: None,   # consommé immédiatement, pas d'expiration utile
    Formule.UNITE: None,
    Formule.STAGE : None,
    Formule.C3: 1, #Validité en mois
    Formule.C5: 3,
    Formule.C10: 6,
    Formule.C20: 10,
    Formule.ABO: None,     # validité gérée par saison (sep → fin juin)
}

# Formules dont le crédit est débité à l'achat (non à la réservation)
DEBIT_A_LACHAT: frozenset[Formule] = frozenset({Formule.ESSAI, Formule.UNITE, Formule.STAGE})

# Formules jamais recrédités en cas d'annulation
JAMAIS_RECREDITE: frozenset[Formule] = frozenset({Formule.ESSAI, Formule.UNITE, Formule.STAGE})

DELAI_ANNULATION = timedelta(hours=24)


@dataclass(frozen=True)
class Credit:
    """Une ligne de l'onglet Crédits."""
    email: str
    type_cours: TypeCours
    formule: Formule
    credits_restants: int
    date_expiration: Optional[date]  # None pour ABO/ESSAI/UNITE
    statut: str  # "actif" | "expiré" | "épuisé"


@dataclass(frozen=True)
class Creneau:
    """Une ligne de l'onglet Créneaux."""
    id_creneau: str
    type_cours: TypeCours
    capacite: int
    places_prises: int
    jour_semaine: str = ""   # ex: "lundi"
    heure: str = ""          # ex: "09:00"
    lieu: str = ""           # ex: "Salle A"


@dataclass(frozen=True)
class Reservation:
    """Une ligne de l'onglet Réservations."""
    id_resa: str
    email_eleve: str
    id_creneau: str
    date_seance: datetime
    statut: str  # "confirmé" | "annulé_à_temps" | "annulé_tardif_signalé" | "effectué"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BookingError(Exception):
    """Erreur métier attendue — à transformer en réponse HTTP 400 dans les routes."""


# ---------------------------------------------------------------------------
# Parsing du nom produit Mollie
# ---------------------------------------------------------------------------

def parse_product_name(reference: str) -> tuple[TypeCours, Formule]:
    """
    Parse une référence produit Mollie du format TYPE_COURS_FORMULE.
    Lève BookingError si le format est invalide.
    """
    parts = reference.strip().upper().rsplit("_", 1)
    if len(parts) != 2:
        raise BookingError(f"Référence produit invalide : '{reference}' (attendu TYPE_COURS_FORMULE)")

    raw_cours, raw_formule = parts
    try:
        type_cours = TypeCours(raw_cours)
    except ValueError:
        raise BookingError(f"Type de cours inconnu : '{raw_cours}'")
    try:
        formule = Formule(raw_formule)
    except ValueError:
        raise BookingError(f"Formule inconnue : '{raw_formule}'")

    return type_cours, formule


# ---------------------------------------------------------------------------
# Calcul des crédits initiaux après achat
# ---------------------------------------------------------------------------

def credits_apres_achat(formule: Formule, date_achat: date) -> tuple[int, Optional[date]]:
    """
    Retourne (credits_initiaux, date_expiration) pour une formule achetée.
    ESSAI/UNITE : crédit débité immédiatement → 0 restant (déjà consommé).
    ABO : 0 crédits (pas de décompte), expiration fin juin de la saison courante.
    """
    if formule in DEBIT_A_LACHAT:
        return 0, None

    if formule == Formule.ABO:
        expiration = _fin_saison_abo(date_achat)
        return 0, expiration  # ABO n'utilise pas credits_restants

    duree = DUREE_VALIDITE_MOIS[formule]
    assert duree is not None
    expiration = _ajouter_mois(date_achat, duree)
    return CREDITS_PAR_FORMULE[formule], expiration


def _fin_saison_abo(reference: date) -> date:
    """Fin de saison ABO = 30 juin de l'année scolaire en cours (sep → juin)."""
    if reference.month >= 9:
        return date(reference.year + 1, 6, 30)
    return date(reference.year, 6, 30)


def _ajouter_mois(d: date, mois: int) -> date:
    """Ajoute un nombre de mois à une date (gestion fin de mois)."""
    month = d.month + mois
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(d.day, _dernjer_jour_du_mois(year, month))
    return date(year, month, day)


def _dernjer_jour_du_mois(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


# ---------------------------------------------------------------------------
# Validation d'une réservation
# ---------------------------------------------------------------------------

def valider_reservation(
    credit: Credit,
    creneau: Creneau,
    date_seance: datetime,
    maintenant: datetime,
    reservations_eleve: list[Reservation],
) -> None:
    """
    Vérifie qu'une réservation est possible. Lève BookingError si non.
    Ne fait aucune écriture — la persistance est à la charge de l'appelant.
    """
    # Cohérence type de cours (types combinés acceptés)
    if not credit_compatible_avec_creneau(credit.type_cours, creneau.type_cours):
        raise BookingError(
            f"Ce crédit est pour '{credit.type_cours}', pas pour '{creneau.type_cours}'"
        )

    # Créneau plein
    if creneau.places_prises >= creneau.capacite:
        raise BookingError("Ce créneau est complet.")

    # Validité de saison pour l'ABO (avant la vérification "passé" — une séance hors
    # saison est invalide même si elle est dans le futur ou le passé proche)
    if credit.formule == Formule.ABO:
        _valider_abo(credit, date_seance, maintenant, reservations_eleve)
        return

    # Séance dans le passé
    if date_seance <= maintenant:
        raise BookingError("Impossible de réserver une séance passée.")

    if credit.formule in DEBIT_A_LACHAT:
        _valider_essai_unite(credit, reservations_eleve)
    else:
        _valider_carte(credit, maintenant)


def _valider_abo(
    credit: Credit,
    date_seance: datetime,
    maintenant: datetime,
    reservations_eleve: list[Reservation],
) -> None:
    # Validité de la saison (avant le check "passé" pour un message d'erreur pertinent)
    fin_saison = _fin_saison_abo(maintenant.date())
    debut_saison = date(fin_saison.year - 1, 9, 1) if fin_saison.month == 6 else date(fin_saison.year, 9, 1)
    if not (debut_saison <= date_seance.date() <= fin_saison):
        raise BookingError("L'abonnement n'est pas valide pour cette date (hors saison sep–juin).")

    if date_seance <= maintenant:
        raise BookingError("Impossible de réserver une séance passée.")

    # Max 1 résa par semaine calendaire pour ce type de cours
    semaine_cible = date_seance.isocalendar()[:2]  # (year, week)
    reservations_actives = [
        r for r in reservations_eleve
        if r.statut == "confirmé"
        and r.id_creneau == credit.type_cours  # comparé plus bas via creneau
    ]
    # On compare la semaine ISO de la séance avec les réservations existantes
    # sur le même type de cours (l'appelant passe reservations_eleve filtrées
    # sur le type de cours de l'élève via credit.type_cours).
    conflit = any(
        r.date_seance.isocalendar()[:2] == semaine_cible
        for r in reservations_eleve
        if r.statut == "confirmé"
    )
    if conflit:
        raise BookingError(
            "Abonnement : une seule réservation par semaine calendaire et par type de cours."
        )


def _valider_essai_unite(credit: Credit, reservations_eleve: list[Reservation]) -> None:
    if credit.credits_restants <= 0:
        raise BookingError(f"Crédit '{credit.formule}' déjà utilisé.")
    # Vérifie qu'aucune réservation confirmée n'existe déjà pour ce crédit
    # (1 résa max, jamais recrédité). L'appelant doit passer les réservations
    # liées à ce crédit précis.
    deja_reserve = any(r.statut == "confirmé" for r in reservations_eleve)
    if deja_reserve:
        raise BookingError(f"Formule '{credit.formule}' : une seule réservation possible.")


def _valider_carte(credit: Credit, maintenant: datetime) -> None:
    if credit.statut != "actif":
        raise BookingError(f"Carte '{credit.formule}' non active (statut : {credit.statut}).")
    if credit.date_expiration and maintenant.date() > credit.date_expiration:
        raise BookingError(
            f"Carte expirée le {credit.date_expiration}."
        )
    if credit.credits_restants <= 0:
        raise BookingError(f"Plus de crédits disponibles sur cette carte '{credit.formule}'.")


# ---------------------------------------------------------------------------
# Annulation
# ---------------------------------------------------------------------------

def peut_annuler_en_self_service(date_seance: datetime, maintenant: datetime) -> bool:
    """True si l'annulation libre est encore possible (> 24h avant la séance)."""
    return maintenant < date_seance - DELAI_ANNULATION


def credits_a_restituer(formule: Formule) -> int:
    """Nombre de crédits à restituer lors d'une annulation à temps."""
    if formule in JAMAIS_RECREDITE:
        return 0
    if formule == Formule.ABO:
        return 0  # pas de décompte
    return 1

# TODO : Logique de l'annulation bancale
def valider_annulation(
    reservation: Reservation,
    maintenant: datetime,
) -> None:
    """
    Vérifie que l'annulation self-service est possible.
    Lève BookingError si hors délai ou déjà annulée.
    """
    if reservation.statut != "confirmé":
        raise BookingError(f"Cette réservation ne peut pas être annulée (statut : {reservation.statut}).")
    if not peut_annuler_en_self_service(reservation.date_seance, maintenant):
        raise BookingError(
            "Annulation impossible : délai de 24h dépassé. "
            "Utilisez le formulaire de signalement."
        )
