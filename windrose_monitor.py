"""Windrose dedicated-server player monitor.

Tails R5.log, parses the periodic state-table dumps, maintains a live roster
of currently-connected players, serves a small web page, and posts join/leave
notifications to a Discord webhook (optional).

Standard library only.
    python windrose_monitor.py --log path/to/R5.log --webhook https://discord.com/api/webhooks/...
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import queue
import random
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib.parse import unquote, urlsplit
from urllib import request as urlrequest

log = logging.getLogger("windrose_monitor")


SECTION_HEADERS = ("Connected Accounts", "Reserved Accounts", "Disconnected Accounts")

JOIN_MESSAGES = [
    "Ahoy! {name} has boarded!",
    "{name} joined the crew, yarr!",
    "Welcome aboard, {name}!",
    "{name} is ready for booty!",
    "A new swashbuckler, {name}, joins the fray!",
]

LEAVE_MESSAGES = [
    "{name} walked the plank!",
    "{name} jumped ship!",
    "{name} is off to bury gold!",
    "{name} abandoned the fleet!",
    "{name} sailed into the sunset!",
]

ROW_RE = re.compile(
    r"^\s*\d+\.\s+"
    r"Name '([^']*)'\.\s+"
    r"AccountId '([^']+)'\.\s+"
    r"State '([^']+)'\.\s+"
    r"NetAddress '(?:R5:)?([^']*)'"
)

LOG_LINE_RE = re.compile(r"^\[\d{4}\.\d{2}\.\d{2}-")
FOOTER_RE = re.compile(r"^\s*\[D:\\Source")
TIME_IN_GAME_RE = re.compile(r"TimeInGame\s+(\+?\d{2}:\d{2}:\d{2}\.\d+)")
CONNECTED_IN_RE = re.compile(r"Connected in\s+(\+?\d{2}:\d{2}:\d{2}\.\d+)")
TIME_ON_SERVER_RE = re.compile(r"TimeOnServer\s+(\+?\d{2}:\d{2}:\d{2}\.\d+)")
RESERVE_MOMENT_RE = re.compile(r"ReserveMoment\s+(\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2})")
FAREWELL_RE = re.compile(r"FarewellReason\s+(.+?)\s*$")
# Detects the SaveServerDescription log line that contains server config JSON
SERVER_DESC_RE = re.compile(r"R5LogCoopProxy.*SaveServerDescription.*Saved server description")

SERVER_TZ = timezone.utc
DEFAULT_STATE_PATH = "player_state.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML_PATH = os.path.join(BASE_DIR, "index.html")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")


def _parse_log_time(s: str) -> datetime | None:
    # Strip ":NNN" milliseconds suffix if present (log format: "2026.04.27-18.42.40:012")
    colon = s.find(":")
    if colon != -1:
        s = s[:colon]
    try:
        naive = datetime.strptime(s, "%Y.%m.%d-%H.%M.%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=SERVER_TZ)


def _parse_duration(s: str | None) -> timedelta | None:
    if not s:
        return None
    s = s.lstrip("+")
    try:
        h, m, sec = s.split(":")
        return timedelta(hours=int(h), minutes=int(m), seconds=float(sec))
    except (ValueError, AttributeError):
        return None


def _human_duration(delta: timedelta) -> str:
    """Return a bare duration string: '45 min', '2 hr 15 min', '1 day 3 hr'."""
    seconds = max(0.0, delta.total_seconds())
    if seconds < 60:
        return "< 1 min"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = int(seconds // 3600)
    mins_rem = int((seconds % 3600) // 60)
    if hours < 24:
        parts = [f"{hours} hr"]
        if mins_rem:
            parts.append(f"{mins_rem} min")
        return " ".join(parts)
    days = int(seconds // 86400)
    hrs_rem = int((seconds % 86400) // 3600)
    parts = [f"{days} day{'s' if days != 1 else ''}"]
    if hrs_rem:
        parts.append(f"{hrs_rem} hr")
    return " ".join(parts)


def _human_ago(delta: timedelta) -> str:
    dur = _human_duration(delta)
    return "just now" if dur == "< 1 min" else f"{dur} ago"


def _format_timedelta(td: timedelta) -> str:
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60

    parts = []
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} min{'s' if minutes != 1 else ''}")

    if not parts:
        return "0 mins" if total_seconds == 0 else "< 1 min"
    return " ".join(parts)


def _format_duration_str(s: str | None) -> str:
    if not s:
        return ""
    td = _parse_duration(s)
    if td is None:
        return s
    return _format_timedelta(td)


def _parse_iso_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _json_ready_row(row: dict) -> dict:
    out = dict(row)
    for key, value in list(out.items()):
        if isinstance(value, datetime):
            out[key] = value.astimezone(timezone.utc).isoformat()
    return out


class Roster:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._players: dict[str, dict] = {}

    def snapshot(self) -> tuple[list[dict], str]:
        with self._lock:
            return list(self._players.values()), datetime.now(timezone.utc).isoformat()

    def apply(self, connected: list[dict], as_of: datetime | None) -> tuple[list[dict], list[dict]]:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            prev = self._players
            new: dict[str, dict] = {}
            joined: list[dict] = []
            for row in connected:
                name = row["name"]
                row_with_meta = {**row, "as_of": as_of}
                if name in prev:
                    new[name] = {**row_with_meta, "joined_at": prev[name]["joined_at"]}
                else:
                    new[name] = {**row_with_meta, "joined_at": now_iso}
                    joined.append(new[name])
            left = [prev[name] for name in prev if name not in new]
            self._players = new
            return joined, left


class KnownPlayers:
    """Tracks the last time we observed each player leave the server.

    Populated from the Disconnected section of every dump (so it backfills
    naturally from log history when the script starts mid-session). Most
    recent disconnect per name wins. Keyed by name rather than account_id
    because account_id can rotate between sessions after server upgrades.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._players: dict[str, dict] = {}

    def load(self, rows: list[dict]) -> int:
        loaded = 0
        with self._lock:
            for row in rows:
                name = row.get("name")
                if not name:
                    continue
                parsed = dict(row)
                parsed["left_at"] = _parse_iso_datetime(row.get("left_at"))
                self._players[name] = parsed
                loaded += 1
        return loaded

    def update_from_disconnected(self, rows: list[dict]) -> bool:
        changed = False
        with self._lock:
            for row in rows:
                name = row.get("name")
                if not name:
                    continue
                left_at = row.get("left_at")
                existing = self._players.get(name)
                if existing and existing.get("left_at") and left_at and existing["left_at"] >= left_at:
                    continue  # we already have a more recent record
                if existing and existing.get("left_at") and left_at is None:
                    continue  # avoid replacing a timestamped record with incomplete log data
                self._players[name] = {
                    "name": name,
                    "account_id": row.get("account_id"),
                    "session_id": row.get("session_id"),
                    "state": row.get("state"),
                    "time_in_game": row.get("time_in_game"),
                    "farewell_reason": row.get("farewell_reason"),
                    "left_at": left_at,  # aware datetime or None
                }
                changed = True
        return changed

    def record_left(self, row: dict, left_at: datetime | None, played_raw: str | None) -> bool:
        name = row.get("name")
        if not name:
            return False
        with self._lock:
            existing = self._players.get(name)
            if existing and existing.get("left_at") and left_at and existing["left_at"] >= left_at:
                return False
            self._players[name] = {
                "name": name,
                "account_id": row.get("account_id"),
                "session_id": row.get("session_id"),
                "state": "left",
                "time_in_game": played_raw,
                "farewell_reason": "left",
                "left_at": left_at,
            }
        return True

    def snapshot(self, exclude_names: set[str] | None = None) -> list[dict]:
        exclude = exclude_names or set()
        with self._lock:
            rows = [p for name, p in self._players.items() if name not in exclude]
        rows.sort(key=lambda p: p.get("left_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows

    def to_json_rows(self) -> list[dict]:
        with self._lock:
            rows = [_json_ready_row(p) for p in self._players.values()]
        rows.sort(key=lambda p: p.get("left_at") or "", reverse=True)
        return rows


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = path

    def load_known_players(self, known: KnownPlayers) -> int:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return 0
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Could not load state file %s: %s", self.path, e)
            return 0

        rows = data.get("recently_seen", [])
        if not isinstance(rows, list):
            log.warning("State file %s has invalid recently_seen data; ignoring", self.path)
            return 0
        return known.load(rows)

    def save_known_players(self, known: KnownPlayers) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        payload = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "recently_seen": known.to_json_rows(),
        }
        tmp_path = f"{self.path}.tmp"
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, self.path)
        except OSError as e:
            log.warning("Could not save state file %s: %s", self.path, e)


