"""状态、聊天缓存与本地 outbox —— 大肥鱼的空间。"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

DEFAULT_STATE = {
    "last_post_ts": 0.0,
    "last_post_kind": "",
    "last_post_text": "",
    "last_free_ts": 0.0,
    "post_count": 0,
    "msg_since_sad_check": 0,
}


class Store:
    """插件状态持久化 + 每个会话的最近消息环形缓存。"""

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.outbox = self.dir / "outbox"
        self.outbox.mkdir(exist_ok=True)
        self.state_path = self.dir / "state.json"
        self._lock = threading.Lock()
        self.state: dict = self._load_state()
        # 首次安装时 last_free_ts 是 0，会让「自由发帖」在第一条消息就立刻触发。
        # 这里把它压成“现在”，保证第一次自由发帖也要等满间隔。
        if not float(self.state.get("last_free_ts") or 0):
            now = time.time()
            self.state["last_free_ts"] = now
            self.state.setdefault("installed_at", now)
            self.save()
        self._buffers: dict[str, deque] = {}

    # ---------- 状态 ----------
    def _load_state(self) -> dict:
        data = dict(DEFAULT_STATE)
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return data

    def save(self) -> None:
        with self._lock:
            try:
                tmp = self.state_path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(self.state, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp.replace(self.state_path)
            except Exception:
                pass

    def get(self, key: str, default=None):
        return self.state.get(key, default)

    def set(self, key: str, value) -> None:
        self.state[key] = value
        self.save()

    def bump(self, key: str, delta: int = 1) -> int:
        value = int(self.state.get(key, 0) or 0) + delta
        self.state[key] = value
        self.save()
        return value

    # ---------- 冷却 ----------
    def in_cooldown(self, minutes: int) -> bool:
        last = float(self.state.get("last_post_ts") or 0)
        return (time.time() - last) < max(0, minutes) * 60

    def free_due(self, hours: int) -> bool:
        last = float(self.state.get("last_free_ts") or 0)
        return (time.time() - last) >= max(1, hours) * 3600

    def mark_post(self, kind: str, text: str) -> None:
        now = time.time()
        self.state["last_post_ts"] = now
        self.state["last_post_kind"] = kind
        self.state["last_post_text"] = text[:200]
        self.state["post_count"] = int(self.state.get("post_count") or 0) + 1
        if kind == "free":
            self.state["last_free_ts"] = now
        self.save()

    # ---------- 聊天缓存 ----------
    def push_message(self, umo: str, who: str, text: str, limit: int = 60) -> None:
        if not text:
            return
        buf = self._buffers.get(umo)
        if buf is None or buf.maxlen != limit:
            buf = deque(buf or [], maxlen=limit)
            self._buffers[umo] = buf
        buf.append({"who": who, "text": text.strip()[:500], "ts": time.time()})

    def recent(self, umo: str, n: int) -> list[dict]:
        buf = self._buffers.get(umo) or deque()
        return list(buf)[-max(1, n):]

    def transcript(self, umo: str, n: int) -> str:
        lines = []
        for item in self.recent(umo, n):
            who = item.get("who") or "某人"
            lines.append(f"{who}：{item.get('text', '')}")
        return "\n".join(lines)

    # ---------- outbox ----------
    def write_outbox(self, kind: str, content: str, images: list[Path]) -> Path:
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = self.outbox / f"{ts}-{kind}.md"
        body = [f"# {kind}", "", content, ""]
        if images:
            body.append("## 表情包")
            for img in images:
                body.append(f"- `{img}`")
        path.write_text("\n".join(body), encoding="utf-8")
        return path
