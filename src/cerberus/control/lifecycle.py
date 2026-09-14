"""Config lifecycle — validate / shadow / activate / rollback over immutable documents.

Git owns authoring and history; this module owns the runtime swap. Activation
is a single reference assignment (atomic under the event loop), rollback pops
the version stack, and the shadow slot holds a candidate document that is
evaluated policy-only — the dispatch path must never open a provider
connection on its behalf.
"""

from __future__ import annotations

from typing import Protocol

from cerberus.registry.loader import ConfigDocument, load_config_document


class ControlRepository(Protocol):
    def bootstrap(self, initial: ConfigDocument) -> ConfigDocument: ...
    def revision_checksums(self) -> dict[str, str]: ...
    def register(self, document: ConfigDocument) -> None: ...
    def activate(self, document: ConfigDocument, *, action: str = "activate") -> None: ...


class ConfigLifecycle:
    def __init__(self, initial: ConfigDocument, repository: ControlRepository | None = None) -> None:
        self._repository = repository
        self._active = repository.bootstrap(initial) if repository is not None else initial
        self._history: list[ConfigDocument] = []
        self._shadow: ConfigDocument | None = None
        self._version_checksums = (
            repository.revision_checksums() if repository is not None else {initial.version: initial.checksum}
        )

    @property
    def active(self) -> ConfigDocument:
        return self._active

    @property
    def shadow(self) -> ConfigDocument | None:
        return self._shadow

    def validate(self, path: str) -> ConfigDocument:
        """Validate and permanently bind a version to its first observed checksum."""

        document = load_config_document(path)
        existing = self._version_checksums.get(document.version)
        if existing is not None and existing != document.checksum:
            raise ValueError(
                f"configuration version {document.version!r} is already bound to checksum {existing}; "
                f"candidate checksum is {document.checksum}"
            )
        self._version_checksums.setdefault(document.version, document.checksum)
        if self._repository is not None:
            self._repository.register(document)
        return document

    def activate(self, path: str) -> ConfigDocument:
        document = self.validate(path)
        if self._repository is not None:
            self._repository.activate(document)
        self._history.append(self._active)
        self._active = document  # atomic swap: single reference assignment
        return document

    def rollback(self) -> ConfigDocument:
        if not self._history:
            raise LookupError("no prior configuration to roll back to")
        restored = self._history.pop()
        if self._repository is not None:
            self._repository.activate(restored, action="rollback")
        self._active = restored
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
            "storage": "sqlite" if self._repository is not None else "memory",
        }
