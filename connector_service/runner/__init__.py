"""Meeting-runner (vague 4) — le SEUL composant qui voit le socket Docker.

Tire les intentions du portail par HTTP (jeton du compte de service, permission
OPERATE_MEETING_RUNNER nominative), lance un conteneur de bot par session, relaie les états,
rapporte l'issue (codes 0/1/2/3). Le portail n'obtient JAMAIS de droits Docker (plan
UI_REUNIONS §4 D1) — ce démon est le miroir du JobsApiBridge, dans l'autre sens.

`config` (dataclass + chargeur YAML) ← `commands` (argv Docker PURS, testés) ← `daemon`
(boucle asyncio, portail et lanceur INJECTABLES — testée sans réseau ni Docker).
"""
