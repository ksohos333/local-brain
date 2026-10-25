"""CalDAV client for the local Radicale server.

Two jobs, both scoped to what the brain UI needs:

* read-only month view of VEVENTs from the user's calendars
* full CRUD on VTODOs, so to-dos round-trip to the phone via CalDAV

Deliberately stdlib-only (urllib) so a calendar outage can never break the rest
of the backend on a missing dependency. Credentials come from config, never
from the Radicale htpasswd file at runtime.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET

from icalendar import Calendar, Todo

from local_brain import config

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"

PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    f'<d:propfind xmlns:d="{DAV_NS}" xmlns:c="{CALDAV_NS}">'
    "<d:prop><d:resourcetype/><d:displayname/><c:getctag/>"
    "<c:supported-calendar-component-set/><c:calendar-description/></d:prop>"
    "</d:propfind>"
)

XML_CT = "application/xml; charset=utf-8"

COMP_FILTER = (
    '<?xml version="1.0" encoding="utf-8"?>'
    f'<c:calendar-query xmlns:d="{DAV_NS}" xmlns:c="{CALDAV_NS}">'
    "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
    "<c:filter><c:comp-filter name=\"VCALENDAR\">"
    '<c:comp-filter name="{name}"/>'
    "</c:comp-filter></c:filter>"
    "</c:calendar-query>"
)

RANGE_FILTER = (
    '<?xml version="1.0" encoding="utf-8"?>'
    f'<c:calendar-query xmlns:d="{DAV_NS}" xmlns:c="{CALDAV_NS}">'
    "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
    '<c:filter><c:comp-filter name="VCALENDAR">'
    '<c:comp-filter name="VEVENT">'
    '<c:time-range start="{start}" end="{end}"/>'
    "</c:comp-filter></c:comp-filter></c:filter>"
    "</c:calendar-query>"
)


class CalDAVError(RuntimeError):
    """Radicale refused a request, or answered with something unparseable."""


def _config() -> Dict[str, Any]:
    return {
        "url": str(config.get("caldav.url", "")).rstrip("/"),
        "username": config.get("caldav.username", ""),
        "password": config.get("caldav.password", ""),
        "timeout": float(config.get("caldav.timeout", 10.0)),
        "event_calendars": config.get("caldav.event_calendars", []) or [],
        "todo_calendar": config.get("caldav.todo_calendar", ""),
    }


def is_configured() -> bool:
    cfg = _config()
    return bool(cfg["url"] and cfg["username"])


def _caldav_prefix() -> str:
    """Radicale serves per-user homes at /<username>/."""
    cfg = _config()
    return f"/{cfg['username'].strip('/')}"


def _request(
    method: str,
    path: str,
    *,
    body: Optional[bytes] = None,
    content_type: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, str], bytes]:
    cfg = _config()
    url = cfg["url"] + path
    headers = {"User-Agent": "local-brain-caldav/1.0"}
    if content_type:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)

    # Basic auth. Build it by hand so no userinfo ends up in a logged URL.
    import base64

    token = base64.b64encode(f"{cfg['username']}:{cfg['password']}".encode()).decode()
    headers["Authorization"] = f"Basic {token}"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        # 404/401/403 are normal control-flow signals here, not crashes.
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read()
    except urllib.error.URLError as exc:
        raise CalDAVError(f"cannot reach Radicale at {cfg['url']}: {exc.reason}") from exc


def _resource_name(resource_href: str) -> str:
    """Filename part of an href, percent-decoded.

    Radicale returns hrefs with "@" escaped as %40. Hand the decoded name back
    so resource keys stay readable and stable across list/update/delete.
    """
    return urllib.parse.unquote(resource_href.rstrip("/").rsplit("/", 1)[-1])


def _resource_href(collection: str, resource: str) -> str:
    """Join a collection href and resource name, quoting the file segment."""
    return collection.rstrip("/") + "/" + urllib.parse.quote(resource)


def _parse_multistatus(body: bytes) -> List[Tuple[str, Dict[str, str]]]:
    """Flatten a 207 multistatus response into (href, props) pairs."""
    root = ET.fromstring(body)
    out: List[Tuple[str, Dict[str, str]]] = []
    for resp in root.findall(f"{{{DAV_NS}}}response"):
        href_el = resp.find(f"{{{DAV_NS}}}href")
        if href_el is None or not href_el.text:
            continue
        props: Dict[str, str] = {}
        for propstat in resp.findall(f"{{{DAV_NS}}}propstat"):
            status = (propstat.findtext(f"{{{DAV_NS}}}status") or "").strip()
            if status and " 200 " not in status:
                continue
            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue
            for child in prop:
                tag = child.tag
                if tag == f"{{{DAV_NS}}}resourcetype":
                    props["is_collection"] = (
                        child.find(f"{{{DAV_NS}}}collection") is not None
                    )
                    props["is_calendar"] = (
                        child.find(f"{{{CALDAV_NS}}}calendar") is not None
                    )
                elif tag.endswith("displayname") or tag.endswith("getctag"):
                    props[tag.split("}")[-1]] = (child.text or "").strip()
                elif tag.endswith("calendar-data"):
                    props["calendar_data"] = child.text or ""
        out.append((href_el.text, props))
    return out


# --- discovery ---


def discover_calendars() -> List[Dict[str, Any]]:
    """List the user's CalDAV collections and what component types they hold."""
    prefix = _caldav_prefix()
    status, _, body = _request(
        "PROPFIND",
        f"{prefix}/",
        body=PROPFIND_BODY.encode(),
        content_type=XML_CT,
        # Depth 1 is what makes Radicale return the calendar collections; with
        # no Depth it answers for the principal only and discovery finds nothing.
        extra_headers={"Depth": "1"},
    )
    if status == 401:
        raise CalDAVError("Radicale rejected the stored credentials")
    if status != 207:
        raise CalDAVError(f"PROPFIND returned HTTP {status}")

    calendars = []
    for href, props in _parse_multistatus(body):
        if not props.get("is_calendar"):
            continue
        name = props.get("displayname") or href.strip("/")
        calendars.append(
            {
                "href": href,
                "name": name,
                "ctag": props.get("getctag", ""),
            }
        )
    return calendars


