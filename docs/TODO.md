# TODO — Dette technique et évolutions

## Généralisation de la LLM d'arbitrage

### Contexte
La LLM d'arbitrage (actuellement Qwen 35B via llama.cpp) est partiellement nommée en dur dans le code et la config.
La méthode `ensure_arbitrage_llm_ready()` est générique, mais les éléments suivants restent couplés à Qwen.

### À corriger dans le code

**`transcria/gpu/vram_manager.py`**
- `launch_qwen_35b()` → renommer `launch_arbitrage_llm()`
- `stop_qwen_35b()` → renommer `stop_arbitrage_llm()`
- `self.qwen_port` → renommer `self.arbitrage_llm_port`
- `self._qwen_pid` → renommer `self._arbitrage_llm_pid`
- `self.llm_vram_mb` → déjà générique, OK

**`transcria/workflow/runner.py`**
- Tous les appels `self.vram.launch_qwen_35b()` → `launch_arbitrage_llm()`
- Tous les appels `self.vram.stop_qwen_35b()` → `stop_arbitrage_llm()`
- `finally: self.vram.stop_qwen_35b()` (×2) → idem

### À corriger dans la config (`configs/`)

**Clé de port**
- `services.qwen_port` → renommer `services.arbitrage_llm_port`

**Script d'arbitrage**
- `services.arbitrage_script` → déjà générique, OK
- `services.stop_script` → déjà générique, OK

**Section LLM**
- `workflow.summary_llm.model_id` → déjà générique, OK

### À corriger dans les templates / UI
- Vérifier que l'UI n'affiche nulle part "Qwen" en dur (chercher dans `transcria/web/templates/`)

### Principe cible
Tout ce qui touche à la LLM d'arbitrage doit être piloté par la config.
Changer de modèle (Qwen → Mistral, LLaMA, etc.) ne doit nécessiter qu'un changement de config,
zéro modification de code.

---

## Améliorations du lexique (suite)

### Contexte
Voir `docs/LEXIQUE_AMELIORATION.md` pour le détail complet.
Les actions 1 à 5 sont implémentées. Les actions 6 à 9 restent à faire.

### Reste à faire

| Priorité | Action | Fichiers | Risque |
|---|---|---|---|
| 6 | Ajouter `contexts` pour afficher 1 à 3 extraits de validation dans l'UI | `job_wizard.html`, `wizard.js` | moyen UX |
| 7 | Modifier `correction_prompt.txt` pour correction contextuelle, sans remplacement global aveugle | `configs/prompts/correction_prompt.txt` | moyen |
| 8 | Ajouter un contrôle qualité signalant les variantes exactes ou graphies proches non résolues après correction | `quality_report.py`, `lexicon_checks.py`, tests | faible |
| 9 | Ajuster les tests unitaires du parser, du contexte, du lexique et de la qualité | `tests/` | faible |

### Remarque
L'action 8 (check qualité variantes non résolues) est partiellement implémentée — `LexiconChecker.find_unresolved_terms()` existe et le check 7bis est dans `QualityReporter`. L'amélioration restante est le signalement plus fin des graphies proches dans le rapport.

---

## Refactoring code qualité

### Doublons de code
- `is_port_open()` et `_wait_for_port()` existent dans `vram_manager.py` et `llm_backend.py` — factoriser.
- `import subprocess` en double dans `converter.py` (lignes 1 et 3).

### Style
- `__import__("json")` dans `job_context_builder.py:69` — remplacer par un import normal.