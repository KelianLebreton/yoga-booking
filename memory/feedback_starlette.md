---
name: feedback-starlette
description: TemplateResponse Starlette récent — request comme premier argument séparé
metadata:
  type: feedback
---

Dans les versions récentes de Starlette (utilisées dans ce projet), `TemplateResponse` prend `request` comme **premier argument positionnel**, pas dans le dictionnaire de contexte.

```python
# Correct
templates.TemplateResponse(request, "espace.html", {"key": "value"})

# Incorrect — lève TypeError: unhashable type: 'dict'
templates.TemplateResponse("espace.html", {"request": request, "key": "value"})
```

**Why:** Breaking change Starlette 0.28+. L'ancienne syntaxe lève `TypeError: unhashable type: 'dict'`.

**How to apply:** Toujours utiliser la nouvelle syntaxe dans ce projet.
