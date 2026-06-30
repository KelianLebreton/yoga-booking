"""
Envoi d'emails transactionnels via l'API HTTP de Brevo.

On utilise l'API REST (HTTPS, port 443) et NON le SMTP : les hébergeurs comme
Render bloquent les ports SMTP sortants (25/465/587). L'API HTTP passe.

Variables d'environnement requises :
  BREVO_API_KEY    – clé API v3 Brevo (Brevo → SMTP & API → API Keys)
  EMAIL_FROM       – adresse expéditrice vérifiée chez Brevo (ex: noreply@studio-yoga.fr)
  EMAIL_FROM_NAME  – nom affiché de l'expéditeur (optionnel, défaut "Studio Yoga")
  BASE_URL         – URL publique de l'app (pour construire le lien espace)
"""

from __future__ import annotations

import os

import httpx

from auth import lien_espace

_BREVO_URL = "https://api.brevo.com/v3/smtp/email"


async def _envoyer(to: str, subject: str, html: str, text: str) -> None:
    """Envoie un email via l'API Brevo. Lève une exception si l'envoi échoue."""
    api_key = os.environ["BREVO_API_KEY"].strip()
    from_addr = os.environ["EMAIL_FROM"].strip()
    from_name = os.environ.get("EMAIL_FROM_NAME", "Studio Yoga").strip()

    payload = {
        "sender": {"email": from_addr, "name": from_name},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html,
        "textContent": text,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _BREVO_URL,
            json=payload,
            headers={
                "api-key": api_key,
                "accept": "application/json",
                "content-type": "application/json",
            },
            timeout=15.0,
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Brevo API {resp.status_code} : {resp.text[:300]}")


async def envoyer_lien_espace(email: str, nom: str, token: str) -> None:
    """
    Envoie le lien permanent vers l'espace élève après un achat.
    C'est le seul email que l'élève reçoit — il lui sert de point d'entrée permanent.
    """
    url = lien_espace(token)
    prenom = nom.split()[0] if nom else "chère élève"

    subject = "Votre espace personnel – Studio Yoga"
    text = f"""\
Bonjour {prenom},

Votre achat a bien été enregistré. Vous pouvez accéder à votre espace personnel
pour gérer vos réservations à tout moment via ce lien :

{url}

Conservez ce lien précieusement — il vous donnera accès à votre espace à chaque fois.

À bientôt,
Le studio
"""
    html = f"""\
<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"></head>
<body style="font-family: sans-serif; color: #333; max-width: 600px; margin: auto; padding: 24px;">
  <p>Bonjour {prenom},</p>
  <p>Votre achat a bien été enregistré. Vous pouvez accéder à votre espace personnel
  pour gérer vos réservations à tout moment :</p>
  <p style="text-align: center; margin: 32px 0;">
    <a href="{url}"
       style="background: #5b7e6b; color: white; padding: 14px 28px;
              border-radius: 6px; text-decoration: none; font-size: 16px;">
      Accéder à mon espace
    </a>
  </p>
  <p style="font-size: 13px; color: #666;">
    Conservez cet email — ce lien vous donnera accès à votre espace à chaque fois.<br>
    Si le bouton ne fonctionne pas, copiez cette URL dans votre navigateur :<br>
    <a href="{url}">{url}</a>
  </p>
  <p>À bientôt,<br>Le studio</p>
</body>
</html>
"""
    await _envoyer(email, subject, html, text)


async def envoyer_confirmation_reservation(
    email: str,
    nom: str,
    token: str,
    type_cours: str,
    date_seance: str,
    lieu: str,
) -> None:
    """Email de confirmation après une réservation (optionnel — UX bonus)."""
    url = lien_espace(token)
    prenom = nom.split()[0] if nom else "chère élève"

    subject = f"Réservation confirmée – {type_cours} du {date_seance}"
    text = f"""\
Bonjour {prenom},

Votre réservation est confirmée :
  Cours   : {type_cours}
  Date    : {date_seance}
  Lieu    : {lieu}

Pour annuler (jusqu'à 24h avant) ou voir vos autres réservations :
{url}

À bientôt !
"""
    html = f"""\
<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"></head>
<body style="font-family: sans-serif; color: #333; max-width: 600px; margin: auto; padding: 24px;">
  <p>Bonjour {prenom},</p>
  <p>Votre réservation est confirmée :</p>
  <table style="border-collapse: collapse; margin: 16px 0;">
    <tr><td style="padding: 6px 16px 6px 0; color: #666;">Cours</td><td><strong>{type_cours}</strong></td></tr>
    <tr><td style="padding: 6px 16px 6px 0; color: #666;">Date</td><td><strong>{date_seance}</strong></td></tr>
    <tr><td style="padding: 6px 16px 6px 0; color: #666;">Lieu</td><td><strong>{lieu}</strong></td></tr>
  </table>
  <p style="text-align: center; margin: 28px 0;">
    <a href="{url}"
       style="background: #5b7e6b; color: white; padding: 12px 24px;
              border-radius: 6px; text-decoration: none;">
      Gérer mes réservations
    </a>
  </p>
  <p>À bientôt !</p>
</body>
</html>
"""
    await _envoyer(email, subject, html, text)