def _component_support(href: str) -> List[str]:
    status, _, body = _request(
        "PROPFIND",
        href,
        body=PROPFIND_BODY.encode(),
        content_type=XML_CT,
        extra_headers={"Depth": "0"},
    )
    if status != 207:
        return []
    root = ET.fromstring(body)
    # <c:comp name="VEVENT"/> carries its value in the name attribute; the
    # element body is empty, so reading .text returns nothing.
    return [
        (el.get("name") or "").upper()
        for el in root.iter(f"{{{CALDAV_NS}}}comp")
        if el.get("name")
    ]


def _resolve_todo_href(name: str) -> str:
    """Map a configured to-do collection name to its href.

    Accepts "todos", "local/todos" and "/local/todos/" alike. Radicale's user
    home is /<username>/, so a name already prefixed with the username would
    otherwise be double-prefixed into /local/local/todos/.
    """
    cfg = _config()
    username = cfg["username"].strip("/")
    cleaned = name.strip("/")
    if cleaned == username:
        cleaned = "todos"
    elif cleaned.startswith(f"{username}/"):
        cleaned = cleaned[len(username) + 1:]

    calendars = discover_calendars()
    for candidate in (cleaned, f"{username}/{cleaned}"):
        for cal in calendars:
            # A displayname can differ from the folder, so match the href too.
            if candidate in (cal["name"], cal["href"].strip("/")):
                return cal["href"]
    return f"{_caldav_prefix()}/{cleaned}/"


def resolve_collections() -> Tuple[List[str], Optional[str]]:
    """Work out which collections to read events from and to write to-dos to.

    Explicit config wins. Otherwise fall back to: every calendar that supports
    VEVENT for reads, and any calendar that supports VTODO for writes.
    """
    cfg = _config()
    calendars = discover_calendars()
    by_name = {c["name"]: c["href"] for c in calendars}

    configured_events = [by_name[n] for n in cfg["event_calendars"] if n in by_name]
    read_from = configured_events or [c["href"] for c in calendars]

    if cfg["todo_calendar"]:
        # An explicitly named collection is honoured even when it does not exist
        # yet; ensure_todo_collection() creates it. Falling back to auto-detect
        # here would silently ignore the setting.
        todo_calendar = _resolve_todo_href(cfg["todo_calendar"])
    else:
        todo_calendar = None
        for cal in calendars:
            if "VTODO" in _component_support(cal["href"]):
                todo_calendar = cal["href"]
                break
    return read_from, todo_calendar


