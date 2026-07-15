"""Phases du workflow — une phase = un module (vague B1, lot 2).

Convention du lot 2 : chaque module expose des fonctions qui reçoivent le
``WorkflowRunner`` (hôte) en premier argument et rappellent ses coutures
(``runner._gpu_session``, ``runner._run_llm_summary``, ``runner.store``…).
Les tests historiques et les topologies substituent ces coutures au niveau du
runner (instance ou classe) : elles doivent rester le point de passage unique.
Le contrat formel (Protocol ``WorkflowPhase`` + registre) arrive au lot 3 avec
la façade finale.
"""
