# TODO — Dette technique

## Généralisation de la LLM d'arbitrage

### Contexte
La LLM d'arbitrage (actuellement Qwen 35B via vLLM) est partiellement nommée en dur dans le code et la config.
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
