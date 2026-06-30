# Yoga Booking

Application de réservation pour un studio de yoga : espace élève en ligne, crédit
automatique des achats faits sur la boutique Webador (via Mollie), réservation de
séances datées, et synchronisation Google Calendar pour la professeure.

**Pas de base de données** : un classeur **Google Sheets** est l'unique source de
vérité.

---

## Fonctionnalités

- **Espace élève** par lien permanent (JWT dans l'URL, sans mot de passe).
- **Crédit automatique des achats** : un cron interroge l'API Mollie et crédite les
  paiements, puis envoie à l'élève son lien d'accès par email.
- **Réservation** d'une séance : choix formule → type de cours → date → créneau, avec
  places restantes en temps réel.
- **Annulation** self-service jusqu'à 24 h avant (restitution du crédit), sinon
  signalement manuel.
- **Google Calendar** : un événement par séance réservée, avec la liste des
  participants, mis à jour en temps réel.

## Stack technique

- **Python 3** / **FastAPI** (serveur web)
- **Google Sheets** via `gspread` (données)
- **Google Calendar API** (vue professeure)
- **Mollie API** (paiements, en *polling* — voir ci-dessous)
- **Brevo API HTTP** (emails transactionnels)
- Hébergement : **Render**

---

## Comment fonctionnent les paiements (important)

La boutique **Webador** crée des commandes **Mollie**, mais **n'envoie aucun webhook**
exploitable vers cette application. On ne peut donc pas être « notifié » d'un paiement.

À la place, l'application **interroge l'API Mollie périodiquement** (mécanisme de
*réconciliation*) :

1. Un service de cron externe (ex. [cron-job.org](https://cron-job.org)) appelle toutes
   les ~5 minutes :
   `GET /tasks/reconcile-mollie?key=ADMIN_KEY`
2. L'app lit les derniers paiements Mollie, et pour chaque paiement **payé non encore
   traité** : récupère la commande (email + produit), crédite l'élève dans Google Sheets,
   puis lui envoie son lien d'accès par email.
3. Chaque paiement traité est enregistré dans l'onglet **`PaiementsTraites`** pour ne
   **jamais** être crédité deux fois.

> Le produit est identifié par **son nom d'affichage** (Webador ne renseigne pas de
> SKU). La correspondance nom → (type de cours, formule) est dans le dictionnaire
> `_CATALOGUE` de `app/mollie_webhook.py`. Pour ajouter/modifier un produit, éditez ce
> dictionnaire.

---

## Installation (développement local)

```bash
# 1. Dépendances
pip install -r requirements.txt

# 2. Configuration
cp .env.example .env      # puis remplir les valeurs (voir ci-dessous)

# 3. Lancer le serveur (depuis le dossier app/)
cd app && uvicorn main:app --reload
```

L'app écoute sur http://localhost:8000. L'espace élève est sur `/espace?token=...`.

Générer un token de test :
```bash
cd app
python -c "from dotenv import load_dotenv; load_dotenv(); from auth import creer_token_eleve; print(creer_token_eleve('test@example.com'))"
```

## Variables d'environnement

Voir [`.env.example`](.env.example). Principales :

| Variable | Rôle |
|---|---|
| `SPREADSHEET_ID` | ID du classeur Google Sheets |
| `GOOGLE_SERVICE_ACCOUNT_JSON` *ou* `GOOGLE_SERVICE_ACCOUNT_INFO` | Compte de service Google (fichier OU contenu JSON) |
| `GOOGLE_CALENDAR_ID` | Calendrier à synchroniser |
| `MOLLIE_API_KEY` | Clé API Mollie (`live_...` ou `test_...`) |
| `BREVO_API_KEY` | Clé API Brevo (envoi des emails) |
| `EMAIL_FROM` / `EMAIL_FROM_NAME` | Expéditeur des emails (adresse vérifiée chez Brevo) |
| `JWT_SECRET` / `JWT_EXPIRE_DAYS` | Signature et durée des liens espace |
| `BASE_URL` | URL publique de l'app (utilisée dans les liens des emails) |
| `ADMIN_KEY` | Protège les endpoints `/tasks/...` (`?key=...`) |

> ⚠️ `BASE_URL` doit **exactement** correspondre à l'URL de déploiement, sinon les liens
> envoyés par email seront cassés.

Le compte de service Google doit avoir accès au classeur **et** au calendrier (partagé
avec son adresse email, droit « modifier les événements »).

## Tests

```bash
pytest tests/                       # toute la suite
pytest tests/test_booking_logic.py  # logique métier pure (sans réseau)
```

---

## Modèle de données (onglets Google Sheets)

- **Élèves** — `email | nom | téléphone | contact_urgence`
- **Crédits** — `email | type_cours | formule | credits_restants | date_expiration | statut`
- **Créneaux** — `id_créneau | type_cours | jour_semaine | date | heure | lieu | capacité`
  (une ligne par **séance datée** ; `id_créneau` se répète chaque semaine)
- **Réservations** — `id_résa | email_élève | id_créneau | date_séance | date_résa | statut`
- **Signalements** — `email | id_créneau | horodatage | motif`
- **PaiementsTraites** — `payment_id | order_id | email | produit | horodatage` (auto-créé)

## Règles métier

| Formule | Crédit débité à | Restitué si annulation > 24 h |
|---|---|---|
| `ESSAI` / `UNITE` / `STAGE` | l'achat (1 séance) | oui |
| `C5` / `C10` / `C20` | la réservation | oui |
| `ABO` | jamais (illimité) | n/a |

- `ABO` : max 1 réservation / semaine / type de cours ; valable septembre → fin juin.
- Annulation possible jusqu'à 24 h avant la séance ; au-delà, formulaire de signalement
  (traité manuellement par la professeure).

---

## Exploitation / maintenance

- **Automatisation des crédits** : créer un cronjob (cron-job.org) qui appelle
  `GET https://<votre-app>/tasks/reconcile-mollie?key=<ADMIN_KEY>` toutes les 5–10 min.
  Ne pas descendre sous 5 min (le serveur n'a qu'un worker).
- **Resynchroniser le calendrier** (rattrapage/réparation) : ouvrir une fois
  `GET https://<votre-app>/tasks/sync-calendar?key=<ADMIN_KEY>`.
- **Endpoints d'administration** sont protégés par `ADMIN_KEY` (paramètre `?key=`).

## Sécurité

- Ne jamais committer le vrai `.env` ni aucune clé. Le `.env.example` ne contient que des
  noms de variables.
- Les liens espace (tokens JWT) ne sont pas révocables individuellement : changer
  `JWT_SECRET` invalide **tous** les liens existants.
- **Aucune donnée de santé** ne doit être stockée ou transmise par le système (contrainte
  stricte du projet).
- Render bloque le SMTP sortant : les emails passent **uniquement** par l'API HTTP Brevo.
