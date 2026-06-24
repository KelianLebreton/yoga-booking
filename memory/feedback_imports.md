---
name: feedback-imports
description: Ne pas préfixer les imports avec app. — app/ est la racine du projet
metadata:
  type: feedback
---

Ne jamais écrire `from app.booking_logic import ...` ou `from app.sheets_client import ...`.
Écrire directement `from booking_logic import ...`, `from sheets_client import ...`, etc.

**Why:** Le répertoire `app/` est la racine d'exécution du projet. Le préfixe `app.` est redondant et casse les imports.

**How to apply:** Dans tous les fichiers sous `app/`, les imports inter-modules utilisent des chemins relatifs sans préfixe (`from booking_logic import`, `from sheets_client import`, `from auth import`, etc.).
