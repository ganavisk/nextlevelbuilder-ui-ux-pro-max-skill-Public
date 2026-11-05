"""EmailProvider / SMSProvider — outbound confirmations and reminders.

Dispatch happens on the async path with retries; templates live in the
notifications module. SMS is transactional-only (confirmations to callers who
engaged) with opt-out honored — a TCPA constraint, not a style choice.
"""

from abc import ABC, abstractmethod


class EmailProvider(ABC):
    @abstractmethod
    async def send(
        self, *, to: str, subject: str, html: str, from_address: str, idempotency_key: str
    ) -> str:
        """Send an email; return the provider message id."""


class SMSProvider(ABC):
    @abstractmethod
    async def send(self, *, to: str, body: str, idempotency_key: str) -> str:
        """Send a transactional SMS; return the provider message id."""
