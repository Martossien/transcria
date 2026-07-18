# Prompts EXPÉRIMENTAUX — JAMAIS chargés par la production

Ce répertoire parque les variantes de prompts en cours d'évaluation
(PISTES_AMELIORATION §2.4, campagne entamée le 2026-07-18). **Rien ici n'est lu
par le code** : le chargeur (`gpu/prompt_locator.py`) résout uniquement
`configs/prompts/<fichier>.txt` et `configs/prompts/<langue>/<fichier>.txt` par
noms EXACTS — les fichiers ci-dessous portent volontairement des noms non
résolubles (`correction_prompt.V1_DIRECT.txt` ≠ `correction_prompt.txt`).

## Comment tester une variante SANS toucher la production

```bash
# 1. Copier la variante dans un répertoire de test sous le NOM canonique :
mkdir -p /tmp/prompts_test && cp configs/prompts/experimental/correction_prompt.V1_DIRECT.txt \
    /tmp/prompts_test/correction_prompt.txt
# 2. Pointer le chargeur dessus par la config (couture officielle) :
#    workflow:
#      prompts_dir: /tmp/prompts_test
# 3. Rejouer la passe sur une copie de job (voir le protocole ci-dessous).
```

Protocole, premiers résultats et verdicts de lecture :
`configs/prompts/experimental/PROTOCOLE_ET_RESULTATS.md`.
