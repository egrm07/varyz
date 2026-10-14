"""Lightweight canonical wire payloads compatible with the local owner ledgers."""

import re
import unicodedata

from varyz.privacy.secrets_scan import redact_tree
from varyz.scopes import normalize_scope as canonical_scope


def normalize_scope(scope):
    value = canonical_scope(scope)
    if value.startswith(("org:", "published:")):
        raise ValueError("Cloud sync supports your own preferences only")
    return value


def canonical_payload(kind, payload):
    if payload is None:
        return None
    _, labels = redact_tree(payload)
    if labels:
        raise ValueError("Preference contains detected credentials; remove them before cloud sync")
    result = dict(payload)
    for key, value in result.items():
        if isinstance(value, str):
            result[key] = (
                value.strip()
                if kind == "answer" and key in ("question", "answer")
                else " ".join(value.split())
            )
    result["scope"] = normalize_scope(result["scope"])
    if kind in ("rule", "workflow"):
        result.setdefault("rationale", "")
        result.setdefault("domain", "")
        domain = unicodedata.normalize("NFKC", result["domain"]).casefold().strip()
        domain = re.sub(r"[\s_]+", "-", domain)
        if domain in ("", "*", "any", "all", "general"):
            domain = ""
        if domain and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", domain):
            raise ValueError("Domain must be a short slug")
        result["domain"] = domain
        if len(result["rationale"]) > 500:
            raise ValueError("Rationale is limited to 500 characters")
    if kind == "rule":
        result.setdefault("kind", "rule")
        if len(result["statement"]) > 500:
            raise ValueError("Rule is limited to 500 characters")
        if result["kind"] == "vision" and result["scope"] == "*":
            raise ValueError("A project vision needs a project scope")
    if kind == "workflow":
        result["name"] = result["name"].strip(" -:")
        if not result["name"] or len(result["name"]) > 80 or not 2 <= len(result["steps"]) <= 20:
            raise ValueError("Workflow needs a name up to 80 characters and 2–20 steps")
        steps = []
        for original in result["steps"]:
            step = " ".join(original["step"].split()).strip(" -:")
            how = " ".join(original.get("how", "").split())
            if not step or len(step) > 200 or len(how) > 500:
                raise ValueError("Steps are limited to 200 characters and guidance to 500")
            value = {"step": step}
            if how:
                value["how"] = how
            if original.get("optional") is True:
                value["optional"] = True
            steps.append(value)
        names = [
            " ".join(unicodedata.normalize("NFKC", s["step"]).casefold().split())
            .rstrip(".!;:")
            .rstrip()
            for s in steps
        ]
        if len(set(names)) != len(names):
            raise ValueError("Workflow steps must be unique")
        result["steps"] = steps
    return result
