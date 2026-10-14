"""Signup email validation without third-party address verification APIs."""

from functools import lru_cache
from pathlib import Path

import dns.resolver
import idna
from email_validator import EmailNotValidError, validate_email
from email_validator.deliverability import validate_email_deliverability


class InvalidSignupEmail(ValueError):
    pass


class SignupDNSUnavailable(ValueError):
    pass


def normalize_email(raw):
    """Use the same ASCII domain for signup, login and recovery (including IDNs)."""
    value = raw.strip().lower()
    local, separator, domain = value.rpartition("@")
    if not separator:
        return value
    try:
        return local + "@" + idna.encode(domain, uts46=True).decode("ascii")
    except idna.IDNAError:
        return value  # Signup's strict validator rejects invalid domains.


@lru_cache(maxsize=1)
def disposable_domains():
    return frozenset(Path(__file__).with_name("disposable_domains.txt").read_text().splitlines())


def blocked_domain(domain):
    labels = domain.lower().split(".")
    return any(".".join(labels[i:]) in disposable_domains() for i in range(len(labels) - 1))


@lru_cache(maxsize=1)
def resolver():
    value = dns.resolver.Resolver()
    value.timeout = 1
    value.lifetime = 2
    value.cache = dns.resolver.LRUCache(max_size=1000)
    return value


def validate_signup_email(raw):
    try:
        email = validate_email(raw, check_deliverability=False, allow_smtputf8=False)
        domain = email.ascii_domain
        if blocked_domain(domain):
            raise InvalidSignupEmail("Use a permanent email address, not a disposable mailbox")
        delivery = validate_email_deliverability(domain, domain, dns_resolver=resolver())
        if not delivery.get("mx"):
            raise SignupDNSUnavailable("Email domain could not be checked; please retry shortly")
        return email.ascii_email.lower()
    except EmailNotValidError:
        raise InvalidSignupEmail(
            "Enter a valid email address on a domain that accepts mail"
        ) from None
