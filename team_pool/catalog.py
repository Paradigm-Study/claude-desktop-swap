"""Validate the credential-free pool metadata shown to enrolled team members."""
from __future__ import annotations

import re

from .client import ClientError, _number, _safe_string

REASONS = {"quarantined", "refreshing", "unverified", "token-expiring",
           "quota-unavailable", "quota-stale", "quota-exhausted"}
FIELDS = {"id", "account", "usage", "fetched", "expires_at", "state", "eligible", "reason"}


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None


def validate_catalog(value):
    try:
        if (not isinstance(value, dict) or set(value) != {"accounts", "account_id", "threshold"}
                or not isinstance(value["accounts"], list) or len(value["accounts"]) > 100
                or (value["account_id"] is not None and not identifier(value["account_id"]))
                or not _number(value["threshold"]) or not 1 <= value["threshold"] <= 100):
            raise ValueError
        accounts, seen = [], set()
        for row in value["accounts"]:
            if (not isinstance(row, dict) or set(row) != FIELDS or not identifier(row["id"])
                    or row["id"] in seen or row["state"] not in ("ready", "refreshing", "quarantined")
                    or not isinstance(row["eligible"], bool)
                    or row["reason"] not in REASONS | {None}
                    or row["eligible"] != (row["reason"] is None)
                    or (row["fetched"] is not None and (not _number(row["fetched"]) or row["fetched"] < 0))
                    or not _number(row["expires_at"]) or row["expires_at"] <= 0):
                raise ValueError
            account = row["account"]
            if (not isinstance(account, dict) or set(account) != {"emailAddress", "subscriptionType"}
                    or not _safe_string(account["emailAddress"], maximum=320)
                    or not _safe_string(account["subscriptionType"], maximum=64)):
                raise ValueError
            usage = row["usage"]
            if usage is not None:
                if (not isinstance(usage, dict) or not {"five_hour", "seven_day"} <= set(usage)
                        or len(usage) > 30 or any(not isinstance(key, str)
                            or not re.fullmatch(r"(?:five_hour|seven_day(?:_[A-Za-z0-9_-]{1,64})?|weekly_scoped:[^\x00-\x1f\x7f]{1,96})", key)
                            or not _number(percent) or percent < 0 for key, percent in usage.items())):
                    raise ValueError
            if row["eligible"] and (row["state"] != "ready" or usage is None or row["fetched"] is None
                                    or any(percent >= value["threshold"] for percent in usage.values())):
                raise ValueError
            seen.add(row["id"])
            accounts.append({**row, "account": dict(account), "usage": dict(usage) if usage is not None else None})
        return {"accounts": accounts, "account_id": value["account_id"], "threshold": value["threshold"]}
    except (ValueError, TypeError, KeyError):
        raise ClientError("The coordinator returned invalid pool metadata. No account was changed.") from None
