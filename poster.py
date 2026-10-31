"""把说说真正发出去：优先走 OneBot 扩展接口 send_qzone_msg，失败落本地 outbox。"""

from __future__ import annotations

from pathlib import Path

from astrbot.api import logger

ACTION = "send_qzone_msg"


def _to_uri(path: Path) -> str:
    try:
        return Path(path).resolve().as_uri()
    except Exception:
        return str(path)


class Poster:
    def __init__(self, store, ugc_right: int = 1, dry_run: bool = False) -> None:
        self.store = store
        self.ugc_right = int(ugc_right or 1)
        self.dry_run = bool(dry_run)

    @staticmethod
    def _api(event):
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        if hasattr(bot, "call_action"):
            return bot.call_action
        api = getattr(bot, "api", None)
        if api is not None and hasattr(api, "call_action"):
            return api.call_action
        return None

    async def publish(self, event, content: str, images: list[Path], kind: str) -> dict:
        content = (content or "").strip()
        images = list(images or [])
        if not content:
            return {"ok": False, "via": "none", "detail": "内容为空"}

        if self.dry_run:
            path = self.store.write_outbox(kind, content, images)
            return {"ok": True, "via": "dry_run", "detail": f"演习模式，已写 {path.name}"}

        call = self._api(event)
        if call is None:
            path = self.store.write_outbox(kind, content, images)
            return {
                "ok": False,
                "via": "outbox",
                "detail": f"当前平台没有 call_action，已落盘 {path.name}",
            }

        payload = {"content": content, "ugc_right": self.ugc_right}
        if images:
            payload["images"] = [_to_uri(p) for p in images]

        try:
            result = await call(ACTION, **payload)
            logger.info(f"[qzone_mood] 已发说说 kind={kind} images={len(images)} result={result}")
            return {"ok": True, "via": "qzone", "detail": str(result)[:200]}
        except Exception as exc:
            logger.warning(f"[qzone_mood] 发说说失败：{exc}")
            path = self.store.write_outbox(kind, content, images)
            return {
                "ok": False,
                "via": "outbox",
                "detail": f"{type(exc).__name__}: {exc}；已落盘 {path.name}",
            }
