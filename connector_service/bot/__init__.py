"""Bot de réunion (fallback universel, SORTANT-seul) — capture navigateur, opt-in isolé.

Voie de SECOURS quand l'API officielle est indisponible/non autorisée (ou derrière un
firewall sans entrant) : un navigateur headless rejoint la réunion comme un participant et
capte l'audio par piste via interception WebRTC in-page, qu'il pousse sur le PONT PCM neutre
(`connector_service.live.bridge_source`) → même pipeline que les transports officiels.

Isolation stricte : ce sous-paquet n'est JAMAIS dans l'image par défaut (Chrome + Xvfb +
Playwright = phase installeur opt-in dédiée, 1 conteneur/réunion). Le CŒUR (cycle de vie,
décodage du pont) est testable en CI ; le pilotage navigateur réel (Playwright) et le payload
JS de capture sont confirmés au gate manuel — banc d'essai = Jitsi (public, sans compte).
"""
