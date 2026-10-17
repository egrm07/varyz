"""Safe account access notices shared by HTTP, MCP and local clients."""

MESSAGES = {
    "subscription_expired": "your paid subscription period has run out",
    "subscription_canceled": "your subscription has ended",
    "payment_past_due": "your subscription payment is past due",
    "subscription_paused": "your subscription is paused",
    "payment_refunded": "your subscription payment was refunded",
    "free_period_expired": "your free Cloud period has run out",
    "free_access_revoked": "your free Cloud access has ended",
    "subscription_required": "this account has no active subscription or free Cloud period",
}


def notice(reason):
    reason = reason if reason in MESSAGES else "subscription_required"
    return {
        "active": False,
        "reason": reason,
        "message": f"Varyz Cloud has stopped working because {MESSAGES[reason]}. "
        "Local Varyz remains available. Open your Cloud account to restore Cloud access.",
        "instruction": "Tell the user this message. Stop Cloud calls until access is restored. "
        "Do not reconnect OAuth or repeat setup to fix billing. Local Varyz can still be used.",
    }


def account_access(owner):
    if owner["status"] == "active":
        return {"active": True, "reason": None, "message": None}
    if owner["status"] == "unverified":
        return {"active": None, "reason": "billing_unavailable", "message":
                "Cloud billing status could not be verified. Try later; local Varyz is available."}
    reason = {
        "expired": "subscription_expired",
        "canceled": "subscription_canceled",
        "past_due": "payment_past_due",
        "paused": "subscription_paused",
        "refunded": "payment_refunded",
    }.get(owner["status"], owner.get("access_reason", "subscription_required"))
    return notice(reason)


def response_notice(body):
    """Never relay arbitrary server text, preferences or credentials as a notice."""
    detail = body.get("detail", body) if isinstance(body, dict) else {}
    return notice(detail.get("reason") if isinstance(detail, dict) else None)
