"""Schéma de config — socle : résultat de validation + vérificateurs primitifs typés."""
from __future__ import annotations

import re
from typing import Any


class ValidationResult:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    @property
    def all_messages(self) -> list[str]:
        return self.errors + self.warnings

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

def _check_str(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    val = obj.get(key)
    if val is None:
        r.add_error(f"{path}: valeur manquante")
    elif not isinstance(val, str):
        r.add_error(f"{path}: doit être une chaîne (reçu {type(val).__name__})")
    elif val.strip() == "":
        r.add_error(f"{path}: chaîne vide")

def _check_bool(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    val = obj.get(key)
    if val is not None and not isinstance(val, bool):
        r.add_error(f"{path}: doit être true/false (reçu {type(val).__name__})")

def _check_time_string(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    val = obj.get(key)
    if not isinstance(val, str):
        r.add_error(f"{path}: doit être une chaîne HH:MM")
        return
    if not re.match(r"^\d{2}:\d{2}$", val):
        r.add_error(f"{path}: doit être au format HH:MM")
        return
    hour, minute = [int(part) for part in val.split(":")]
    if hour > 23 or minute > 59:
        r.add_error(f"{path}: heure invalide")

def _check_int_range(
    obj: dict, key: str, path: str, vmin: int, vmax: int, r: ValidationResult
) -> None:
    val = obj.get(key)
    if val is None:
        r.add_error(f"{path}: valeur manquante")
        return
    if isinstance(val, bool):
        r.add_error(f"{path}: doit être un nombre entier, pas un booléen")
        return
    if not isinstance(val, (int, float)):
        r.add_error(f"{path}: doit être un nombre (reçu {type(val).__name__})")
        return
    val = int(val)
    if val < vmin or val > vmax:
        r.add_error(f"{path}={val}: doit être entre {vmin} et {vmax}")

def _check_optional_number(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    val = obj.get(key)
    if val is None:
        return
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        r.add_error(f"{path}: doit être un nombre ou null")

def _check_optional_positive_int(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    val = obj.get(key)
    if val is None:
        return
    if isinstance(val, bool) or not isinstance(val, int):
        r.add_error(f"{path}: doit être un entier positif ou null")
        return
    if val < 1:
        r.add_error(f"{path}: doit être >= 1 ou null")

def _check_regex_string(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    val = obj.get(key)
    if val is None:
        return
    if not isinstance(val, str):
        r.add_error(f"{path}: doit être une chaîne ou null")
        return
    if not val.strip():
        return
    try:
        re.compile(val)
    except re.error as exc:
        r.add_error(f"{path}: regex invalide ({exc})")

def _check_regex_list(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    values = obj.get(key, [])
    if values is None:
        return
    if not isinstance(values, list):
        r.add_error(f"{path}: doit être une liste")
        return
    for index, value in enumerate(values):
        item_path = f"{path}[{index}]"
        if not isinstance(value, str) or not value.strip():
            r.add_error(f"{item_path}: doit être une chaîne non vide")
            continue
        try:
            re.compile(value)
        except re.error as exc:
            r.add_error(f"{item_path}: regex invalide ({exc})")

def _check_port_value(val: Any, path: str, r: ValidationResult) -> None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        r.add_error(f"{path}: doit être un port numérique")
        return
    port = int(val)
    if port < 1 or port > 65535:
        r.add_error(f"{path}={port}: doit être entre 1 et 65535")
