"""
Envoi d'emails transactionnels via SMTP (Brevo ou tout serveur SMTP).

Variables d'environnement requises :
  SMTP_HOST       – ex: smtp-relay.brevo.com
  SMTP_PORT       – ex: 587
  SMTP_USER       – identifiant SMTP
  SMTP_PASSWORD   – mot de passe / clé API SMTP
  EMAIL_FROM      – adresse expéditrice (ex: noreply@studio-yoga.fr)
  BASE_URL        – URL publique de l'app (pour construire le lien espace)
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from auth import lien_espace


def _smtp_connection() -> smtplib.SMTP:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]

    smtp = smtplib.SMTP(host, port)
    smtp.starttls()
    smtp.login(user, password)
    return smtp


def _envoyer(to: str, subject: str, html: str, text: str) -> None:
    from_addr = os.environ["EMAIL_FROM"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    with _smtp_connection() as smtp:
        smtp.sendmail(from_addr, to, msg.as_string())


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
    _envoyer(email, subject, html, text)


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
    _envoyer(email, subject, html, text)