def ensure_todo_collection() -> str:
    """Find, or create, a collection that accepts VTODO components.

    Radicale ships empty calendars that advertise only VEVENT. Rather than
    force to-dos into a calendar the phone will render as events, keep them in
    their own collection.

    The href from resolve_collections() may be a computed path rather than a
    discovered one, so existence is verified here rather than inferred from
    discovery -- otherwise the first PUT lands on a missing parent and Radicale
    answers 409.
    """
    _, todo_calendar = resolve_collections()
    if not todo_calendar:
        raise CalDAVError("no writable to-do collection could be determined")

    status, _, _ = _request(
        "PROPFIND",
        todo_calendar,
        body=PROPFIND_BODY.encode(),
        content_type=XML_CT,
        extra_headers={"Depth": "0"},
    )
    if status == 207:
        return todo_calendar

    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<c:mkcalendar xmlns:d="{DAV_NS}" xmlns:c="{CALDAV_NS}">'
        "<d:set><d:prop>"
        "<d:displayname>To-dos</d:displayname>"
        "<c:supported-calendar-component-set>"
        '<c:comp name="VTODO"/><c:comp name="VEVENT"/>'
        "</c:supported-calendar-component-set>"
        "</d:prop></d:set></c:mkcalendar>"
    ).encode()
    status, _, resp_body = _request(
        "MKCALENDAR", todo_calendar, body=body, content_type=XML_CT
    )
    if status not in (200, 201):
        raise CalDAVError(
            f"could not create to-do collection {todo_calendar}: "
            f"HTTP {status} {resp_body[:200]!r}"
        )
    return todo_calendar


# --- parsing helpers ---


def _as_iso(value: Any) -> Optional[str]:
    """Normalise an icalendar date/datetime property to an ISO string."""
    dt = getattr(value, "dt", value)
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=None)
        return dt.isoformat()
    if isinstance(dt, date):
        return dt.isoformat()
    return str(dt)


def _ics_lines(text: str) -> str:
    """Normalise a multistatus calendar-data blob to CRLF line endings."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _iter_components(blob: str, name: str) -> Iterable[Any]:
    try:
        cal = Calendar.from_ical(blob)
    except Exception:
        return
    for component in cal.walk(name):
        yield component


def _unescape(text: str) -> str:
    return (
        text.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _text_of(component: Any, key: str) -> str:
    raw = component.get(key)
    if raw is None:
        return ""
    return _unescape(str(raw))


# --- read: events ---


def _naive(value: Any) -> Any:
    """Drop tzinfo, keeping wall time.

    Radicale mixes floating (naive) and TZID-qualified (aware) dates, and the
    month window arrives naive from the UI. Comparing those directly raises
    TypeError. The month grid is a wall-clock view, so normalising to naive is
    both safe and what the front end wants.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _expand_event(component: Any, window_start: datetime, window_end: datetime) -> List[Tuple[Optional[datetime], Optional[datetime]]]:
    """Yield (start, end) occurrences that fall inside the window.

    Non-recurring events yield themselves. Recurring ones are expanded with
    dateutil so a weekly standup shows on every week of the month rather than
    only on the week it was created.
    """
    dtstart_prop = component.get("dtstart")
    dtstart = getattr(dtstart_prop, "dt", None) if dtstart_prop is not None else None
    if dtstart is None:
        return []

    all_day = not isinstance(dtstart, datetime)
    dtstart = _naive(dtstart)

    duration = component.get("duration")
    if duration is not None:
        delta = getattr(duration, "dt", duration)
        delta = _naive(delta) if isinstance(delta, timedelta) else delta
    else:
        dtend_prop = component.get("dtend")
        dtend = getattr(dtend_prop, "dt", None) if dtend_prop is not None else None
        dtend = _naive(dtend) if isinstance(dtend, datetime) else dtend
        delta = (dtend - dtstart) if isinstance(dtend, datetime) else None

    rrule_prop = component.get("rrule")

    def end_of(start: Any) -> Any:
        if delta is None:
            return start
        try:
            return start + delta
        except TypeError:
            return start

    if rrule_prop is None:
        probe_start = datetime.combine(dtstart, datetime.min.time()) if all_day else dtstart
        probe_end = end_of(probe_start)
        # All-day DTEND is exclusive, so pad by a day to keep single-day
        # events from reading as zero-length and dropping out of the window.
        if all_day and probe_end == probe_start:
            probe_end = probe_start + timedelta(days=1)
        if probe_end > window_start and probe_start < window_end:
            return [(dtstart, end_of(dtstart))]
        return []

    try:
        from dateutil.rrule import rrulestr

        rule = rrulestr(rrule_prop.to_ical().decode())
        occurrences = [
            (_naive(start), end_of(_naive(start)))
            for start in rule.between(window_start, window_end, inc=True)
        ]
        if not occurrences:
            # An RRULE does not have to include the original DTSTART.
            fallback = datetime.combine(dtstart, datetime.min.time()) if all_day else dtstart
            if fallback < window_end and end_of(fallback) > window_start:
                occurrences = [(dtstart, end_of(dtstart))]
        return occurrences
    except Exception:
        return []


