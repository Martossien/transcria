"""Gouvernance de la LLM d'arbitrage « cloud » (docs/LLM_CLOUD.md § 3.2 — « que ce soit dit »).

Validation admin en base (caducité par version du texte), interdiction par groupe, garde des
entrées de traitement (web ET rempart worker), provenance par job. La politique est PURE
(``policy.py``) ; ``store.py`` parle à la base ; ``gate.py`` assemble les deux ; ``provenance.py``
écrit la trace par job. Le prédicat du mode cloud reste ``gpu.arbitrage_endpoint.is_cloud_backend``.
"""