class DiscordNotifier:
    def __init__(self, webhook_url: str | None) -> None:
        self.webhook_url = webhook_url
        self.quiet = False  # when True, post() is dropped (used during bootstrap replay)
        self.q: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        if webhook_url:
            threading.Thread(target=self._worker, name="discord", daemon=True).start()

    def post(self, content: str) -> None:
        if self.webhook_url and not self.quiet:
            self.q.put(content)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                content = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                payload = json.dumps({"content": content}).encode("utf-8")
                req = urlrequest.Request(
                    self.webhook_url,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "WindroseMonitor/1.0 (Python-urllib)",
                    },
                )
                with urlrequest.urlopen(req, timeout=5) as resp:
                    pass  # success
            except urlerror.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                log.warning("Discord webhook failed: %s - %s", e, body)
            except urlerror.URLError as e:
                log.warning("Discord webhook failed: %s", e)
            except Exception:
                log.exception("Discord notifier error")


class DumpParser:
    def __init__(
        self,
        roster: Roster,
        known: KnownPlayers,
        notifier: DiscordNotifier,
        on_known_changed=None,
    ) -> None:
        self.roster = roster
        self.known = known
        self.notifier = notifier
        self.on_known_changed = on_known_changed
        self.in_dump = False
        self.section: str | None = None
        self.pending: dict[str, list[dict]] = {}
        self.dump_count = 0
        self.last_log_time: datetime | None = None
        self._server_lock = threading.Lock()
        self.server_started_at: datetime | None = None
        self.server_info: dict | None = None
        self._in_server_desc = False
        self._server_desc_buf: list[str] = []

    def set_server_state(self, started_at: datetime, info: dict) -> None:
        with self._server_lock:
            self.server_started_at = started_at
            self.server_info = info

    def get_server_state(self) -> tuple[datetime | None, dict | None]:
        with self._server_lock:
            return self.server_started_at, self.server_info

    def feed(self, line: str) -> None:
        line = line.rstrip("\r\n")

        # Accumulate the multi-line server description JSON block.
        # This check must come first so JSON lines aren't misrouted.
        if self._in_server_desc:
            if "[D:\\" in line:  # source-file footer signals end of block
                end = line.rfind("}")
                if end >= 0:
                    self._server_desc_buf.append(line[:end + 1])
                self._in_server_desc = False
                self._finalize_server_desc()
            else:
                self._server_desc_buf.append(line)
            return

        if line.startswith("["):
            bracket = line.find("]")
            if bracket != -1:
                dt = _parse_log_time(line[1:bracket])
                if dt:
                    self.last_log_time = dt
                if SERVER_DESC_RE.search(line):
                    self._in_server_desc = True
                    self._server_desc_buf = []
                    if bracket != -1 and dt:
                        # Store the timestamp now; JSON is on the following lines
                        with self._server_lock:
                            self.server_started_at = dt

        if not self.in_dump:
            if line in SECTION_HEADERS:
                self.in_dump = True
                self.section = line
                self.pending = {h: [] for h in SECTION_HEADERS}
            return

        if line in SECTION_HEADERS:
            self.section = line
            return

        if LOG_LINE_RE.match(line) or FOOTER_RE.match(line):
            self._finalize()
            self.in_dump = False
            self.section = None
            self.pending = {}
            return

        m = ROW_RE.match(line)
        if m and self.section:
            name, account_id, state, session_id = m.groups()
            row = {
                "name": name,
                "account_id": account_id,
                "state": state,
                "session_id": session_id,
            }
            tig = TIME_IN_GAME_RE.search(line)
            row["time_in_game"] = tig.group(1) if tig else None
            cin = CONNECTED_IN_RE.search(line)
            row["connected_in"] = cin.group(1) if cin else None
            tos = TIME_ON_SERVER_RE.search(line)
            row["time_on_server"] = tos.group(1) if tos else None
            rm = RESERVE_MOMENT_RE.search(line)
            row["reserve_moment"] = rm.group(1) if rm else None
            fw = FAREWELL_RE.search(line)
            row["farewell_reason"] = fw.group(1).strip() if fw else None
            # Compute aware "left_at" for Disconnected rows from
            # ReserveMoment + TimeOnServer (the only timestamp available
            # within the row itself).
            row["left_at"] = None
            if row["reserve_moment"] and row["time_on_server"]:
                rm_dt = _parse_log_time(row["reserve_moment"])
                tos_td = _parse_duration(row["time_on_server"])
                if rm_dt and tos_td is not None:
                    row["left_at"] = rm_dt + tos_td
            self.pending[self.section].append(row)

    def _finalize(self) -> None:
        connected = self.pending.get("Connected Accounts", [])
        reserved = self.pending.get("Reserved Accounts", [])
        disconnected = self.pending.get("Disconnected Accounts", [])

        # Online = Reserved (handshaking) + Connected (playing).
        # Dedupe by name; Connected wins if a player appears in both.
        online: dict[str, dict] = {}
        for row in reserved:
            online[row["name"]] = row
        for row in connected:
            online[row["name"]] = row

        joined, left = self.roster.apply(list(online.values()), self.last_log_time)
        self.dump_count += 1

        # Update last-seen tracker from every Disconnected row in this dump.
        # This naturally backfills history when the script starts mid-session.
        known_changed = self.known.update_from_disconnected(disconnected)

        # The Disconnected section can contain MULTIPLE entries for the same
        # player (one per past session). Match the leaving player to their
        # exit record by session_id, which is unique per connection.
        exit_by_session = {
            row["session_id"]: row
            for row in disconnected
            if row.get("session_id")
        }

        for row in joined:
            name = row.get("name") or "(unnamed)"
            msg = random.choice(JOIN_MESSAGES).format(name=name)
            self.notifier.post(f"Connected | {msg}")
            log.info("JOIN  %s (%s) state=%s", name, row.get("account_id"), row.get("state"))

        for row in left:
            exit_row = exit_by_session.get(row.get("session_id") or "")
            if exit_row:
                reason = exit_row.get("farewell_reason") or exit_row.get("state") or "left"
                played_raw = exit_row.get("time_in_game") or ""
                played = _format_duration_str(played_raw)
            else:
                reason = "left"
                played_raw = row.get("time_in_game") or ""
                td = _parse_duration(played_raw)
                if td and row.get("as_of") and self.last_log_time:
                    elapsed = self.last_log_time - row["as_of"]
                    td += elapsed
                    played = _format_timedelta(td)
                else:
                    played = _format_duration_str(played_raw)
                known_changed = self.known.record_left(row, self.last_log_time, played_raw) or known_changed

            name = row.get("name") or "(unnamed)"
            played_part = f" played {played}" if played else ""
            msg = random.choice(LEAVE_MESSAGES).format(name=name)
            self.notifier.post(f"Disconnected | {msg}{played_part}")
            log.info("LEAVE %s (%s) reason=%s played=%s", name, row.get("account_id"), reason, played_raw)

        log.debug(
            "dump #%d parsed: connected=%d reserved=%d disconnected=%d",
            self.dump_count, len(connected), len(reserved), len(disconnected),
        )

        if known_changed and self.on_known_changed:
            try:
                self.on_known_changed()
            except Exception:
                log.exception("state persistence callback failed")

    def _finalize_server_desc(self) -> None:
        raw = "\n".join(self._server_desc_buf)
        start = raw.find("{")
        if start == -1:
            return
        try:
            data = json.loads(raw[start:])
        except json.JSONDecodeError as e:
            log.warning("Failed to parse server description JSON: %s", e)
            return
        desc = data.get("ServerDescription_Persistent", {})
        info = {
            "server_name": desc.get("ServerName", ""),
            "max_players": desc.get("MaxPlayerCount", 0),
            "password_protected": desc.get("IsPasswordProtected", False),
            "invite_code": desc.get("InviteCode", ""),
            "world_id": desc.get("WorldIslandId", ""),
            "deployment_id": data.get("DeploymentId", ""),
        }
        with self._server_lock:
            self.server_info = info
        log.info(
            "Server: %s | max %d players | %s | world %s",
            info["server_name"], info["max_players"],
            "password protected" if info["password_protected"] else "open",
            info["world_id"][:8],
        )


