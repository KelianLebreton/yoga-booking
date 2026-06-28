"""Tests unitaires de booking_logic.py — aucune I/O."""

from datetime import date, datetime, timedelta

import pytest

from app.booking_logic import (
    DELAI_ANNULATION,
    BookingError,
    Credit,
    Creneau,
    Formule,
    Reservation,
    TypeCours,
    _ajouter_mois,
    _fin_saison_abo,
    creneau_datetime,
    credits_a_restituer,
    credits_apres_achat,
    parse_product_name,
    peut_annuler_en_self_service,
    valider_annulation,
    valider_reservation,
)

# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------

NOW = datetime(2025, 10, 15, 10, 0)
SEANCE_FUTURE = NOW + timedelta(hours=48)
SEANCE_DEMAIN = NOW + timedelta(hours=25)  # juste dans les délais
SEANCE_DANS_23H = NOW + timedelta(hours=23)  # hors délai


def make_credit(
    formule: Formule = Formule.C10,
    type_cours: TypeCours = TypeCours.VINYASA,
    credits_restants: int = 5,
    date_expiration: date | None = date(2026, 6, 30),
    statut: str = "actif",
) -> Credit:
    return Credit(
        email="eleve@test.com",
        type_cours=type_cours,
        formule=formule,
        credits_restants=credits_restants,
        date_expiration=date_expiration,
        statut=statut,
    )


def make_creneau(
    type_cours: TypeCours = TypeCours.VINYASA,
    capacite: int = 15,
    places_prises: int = 0,
    date: str = "",
    heure: str = "",
) -> Creneau:
    return Creneau(
        id_creneau="cr-001",
        type_cours=type_cours,
        capacite=capacite,
        places_prises=places_prises,
        date=date,
        heure=heure,
    )


def make_reservation(
    statut: str = "confirmé",
    date_seance: datetime = SEANCE_FUTURE,
    id_creneau: str = "cr-001",
) -> Reservation:
    return Reservation(
        id_resa="r-001",
        email_eleve="eleve@test.com",
        id_creneau=id_creneau,
        date_seance=date_seance,
        statut=statut,
    )


# ---------------------------------------------------------------------------
# parse_product_name
# ---------------------------------------------------------------------------

class TestParseProductName:
    def test_cas_nominal(self):
        assert parse_product_name("VINYASA_C10") == (TypeCours.VINYASA, Formule.C10)

    def test_casse_insensible(self):
        assert parse_product_name("aerien_abo") == (TypeCours.AERIEN, Formule.ABO)

    def test_tous_types_cours(self):
        for t in TypeCours:
            tc, _ = parse_product_name(f"{t.value}_ESSAI")
            assert tc == t

    def test_toutes_formules(self):
        for f in Formule:
            _, fm = parse_product_name(f"VINYASA_{f.value}")
            assert fm == f

    def test_format_invalide_trop_de_parties(self):
        with pytest.raises(BookingError):
            parse_product_name("VINYASA_C10_EXTRA")

    def test_format_invalide_pas_assez(self):
        with pytest.raises(BookingError):
            parse_product_name("VINYASA")

    def test_type_cours_inconnu(self):
        with pytest.raises(BookingError, match="Type de cours inconnu"):
            parse_product_name("PILATES_C10")

    def test_formule_inconnue(self):
        with pytest.raises(BookingError, match="Formule inconnue"):
            parse_product_name("VINYASA_C50")


# ---------------------------------------------------------------------------
# credits_apres_achat
# ---------------------------------------------------------------------------

