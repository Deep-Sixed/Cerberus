"""Config lifecycle — validate / shadow / activate / rollback over immutable documents.

Git owns authoring and history; this module owns the runtime swap. Activation
is a single reference assignment (atomic under the event loop), rollback pops
the version stack, and the shadow slot holds a candidate document that is
evaluated policy-only — the dispatch path must never open a provider
connection on its behalf.
"""

from __future__ import annotations

from cerberus.registry.loader import ConfigDocument, load_config_document


class ConfigLifecycle:
    def __init__(self, initial: ConfigDocument) -> None:
        self._active = initial
        self._history: list[ConfigDocument] = []
        self._shadow: ConfigDocument | None = None

    @property
    def active(self) -> ConfigDocument:
        return self._active

    @property
    def shadow(self) -> ConfigDocument | None:
        return self._shadow

    @staticmethod
    def validate(path: str) -> ConfigDocument:
        """Full validation including credential presence; raises on any failure."""
        return load_config_document(path)

    def activate(self, path: str) -> ConfigDocument:
        document = self.validate(path)
        self._history.append(self._active)
        self._active = document  # atomic swap: single reference assignment
        return document

    def rollback(self) -> ConfigDocument:
        if not self._history:
            raise LookupError("no prior configuration to roll back to")
        self._active = self._history.pop()
        return self._active

    def arm_shadow(self, path: str | None) -> ConfigDocument | None:
        if path is None:
            self._shadow = None
            return None
        self._shadow = self.validate(path)
        return self._shadow

    def status(self) -> dict:
        return {
            "active": {
                "version": self._active.version,
                "checksum": self._active.checksum,
                "source_path": self._active.source_path,
            },
            "shadow": (
                {
                    "version": self._shadow.version,
                    "checksum": self._shadow.checksum,
                    "source_path": self._shadow.source_path,
                }
                if self._shadow is not None
                else None
            ),
            "rollback_depth": len(self._history),
        }
