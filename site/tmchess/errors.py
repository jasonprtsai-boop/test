"""Shared application errors used across service-layer modules."""

from __future__ import annotations

from .board import Board


class CommandConflict(RuntimeError):
    """A valid command cannot run in the current application state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StatePersistenceError(RuntimeError):
    """A control command could not be committed to durable storage."""

    code = "state_persistence_failed"


class VerificationError(RuntimeError):
    """Observed physical state does not satisfy the workflow expectation."""

    def __init__(self, message: str, observed: Board | None = None) -> None:
        super().__init__(message)
        self.observed = observed
