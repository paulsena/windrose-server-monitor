"""Windrose dedicated-server player monitor.

Tails R5.log, parses the periodic state-table dumps, maintains a live roster
of currently-connected players, serves a small web page, and posts join/leave
notifications to a Discord webhook (optional).

Standard library only.
    python windrose_monitor.py --log path/to/R5.log
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import random
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib import request as urlrequest

log = logging.getLogger("windrose_monitor")
webhook = os.getenv("DISCORD_WEBHOOK_URL", "")


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


def _parse_duration(s: str) -> timedelta | None:
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
                aid = row["account_id"]
                row_with_meta = {**row, "as_of": as_of}
                if aid in prev:
                    new[aid] = {**row_with_meta, "joined_at": prev[aid]["joined_at"]}
                else:
                    new[aid] = {**row_with_meta, "joined_at": now_iso}
                    joined.append(new[aid])
            left = [prev[aid] for aid in prev if aid not in new]
            self._players = new
            return joined, left


class KnownPlayers:
    """Tracks the last time we observed each account leave the server.

    Populated from the Disconnected section of every dump (so it backfills
    naturally from log history when the script starts mid-session). Most
    recent disconnect per account_id wins.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._players: dict[str, dict] = {}

    def update_from_disconnected(self, rows: list[dict]) -> None:
        with self._lock:
            for row in rows:
                aid = row.get("account_id")
                if not aid:
                    continue
                left_at = row.get("left_at")
                existing = self._players.get(aid)
                if existing and existing.get("left_at") and left_at and existing["left_at"] >= left_at:
                    continue  # we already have a more recent record
                self._players[aid] = {
                    "name": row.get("name") or existing.get("name") if existing else row.get("name"),
                    "account_id": aid,
                    "session_id": row.get("session_id"),
                    "state": row.get("state"),
                    "time_in_game": row.get("time_in_game"),
                    "farewell_reason": row.get("farewell_reason"),
                    "left_at": left_at,  # aware datetime or None
                }

    def snapshot(self, exclude_account_ids: set[str] | None = None) -> list[dict]:
        exclude = exclude_account_ids or set()
        with self._lock:
            rows = [p for aid, p in self._players.items() if aid not in exclude]
        rows.sort(key=lambda p: p.get("left_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows


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
    def __init__(self, roster: Roster, known: KnownPlayers, notifier: DiscordNotifier) -> None:
        self.roster = roster
        self.known = known
        self.notifier = notifier
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
        # Dedupe by account_id; Connected wins if a player appears in both.
        online: dict[str, dict] = {}
        for row in reserved:
            online[row["account_id"]] = row
        for row in connected:
            online[row["account_id"]] = row

        joined, left = self.roster.apply(list(online.values()), self.last_log_time)
        self.dump_count += 1

        # Update last-seen tracker from every Disconnected row in this dump.
        # This naturally backfills history when the script starts mid-session.
        self.known.update_from_disconnected(disconnected)

        # The Disconnected section can contain MULTIPLE entries for the same
        # account_id (one per past session). Match the leaving player to their
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

            name = row.get("name") or "(unnamed)"
            played_part = f" played {played}" if played else ""
            msg = random.choice(LEAVE_MESSAGES).format(name=name)
            self.notifier.post(f"Disconnected | {msg}{played_part}")
            log.info("LEAVE %s (%s) reason=%s played=%s", name, row.get("account_id"), reason, played_raw)

        log.debug(
            "dump #%d parsed: connected=%d reserved=%d disconnected=%d",
            self.dump_count, len(connected), len(reserved), len(disconnected),
        )

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
    """Return the byte offset of the last 'Connected Accounts' line, or 0.

    Reads the file in 512 KB chunks from the end, stopping as soon as a dump
    header is found. Adjacent chunks overlap by the needle length so a match
    can never fall on a chunk boundary undetected. Falls back to 0 (read from
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
                        return start + idx + 1  # skip leading \n, land on "Connected Accounts"
                if start == 0:
                    break
                pos = start + _OVERLAP  # re-examine tail of this chunk next iteration
        return 0
    except OSError:
        return 0


def tail_file(
    path: str,
    parser: DumpParser,
    from_start: bool,
    on_bootstrap_done,
    stop: threading.Event,
) -> None:
    f = None
    pos = 0
    buf = ""
    file_id: tuple[int, int] = (0, 0)
    first_open = True
    reopen_from_start = False
    in_bootstrap = from_start

    while not stop.is_set():
        if f is None:
            try:
                f = open(path, "r", encoding="utf-8", errors="replace")
            except OSError as e:
                log.warning("cannot open %s: %s; retrying", path, e)
                time.sleep(2)
                continue

            if first_open and from_start:
                # Bootstrap: seek to start of the last dump rather than the
                # beginning of the whole file, so startup is fast on large logs.
                offset = _find_last_dump_offset(path)
                f.seek(offset)
                log.info("bootstrap: fast-seeking to offset %d (last dump)", offset)
            elif reopen_from_start:
                # After rotation: read the new file from the beginning.
                f.seek(0)
            else:
                # --no-replay: tail from current end.
                f.seek(0, os.SEEK_END)

            try:
                st = os.fstat(f.fileno())
                file_id = (st.st_dev, st.st_ino)
            except OSError:
                file_id = (0, 0)

            pos = f.tell()
            buf = ""
            first_open = False
            reopen_from_start = False
            log.info("tailing %s from offset %d", path, pos)

        # Detect log rotation (inode change) or truncation (size went backwards).
        # On Windows NTFS via WSL2 st_ino is synthesised and reliable; on plain
        # Windows it may be 0, in which case we fall back to size-only detection.
        try:
            path_stat = os.stat(path)
            path_id = (path_stat.st_dev, path_stat.st_ino)
            rotated = (file_id[1] != 0 and path_id != file_id)
            if path_stat.st_size < pos or rotated:
                reason = "rotated" if rotated else "truncated"
                log.info("log %s; reopening", reason)
                f.close()
                f = None
                reopen_from_start = True
                continue
        except OSError:
            f.close()
            f = None
            time.sleep(0.5)
            continue

        chunk = f.read(8192)
        if not chunk:
            if in_bootstrap:
                in_bootstrap = False
                try:
                    on_bootstrap_done()
                except Exception:
                    log.exception("on_bootstrap_done callback failed")
            time.sleep(0.5)
            continue
        pos = f.tell()
        buf += chunk
        while True:
            nl = buf.find("\n")
            if nl == -1:
                break
            line = buf[:nl]
            buf = buf[nl + 1:]
            parser.feed(line)


WINDROSE_SVG = """\
<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <defs>
    <g id="major-arm">
      <polygon points="50,6 47,48 50,50" fill="#a07a30"/>
      <polygon points="50,6 53,48 50,50" fill="#e6c275"/>
    </g>
    <g id="minor-arm">
      <polygon points="50,20 48,49 50,50" fill="#7a5b22"/>
      <polygon points="50,20 52,49 50,50" fill="#c9a55b"/>
    </g>
  </defs>
  <circle cx="50" cy="50" r="46" fill="none" stroke="#d4a657" stroke-width="0.8"/>
  <circle cx="50" cy="50" r="38" fill="none" stroke="#d4a657" stroke-width="0.4" opacity="0.6"/>
  <use href="#minor-arm" transform="rotate(45 50 50)"/>
  <use href="#minor-arm" transform="rotate(135 50 50)"/>
  <use href="#minor-arm" transform="rotate(225 50 50)"/>
  <use href="#minor-arm" transform="rotate(315 50 50)"/>
  <use href="#major-arm"/>
  <use href="#major-arm" transform="rotate(90 50 50)"/>
  <use href="#major-arm" transform="rotate(180 50 50)"/>
  <use href="#major-arm" transform="rotate(270 50 50)"/>
  <circle cx="50" cy="50" r="2.5" fill="#d4a657"/>
  <text x="50" y="4.5" font-size="5" fill="#d4a657" text-anchor="middle"
        font-family="Georgia, serif" font-weight="700">N</text>
</svg>"""

PAGE_TMPL = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<style>
  :root {{
    --bg: #0d1422;
    --bg-2: #131c2e;
    --panel: #182338;
    --border: #233149;
    --text: #e8eef9;
    --muted: #8395ad;
    --gold: #d4a657;
    --gold-dim: #a07a30;
    --green: #6dcf91;
    --red: #d97a7a;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: radial-gradient(ellipse at top, var(--bg-2), var(--bg) 60%);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    min-height: 100vh;
    padding: 0 1rem 4rem;
  }}
  .container {{ max-width: 920px; margin: 0 auto; }}
  .banner {{
    display: flex; align-items: center; gap: 1.5rem;
    margin-top: 2rem;
    padding: 1.5rem 1.75rem;
    background: linear-gradient(135deg, var(--panel), #1d2a44);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: 0 6px 28px rgba(0,0,0,0.35);
  }}
  .banner svg {{ width: 86px; height: 86px; flex-shrink: 0; filter: drop-shadow(0 2px 6px rgba(0,0,0,0.4)); }}
  .banner h1 {{
    margin: 0;
    font-family: Georgia, "Times New Roman", serif;
    font-size: 1.7rem;
    color: var(--gold);
    letter-spacing: 0.02em;
  }}
  .banner .subtitle {{
    margin-top: 0.2rem;
    color: var(--muted);
    font-size: 0.85rem;
  }}
  .banner .server-status {{
    margin-top: 0.45rem;
    font-size: 0.85rem;
    color: var(--green);
  }}
  .info-chips {{
    display: flex; flex-wrap: wrap; gap: 0.4rem; margin-top: 0.55rem;
  }}
  .chip {{
    display: inline-flex; align-items: center; gap: 0.3rem;
    padding: 0.18rem 0.55rem;
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 0.78rem;
    color: var(--muted);
  }}
  .chip-lock {{ color: var(--gold); border-color: rgba(212,166,87,0.3); background: rgba(212,166,87,0.06); }}
  .chip-open {{ color: var(--green); border-color: rgba(109,207,145,0.25); background: rgba(109,207,145,0.05); }}
  .chip-mono {{ font-family: "Cascadia Code", Consolas, "SF Mono", Menlo, monospace; font-size: 0.74rem; }}
  .section {{
    margin-top: 1.25rem;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 1.25rem 1.5rem;
  }}
  .section-head {{
    display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 0.75rem;
  }}
  .section-head h2 {{
    margin: 0;
    font-family: Georgia, serif;
    font-size: 1.1rem;
    color: var(--gold);
    letter-spacing: 0.04em;
  }}
  .pill {{
    display: inline-block;
    padding: 0.15rem 0.65rem;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 600;
    letter-spacing: 0.03em;
  }}
  .pill.online {{ background: rgba(109,207,145,0.15); color: var(--green); border: 1px solid rgba(109,207,145,0.35); }}
  .pill.offline {{ background: rgba(131,149,173,0.12); color: var(--muted); border: 1px solid rgba(131,149,173,0.3); }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{
    text-align: left; padding: 0.45rem 0.5rem; font-size: 0.75rem;
    color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em;
    border-bottom: 1px solid var(--border); font-weight: 600;
  }}
  td {{
    padding: 0.65rem 0.5rem; font-size: 0.95rem;
    border-bottom: 1px solid var(--border);
  }}
  tr:last-child td {{ border-bottom: none; }}
  td.name {{ font-weight: 600; }}
  td.muted, .muted {{ color: var(--muted); font-size: 0.88rem; }}
  td.mono, code {{
    font-family: "Cascadia Code", Consolas, "SF Mono", Menlo, monospace;
    font-size: 0.8rem; color: var(--muted);
  }}
  .dot {{
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    margin-right: 0.55rem; vertical-align: middle;
  }}
  .dot.on {{ background: var(--green); box-shadow: 0 0 8px rgba(109,207,145,0.6); }}
  .dot.off {{ background: var(--muted); opacity: 0.5; }}
  .empty {{ color: var(--muted); padding: 1rem 0; text-align: center; font-style: italic; }}
  .as-of {{ color: var(--muted); font-size: 0.78rem; text-align: center; margin-top: 1.25rem; }}
  @media (max-width: 540px) {{
    .banner {{ flex-direction: column; text-align: center; padding: 1.25rem; }}
    .banner svg {{ width: 72px; height: 72px; }}
    .banner h1 {{ font-size: 1.35rem; }}
    th.hide-sm, td.hide-sm {{ display: none; }}
  }}
</style>
</head><body>
<div class="container">
  <header class="banner">
    {svg}
    <div>
      {banner_head}
    </div>
  </header>

  <section class="section">
    <div class="section-head">
      <h2>Online now</h2>
      <span class="pill online">{online_count} online</span>
    </div>
    {online_table}
  </section>

  <section class="section">
    <div class="section-head">
      <h2>Recently seen</h2>
      <span class="pill offline">{offline_count} known</span>
    </div>
    {offline_table}
  </section>

  <div class="as-of">updated <span class="ts-local" data-utc="{as_of_utc}">{as_of_utc}</span></div>
</div>
<script>
(function() {{
  var fmt = {{year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'}};
  document.querySelectorAll('.ts-local[data-utc]').forEach(function(el) {{
    var d = new Date(el.getAttribute('data-utc'));
    if (!isNaN(d)) el.textContent = d.toLocaleString(undefined, fmt);
  }});
}})();
</script>
</body></html>
"""


def _format_dt_cell(dt: datetime | None) -> str:
    """Return HTML for a timestamp cell: a span with data-utc for JS local-time conversion."""
    if dt is None:
        return "—"
    utc = dt.astimezone(timezone.utc)
    iso = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f'<span class="ts-local" data-utc="{iso}">{iso}</span>'


def _ago_str(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return _human_ago(datetime.now(timezone.utc) - dt)


def _render_banner_head(server_info: dict | None, started_at: datetime | None) -> str:
    now = datetime.now(timezone.utc)

    if server_info:
        name = escape(server_info.get("server_name") or "Windrose Server")
        max_p = server_info.get("max_players", "?")
        pw = server_info.get("password_protected", False)
        world_id = server_info.get("world_id", "")
        world_short = escape(world_id[:8] + "…") if world_id else ""
        deploy = escape(server_info.get("deployment_id") or "")

        lock_chip = (
            '<span class="chip chip-lock">\U0001f512 Password Protected</span>'
            if pw else
            '<span class="chip chip-open">\U0001f513 Open Access</span>'
        )
        chips = [
            lock_chip,
            f'<span class="chip">\U0001f465 {max_p} players max</span>',
        ]
        if deploy:
            chips.append(f'<span class="chip chip-mono">Server Version: {escape(deploy)}</span>')
        chips_html = '<div class="info-chips">' + "".join(chips) + "</div>"
        world_line = (
            f'<div class="info-chips">'
            f'<span class="chip chip-mono">\U0001f5fa️ World ID: {escape(world_id)}</span>'
            f'</div>'
            if world_id else ""
        )
    else:
        name = "Windrose Server"
        chips_html = ""
        world_line = ""

    if started_at:
        uptime = _human_duration(now - started_at)
        started_iso = started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        status = (
            f'<span class="dot on"></span>Online'
            f'&nbsp;&nbsp;·&nbsp;&nbsp;up {uptime}'
            f'&nbsp;&nbsp;·&nbsp;&nbsp;since '
            f'<span class="ts-local" data-utc="{started_iso}">{started_iso}</span>'
        )
    else:
        status = '<span class="dot off"></span>Status unknown'

    return (
        f'<h1>{name}</h1>\n'
        f'      <div class="subtitle">Windrose Server Monitor</div>\n'
        f'      <div class="server-status">{status}</div>\n'
        f'      {chips_html}\n'
        f'      {world_line}'
    )


def make_handler(roster: Roster, known: KnownPlayers, parser: DumpParser):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("http %s - " + fmt, self.address_string(), *args)

        def do_GET(self):
            online, as_of = roster.snapshot()
            online_aids = {p["account_id"] for p in online}
            recent = known.snapshot(exclude_account_ids=online_aids)

            started, server_info = parser.get_server_state()

            if self.path.startswith("/api/players"):
                body = json.dumps(
                    {
                        "online": _jsonify(online),
                        "recently_seen": _jsonify(recent),
                        "as_of": as_of,
                        "online_count": len(online),
                        "server_started_at": started.isoformat() if started else None,
                        "server_info": server_info,
                    },
                    indent=2,
                    default=str,
                ).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json")
                return

            if self.path == "/" or self.path.startswith("/?"):
                online_table = _render_online(online)
                offline_table = _render_recent(recent)
                as_of_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                banner_head = _render_banner_head(server_info, started)
                page_title = (
                    escape(server_info["server_name"]) + " · Windrose Monitor"
                    if server_info and server_info.get("server_name")
                    else "Windrose Server Monitor"
                )
                body = PAGE_TMPL.format(
                    svg=WINDROSE_SVG,
                    page_title=page_title,
                    online_count=len(online),
                    offline_count=len(recent),
                    online_table=online_table,
                    offline_table=offline_table,
                    as_of_utc=as_of_utc,
                    banner_head=banner_head,
                ).encode("utf-8")
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


def _jsonify(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        c = dict(r)
        for k, v in list(c.items()):
            if isinstance(v, datetime):
                c[k] = v.isoformat()
        out.append(c)
    return out


def _render_online(players: list[dict]) -> str:
    if not players:
        return '<div class="empty">No players online right now.</div>'

    now = datetime.now(timezone.utc)
    rendered_rows = []
    for p in players:
        name = escape(p.get('name') or '(unnamed)')
        raw_dur = p.get('time_in_game')
        as_of = p.get('as_of')  # aware datetime from log

        # Extrapolate
        td = _parse_duration(raw_dur)
        if td and as_of:
            as_of_utc = as_of.astimezone(timezone.utc)
            elapsed = now - as_of_utc
            if elapsed.total_seconds() > 0:
                td += elapsed
            display_dur = _format_timedelta(td)
        else:
            display_dur = _format_duration_str(raw_dur) or "—"

        rendered_rows.append(
            f"<tr>"
            f"<td class='name'><span class='dot on'></span>{name}</td>"
            f"<td>{escape(display_dur)}</td>"
            f"</tr>"
        )

    return (
        "<table><thead><tr>"
        "<th>Name</th><th>Time in game</th>"
        "</tr></thead><tbody>" + "".join(rendered_rows) + "</tbody></table>"
    )


def _render_recent(players: list[dict]) -> str:
    if not players:
        return '<div class="empty">No history yet.</div>'
    rows = "".join(
        f"<tr>"
        f"<td class='name'><span class='dot off'></span>{escape(p.get('name') or '(unnamed)')}</td>"
        f"<td>{_format_dt_cell(p.get('left_at'))}</td>"
        f"<td class='muted'>{escape(_ago_str(p.get('left_at')))}</td>"
        f"<td class='muted hide-sm'>{escape(_format_duration_str(p.get('time_in_game')) or '—')}</td>"
        f"</tr>"
        for p in players
    )
    return (
        "<table><thead><tr>"
        "<th>Name</th><th>Last seen</th><th>How long ago</th>"
        "<th class='hide-sm'>Last session played</th>"
        "</tr></thead><tbody>" + rows + "</tbody></table>"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Windrose dedicated-server player monitor")
    p.add_argument("--log", default="R5.log", help="path to R5.log")
    p.add_argument("--no-replay", action="store_true",
                   help="skip replaying the existing log; only watch for new events")
    p.add_argument("--host", default="0.0.0.0", help="HTTP bind host (default all interfaces)")
    p.add_argument("--port", type=int, default=8080, help="HTTP bind port")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if webhook:
        log.info("Discord notifications enabled")
    else:
        log.info("DISCORD_WEBHOOK_URL not set; Discord notifications disabled")
    notifier = DiscordNotifier(webhook)

    roster = Roster()
    known = KnownPlayers()
    parser = DumpParser(roster, known, notifier)
    stop = threading.Event()

    do_replay = not args.no_replay
    if do_replay:
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
        args=(args.log, parser, do_replay, on_bootstrap_done, stop),
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
