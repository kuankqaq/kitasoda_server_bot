"""
kitcoda — L4D2 server query plugin (NoneBot2 + OneBot v11).

Commands:
  /查服              —— 渲染所有有玩家的服务器卡片；不足 4 张时按 id 顺序补足
  /查服 <编号>       —— 文字详情（编号取自 /查服 列表）
  /查人 <关键字>     —— 0 命中: 文字; 1~4 命中: 图片; ≥5 命中: 文字列表
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from nonebot import get_driver, on_command, require
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

require("nonebot_plugin_htmlrender")
from nonebot_plugin_htmlrender import template_to_pic  # noqa: E402

from .helpers import (  # noqa: E402
    build_server_card,
    clean_map_name,
    find_player,
    fmt_seconds,
    infer_difficulty,
    infer_mode,
    map_progress,
    players_have_steam_ids,
    score_to_steamid64,
)
from .sse_client import store  # noqa: E402

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
MIN_CARDS = 6
PLAYER_IMG_THRESHOLD = 4  # >4 → text list

driver = get_driver()


@driver.on_startup
async def _start_sse() -> None:
    store.start()


@driver.on_shutdown
async def _stop_sse() -> None:
    await store.stop()


def _ordered_server_list() -> list[dict]:
    """Canonical server ordering shared by /查服 list and /查服 N detail.

    Active servers (numPlayers > 0) sorted by player count desc then id;
    if fewer than MIN_CARDS, top up with idle servers in id-ascending order.
    """
    all_known = store.all()
    actives = [s for s in all_known if (s.get("numPlayers") or 0) > 0]
    actives.sort(
        key=lambda s: (-(s.get("numPlayers") or 0), s.get("id") or 0)
    )
    chosen_ids = {s.get("id") for s in actives}
    idle_pool = sorted(
        (s for s in all_known if s.get("id") not in chosen_ids),
        key=lambda s: s.get("id") or 0,
    )
    ordered = list(actives)
    if len(ordered) < MIN_CARDS:
        ordered.extend(idle_pool[: MIN_CARDS - len(ordered)])
    return ordered


def _index_of(server_id: int, ordered: list[dict]) -> Optional[int]:
    for i, s in enumerate(ordered, start=1):
        if s.get("id") == server_id:
            return i
    return None


# ─────────────────────────── /查服 ───────────────────────────

server_cmd = on_command("查服", priority=10, block=True)


@server_cmd.handle()
async def handle_server_query(
    matcher: Matcher,
    args: Message = CommandArg(),
) -> None:
    if not store.ready:
        if not await store.wait_ready(timeout=8.0):
            await matcher.finish("数据流尚未就绪，请稍后再试。")
            return

    arg_text = args.extract_plain_text().strip()

    # ===== sub-command: /查服 <N> 显示详情 =====
    if arg_text:
        if not arg_text.isdigit():
            await matcher.finish("用法: /查服 [编号]\n编号见 /查服 列表中的 # 号")
            return
        index = int(arg_text)
        await _send_server_detail(matcher, index)
        return

    # ===== /查服 列表 =====
    ordered = _ordered_server_list()

    cards: list[dict] = []
    for s in ordered:
        c = build_server_card(s, allow_idle=True)
        if c:
            cards.append(c)

    if not cards:
        await matcher.finish("当前数据流没有任何服务器条目。")
        return

    # attach 1-based display index
    for i, c in enumerate(cards, start=1):
        c["index"] = i

    total_players = sum(c["num"] for c in cards if not c["idle"])
    active_count = sum(1 for c in cards if not c["idle"])
    stats = {
        "total_players": total_players,
        "active_count": active_count,
        "known_count": len(store.all()),
        "updated_at": _fmt_now(),
    }

    img = await _render(
        "servers.html",
        templates={"servers": cards, "stats": stats},
        viewport=_calc_servers_viewport(cards),
    )
    if img is None:
        await matcher.finish("图片渲染失败，请稍后重试。")
        return

    await matcher.finish(MessageSegment.image(img))


async def _send_server_detail(matcher: Matcher, index: int) -> None:
    """Re-build the same ordered list as /查服 to resolve a 1-based index."""
    ordered = _ordered_server_list()

    if index <= 0 or index > len(ordered):
        await matcher.finish(
            f"编号 {index} 不存在，当前列表共 {len(ordered)} 项。"
        )
        return

    s = ordered[index - 1]
    text = _format_server_detail_text(s, index)
    await matcher.finish(text)


def _format_server_detail_text(server: dict, index: int) -> str:
    name = server.get("name") or "(无名)"
    num = int(server.get("numPlayers") or 0)
    maxp = int(server.get("maxPlayers") or 0)
    cn_map = clean_map_name(server) or "(未知地图)"
    progress = map_progress(server)
    if progress:
        cn_map = f"{cn_map} [{progress}]"
    mode = "对抗" if infer_mode(server) == "versus" else "合作"
    diff = infer_difficulty(server) or "?"
    addr = server.get("address") or ""

    additional = server.get("additional") or {}
    region = additional.get("region") or "未知"

    players = server.get("players") or []
    if players:
        names = [p.get("name") or "(unknown)" for p in players]
        player_list = " / ".join(names)
    else:
        player_list = "(无)"

    lines = [
        f"#{index} {name}",
        f"人数: {num}/{maxp}",
        f"地图: {cn_map}",
        f"模式: {mode} · {diff}",
        f"地区: {region}",
        f"玩家列表: {player_list}",
        f"connect {addr}",
    ]
    if (server.get("teamWipes") or 0) > 0:
        lines.insert(4, f"团灭: {server['teamWipes']} 次")
    return "\n".join(lines)


# ─────────────────────────── /查人 ───────────────────────────

player_cmd = on_command("查人", priority=10, block=True)


@player_cmd.handle()
async def handle_player_query(
    matcher: Matcher,
    args: Message = CommandArg(),
) -> None:
    query = args.extract_plain_text().strip()
    if not query:
        await matcher.finish("用法: /查人 <玩家昵称关键字>")
        return

    if not store.ready:
        if not await store.wait_ready(timeout=8.0):
            await matcher.finish("数据流尚未就绪，请稍后再试。")
            return

    cards = [c for c in (build_server_card(s) for s in store.all()) if c]
    matches = find_player(cards, query)

    # ===== 0 hits → text =====
    if not matches:
        await matcher.finish(f"未找到名为「{query}」的玩家。")
        return

    # tag every match with the canonical /查服 index (None if not in current list)
    ordered = _ordered_server_list()
    for m in matches:
        m["index"] = _index_of(m["server"].get("id"), ordered)

    # ===== ≥5 hits → text list =====
    if len(matches) > PLAYER_IMG_THRESHOLD:
        await matcher.finish(_format_player_text_list(query, matches))
        return

    # ===== 1~4 hits → image =====
    hits_for_render = []
    for m in matches:
        s = m["server"]
        p = m["player"]
        all_players = s.get("players") or []
        steam_id64: Optional[str] = None
        if players_have_steam_ids(all_players) and isinstance(p.get("score"), int):
            try:
                steam_id64 = score_to_steamid64(p["score"])
            except Exception:  # noqa: BLE001
                steam_id64 = None
        hits_for_render.append(
            {
                "server": s,
                "player": p,
                "player_time": fmt_seconds(p.get("time", 0)),
                "steam_id64": steam_id64,
                "index": m["index"],
            }
        )

    img = await _render(
        "player.html",
        templates={
            "query": query,
            "hits": hits_for_render,
            "updated_at": _fmt_now(),
        },
        viewport=_calc_player_viewport(len(hits_for_render)),
    )
    if img is None:
        await matcher.finish("图片渲染失败，请稍后重试。")
        return
    await matcher.finish(MessageSegment.image(img))


def _format_player_text_list(query: str, matches: list[dict]) -> str:
    lines = [f"匹配「{query}」共 {len(matches)} 名玩家："]
    for i, m in enumerate(matches, start=1):
        s = m["server"]
        p = m["player"]
        cn_map = s.get("map") or "?"
        if s.get("progress"):
            cn_map = f"{cn_map} [{s['progress']}]"
        idx = m.get("index")
        idx_label = f"#{idx} " if idx else ""
        lines.append(
            f"{i}. {p.get('name', '?')}  @  {idx_label}{s.get('name', '?')}  ·  {cn_map}  ({s.get('num', 0)}/{s.get('max', 0)})"
        )
    lines.append("命中过多，请用更精确关键字再查；可用 /查服 [编号] 看详情。")
    return "\n".join(lines)


# ─────────────────────────── render helpers ───────────────────────────

def _fmt_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _calc_servers_viewport(cards: list[dict]) -> dict:
    cols = 3
    width = 980
    rows = (len(cards) + cols - 1) // cols
    base_row = 120
    extra = 0
    for r in range(rows):
        chunk = cards[r * cols : r * cols + cols]
        max_players = max((c["num"] for c in chunk), default=0)
        chip_lines = max(1, (max_players + 2) // 3)
        extra += min(chip_lines, 3) * 18
    height = 90 + base_row * rows + extra + 40
    return {"width": width, "height": int(max(height, 320))}


def _calc_player_viewport(hit_count: int) -> dict:
    width = 820
    if hit_count == 0:
        return {"width": width, "height": 240}
    height = 90 + hit_count * 240 + 40
    return {"width": width, "height": int(height)}


async def _render(
    template_name: str,
    templates: dict,
    viewport: dict,
) -> Optional[bytes]:
    """Render with JPEG + 1× scale for speed."""
    try:
        return await template_to_pic(
            template_path=TEMPLATE_DIR,
            template_name=template_name,
            templates=templates,
            pages={
                "viewport": viewport,
                "base_url": f"file://{TEMPLATE_DIR}/",
            },
            type="jpeg",
            quality=85,
            device_scale_factor=1,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"[kitcoda] render '{template_name}' failed: {e!r}")
        return None
