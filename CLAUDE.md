# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the FastAPI server (development) — depuis le répertoire app/
cd app && uvicorn main:app --reload

# Run all tests
pytest tests/

# Run a single test file / test
pytest tests/test_booking_logic.py
pytest tests/test_booking_logic.py::TestNom::test_name -v
```

Les variables d'environnement sont chargées depuis `.env` (copier `.env.example`). Voir la section **Environment variables** plus bas.

## Architecture

### Data flow

```
Achat boutique Webador → Mollie (Orders)
   ⚠️ Webador n'émet AUCUN webhook exploitable. On NE reçoit PAS d'événement.
   → Réconciliation par POLLING : un cron (cron-job.org, ~5 min) appelle
     GET /tasks/reconcile-mollie?key=ADMIN_KEY
        → mollie_webhook.py lit GET /v2/payments via l'API Mollie
        → pour chaque paiement `paid` non traité : récupère l'Order (email + produit)
        → sheets_client.py (crée élève + ligne Crédits ; marque PaiementsTraites)
        → email_client.py (envoie le lien espace via l'API HTTP Brevo)

Student browser → GET /espace?token=JWT → auth.py (verify) → sheets_client.py (read)
               → POST /espace/reserver  → booking_logic.py (validate) → sheets_client.py (write)
                                        → calendar_client.py sync_session (temps réel)
               → POST /espace/annuler   → booking_logic.py (validate) → sheets_client.py (write + restitue)
                                        → calendar_client.py sync_session (temps réel)
               → POST /espace/signaler  → sheets_client.py (write Signalements tab only)

Admin/cron → GET /tasks/reconcile-mollie?key=  → crédite les paiements payés
          → GET /tasks/sync-calendar?key=       → reconstruit tout le Google Calendar
```

### Key architectural decisions

**Pas de webhook Mollie — réconciliation par polling.** Webador gère Mollie et n'envoie aucun événement vers l'app. Le crédit des achats se fait donc en interrogeant l'API Mollie périodiquement (cron externe → `/tasks/reconcile-mollie`). La déduplication se fait via l'onglet `PaiementsTraites` (un paiement déjà traité n'est jamais recrédité). Voir `mollie_webhook.py::reconcile_mollie`.

**Identification produit par le NOM, pas le SKU.** Webador ne renseigne pas le champ SKU/référence des lignes de commande. Le produit est identifié via le **nom d'affichage** de `lines[0].name`, comparé au dictionnaire `_CATALOGUE` après normalisation (`_normaliser` : apostrophes typographiques `’`→`'`, espaces autour des `/`). Pour ajouter un produit, ajouter une entrée à `_CATALOGUE`.

**Google Sheets is the single source of truth.** No database. `sheets_client.py` wraps all `gspread` calls. Chaque route lit directement Sheets à chaque requête (pas de cache).

**`booking_logic.py` is pure Python with no I/O.** Toutes les règles métier y vivent, testables sans mock réseau. Les routes appellent `booking_logic.py` (validation) puis `sheets_client.py` (persistance).

**Auth = JWT signé longue durée dans l'URL** (`/espace?token=...`). Pas de mot de passe, pas de session. Le token identifie l'élève par email. Ne jamais logger le token en clair.

**Emails via l'API HTTP Brevo, pas SMTP.** Render bloque les ports SMTP sortants (25/465/587). `email_client.py` poste sur `https://api.brevo.com/v3/smtp/email` (HTTPS). Les variables `SMTP_*` sont obsolètes.

**Google Calendar est dérivé, jamais autoritaire.** `calendar_client.py` écrit **un événement par séance datée** (`id_créneau` + date), synchronisé en temps réel à chaque réservation/annulation (`sync_session`). La prof ne doit jamais éditer Calendar à la main. `/tasks/sync-calendar` reconstruit tout (filet de sécurité).

**Signalements (annulations tardives) jamais traités automatiquement.** `/espace/signaler` écrit seulement dans l'onglet `Signalements`. Aucun remboursement, aucune libération de place — géré à la main par la prof.

## Business rules summary

Ces règles vivent dans `booking_logic.py` :

| Formule | Crédit débité à | Restitué à l'annulation à temps (>24h) |
|---|---|---|
| `ESSAI` / `UNITE` / `STAGE` | l'achat (1 séance) | **oui** |
| `C5` / `C10` / `C20` | la réservation | **oui** |
| `ABO` | jamais (illimité) | n/a |

- `JAMAIS_RECREDITE` est **vide** actuellement : aucun produit n'est non-restituable. Le mécanisme reste en place pour pouvoir en ajouter.
- Contrainte `ABO` : max 1 réservation par semaine calendaire et par type de cours. Valide septembre → fin juin.
- Fenêtre d'annulation : si `now >= date_séance - 24h`, le bouton Annuler est désactivé ; seul le formulaire `signaler` est disponible.
- L'unicité de l'ESSAI/UNITE est garantie par `credits_restants` (1 à l'achat → 0 après réservation), pas par l'historique des réservations.

## Compatibilité des types de cours

`TYPE_COURS` : `AERIEN`, `VINYASA`, `ASHTANGA`, `PRENATAL`, `POSTNATAL`, `AYURVEDA`, `FACIAL`, `PERSO`, et types combinés `ASHTANGA_VINYASA`, `AERIEN_ASHTANGA_VINYASA`, `AERIEN_ETE`, `ASHTANGA_VINYASA_ETE`.

Un crédit combiné couvre plusieurs cours (ex. `ASHTANGA_VINYASA` couvre Ashtanga + Vinyasa). La compatibilité crédit/créneau passe **toujours** par `booking_logic.py::credit_compatible_avec_creneau` — à la réservation **comme** à la restitution (ne jamais comparer les types avec `==`).

## Hard constraint: no health data

Aucune information de santé (grossesse, blessures, certificats médicaux, mineurs) ne doit apparaître dans un champ, formulaire, email ou log. Les élèves communiquent ça directement à la prof, hors système. Ne jamais ajouter un tel champ.

## Data model (Google Sheets tabs)

- **Élèves** : `email | nom | téléphone | contact_urgence`
- **Crédits** : `email | type_cours | formule | credits_restants | date_expiration | statut` (une ligne par formule achetée ; un élève peut avoir plusieurs lignes)
- **Créneaux** : `id_créneau | type_cours | jour_semaine | date | heure | lieu | capacité` — **une ligne par séance datée** (ex. lundi 22/06/2026 09:00). `id_créneau` se **répète** d'une semaine à l'autre. Les places prises ne sont pas stockées : dérivées par séance via `count_reservations_date`.
- **Réservations** : `id_résa | email_élève | id_créneau | date_séance | date_résa | statut` (statuts : `confirmé`, `annulé_à_temps`, `annulé_tardif_signalé`, `effectué`)
- **Signalements** : `email | id_créneau | horodatage | motif`
- **PaiementsTraites** : `payment_id | order_id | email | produit | horodatage` (auto-créé ; déduplication de la réconciliation Mollie)

## Endpoints

- `GET /espace?token=` — dashboard élève (crédits, réservations, formulaire de réservation type→date→créneau)
- `POST /espace/reserver` · `POST /espace/annuler` · `POST /espace/signaler`
- `GET /tasks/reconcile-mollie?key=ADMIN_KEY` — crédite les paiements payés (`&dry_run=1` aperçu, `&payment_id=` cible un seul)
- `GET /tasks/sync-calendar?key=ADMIN_KEY` — reconstruit tout le Google Calendar
- `POST /webhook/mollie` — accuse réception du `hook.ping` Mollie (legacy, inutilisé en pratique)

## Environment variables

Voir `.env.example`. Clés : `SPREADSHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON` ou `GOOGLE_SERVICE_ACCOUNT_INFO`, `GOOGLE_CALENDAR_ID`, `MOLLIE_API_KEY`, `ADMIN_KEY`, `BREVO_API_KEY`, `EMAIL_FROM`, `EMAIL_FROM_NAME`, `JWT_SECRET`, `JWT_EXPIRE_DAYS`, `BASE_URL`. `BASE_URL` doit **exactement** correspondre à l'URL Render (sinon liens d'email cassés).

## Déploiement (Render)

- Lancement : `cd app && uvicorn main:app --host 0.0.0.0 --port $PORT`.
- Render **bloque le SMTP sortant** → emails via API Brevo uniquement.
- Plan gratuit : le service s'endort après ~15 min ; le cron de réconciliation le maintient éveillé.
- L'automatisation des crédits vient d'un **cron externe** (cron-job.org) appelant `/tasks/reconcile-mollie` — Webador ne déclenchant aucun webhook.
