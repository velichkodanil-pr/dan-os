"""Telegram Mini App initData validation (server-side, per official spec).

secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
expected   = HMAC_SHA256(key=secret_key, msg=data_check_string) as hex
data_check_string = sorted "k=v" lines of every field except `hash`.

Any mismatch, stale auth_date, or non-owner user -> None (the API answers 401).
"""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, bot_token: str,
                       max_age: int = 3600, now: int | None = None) -> int | None:
    """Returns the authenticated Telegram user id, or None if invalid."""
    if not init_data or not bot_token:
        return None
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None
    received_hash = data.pop("hash", "")
    if not received_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or (now or int(time.time())) - auth_date > max_age:
        return None
    try:
        user = json.loads(data.get("user", "{}"))
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError):
        return None
    return user_id


def sign_init_data(fields: dict, bot_token: str) -> str:
    """Build a signed initData query string — used by tests to forge/verify."""
    from urllib.parse import urlencode
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})
