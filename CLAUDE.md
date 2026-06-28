# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the FastAPI server (development)
uvicorn app.main:app --reload

# Run all tests
pytest tests/

# Run a single test file
pytest tests/test_booking_logic.py

# Run a single test by name
pytest tests/test_booking_logic.py::test_name -v
```

Environment variables are loaded from `.env` (copy from `.env.example`). Required keys: Google Sheets credentials, Google Calendar credentials, Mollie webhook secret, JWT secret, SMTP/Brevo credentials.

## Architecture

### Data flow

```
Mollie webhook → app/mollie_webhook.py → app/sheets_client.py (write credits/student)
                                       → app/email_client.py  (send permanent link)

Student browser → GET /espace?token=JWT → app/auth.py (verify) → sheets_client.py (read)
               → POST /espace/reserver  → booking_logic.py (validate) → sheets_client.py (write)
               → POST /espace/annuler   → booking_logic.py (validate) → sheets_client.py (write)
               → POST /espace/signaler  → sheets_client.py (write Signalements tab only)

Background/manual → calendar_client.py → Google Calendar (read-only view for teacher)
```

### Key architectural decisions

**Google Sheets is the single source of truth.** No database. `sheets_client.py` wraps all `gspread` calls. Every route reads directly from Sheets on each request — no caching, to keep slot availability real-time.

**`booking_logic.py` is pure Python with no I/O.** All business rules live here, receiving plain Python objects. This makes it fully unit-testable without mocking network calls. Routes call `booking_logic.py` first, then `sheets_client.py` to persist.

**Auth is a long-lived signed JWT in a URL query param** (`/espace?token=...`). No password, no session. The token identifies the student by email. Never log the token in plaintext.

**Google Calendar is derived, never authoritative.** `calendar_client.py` writes to Calendar from Sheets data. The teacher must never edit Calendar directly — it will be overwritten on next sync.

**Signalements (late cancellations) are never processed automatically.** The `/espace/signaler` route only writes to the `Signalements` sheet tab. No credit refund, no slot release — the teacher handles these manually.

## Business rules summary

These rules live in `booking_logic.py`:

| Formule | Credit deducted at | Refunded on cancel |
|---|---|---|
| `ESSAI` / `UNITE` | purchase | never |
| `C5` / `C10` / `C20` | booking | yes, if > 24h before class |
| `ABO` | never (unlimited) | n/a |

`ABO` constraint: max 1 booking per calendar week per course type. Valid September → end of June only.

Cancellation window: if `now >= session_datetime - 24h`, the cancel button is disabled; only the `signaler` form is available.

## Product naming convention (Mollie/Webador)

Product reference must be `[TYPE_COURS]_[FORMULE]` — this is parsed by `mollie_webhook.py`.

- `TYPE_COURS`: `AERIEN`, `VINYASA`, `ASHTANGA`, `PRENATAL`, `AYURVEDA`, `FACIAL`, and combined types `ASHTANGA_VINYASA`, `AERIEN_ASHTANGA_VINYASA`, `AERIEN_ETE`
- `FORMULE`: `ESSAI`, `UNITE`, `C5`, `C10`, `C20`, `ABO`

Combined credit types cover multiple course types: `ASHTANGA_VINYASA` covers Ashtanga + Vinyasa; `AERIEN_ASHTANGA_VINYASA` covers all three. Compatibility logic is in `booking_logic.py::credit_compatible_avec_creneau`.

The display name on the website can be anything; only the SKU/reference field matters.

## Hard constraint: no health data

Health information (pregnancy, injuries, medical certificates, minors) must **never** appear in any field, form, email template, or log. Students communicate this directly to the teacher outside the system. Do not add any such field anywhere.

## Data model (Google Sheets tabs)

- **Élèves**: `email | nom | téléphone | contact_urgence`
- **Crédits**: `email | type_cours | formule | credits_restants | date_expiration | statut` (one row per purchased formula; a student can have multiple rows)
- **Créneaux**: `id_créneau | type_cours | jour_semaine | heure | lieu | capacité` — un créneau est un modèle récurrent (ex: lundi 9h). Le nombre de places prises **n'est pas stocké ici** : il est dérivé par séance datée en comptant les `Réservations` confirmées (`sheets_client.count_reservations_date`). L'ancienne colonne `places_prises` est obsolète.
- **Réservations**: `id_résa | email_élève | id_créneau | date_séance | date_résa | statut` (statuts: `confirmé`, `annulé_à_temps`, `annulé_tardif_signalé`, `effectué`)
- **Signalements**: `email | id_créneau | horodatage | motif`

## Build order

1. `booking_logic.py` + `tests/test_booking_logic.py` — pure business logic first
2. `sheets_client.py` — persistence layer (test against a dev Sheet)
3. `mollie_webhook.py` — payment webhook parsing
4. Student space routes + HTML templates (`/espace`, `/espace/reserver`, etc.)
5. `calendar_client.py` — teacher calendar sync, last (depends on everything above)
