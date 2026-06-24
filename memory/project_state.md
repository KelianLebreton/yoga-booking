---
name: project-state
description: État actuel du projet yoga-booking — ce qui est fait, ce qui reste
metadata:
  type: project
---

Système de réservation sur-mesure pour une professeure de yoga indépendante (100-200 élèves).
Stack : FastAPI + Google Sheets (source de vérité) + Google Calendar (vue prof) + Mollie (paiements) + Gmail SMTP.

**Tous les fichiers sont implémentés et fonctionnels :**
- `app/booking_logic.py` — logique métier pure, 45 tests passent
- `app/sheets_client.py` — CRUD gspread sur les 5 onglets
- `app/mollie_webhook.py` — parsing webhook + créditage élève
- `app/auth.py` — JWT longue durée dans URL
- `app/email_client.py` — Gmail SMTP (mot de passe d'application)
- `app/calendar_client.py` — sync Google Calendar
- `app/main.py` — routes FastAPI + templates Jinja2
- `app/templates/` — base.html, espace.html, erreur.html

**Testé en local et fonctionnel :** dashboard, réservation, annulation, Sheets.

**Ce qui reste :**
- Calendar : le calendrier principal Gmail refuse les écritures API → créer un calendrier secondaire dans le compte du client et mettre son ID dans `.env` (`GOOGLE_CALENDAR_ID`)
- Mollie : pas encore de clés API — le client ne les a pas encore. Brancher le webhook quand disponible.
- Déploiement : prévu sur Render ou Railway (free tier)

**Why:** Projet portfolio freelance du développeur + vrai outil pour le client.

**How to apply:** Quand on reprend, vérifier d'abord si Calendar et Mollie sont maintenant configurés avant de proposer de nouveaux développements.
