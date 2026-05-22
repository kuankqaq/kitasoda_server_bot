"""
Helpers for normalising L4D2 ServerDetail / Player records before rendering.
"""
from __future__ import annotations

import re
from typing import Optional

ZWNJ = "\u200c"
_MAP_TAIL_RE = re.compile(r"\s*\[\d+/\d+\]\s*$")
_SPECTATOR_RE = re.compile(r"^\[S\]\s*")


def is_official_map(server: dict) -> bool:
    m = server.get("map")
    return isinstance(m, str) and m.startswith(ZWNJ)


def clean_map_name(server: dict) -> str:
    m = server.get("map") or ""
    m = m.lstrip(ZWNJ)
    m = _MAP_TAIL_RE.sub("", m)
    return m.strip()


def map_progress(server: dict) -> str:
    """Return '1/4' fragment if present, else ''."""
    m = server.get("map") or ""
    match = re.search(r"\[(\d+/\d+)\]\s*$", m)
    return match.group(1) if match else ""


def infer_mode(server: dict) -> str:
    tags = server.get("tags") or []
    name = server.get("name") or ""
    if "versus" in tags and not re.search(r"役|训练|\d+特\d+秒", name):
        return "versus"
    return "coop"


def infer_difficulty(server: dict) -> str:
    base = server.get("difficulty") or ""
    tags = server.get("tags") or []
    if "realism" in tags:
        return f"写实{base}"
    return base


def normalise_player(p: dict) -> dict:
    name = p.get("name", "") or ""
    is_spec = bool(_SPECTATOR_RE.match(name))
    display = _SPECTATOR_RE.sub("", name).strip() if is_spec else name
    return {
        "name": display or name or "(unknown)",
        "raw_name": name,
        "is_spectator": is_spec,
        "score": p.get("score", 0),
        "time": p.get("time", 0),
    }


def players_have_steam_ids(players: list[dict]) -> bool:
    return any(abs((p.get("score") or 0)) >= 1_000_000 for p in players)


def score_to_steamid64(score: int) -> str:
    y = 1 if score >= 0 else 0
    account = abs(score)
    return str(76561197960265728 + account * 2 + y)


def fmt_seconds(total: int) -> str:
    total = int(total or 0)
    if total < 60:
        return f"{total}s"
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m}m{s:02d}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def build_server_card(server: dict, *, allow_idle: bool = False) -> Optional[dict]:
    """Render-ready dict for one server.

    If `allow_idle` is False (default), servers with no players are rejected.
    """
    num = int(server.get("numPlayers") or 0)
    if num <= 0 and not allow_idle:
        return None
    players = [normalise_player(p) for p in (server.get("players") or [])]
    additional = server.get("additional") or {}
    return {
        "id": server.get("id"),
        "name": server.get("name") or "(无名)",
        "address": server.get("address") or "",
        "num": num,
        "max": int(server.get("maxPlayers") or 0),
        "map": clean_map_name(server) or "(未知地图)",
        "progress": map_progress(server),
        "mode": infer_mode(server),
        "difficulty": infer_difficulty(server) or "",
        "team_wipes": int(server.get("teamWipes") or 0),
        "region": additional.get("region") or "",
        "provider": additional.get("provider") or "",
        "tick": additional.get("tickRate") or "",
        "official": is_official_map(server),
        "oversea": bool(server.get("oversea")),
        "tags": server.get("tags") or [],
        "players": players,
        "idle": num <= 0,
    }


def find_player(servers: list[dict], query: str) -> list[dict]:
    """Return list of {server, player} matches (case-insensitive substring)."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    hits: list[dict] = []
    for s in servers:
        for p in s.get("players") or []:
            name = (p.get("name") or "").lower()
            # also match without [S] prefix
            stripped = _SPECTATOR_RE.sub("", p.get("name") or "").lower()
            if needle in name or needle in stripped:
                hits.append({"server": s, "player": p})
    return hits