class TestCreditsApresAchat:
    def test_essai_un_credit(self):
        # ESSAI = une séance à réserver, sans expiration
        credits, exp = credits_apres_achat(Formule.ESSAI, date(2025, 10, 1))
        assert credits == 1
        assert exp is None

    def test_unite_un_credit(self):
        credits, exp = credits_apres_achat(Formule.UNITE, date(2025, 10, 1))
        assert credits == 1

    def test_c5_credits_et_expiration_3_mois(self):
        credits, exp = credits_apres_achat(Formule.C5, date(2025, 10, 1))
        assert credits == 5
        assert exp == date(2026, 1, 1)

    def test_c10_expiration_6_mois(self):
        _, exp = credits_apres_achat(Formule.C10, date(2025, 10, 1))
        assert exp == date(2026, 4, 1)

    def test_c20_expiration_12_mois(self):
        _, exp = credits_apres_achat(Formule.C20, date(2025, 10, 1))
        assert exp == date(2026, 10, 1)

    def test_abo_zero_credits(self):
        credits, exp = credits_apres_achat(Formule.ABO, date(2025, 10, 1))
        assert credits == 0
        assert exp == date(2026, 6, 30)

    def test_abo_achat_avant_septembre(self):
        # Achat en mars 2025 → fin de saison juin 2025
        _, exp = credits_apres_achat(Formule.ABO, date(2025, 3, 1))
        assert exp == date(2025, 6, 30)

    def test_abo_achat_en_septembre(self):
        # Achat en sept 2025 → fin de saison juin 2026
        _, exp = credits_apres_achat(Formule.ABO, date(2025, 9, 1))
        assert exp == date(2026, 6, 30)

    def test_expiration_fin_de_mois(self):
        # 31 jan + 1 mois → 28 fév (ou 29 en bissextile)
        _, exp = credits_apres_achat(Formule.C5, date(2025, 1, 31))
        assert exp == date(2025, 4, 30)


# ---------------------------------------------------------------------------
# valider_reservation — cartes (C5/C10/C20)
# ---------------------------------------------------------------------------

class TestValiderReservationCarte:
    def test_reservation_valide(self):
        valider_reservation(make_credit(), make_creneau(), SEANCE_FUTURE, NOW, [])

    def test_creneau_plein(self):
        with pytest.raises(BookingError, match="complet"):
            valider_reservation(
                make_credit(), make_creneau(capacite=10, places_prises=10),
                SEANCE_FUTURE, NOW, []
            )

    def test_seance_dans_le_passe(self):
        with pytest.raises(BookingError, match="passée"):
            valider_reservation(
                make_credit(), make_creneau(),
                NOW - timedelta(hours=1), NOW, []
            )

    def test_type_cours_incompatible(self):
        credit = make_credit(type_cours=TypeCours.AERIEN)
        creneau = make_creneau(type_cours=TypeCours.VINYASA)
        with pytest.raises(BookingError, match="AERIEN"):
            valider_reservation(credit, creneau, SEANCE_FUTURE, NOW, [])

    def test_carte_expiree(self):
        credit = make_credit(date_expiration=date(2020, 1, 1))
        with pytest.raises(BookingError, match="expirée"):
            valider_reservation(credit, make_creneau(), SEANCE_FUTURE, NOW, [])

    def test_plus_de_credits(self):
        credit = make_credit(credits_restants=0)
        with pytest.raises(BookingError, match="crédits"):
            valider_reservation(credit, make_creneau(), SEANCE_FUTURE, NOW, [])

    def test_carte_non_active(self):
        credit = make_credit(statut="épuisé")
        with pytest.raises(BookingError, match="non active"):
            valider_reservation(credit, make_creneau(), SEANCE_FUTURE, NOW, [])


# ---------------------------------------------------------------------------
# valider_reservation — ESSAI / UNITE
# ---------------------------------------------------------------------------