_DUMP_NEEDLES = (b"\nConnected Accounts\n", b"\nConnected Accounts\r\n")
_CHUNK = 512 * 1024
# Overlap consecutive chunks by this many bytes so a match can't straddle a boundary undetected.
_OVERLAP = max(len(n) for n in _DUMP_NEEDLES) - 1  # 20 bytes
# Matches the start of a bracketed log line: "\n[YYYY.MM.DD-" (or BOF).
# Used to find the timestamp line that precedes a dump so the parser can
# set last_log_time before consuming dump rows.
_TIMESTAMP_LINE_RE = re.compile(rb"(?:\n|\A)\[\d{4}\.\d{2}\.\d{2}-")


def _scan_server_info(path: str, scan_bytes: int = 2_000_000) -> tuple[datetime | None, dict | None]:
    """Scan the first scan_bytes of the log for the SaveServerDescription block.

    Returns (started_at, server_info).  The block appears near the top of every
    fresh log, so 2 MB is more than enough.  Both values are None if not found.
    """
    started_at: datetime | None = None
    server_info: dict | None = None
    in_json = False
    json_lines: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(scan_bytes)
        for line in data.splitlines():
            if in_json:
                if "[D:\\" in line:
                    end = line.rfind("}")
                    if end >= 0:
                        json_lines.append(line[:end + 1])
                    in_json = False
                    raw = "\n".join(json_lines)
                    start = raw.find("{")
                    if start != -1:
                        try:
                            data_j = json.loads(raw[start:])
                            desc = data_j.get("ServerDescription_Persistent", {})
                            server_info = {
                                "server_name": desc.get("ServerName", ""),
                                "max_players": desc.get("MaxPlayerCount", 0),
                                "password_protected": desc.get("IsPasswordProtected", False),
                                "invite_code": desc.get("InviteCode", ""),
                                "world_id": desc.get("WorldIslandId", ""),
                                "deployment_id": data_j.get("DeploymentId", ""),
                            }
                        except json.JSONDecodeError:
                            pass
                else:
                    json_lines.append(line)
                continue
            if not line.startswith("["):
                continue
            bracket = line.find("]")
            if bracket == -1:
                continue
            dt = _parse_log_time(line[1:bracket])
            if dt and SERVER_DESC_RE.search(line):
                started_at = dt
                in_json = True
                json_lines = []
    except OSError:
        pass
    return started_at, server_info


