"""Named, versioned, deterministic local transformations."""

from __future__ import annotations

import hashlib
import re

from varyz.privacy.models import (
    ContextItem,
    ContextRequest,
    TransformedItem,
)

DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[T ][0-9:+.-]+)?\b")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d ()-]{7,}\d")
CREDENTIAL_RE = re.compile(
    r"(?i)\b(password|passcode|api[_ -]?key|secret|token)\b\s*[:=]\s*\S+"
)
FINANCIAL_RE = re.compile(
    r"(?i)\b(?:iban|account number|card number)\b\s*[:=]?\s*[A-Z0-9 -]{6,}"
)
MEDICAL_RE = re.compile(
    r"(?i)\b(?:diagnosis|medication|prescription)\b\s*[:=]?\s*[^.;\n]+"
)


class TransformationError(ValueError):
    pass


class TransformationRegistry:
    versions = {
        "select_claim": "1",
        "select_sentences": "1",
        "pseudonymize_people": "1",
        "generalize_location": "1",
        "generalize_date": "1",
        "redact_sensitive_attributes": "1",
        "aggregate_count": "1",
        "local_answer": "1",
    }

    def apply(
        self, name: str, item: ContextItem, request: ContextRequest
    ) -> TransformedItem:
        if name not in self.versions:
            raise TransformationError(f"unknown transformation: {name}")
        method = getattr(self, f"_{name}")
        output = method(item, request)
        return TransformedItem(
            item_id=item.item_id,
            resource_type=item.resource_type,
            resource_id=item.resource_id,
            content=output,
            source_ids=item.source_ids or (item.resource_id,),
            transformation=name,
            transformation_version=self.versions[name],
            input_hash=item.input_hash,
            output_hash=hashlib.sha256(output.encode("utf-8")).hexdigest(),
            metadata=item.metadata,
        )

    def identity(self, item: ContextItem) -> TransformedItem:
        return TransformedItem(
            item_id=item.item_id,
            resource_type=item.resource_type,
            resource_id=item.resource_id,
            content=item.content,
            source_ids=item.source_ids or (item.resource_id,),
            input_hash=item.input_hash,
            output_hash=item.input_hash,
            metadata=item.metadata,
        )

    @staticmethod
    def _select_claim(item: ContextItem, request: ContextRequest) -> str:
        if item.resource_type != "claim":
            raise TransformationError("select_claim requires a claim item")
        return item.content

    @staticmethod
    def _select_sentences(item: ContextItem, request: ContextRequest) -> str:
        keywords = {
            token.lower()
            for token in re.findall(r"[\w'-]{3,}", request.intent)
            if token.lower() not in {"what", "when", "where", "which", "about", "with"}
        }
        if not keywords:
            raise TransformationError("intent has no deterministic selection keywords")
        sentences = re.split(r"(?<=[.!?])\s+|\n+", item.content)
        selected = [
            sentence.strip()
            for sentence in sentences
            if sentence.strip()
            and keywords & set(re.findall(r"[\w'-]+", sentence.lower()))
        ]
        if not selected:
            raise TransformationError("no deterministically relevant sentence")
        return " ".join(selected)

    @staticmethod
    def _pseudonymize_people(item: ContextItem, request: ContextRequest) -> str:
        people = sorted(set(item.metadata.get("people", [])), key=len, reverse=True)
        if not people:
            raise TransformationError("no people metadata was supplied")
        output = item.content
        for person in people:
            if not person:
                continue
            suffix = hashlib.sha256(str(person).lower().encode()).hexdigest()[:8]
            output = re.sub(re.escape(str(person)), f"Person-{suffix}", output, flags=re.I)
        if output == item.content:
            raise TransformationError("listed person was not present in content")
        return output

    @staticmethod
    def _generalize_location(item: ContextItem, request: ContextRequest) -> str:
        exact = item.metadata.get("exact_location")
        broader = item.metadata.get("city") or item.metadata.get("country")
        if not exact or not broader or str(exact) not in item.content:
            raise TransformationError("location metadata cannot be safely generalized")
        return item.content.replace(str(exact), str(broader))

    @staticmethod
    def _generalize_date(item: ContextItem, request: ContextRequest) -> str:
        precision = str(item.metadata.get("date_precision", "month"))
        if precision not in {"month", "year"}:
            raise TransformationError("date precision must be month or year")

        def replace(match: re.Match[str]) -> str:
            return match.group(1) if precision == "year" else f"{match.group(1)}-{match.group(2)}"

        output = DATE_RE.sub(replace, item.content)
        if output == item.content:
            raise TransformationError("no exact date was present")
        return output

    @staticmethod
    def _redact_sensitive_attributes(item: ContextItem, request: ContextRequest) -> str:
        output = item.content
        for pattern, label in (
            (EMAIL_RE, "[email removed]"),
            (PHONE_RE, "[phone removed]"),
            (CREDENTIAL_RE, "[credential removed]"),
            (FINANCIAL_RE, "[financial detail removed]"),
            (MEDICAL_RE, "[medical detail removed]"),
        ):
            output = pattern.sub(label, output)
        if output == item.content:
            raise TransformationError("no registered sensitive attribute was found")
        return output

    @staticmethod
    def _aggregate_count(item: ContextItem, request: ContextRequest) -> str:
        group_size = int(item.metadata.get("group_size", 0))
        minimum = int(item.metadata.get("minimum_group_size", 3))
        if group_size < minimum:
            raise TransformationError("aggregate group is below minimum size")
        return str(item.metadata.get("aggregate", group_size))

    @staticmethod
    def _local_answer(item: ContextItem, request: ContextRequest) -> str:
        if item.resource_type != "summary" or not item.metadata.get("generated_locally"):
            raise TransformationError("local_answer requires a locally generated summary")
        return item.content
