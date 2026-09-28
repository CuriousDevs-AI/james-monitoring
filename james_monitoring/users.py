"""People who sign in to the console, and what each role may do.

The founder signs in with the console key (`jm run` prints it). Everyone else gets a personal link made in
Settings → Sign-in links; only a SHA-256 of their key is stored, so config.yaml never holds a usable secret.

    owner   everything
    admin   everything except managing sign-in links (settings, people, models, approvals)
    member  read everything they can see, talk in All hands and project rooms, create and move tasks
    viewer  read only
    client  only their own projects, read only (the client portal)

Private rooms: the founder's 1:1s with the team are theirs (admins too); the personal assistant's room and
personal tasks are the founder's alone.
"""
from __future__ import annotations

import hashlib
import secrets

ROLE_PERMS = {
    "owner": {"read", "chat", "task", "approve", "admin", "owner", "portal"},
    "admin": {"read", "chat", "task", "approve", "admin", "portal"},
    "member": {"read", "chat", "task", "portal"},
    "viewer": {"read", "portal"},
    "client": {"portal"},
}
ROLE_TEXT = {"admin": "Admin — runs the company with you (not sign-in links)",
             "member": "Team member — chats in rooms, creates and moves tasks",
             "viewer": "Viewer — can look, can't change anything",
             "client": "Client — sees only their projects (portal)"}


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return "u_" + secrets.token_urlsafe(24)


def find_user(cfg, key: str):
    """The User for a sign-in key, or None. Constant-time compare on the hashes."""
    if not key or not key.startswith("u_") or cfg is None:
        return None
    h = hash_key(key)
    for u in cfg.users:
        if u.key_sha256 and secrets.compare_digest(u.key_sha256, h):
            return u
    return None


def can(user: dict, perm: str) -> bool:
    return perm in ROLE_PERMS.get(user.get("role", ""), set())
