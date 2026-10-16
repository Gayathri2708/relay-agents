"""API keys for the Relay HTTP API.

Two roles: a reporter may submit alerts and read runs; an approver may also approve, edit or reject
actions, resolve incidents and draft postmortems. With keys configured, who made a decision is taken
from the key, so it cannot be spoofed in the request body.
"""

import hmac
import logging
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

log = logging.getLogger(__name__)

Role = Literal["reporter", "approver"]
ROLES: tuple[Role, ...] = ("reporter", "approver")


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role
    authenticated: bool = True

    def can(self, role: Role) -> bool:
        return ROLES.index(self.role) >= ROLES.index(role)


ANONYMOUS = Principal("anonymous", "approver", authenticated=False)  # only when no keys are configured

header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Relay API key")


class KeyRing:
    def __init__(self, keys: dict[str, dict[str, str]]):
        self._keys: list[tuple[str, Principal]] = []
        for name, entry in keys.items():
            role = entry.get("role", "reporter")
            if role not in ROLES:
                raise ValueError(f"RELAY_API_KEYS: {name} has unknown role {role!r}")
            if len(entry.get("key", "")) < 16:
                raise ValueError(f"RELAY_API_KEYS: the key for {name} must be at least 16 characters")
            self._keys.append((entry["key"], Principal(name, role)))
        if not self._keys:
            log.warning("RELAY_API_KEYS is empty: the API is open to anyone who can reach it (development only)")

    @property
    def enabled(self) -> bool:
        return bool(self._keys)

    def lookup(self, key: str | None) -> Principal | None:
        if not self.enabled:
            return ANONYMOUS
        if not key:
            return None
        # Compare against every key in constant time so timing reveals nothing about which key nearly matched.
        found = None
        for secret, principal in self._keys:
            if hmac.compare_digest(secret.encode(), key.encode()):
                found = principal
        return found


def requires(keyring_getter, role: Role):
    def dependency(key: str | None = Security(header)) -> Principal:
        principal = keyring_getter().lookup(key)
        if principal is None:
            raise HTTPException(401, "Missing or invalid API key. Send it in the X-API-Key header.")
        if not principal.can(role):
            raise HTTPException(403, f"This needs the {role} role; {principal.name} is a {principal.role}.")
        return principal

    return dependency
