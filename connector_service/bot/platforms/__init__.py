"""Drivers navigateur par plateforme (Playwright) — implémentent `BrowserDriver`. Gate manuel.

Banc d'essai = Jitsi (public, sans compte). Un futur driver (Teams, Meet — v2 du plan temps
réel) réutilise le même `BrowserDriver` + payload de capture, en ne changeant que les
sélecteurs d'UI et le join. Le driver Zoom-web a été RETIRÉ (impasse : reCAPTCHA sur toute
automatisation du client Web, cf. docs/TEMPS_REEL_REUNIONS.md — la voie Zoom est le SDK natif).
"""
