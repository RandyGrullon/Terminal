"""What the practice-broker mirrors share (app.broker): the error every practice client raises."""
from __future__ import annotations


class BrokerError(Exception):
    """A Spanish message for the user; `status` is the HTTP status (0 = no answer, 401 = the keys were refused)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = int(status)