class TestValiderReservationEssaiUnite:
    def test_essai_credit_disponible(self):
        credit = make_credit(formule=Formule.ESSAI, credits_restants=1)
        valider_reservation(credit, make_creneau(), SEANCE_FUTURE, NOW, [])

    def test_essai_credit_epuise(self):
        credit = make_credit(formule=Formule.ESSAI, credits_restants=0)
        with pytest.raises(BookingError, match="déjà utilisé"):
            valider_reservation(credit, make_creneau(), SEANCE_FUTURE, NOW, [])

    def test_essai_autre_reservation_ne_bloque_pas(self):
        """Une réservation faite avec une autre formule ne bloque pas l'essai
        (l'unicité est garantie par credits_restants, pas par les réservations)."""
        credit = make_credit(formule=Formule.ESSAI, credits_restants=1)
        valider_reservation(
            credit, make_creneau(), SEANCE_FUTURE, NOW, [make_reservation()]
        )

    def test_unite_autre_reservation_ne_bloque_pas(self):
        credit = make_credit(formule=Formule.UNITE, credits_restants=1)
        valider_reservation(
            credit, make_creneau(), SEANCE_FUTURE, NOW, [make_reservation()]
        )

    def test_essai_credit_epuise_bloque(self):
        """Après usage, credits_restants=0 → l'essai ne peut plus être réservé."""
        credit = make_credit(formule=Formule.ESSAI, credits_restants=0)
        with pytest.raises(BookingError, match="déjà utilisé"):
            valider_reservation(
                credit, make_creneau(), SEANCE_FUTURE, NOW, [make_reservation()]
            )


# ---------------------------------------------------------------------------
# valider_reservation — ABO
# ---------------------------------------------------------------------------

class TestValiderReservationAbo:
    def _credit_abo(self) -> Credit:
        return make_credit(formule=Formule.ABO, credits_restants=0, date_expiration=None)

    def test_abo_valide(self):
        seance = datetime(2025, 10, 20, 9, 0)
        valider_reservation(self._credit_abo(), make_creneau(), seance, NOW, [])

    def test_abo_hors_saison_ete(self):
        seance = datetime(2026, 7, 15, 9, 0)  # futur mais hors saison
        with pytest.raises(BookingError, match="saison"):
            valider_reservation(self._credit_abo(), make_creneau(), seance, NOW, [])

    def test_abo_hors_saison_aout(self):
        seance = datetime(2026, 8, 1, 9, 0)  # futur mais hors saison
        with pytest.raises(BookingError, match="saison"):
            valider_reservation(self._credit_abo(), make_creneau(), seance, NOW, [])

    def test_abo_une_resa_semaine_meme_semaine_bloque(self):
        # NOW = 15 oct 2025 (semaine 42)
        seance_existante = datetime(2025, 10, 14, 9, 0)   # semaine 42 aussi
        seance_nouvelle = datetime(2025, 10, 16, 11, 0)   # semaine 42
        resa = make_reservation(date_seance=seance_existante)
        with pytest.raises(BookingError, match="une seule"):
            valider_reservation(
                self._credit_abo(), make_creneau(), seance_nouvelle, NOW, [resa]
            )

    def test_abo_semaine_differente_ok(self):
        seance_existante = datetime(2025, 10, 13, 9, 0)   # semaine 42
        seance_nouvelle = datetime(2025, 10, 20, 11, 0)   # semaine 43
        resa = make_reservation(date_seance=seance_existante)
        valider_reservation(self._credit_abo(), make_creneau(), seance_nouvelle, NOW, [resa])

    def test_abo_resa_annulee_ne_bloque_pas(self):
        seance_existante = datetime(2025, 10, 14, 9, 0)
        seance_nouvelle = datetime(2025, 10, 16, 11, 0)
        resa = make_reservation(date_seance=seance_existante, statut="annulé_à_temps")
        valider_reservation(self._credit_abo(), make_creneau(), seance_nouvelle, NOW, [resa])


# ---------------------------------------------------------------------------
# Annulation
# ---------------------------------------------------------------------------

