"""Portable account/device archive import.

The connector is intentionally export-first: Apple Health, Google Takeout,
Spotify extended history, and other services expose fuller histories in
download archives than their day-to-day APIs.  A ZIP stays local and is read
in place; recognized high-volume records are compacted by day.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar
from xml.etree import ElementTree

from varyz.connectors.base import SkipItem, register
from varyz.connectors.documents import EXTENSIONS as DOCUMENT_EXTENSIONS
from varyz.connectors.documents import UnreadableDocument, extract_document_text
from varyz.domain import Cursor, Kind, NormalizedEntry, RawItem, TimePrecision

log = logging.getLogger(__name__)


@register
class PersonalArchiveConnector:
    connector_id: ClassVar[str] = "personal_archive"

    def discover(
        self, options: dict[str, Any], cursor: Cursor | None
    ) -> Iterator[tuple[RawItem, Cursor | None]]:
        if not options.get("approved", False):
            raise ValueError("personal archives require explicit approval in Varyz Imports")
        for raw_path in options.get("paths", []):
            path = Path(raw_path)
            if not path.is_file():
                continue
            archive_key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
            if zipfile.is_zipfile(path):
                try:
                    with zipfile.ZipFile(path) as archive:
                        members = [
                            member
                            for member in archive.infolist()
                            if not member.is_dir() and member.file_size <= 250 * 1024 * 1024
                        ]
                        manifest = {
                            "record_type": "archive",
                            "path": str(path.resolve()),
                            "members": [member.filename for member in members[:2000]],
                            "member_count": len(members),
                        }
                        yield _item(
                            f"{archive_key}:manifest",
                            json.dumps(manifest, ensure_ascii=False),
                            ".json",
                            manifest,
                        )
                        for member in members:
                            try:
                                raw = archive.read(member)
                            except (OSError, RuntimeError, zipfile.BadZipFile):
                                continue
                            yield from _member_items(
                                archive_key,
                                member.filename,
                                raw,
                            )
                except (OSError, zipfile.BadZipFile):
                    continue
            else:
                try:
                    raw = path.read_bytes()
                except OSError:
                    continue
                yield from _member_items(archive_key, path.name, raw)

    def normalize(self, item: RawItem) -> NormalizedEntry:
        metadata = item.metadata
        record_type = metadata.get("record_type", "document")
        if record_type == "health_day":
            body = _health_body(metadata)
            occurred = _day(metadata["day"])
            return NormalizedEntry(
                kind=Kind.NOTE,
                external_id=item.external_id,
                title=f"Health — {metadata['day']}",
                body=body,
                occurred_start=occurred,
                time_precision=TimePrecision.DAY,
                significance=0.18,
            )
        if record_type == "activity_day":
            body = _activity_body(metadata)
            occurred = _day(metadata["day"])
            return NormalizedEntry(
                kind=Kind.NOTE,
                external_id=item.external_id,
                title=f"{metadata['source']} activity — {metadata['day']}",
                body=body,
                occurred_start=occurred,
                time_precision=TimePrecision.DAY,
                significance=0.10,
            )
        if record_type == "location":
            occurred = _parse_time(metadata.get("start"))
            body = "\n".join(
                line
                for line in (
                    f"Place: {metadata.get('name')}" if metadata.get("name") else "",
                    f"Address: {metadata.get('address')}" if metadata.get("address") else "",
                    f"End: {metadata.get('end')}" if metadata.get("end") else "",
                    f"Source file: {metadata.get('member')}",
                )
                if line
            )
            return NormalizedEntry(
                kind=Kind.NOTE,
                external_id=item.external_id,
                title=f"Visited {metadata.get('name') or 'a place'}",
                body=body,
                occurred_start=occurred,
                lat=metadata.get("lat"),
                lon=metadata.get("lon"),
                significance=0.35,
            )
        if record_type == "archive":
            members = metadata.get("members", [])
            body = (
                f"Local archive: {metadata.get('path')}\n"
                f"Files: {metadata.get('member_count', len(members))}\n\n"
                + "\n".join(f"- {name}" for name in members[:300])
            )
            return NormalizedEntry(
                kind=Kind.DOCUMENT,
                external_id=item.external_id,
                title=Path(str(metadata.get("path", "Personal archive"))).name,
                body=body,
                significance=0.08,
            )
        member = str(metadata.get("member", "Imported document"))
        try:
            body = extract_document_text(item.payload_bytes(), item.payload_ext)
        except UnreadableDocument as exc:
            # An export archive is assembled by somebody else's exporter and
            # routinely carries a member that is not the format its name
            # claims. One of those must cost the reader one line, not a
            # third-party traceback and an error count that never clears.
            log.warning("skipping %s inside the archive: %s", member, exc)
            raise SkipItem(item.external_id) from None
        return NormalizedEntry(
            kind=Kind.DOCUMENT,
            external_id=item.external_id,
            title=PurePosixPath(member).stem,
            body=body,
            occurred_start=_parse_time(metadata.get("occurred")),
            significance=0.35,
        )


def _member_items(
    archive_key: str, member_name: str, raw: bytes
) -> Iterator[tuple[RawItem, Cursor | None]]:
    lowered = member_name.lower()
    if lowered.endswith("export.xml") and b"<HealthData" in raw[:4096]:
        for day, summary in _apple_health(raw).items():
            metadata = {
                "record_type": "health_day",
                "source": "Apple Health",
                "member": member_name,
                "day": day,
                **summary,
            }
            yield _item(
                f"{archive_key}:health:{day}",
                json.dumps(metadata, ensure_ascii=False),
                ".json",
                metadata,
            )
        return

    if lowered.endswith(".json"):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
        if data is not None:
            locations = _location_records(data, member_name)
            if locations:
                for index, metadata in enumerate(locations):
                    stable = metadata.get("start") or str(index)
                    yield _item(
                        f"{archive_key}:location:{hashlib.sha256(str(stable).encode()).hexdigest()[:20]}",
                        json.dumps(metadata, ensure_ascii=False),
                        ".json",
                        metadata,
                    )
                return
            activities = _activity_records(data, member_name)
            if activities:
                for day, records in sorted(activities.items()):
                    source = _activity_source(member_name)
                    metadata = {
                        "record_type": "activity_day",
                        "source": source,
                        "member": member_name,
                        "day": day,
                        "records": records,
                    }
                    yield _item(
                        f"{archive_key}:activity:{source}:{day}",
                        json.dumps(metadata, ensure_ascii=False),
                        ".json",
                        metadata,
                    )
                return

    ext = PurePosixPath(member_name).suffix.lower()
    if ext in DOCUMENT_EXTENSIONS and len(raw) <= 50 * 1024 * 1024:
        metadata = {
            "record_type": "document",
            "member": member_name,
        }
        yield (
            RawItem(
                external_id=f"{archive_key}:file:{member_name}",
                payload=raw,
                payload_ext=ext,
                metadata=metadata,
            ),
            None,
        )


def _apple_health(raw: bytes) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"metrics": {}, "workouts": Counter()}
    )
    for _, element in ElementTree.iterparse(io.BytesIO(raw), events=("end",)):
        if element.tag == "Record":
            start = element.attrib.get("startDate")
            value = element.attrib.get("value")
            metric = _short_health_type(element.attrib.get("type", "Health metric"))
            parsed = _parse_time(start)
            if parsed and value is not None:
                metrics = summaries[parsed.date().isoformat()]["metrics"]
                try:
                    numeric = float(value)
                except ValueError:
                    summary = metrics.setdefault(
                        metric, {"count": 0, "values": Counter()}
                    )
                    summary["count"] += 1
                    summary["values"][str(value)] += 1
                else:
                    summary = metrics.setdefault(
                        metric,
                        {
                            "count": 0,
                            "sum": 0.0,
                            "min": numeric,
                            "max": numeric,
                        },
                    )
                    summary["count"] += 1
                    summary["sum"] += numeric
                    summary["min"] = min(summary["min"], numeric)
                    summary["max"] = max(summary["max"], numeric)
        elif element.tag == "Workout":
            start = _parse_time(element.attrib.get("startDate"))
            workout = _short_health_type(element.attrib.get("workoutActivityType", "Workout"))
            duration = float(element.attrib.get("duration", 0) or 0)
            if start:
                summaries[start.date().isoformat()]["workouts"][workout] += round(duration, 2)
        element.clear()
    return {
        day: {
            "metrics": {
                metric: _finish_health_metric(metric_summary)
                for metric, metric_summary in summary["metrics"].items()
            },
            "workouts": dict(summary["workouts"]),
        }
        for day, summary in summaries.items()
    }


def _finish_health_metric(summary: dict[str, Any]) -> dict[str, Any]:
    if "sum" in summary:
        return {
            "count": summary["count"],
            "sum": round(summary["sum"], 3),
            "average": round(summary["sum"] / summary["count"], 3),
            "min": round(summary["min"], 3),
            "max": round(summary["max"], 3),
        }
    return {
        "count": summary["count"],
        "values": dict(summary["values"].most_common(8)),
    }


def _location_records(data: Any, member: str) -> list[dict[str, Any]]:
    objects = data.get("timelineObjects", []) if isinstance(data, dict) else []
    segments = data.get("semanticSegments", []) if isinstance(data, dict) else []
    output = []
    for obj in objects:
        visit = obj.get("placeVisit")
        if not visit:
            continue
        location = visit.get("location", {})
        duration = visit.get("duration", {})
        output.append(
            {
                "record_type": "location",
                "member": member,
                "name": location.get("name"),
                "address": location.get("address"),
                "lat": _e7(location.get("latitudeE7")),
                "lon": _e7(location.get("longitudeE7")),
                "start": duration.get("startTimestamp")
                or _millis_iso(duration.get("startTimestampMs")),
                "end": duration.get("endTimestamp")
                or _millis_iso(duration.get("endTimestampMs")),
            }
        )
    for segment in segments:
        visit = segment.get("visit")
        if not visit:
            continue
        top = (visit.get("topCandidate") or {}).get("placeLocation", {})
        geo_lat, geo_lon = _geo_pair(top.get("latLng"))
        latitude = _geo_value(top, "latitude")
        longitude = _geo_value(top, "longitude")
        output.append(
            {
                "record_type": "location",
                "member": member,
                "name": (visit.get("topCandidate") or {}).get("placeId"),
                "address": top.get("address"),
                "lat": latitude if latitude is not None else geo_lat,
                "lon": longitude if longitude is not None else geo_lon,
                "start": segment.get("startTime"),
                "end": segment.get("endTime"),
            }
        )
    return [record for record in output if record.get("start")]


def _geo_pair(value: Any) -> tuple[float | None, float | None]:
    if not isinstance(value, str):
        return None, None
    cleaned = value.removeprefix("geo:").strip()
    try:
        latitude, longitude = cleaned.split(",", 1)
        return float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None, None


def _activity_records(data: Any, member: str) -> dict[str, list[dict[str, Any]]]:
    records = data if isinstance(data, list) else []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if not isinstance(record, dict):
            continue
        when = (
            record.get("time")
            or record.get("ts")
            or record.get("endTime")
            or record.get("played_at")
        )
        parsed = _parse_time(str(when)) if when else None
        if not parsed:
            continue
        title = (
            record.get("title")
            or record.get("master_metadata_track_name")
            or record.get("name")
        )
        if not title:
            continue
        grouped[parsed.date().isoformat()].append(
            {
                "at": parsed.isoformat(),
                "title": str(title)[:500],
                "artist": record.get("master_metadata_album_artist_name"),
                "url": record.get("titleUrl") or record.get("url"),
                "milliseconds": record.get("ms_played"),
                "details": record.get("details"),
            }
        )
    return grouped


def _activity_source(member: str) -> str:
    lowered = member.lower()
    if "spotify" in lowered or "streaming_history" in lowered or "endsong" in lowered:
        return "Spotify"
    if "youtube" in lowered or "watch-history" in lowered:
        return "YouTube"
    if "my activity" in lowered or "myactivity" in lowered:
        return "Google"
    return PurePosixPath(member).stem[:60] or "Account"


def _activity_body(metadata: dict[str, Any]) -> str:
    records = metadata.get("records", [])
    titles = Counter(
        (
            record.get("title", ""),
            record.get("artist") or "",
            record.get("url") or "",
        )
        for record in records
    )
    total_ms = sum(int(record.get("milliseconds") or 0) for record in records)
    lines = [
        f"Source: {metadata.get('source')}",
        f"Activities: {len(records)}",
    ]
    if total_ms:
        lines.append(f"Listening time: {round(total_ms / 60000, 1)} minutes")
    lines.append("")
    for (title, artist, url), count in titles.most_common(100):
        label = title
        if artist:
            label += f" — {artist}"
        if url:
            label += f" — {url}"
        if count > 1:
            label += f" · {count} times"
        lines.append(f"- {label}")
    return "\n".join(lines)


def _health_body(metadata: dict[str, Any]) -> str:
    lines = ["Source: Apple Health export"]
    for metric, summary in sorted(metadata.get("metrics", {}).items()):
        if "average" in summary:
            lines.append(
                f"{metric}: total {summary['sum']}, avg {summary['average']}, "
                f"min {summary['min']}, "
                f"max {summary['max']}, {summary['count']} records"
            )
        else:
            lines.append(f"{metric}: {summary.get('values')} ({summary.get('count')} records)")
    for workout, minutes in sorted(metadata.get("workouts", {}).items()):
        lines.append(f"Workout — {workout}: {minutes} minutes")
    return "\n".join(lines)


def _item(
    external_id: str,
    payload: str,
    ext: str,
    metadata: dict[str, Any],
) -> tuple[RawItem, None]:
    return (
        RawItem(
            external_id=external_id,
            payload=payload,
            payload_ext=ext,
            metadata=metadata,
        ),
        None,
    )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    # Apple Health: "2026-01-02 12:30:00 +0200"
    normalized = re.sub(r" ([+-]\d{2})(\d{2})$", r"\1:\2", normalized)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _day(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _short_health_type(value: str) -> str:
    return value.removeprefix("HKQuantityTypeIdentifier").removeprefix(
        "HKCategoryTypeIdentifier"
    ).removeprefix("HKWorkoutActivityType")


def _e7(value: Any) -> float | None:
    return round(float(value) / 10_000_000, 7) if value is not None else None


def _geo_value(location: dict[str, Any], name: str) -> float | None:
    value = location.get(name)
    if value is None:
        return None
    return float(value)


def _millis_iso(value: Any) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()
