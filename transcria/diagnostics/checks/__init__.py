"""Vérifications du doctor, par domaine — voir `transcria.diagnostics.doctor` (façade).

`common` (socle) ← `probes` (sondes effectives) ← modules de domaine. Aucun module de
domaine n'importe un autre domaine : une dépendance croisée passe par `common`/`probes`
ou n'a pas sa place ici.
"""
