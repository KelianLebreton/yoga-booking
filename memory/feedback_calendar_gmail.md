---
name: feedback-calendar-gmail
description: Google Calendar API — impossible d'écrire dans le calendrier principal Gmail via compte de service
metadata:
  type: feedback
---

Un compte de service Google **ne peut pas écrire dans le calendrier principal** d'un utilisateur Gmail (`user@gmail.com`), même avec les droits de partage. L'API retourne 404.

Il faut créer un **calendrier secondaire** dans le compte Gmail du client et partager celui-là avec le compte de service.

**Why:** Le calendrier principal Gmail est protégé contre les écritures API externes par Google.

**How to apply:** Toujours utiliser un calendrier secondaire pour les intégrations API. L'ID ressemble à `abc123@group.calendar.google.com`, pas à une adresse email.