def _find_last_dump_offset(path: str) -> int:
    """Return the byte offset to start bootstrap reading from, or 0.

    Locates the last 'Connected Accounts' line, then walks back to the
    timestamp-prefixed log line immediately preceding it. Reading from there
    (rather than from the dump header itself) lets the parser observe the
    timestamp line first and populate last_log_time, so dump rows get a valid
    `as_of` for time-in-game extrapolation.

    Reads the file in 512 KB chunks from the end. Falls back to 0 (read from
    start) only if the file contains no dump at all.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return 0
            pos = size
            while pos > 0:
                start = max(0, pos - _CHUNK)
                f.seek(start)
                data = f.read(pos - start)
                for needle in _DUMP_NEEDLES:
                    idx = data.rfind(needle)
                    if idx != -1:
                        # Walk back to the timestamp line that precedes the dump.
                        # If we can't find one in this chunk (rare, only on tiny
                        # logs), fall back to the dump header itself.
                        prev = data[:idx]
                        ts_match = None
                        for m in _TIMESTAMP_LINE_RE.finditer(prev):
                            ts_match = m
                        if ts_match is not None:
                            ts_start = ts_match.start()
                            # Skip the leading \n (if not BOF + this is the first chunk).
                            if prev[ts_start:ts_start + 1] == b"\n":
                                ts_start += 1
                            return start + ts_start
                        return start + idx + 1  # land on "Connected Accounts"
                if start == 0:
                    break
                pos = start + _OVERLAP  # re-examine tail of this chunk next iteration
        return 0
    except OSError:
        return 0


def tail_file(
    path: str,
    parser: DumpParser,
    on_bootstrap_done,
    stop: threading.Event,
) -> None:
    """Poll the log: open, seek to last position, drain to EOF, close, sleep.

    Re-opening each cycle is the simplest way to handle rotation reliably on
    Windows, where stat-based detection (st_ino is often 0, st_size can be
    cached) is unreliable. If the file is smaller than our tracked position
    on a re-open, we treat it as rotation/truncation and read from the start.
    """
    pos = -1  # -1 sentinel: first open, do bootstrap fast-seek
    buf = ""

    while not stop.is_set():
        try:
            f = open(path, "r", encoding="utf-8", errors="replace")
        except OSError as e:
            log.warning("cannot open %s: %s; retrying", path, e)
            time.sleep(2)
            continue

        try:
            with f:
                size = os.fstat(f.fileno()).st_size

                if pos == -1:
                    offset = _find_last_dump_offset(path)
                    f.seek(offset)
                    log.info("bootstrap: fast-seeking to offset %d (last dump)", offset)
                elif size < pos:
                    log.info("log shrank (size=%d < pos=%d); reading from start", size, pos)
                    buf = ""  # discard partial line carried over from the rotated-out file
                    f.seek(0)
                else:
                    f.seek(pos)

                while not stop.is_set():
                    chunk = f.read(8192)
                    if not chunk:
                        pos = f.tell()
                        break
                    buf += chunk
                    while True:
                        nl = buf.find("\n")
                        if nl == -1:
                            break
                        line = buf[:nl]
                        buf = buf[nl + 1:]
                        parser.feed(line)
        except OSError as e:
            log.warning("error reading %s: %s; retrying", path, e)
            time.sleep(2)
            continue

        if pos != -1 and on_bootstrap_done is not None:
            try:
                on_bootstrap_done()
            except Exception:
                log.exception("on_bootstrap_done callback failed")
            on_bootstrap_done = None  # only fire once

        time.sleep(10)


PUBLIC_PLAYER_FIELDS = {
    "name",
    "state",
    "time_in_game",
    "connected_in",
    "time_on_server",
    "farewell_reason",
    "left_at",
    "joined_at",
    "as_of",
}


def _public_player_row(row: dict) -> dict:
    return {
        key: value
        for key, value in _json_ready_row(row).items()
        if key in PUBLIC_PLAYER_FIELDS and value is not None
    }


def is_api_players_path(path: str) -> bool:
    return urlsplit(path).path == "/api/players"


def is_dashboard_path(path: str) -> bool:
    return urlsplit(path).path in {"/", "/index.html"}


def resolve_asset_path(path: str) -> str | None:
    parsed_path = unquote(urlsplit(path).path)
    if not parsed_path.startswith("/assets/"):
        return None

    relative = parsed_path.removeprefix("/assets/")
    if not relative or relative.startswith("."):
        return None

    candidate = os.path.abspath(os.path.join(ASSETS_DIR, relative))
    assets_root = os.path.abspath(ASSETS_DIR)
    if os.path.commonpath([assets_root, candidate]) != assets_root:
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate


def load_dashboard_html() -> str:
    with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()


def build_api_payload(roster: Roster, known: KnownPlayers, parser: DumpParser) -> dict:
    online, as_of = roster.snapshot()
    online_names = {p["name"] for p in online}
    recent = known.snapshot(exclude_names=online_names)
    started, server_info = parser.get_server_state()

    return {
        "online": [_public_player_row(row) for row in online],
        "recently_seen": [_public_player_row(row) for row in recent],
        "as_of": as_of,
        "online_count": len(online),
        "server_started_at": started.isoformat() if started else None,
        "server_info": server_info,
    }


def make_handler(roster: Roster, known: KnownPlayers, parser: DumpParser):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("http %s - " + fmt, self.address_string(), *args)

        def do_GET(self):
            asset_path = resolve_asset_path(self.path)
            if asset_path:
                with open(asset_path, "rb") as f:
                    body = f.read()
                ctype = mimetypes.guess_type(asset_path)[0] or "application/octet-stream"
                self._send(HTTPStatus.OK, body, ctype)
                return

            if is_api_players_path(self.path):
                body = json.dumps(
                    build_api_payload(roster, known, parser),
                    indent=2,
                    default=str,
                ).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json")
                return

            if is_dashboard_path(self.path):
                body = load_dashboard_html().encode("utf-8")
                self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
                return

            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")

        def _send(self, status: HTTPStatus, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Windrose dedicated-server player monitor")
    p.add_argument("--log", default="R5.log", help="path to R5.log")
    p.add_argument("--host", default="0.0.0.0", help="HTTP bind host (default all interfaces)")
    p.add_argument("--port", type=int, default=8080, help="HTTP bind port")
    p.add_argument("--state", default=DEFAULT_STATE_PATH,
                   help=f"path to persisted monitor state (default {DEFAULT_STATE_PATH})")
    p.add_argument("--webhook", default=None,
                   help="Discord webhook URL (overrides DISCORD_WEBHOOK_URL env var)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    webhook = args.webhook or os.getenv("DISCORD_WEBHOOK_URL", "")
    if webhook:
        log.info("Discord notifications enabled")
    else:
        log.info("Discord notifications disabled (set --webhook or DISCORD_WEBHOOK_URL to enable)")
    notifier = DiscordNotifier(webhook)

    roster = Roster()
    known = KnownPlayers()
    state_store = StateStore(args.state)
    loaded_known = state_store.load_known_players(known)
    if loaded_known:
        log.info("Loaded %d recently seen players from %s", loaded_known, args.state)
    else:
        log.info("No persisted player state loaded from %s", args.state)

    parser = DumpParser(
        roster,
        known,
        notifier,
        on_known_changed=lambda: state_store.save_known_players(known),
    )
    stop = threading.Event()

    # Suppress notifications during the replay of historical content;
    # we'll un-suppress in on_bootstrap_done after reaching EOF.
    notifier.quiet = True
    log.info("Replaying last dump to backfill state (Discord notifications suppressed)...")

    def on_bootstrap_done() -> None:
        notifier.quiet = False
        online, _ = roster.snapshot()
        _, srv = parser.get_server_state()
        log.info("Bootstrap complete: %d online, %d known players", len(online), len(known.snapshot()))

        if srv and srv.get("server_name"):
            pw = "\U0001f512" if srv.get("password_protected") else "\U0001f513"
            max_p = srv.get("max_players", "?")
            header = f"⚓ Monitor online  |  {srv['server_name']} {pw}  |  max {max_p} players"
        else:
            header = "⚓ Monitor online"

        if online:
            names = ", ".join(p.get("name") or "(unnamed)" for p in online)
            notifier.post(f"{header}\n{len(online)} online: {names}")
        else:
            notifier.post(f"{header}\nNo players online")

    # One-time scan of the log start for server info (appears within first ~1000 lines).
    started_at, server_info = _scan_server_info(args.log)
    if started_at:
        with parser._server_lock:
            parser.server_started_at = started_at
            parser.server_info = server_info
        log.info("Server started at %s UTC", started_at.strftime("%Y-%m-%d %H:%M:%S"))
        if server_info:
            log.info("Server: %s | max %d | %s",
                     server_info.get("server_name"), server_info.get("max_players"),
                     "password" if server_info.get("password_protected") else "open")
    else:
        log.info("Server info not found in log (will detect when server writes it)")

    threading.Thread(
        target=tail_file,
        args=(args.log, parser, on_bootstrap_done, stop),
        name="tailer",
        daemon=True,
    ).start()

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(roster, known, parser))
    log.info("HTTP listening on http://%s:%d", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        stop.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