def fetch_events(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """All events overlapping [start, end) across the user's calendars."""
    stamp = "%Y%m%dT%H%M%SZ"
    body = RANGE_FILTER.format(start=start.strftime(stamp), end=end.strftime(stamp)).encode()

    read_from, _ = resolve_collections()
    events: List[Dict[str, Any]] = []
    # Radicale does not stamp the source calendar onto each object, so carry the
    # href -> name mapping through and report it per event.
    names = {c["href"]: c["name"] for c in discover_calendars()}

    for href in read_from:
        status, _, resp = _request(
            "REPORT", href, body=body, content_type=XML_CT,
            extra_headers={"Depth": "1"},
        )
        if status != 207:
            continue
        for resource_href, props in _parse_multistatus(resp):
            blob = props.get("calendar_data")
            if not blob:
                continue
            name = _resource_name(resource_href)
            calendar_name = names.get(href, href.strip("/"))
            for component in _iter_components(_ics_lines(blob), "VEVENT"):
                if str(component.get("status", "")).upper() == "CANCELLED":
                    continue
                summary = _text_of(component, "summary")
                location = _text_of(component, "location")
                for occ_start, occ_end in _expand_event(component, start, end):
                    events.append(
                        {
                            "uid": str(component.get("uid", "")),
                            "resource": name,
                            "calendar": calendar_name,
                            "summary": summary or "(no title)",
                            "location": location,
                            "start": _as_iso(occ_start),
                            "end": _as_iso(occ_end),
                            "all_day": not isinstance(
                                getattr(occ_start, "dt", occ_start), datetime
                            ),
                            "description": _text_of(component, "description")[:500],
                        }
                    )
    events.sort(key=lambda e: (e["start"] or "", e["summary"]))
    return events


# --- read/write: todos ---


def _todo_to_dict(todo: Any, resource: str, etag: str = "") -> Dict[str, Any]:
    due = todo.get("due")
    created = todo.get("created")
    completed = todo.get("completed")
    percent = todo.get("percent-complete")
    status = str(todo.get("status", "NEEDS-ACTION")).upper()
    return {
        "uid": str(todo.get("uid", "")),
        "resource": resource,
        "etag": etag,
        "summary": _text_of(todo, "summary"),
        "description": _text_of(todo, "description"),
        "due": _as_iso(due) if due is not None else None,
        "priority": int(todo["priority"]) if todo.get("priority") is not None else None,
        "status": status,
        "done": status == "COMPLETED" or str(percent) == "100",
        "percent": int(percent) if percent is not None else None,
        "created": _as_iso(created) if created is not None else None,
        "completed": _as_iso(completed) if completed is not None else None,
    }


def _fetch_todos_from(href: str) -> List[Dict[str, Any]]:
    status, _, resp = _request(
        "REPORT",
        href,
        body=COMP_FILTER.format(name="VTODO").encode(),
        content_type=XML_CT,
        extra_headers={"Depth": "1"},
    )
    if status != 207:
        return []
    out = []
    for resource_href, props in _parse_multistatus(resp):
        blob = props.get("calendar_data")
        if not blob:
            continue
        name = _resource_name(resource_href)
        for todo in _iter_components(_ics_lines(blob), "VTODO"):
            out.append(_todo_to_dict(todo, name, props.get("getetag", "")))
    return out


def list_todos() -> List[Dict[str, Any]]:
    collection = ensure_todo_collection()
    todos = _fetch_todos_from(collection)
    # Open items first, then by due date, undated last.
    todos.sort(key=lambda t: (t["done"], t["due"] is None, t["due"] or "", t["summary"].lower()))
    return todos


def _build_ics(todo_dict: Dict[str, Any], prodid: str = "-//local-brain//VTODO//EN") -> bytes:
    cal = Calendar()
    cal.add("prodid", prodid)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")

    todo = Todo()
    todo.add("uid", todo_dict["uid"])
    todo.add("dtstamp", datetime.now())
    todo.add("summary", todo_dict.get("summary") or "")
    if todo_dict.get("description"):
        todo.add("description", todo_dict["description"])
    if todo_dict.get("due"):
        due = todo_dict["due"]
        todo.add("due", date.fromisoformat(due) if len(due) == 10 else datetime.fromisoformat(due))
    if todo_dict.get("priority"):
        todo.add("priority", todo_dict["priority"])
    todo.add("status", "COMPLETED" if todo_dict.get("done") else "NEEDS-ACTION")
    if todo_dict.get("done"):
        todo.add("percent-complete", 100)
        todo.add("completed", datetime.now())
    else:
        todo.add("percent-complete", 0)
    if todo_dict.get("created"):
        # CREATED is a DATE-TIME property; icalendar rejects a bare ISO string.
        created = todo_dict["created"]
        try:
            todo.add(
                "created",
                datetime.fromisoformat(created)
                if len(created) > 10
                else datetime.combine(date.fromisoformat(created), datetime.min.time()),
            )
        except (TypeError, ValueError):
            pass

    cal.add_component(todo)
    return cal.to_ical()


def create_todo(
    summary: str,
    due: Optional[str] = None,
    description: str = "",
    priority: Optional[int] = None,
) -> Dict[str, Any]:
    if not summary or not summary.strip():
        raise CalDAVError("a to-do needs a summary")

    collection = ensure_todo_collection()
    uid = f"{uuid.uuid4().hex}@local-brain"
    payload = {
        "uid": uid,
        "summary": summary.strip(),
        "description": description or "",
        "due": due,
        "priority": priority,
        "done": False,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    href = _resource_href(collection, uid + ".ics")
    status, _, body = _request(
        "PUT",
        href,
        body=_build_ics(payload),
        content_type="text/calendar; charset=utf-8",
        extra_headers={"If-None-Match": "*"},
    )
    if status not in (200, 201, 204):
        raise CalDAVError(f"Radicale refused the to-do: HTTP {status} {body[:200]!r}")
    return {**payload, "resource": uid + ".ics"}


def _current_ics(collection: str, resource: str) -> Tuple[Dict[str, Any], str]:
    href = _resource_href(collection, resource)
    status, headers, body = _request("GET", href)
    if status != 200:
        raise CalDAVError(f"to-do {resource} not found (HTTP {status})")
    todos = list(_iter_components(_ics_lines(body.decode("utf-8", "replace")), "VTODO"))
    if not todos:
        raise CalDAVError(f"{resource} contains no VTODO")
    etag = headers.get("etag", "").strip('"')
    return _todo_to_dict(todos[0], resource, etag), body


def update_todo(
    resource: str,
    summary: Optional[str] = None,
    due: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[int] = None,
    done: Optional[bool] = None,
) -> Dict[str, Any]:
    collection = ensure_todo_collection()
    current, _ = _current_ics(collection, resource)
    merged = {
        "uid": current["uid"],
        "summary": summary if summary is not None else current["summary"],
        "description": description if description is not None else current["description"],
        "due": due if due is not None else current["due"],
        "priority": priority if priority is not None else current["priority"],
        "done": done if done is not None else current["done"],
        "created": current["created"],
    }
    href = _resource_href(collection, resource)
    status, _, body = _request(
        "PUT",
        href,
        body=_build_ics(merged),
        content_type="text/calendar; charset=utf-8",
    )
    if status not in (200, 201, 204):
        raise CalDAVError(f"Radicale refused the update: HTTP {status} {body[:200]!r}")
    return {**merged, "resource": resource}


def delete_todo(resource: str) -> None:
    collection = ensure_todo_collection()
    status, _, body = _request("DELETE", _resource_href(collection, resource))
    if status not in (200, 204):
        raise CalDAVError(f"Radicale refused the delete: HTTP {status} {body[:200]!r}")


def health() -> Dict[str, Any]:
    """Report reachability without leaking anything about the credentials."""
    if not is_configured():
        return {"ok": False, "error": "not configured"}
    try:
        calendars = discover_calendars()
        _, todo_calendar = resolve_collections()
        return {
            "ok": True,
            "url": _config()["url"],
            "calendars": [c["name"] for c in calendars],
            "todo_calendar": todo_calendar,
        }
    except CalDAVError as exc:
        return {"ok": False, "url": _config()["url"], "error": str(exc)}