class TestAnnulation:
    def test_peut_annuler_plus_de_24h(self):
        assert peut_annuler_en_self_service(SEANCE_DEMAIN, NOW) is True

    def test_ne_peut_pas_annuler_moins_de_24h(self):
        assert peut_annuler_en_self_service(SEANCE_DANS_23H, NOW) is False

    def test_limite_exacte_24h_refusee(self):
        # Exactement 24h → hors délai (condition stricte)
        seance = NOW + DELAI_ANNULATION
        assert peut_annuler_en_self_service(seance, NOW) is False

    def test_valider_annulation_ok(self):
        resa = make_reservation(statut="confirmé", date_seance=SEANCE_DEMAIN)
        valider_annulation(resa, NOW)  # ne lève pas

    def test_valider_annulation_hors_delai(self):
        resa = make_reservation(statut="confirmé", date_seance=SEANCE_DANS_23H)
        with pytest.raises(BookingError, match="signalement"):
            valider_annulation(resa, NOW)

    def test_valider_annulation_deja_annulee(self):
        resa = make_reservation(statut="annulé_à_temps")
        with pytest.raises(BookingError, match="statut"):
            valider_annulation(resa, NOW)


# ---------------------------------------------------------------------------
# credits_a_restituer
# ---------------------------------------------------------------------------

class TestCreditsARestituer:
    def test_carte_restitue_1(self):
        assert credits_a_restituer(Formule.C5) == 1
        assert credits_a_restituer(Formule.C10) == 1
        assert credits_a_restituer(Formule.C20) == 1

    def test_essai_jamais_restitue(self):
        assert credits_a_restituer(Formule.ESSAI) == 0

    def test_unite_jamais_restitue(self):
        assert credits_a_restituer(Formule.UNITE) == 0

    def test_abo_zero(self):
        assert credits_a_restituer(Formule.ABO) == 0


# ---------------------------------------------------------------------------
# creneau_datetime
# ---------------------------------------------------------------------------

class TestCreneauDatetime:
    def test_format_francais(self):
        c = make_creneau(date="17/10/2025", heure="10:00:00")
        assert creneau_datetime(c) == datetime(2025, 10, 17, 10, 0)

    def test_heure_sans_secondes(self):
        c = make_creneau(date="17/10/2025", heure="10:00")
        assert creneau_datetime(c) == datetime(2025, 10, 17, 10, 0)

    def test_format_iso(self):
        c = make_creneau(date="2025-10-17", heure="10:00:00")
        assert creneau_datetime(c) == datetime(2025, 10, 17, 10, 0)

    def test_sans_date_retourne_none(self):
        assert creneau_datetime(make_creneau()) is None

    def test_date_illisible_retourne_none(self):
        assert creneau_datetime(make_creneau(date="pas une date", heure="10:00")) is None


# ---------------------------------------------------------------------------
# valider_reservation — garde-fou date programmée (séance existe dans Créneaux)
# ---------------------------------------------------------------------------

class TestValiderReservationDateProgrammee:
    def test_date_correspond_ok(self):
        # SEANCE_FUTURE = 17/10/2025 10:00
        creneau = make_creneau(date="17/10/2025", heure="10:00:00")
        valider_reservation(make_credit(), creneau, SEANCE_FUTURE, NOW, [])

    def test_date_ne_correspond_pas(self):
        # Le créneau est programmé le 17/10 mais on tente de réserver un autre jour
        creneau = make_creneau(date="17/10/2025", heure="10:00:00")
        mauvaise_date = datetime(2025, 10, 18, 10, 0)
        with pytest.raises(BookingError, match="n'existe pas"):
            valider_reservation(make_credit(), creneau, mauvaise_date, NOW, [])

    def test_heure_ne_correspond_pas(self):
        creneau = make_creneau(date="17/10/2025", heure="10:00:00")
        mauvaise_heure = datetime(2025, 10, 17, 18, 0)
        with pytest.raises(BookingError, match="n'existe pas"):
            valider_reservation(make_credit(), creneau, mauvaise_heure, NOW, [])

    def test_creneau_sans_date_pas_de_controle(self):
        # Créneau récurrent (date vide) : le garde-fou est ignoré, résa valide
        valider_reservation(make_credit(), make_creneau(), SEANCE_FUTURE, NOW, [])
