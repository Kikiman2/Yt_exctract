"""User accounts: create, list, change role / password, disable, delete.

Rules that keep the install reachable: the last enabled admin can't be demoted,
disabled or deleted, and nobody can lock themselves out through the users page
(change your own role / enabled state / account removal is refused; use another admin)."""

from __future__ import annotations

import asyncio

from .. import auth, db


def _json(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "disabled": bool(row["disabled"]),
        "created_at": row["created_at"],
        "request_count": row.get("request_count", 0),
    }


def _check_role(role) -> str:
    if role not in auth.ROLES:
        raise ValueError(f"role must be one of: {', '.join(auth.ROLES)}")
    return role


def _check_username(username) -> str:
    name = (username or "").strip() if isinstance(username, str) else ""
    if not auth.USERNAME_RE.match(name):
        raise ValueError("Username must be 3-32 characters: letters, digits, dot, dash or underscore")
    return name


async def list_users() -> list[dict]:
    return [_json(r) for r in await db.list_users()]


async def _json_by_id(user_id: int) -> dict:
    for row in await db.list_users():
        if row["id"] == user_id:
            return _json(row)
    raise KeyError(user_id)


async def create_user(username: str, password: str, role: str = "user") -> dict:
    name = _check_username(username)
    _check_role(role)
    auth.check_password_rules(password or "")
    password_hash = await asyncio.to_thread(auth.hash_password, password)
    user_id = await db.create_user(name, password_hash, role)
    if user_id is None:
        raise ValueError(f"The username '{name}' is already taken")
    await db.add_event("info", "users", f"Created {role} account '{name}'")
    return await _json_by_id(user_id)


async def _require(user_id: int) -> dict:
    row = await db.get_user(user_id)
    if row is None:
        raise KeyError("User not found")
    return row


async def update_user(actor: dict, user_id: int, fields: dict) -> dict:
    target = await _require(user_id)
    unknown = set(fields) - {"role", "disabled", "password"}
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    clean: dict = {}
    if "role" in fields:
        role = _check_role(fields["role"])
        if role != target["role"]:
            if user_id == actor["id"]:
                raise ValueError("You can't change your own role")
            clean["role"] = role
    if "disabled" in fields:
        if not isinstance(fields["disabled"], bool):
            raise ValueError("disabled must be true or false")
        if fields["disabled"] != bool(target["disabled"]):
            if user_id == actor["id"]:
                raise ValueError("You can't disable your own account")
            clean["disabled"] = int(fields["disabled"])
    if "password" in fields:
        password = fields["password"] if isinstance(fields["password"], str) else ""
        auth.check_password_rules(password)
        clean["password_hash"] = await asyncio.to_thread(auth.hash_password, password)
    if (clean.get("role") == "user" or clean.get("disabled") == 1) and target["role"] == "admin":
        if await db.count_active_admins(excluding=user_id) == 0:
            raise ValueError("There must be at least one enabled administrator")
    await db.update_user(user_id, clean)
    if clean:
        changed = ", ".join(k.replace("password_hash", "password") for k in clean)
        await db.add_event("info", "users", f"Updated '{target['username']}' ({changed})")
    return await _json_by_id(user_id)


async def delete_user(actor: dict, user_id: int) -> None:
    target = await _require(user_id)
    if user_id == actor["id"]:
        raise ValueError("You can't delete your own account")
    if target["role"] == "admin" and await db.count_active_admins(excluding=user_id) == 0:
        raise ValueError("There must be at least one enabled administrator")
    await db.delete_user(user_id)
    await db.add_event("info", "users", f"Deleted account '{target['username']}'")


async def change_own_password(user: dict, current: str, new: str) -> None:
    stored = await db.get_password_hash(user["id"])
    ok = bool(stored) and await asyncio.to_thread(auth.verify_password, current or "", stored)
    if not ok:
        raise ValueError("Current password is wrong")
    auth.check_password_rules(new or "")
    await db.update_user(user["id"], {"password_hash": await asyncio.to_thread(auth.hash_password, new)})
    await db.add_event("info", "users", f"'{user['username']}' changed their password")
