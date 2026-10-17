"""Credential storage for trusted connectors, never an agent tool.

Legacy environment storage remains explicit; Keychain mode never falls back to it.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


class SecretStoreError(ValueError):
    pass


class SecretStore:
    def __init__(self, base: Path, settings: dict | None = None):
        settings = {} if settings is None else settings
        if not isinstance(settings, dict):
            raise SecretStoreError("secrets must contain backend and, for Keychain, a namespace.")
        self.base = Path(base)
        self.backend = settings.get("backend", "env")
        self.namespace = settings.get("namespace", "")
        if self.backend not in ("env", "keychain"):
            raise SecretStoreError("secrets.backend must be env or keychain; no automatic fallback is allowed.")
        if self.backend == "keychain" and (not isinstance(self.namespace, str) or
                not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{2,79}", self.namespace)):
            raise SecretStoreError("Keychain needs a stable secrets.namespace (3–80 letters, numbers, dots, - or _).")

    @property
    def service(self) -> str:
        return f"james-monitoring:{self.namespace}"

    @staticmethod
    def _name(name: str) -> None:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", name):
            raise SecretStoreError("Credential references must be uppercase names, 3–64 characters.")

    def _keychain(self):
        if sys.platform != "darwin":
            raise SecretStoreError("Keychain storage requires macOS. No plaintext fallback was used.")
        try:
            # Select the native backend directly: no third-party, null or plaintext backend discovery.
            from keyring.backends.macOS import Keyring
            return Keyring()
        except Exception:
            raise SecretStoreError("Native Keychain backend unavailable. Install james-monitoring[keychain].") from None

    def get(self, name: str) -> str:
        if not name:
            return ""
        self._name(name)
        if self.backend == "env":
            return os.environ.get(name, "")
        try:
            return self._keychain().get_password(self.service, name) or ""
        except SecretStoreError:
            raise
        except Exception:
            raise SecretStoreError("Keychain read failed or access was denied. No environment fallback was used.") from None

    def save(self, pairs: dict[str, str]) -> None:
        for name, value in pairs.items():
            self._name(name)
            if not isinstance(value, str) or not value or any(c in value for c in "\r\n\0"):
                raise SecretStoreError("Credential values must be nonempty single-line strings.")
        if not pairs:
            return
        if self.backend == "env":
            from .fileio import set_env
            set_env(self.base / ".env", pairs)
            return
        try:
            backend = self._keychain()
            for name, value in pairs.items():
                backend.set_password(self.service, name, value)
        except SecretStoreError:
            raise
        except Exception:
            # Native exceptions can include sensitive inputs; never forward their text.
            raise SecretStoreError("Keychain save failed or access was denied. Check saved entries before retrying.") from None

    def status(self, name: str) -> str:
        """Presentation only: an unavailable store is not an absent credential. Reads still raise."""
        try:
            return "configured" if self.get(name) else "missing"
        except SecretStoreError:
            return "unavailable"


def store_for(base: Path) -> SecretStore:
    from .fileio import read_config
    path = Path(base) / "config.yaml"
    raw = read_config(path) if path.exists() else {}
    return SecretStore(base, raw.get("secrets"))


def save_secrets(base: Path, pairs: dict[str, str]) -> None:
    store_for(base).save(pairs)
