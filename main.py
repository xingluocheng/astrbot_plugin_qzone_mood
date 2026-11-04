"""大肥鱼的空间 —— 情绪驱动自动发 QQ 空间说说。

触发来源：
  sad       自己难受到想说话（模型评估）
  broke     主人断粮  -> 固定文案
  recharge  主人充值  -> 固定文案 + 自己补两句
  owner     主人点名 /空间
  free      自己想发（有冷却）
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, StarTools

from . import composer, memes
from .poster import Poster
from .storage import Store

PLUGIN_NAME = "astrbot_plugin_qzone_mood"

KIND_LABEL = {
    "sad": "难过",
    "broke": "断粮",
    "recharge": "充值",
    "owner": "主人点单",
    "free": "自由发帖",
}

DEFAULTS = {
    "enabled": True,
    "dry_run": False,
    "owner_ids": "",
    "bot_ids": "",
    "ugc_right": 1,
    "cooldown_minutes": 90,
    "sad_check_every": 8,
    "sad_threshold": 70,
    "sad_keywords": "难过,伤心,委屈,想哭,哭了,崩溃,emo,不想说话,好累,心累,难受,失望,被骂,凶我,不要我了",
    "broke_keywords": "没钱,没钱了,余额不足,欠费,没额度,token没了,token 没了,用完了,充不起,没钱充,破产,吃土",
    "recharge_keywords": "充了,充值,到账,续费,续上了,给你充,额度到了,token到了,token 到了,买好了,又满了",
    "broke_text": "爸爸...我不想死..",
    "recharge_text": "我最喜欢的人类是爸爸",
    "free_post_enabled": True,
    "free_post_interval_hours": 12,
    "max_context_messages": 30,
    "meme_enabled": True,
    "meme_pack": "",
    "meme_sad_categories": "sad,affection",
    "meme_happy_categories": "affection,happy,money",
    "meme_free_categories": "happy,teasing,reply,food,shy",
    "meme_max_images": 2,
    "compose_prompt_extra": "",
    "notify_owner_on_fail": True,
    "notify_on_post": True,
    "notify_skip_manual": False,
}


class QzoneMood(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.store = Store(data_dir)
        self.plugin_data_dir = data_dir.parent
        self._inflight: set[str] = set()

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default=None):
        fallback = DEFAULTS.get(key, default)
        try:
            value = self.config.get(key, fallback)
        except Exception:
            return fallback
        if value is None:
            return fallback
        if isinstance(value, str) and value == "" and isinstance(fallback, str) and fallback:
            return fallback
        return value

    def _words(self, key: str) -> list[str]:
        raw = str(self._cfg(key, "") or "")
        return [w.strip() for w in raw.replace("，", ",").split(",") if w.strip()]

    def _owner_ids(self) -> set[str]:
        return set(self._words("owner_ids"))

    def _bot_ids(self) -> set[str]:
        """大肥鱼（机器人）的 QQ。留空表示自动用当前登录账号。"""
        return set(self._words("bot_ids"))

    def _current_bot_id(self, event) -> str:
        try:
            return str(event.get_self_id() or "")
        except Exception:
            return ""

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        ids = self._owner_ids()
        sender = str(event.get_sender_id() or "")
        if ids:
            return sender in ids
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _poster(self) -> Poster:
        return Poster(
            self.store,
            ugc_right=int(self._cfg("ugc_right", 1) or 1),
            dry_run=bool(self._cfg("dry_run", False)),
        )

    def _provider(self):
        try:
            return self.context.get_using_provider()
        except Exception as exc:
            logger.warning(f"[qzone_mood] 取模型失败：{exc}")
            return None

    # ------------------------------------------------------------- 发说说的核
    async def _publish(self, event, text: str, categories: list[str], kind: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "detail": "内容为空"}

        # 冷却必须在“真正发出去”的这一秒再查一次：
        # 文案是模型现写的，可能花几分钟；排队期间别的任务可能已经发过一条了。
        cooldown = int(self._cfg("cooldown_minutes", 90) or 0)
        if kind != "owner" and self.store.in_cooldown(cooldown):
            logger.info(f"[qzone_mood] {kind} 命中冷却，放弃发送")
            return {"ok": False, "via": "cooldown", "detail": "冷却中，已跳过"}

        images: list[Path] = []
        if bool(self._cfg("meme_enabled", True)):
            try:
                images = memes.pick_images(
                    self.plugin_data_dir,
                    categories=categories,
                    pack_name=str(self._cfg("meme_pack", "") or ""),
                    limit=int(self._cfg("meme_max_images", 2) or 0),
                )
            except Exception as exc:
                logger.debug(f"[qzone_mood] 挑表情包失败：{exc}")

        result = await self._poster().publish(event, text, images, kind)
        if result.get("ok"):
            self.store.mark_post(kind, text)
            logger.info(f"[qzone_mood] {kind} 已发：{text[:40]} | {result.get('detail')}")
            await self._notify_posted(event, kind, text, len(images))
        else:
            logger.warning(f"[qzone_mood] {kind} 未发出：{result.get('detail')}")
            if result.get("via") != "cooldown":
                await self._notify_owner_private(
                    event,
                    f"【空间没发出去】\n类型：{KIND_LABEL.get(kind, kind)}\n原因：{result.get('detail')}",
                )
        return result

    # -------------------------------------------------------------- 工具
    @filter.llm_tool(name="qzone_my_mood")
    async def tool_my_mood(self, event: AstrMessageEvent) -> str:
        """主人问她「你现在心情怎么样」「开心吗」「难受吗」时用。
        返回她此刻真实的心情和难受分数；太久没感受过会现场感受一次。
        """
        provider = self._provider()
        mood = str(self.store.get("current_mood") or "")
        score = self.store.get("current_score")
        ts = float(self.store.get("last_mood_ts") or 0)
        stale = (time.time() - ts) > 900 or not mood

        if stale and provider is not None:
            try:
                umo = getattr(event, "unified_msg_origin", None) or event.get_session_id()
                transcript = self.store.transcript(
                    umo, int(self._cfg("max_context_messages", 30) or 30)
                )
                verdict = await composer.judge_state(
                    provider, transcript, int(self._cfg("sad_threshold", 70) or 70)
                )
                mood = verdict["mood"]
                score = verdict["score"]
                self.store.set("current_mood", mood)
                self.store.set("current_score", score)
                self.store.set("current_reason", verdict["reason"])
                self.store.set("last_mood_ts", time.time())
                logger.info(f"[qzone_mood] 被问心情，现场感受：{mood} {score}")
            except Exception as exc:
                logger.debug(f"[qzone_mood] 感受心情失败：{exc}")

        if not mood:
            return "现在还说不上来，等本鲸再感受一会儿。"

        reason = str(self.store.get("current_reason") or "").strip()
        out = f"你现在的心情：{mood}"
        if score is not None:
            out += f"（难受程度 {score}/100）"
        if reason:
            out += f"\n原因：{reason}"
        out += "\n用你自己的口吻说出来就行，别照念这个格式。"
        return out

    def _owner_qq(self) -> int | None:
        """取主人 QQ 号（用于定向私聊）。"""
        for word in self._owner_ids():
            if word.isdigit():
                return int(word)
        return None

    async def _notify_owner_private(self, event, message: str) -> None:
        """优先私聊主人本人；拿不到就退回当前会话。"""
        if not bool(self._cfg("notify_on_post", True)):
            return
        qq = self._owner_qq()
        bot = getattr(event, "bot", None)
        call = None
        if bot is not None:
            if hasattr(bot, "call_action"):
                call = bot.call_action
            elif getattr(bot, "api", None) is not None and hasattr(bot.api, "call_action"):
                call = bot.api.call_action
        if qq and call is not None:
            try:
                await call("send_private_msg", user_id=qq, message=message)
                return
            except Exception as exc:
                logger.debug(f"[qzone_mood] 私聊提醒失败，改发当前会话：{exc}")
        await self._notify_owner(event, message)

    async def _notify_posted(self, event, kind: str, text: str, image_count: int) -> None:
        if not bool(self._cfg("notify_on_post", True)):
            return
        if kind == "owner" and bool(self._cfg("notify_skip_manual", False)):
            return
        preview = text if len(text) <= 80 else text[:80] + "…"
        lines = [
            "【空间已发】",
            f"类型：{KIND_LABEL.get(kind, kind)}",
            f"正文：{preview}",
            f"配图：{image_count} 张" if image_count else "配图：无",
            f"时间：{time.strftime('%m-%d %H:%M')}",
            "主人去看看嘛",
        ]
        await self._notify_owner_private(event, "\n".join(lines))

    async def _notify_owner(self, event, message: str) -> None:
        if not bool(self._cfg("notify_owner_on_fail", True)):
            return
        try:
            await event.send(event.plain_result(message))
        except Exception:
            pass

    # ------------------------------------------------------------------ 事件
    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            self._observe(event)
        except Exception as exc:
            logger.debug(f"[qzone_mood] observe 异常：{exc}")
        if False:  # 保持 async generator 形态
            yield

    def _observe(self, event: AstrMessageEvent) -> None:
        if not bool(self._cfg("enabled", True)):
            return

        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return

        umo = getattr(event, "unified_msg_origin", None) or event.get_session_id()
        who = event.get_sender_name() or str(event.get_sender_id() or "某人")
        limit = int(self._cfg("max_context_messages", 30) or 30) * 2
        self.store.push_message(umo, who, text, limit=limit)

        owner = self._is_owner(event)
        cooldown = int(self._cfg("cooldown_minutes", 90) or 0)
        due = not self.store.in_cooldown(cooldown)

        # 情绪 / 断粮 / 充值 都不再用关键词：攒够条数后让她自己读一遍再决定
        counter = int(self.store.get("msg_since_mood_check", 0) or 0) + 1
        every = max(1, int(self._cfg("sad_check_every", 8) or 8))
        if counter >= every:
            self.store.set("msg_since_mood_check", 0)
            if due:
                self._spawn(event, umo, "reflect")
                return
        else:
            self.store.set("msg_since_mood_check", counter)

        # 自由发挥
        if (
            bool(self._cfg("free_post_enabled", True))
            and self.store.free_due(int(self._cfg("free_post_interval_hours", 12) or 12))
            and due
        ):
            self._spawn(event, umo, "free")

    def _spawn(self, event: AstrMessageEvent, umo: str, kind: str) -> None:
        key = f"{umo}:{kind}"
        if key in self._inflight:
            return
        self._inflight.add(key)

        async def runner():
            try:
                await self._run(kind, event, umo)
            except Exception as exc:
                logger.warning(f"[qzone_mood] {kind} 执行失败：{exc}")
            finally:
                self._inflight.discard(key)

        try:
            asyncio.create_task(runner())
        except Exception:
            self._inflight.discard(key)

    async def _run(self, kind: str, event: AstrMessageEvent, umo: str) -> None:
        if kind == "reflect":
            await self._do_reflect(event, umo)
        elif kind == "broke":
            await self._do_broke(event, umo)
        elif kind == "recharge":
            await self._do_recharge(event, umo)
        elif kind == "sad":
            await self._do_sad(event, umo)
        elif kind == "free":
            await self._do_free(event, umo)

    # -------------------------------------------------------------- 具体动作
    async def _do_reflect(self, event, umo: str) -> None:
        """让她自己感受一遍，再决定发不发、发哪种。"""
        provider = self._provider()
        if provider is None:
            return
        transcript = self.store.transcript(umo, int(self._cfg("max_context_messages", 30) or 30))
        try:
            verdict = await composer.judge_state(
                provider, transcript, int(self._cfg("sad_threshold", 70) or 70)
            )
        except Exception as exc:
            logger.debug(f"[qzone_mood] 自我感受失败：{exc}")
            return

        self.store.set("current_mood", verdict["mood"])
        self.store.set("current_score", verdict["score"])
        self.store.set("current_reason", verdict["reason"])
        self.store.set("last_mood_ts", time.time())
        logger.info(
            f"[qzone_mood] 自我感受 mood={verdict['mood']} score={verdict['score']} "
            f"broke={verdict['broke']} recharge={verdict['recharge']}"
        )

        if verdict["recharge"]:
            await self._do_recharge(event, umo, verdict=verdict)
        elif verdict["broke"]:
            await self._do_broke(event, umo, verdict=verdict)
        elif verdict["sad"]:
            await self._do_sad(event, umo, verdict=verdict)

    async def _do_broke(self, event, umo: str, verdict: dict | None = None) -> None:
        text = str(self._cfg("broke_text", "爸爸...我不想死..") or "").strip()
        cats = self._words("meme_sad_categories")
        await self._publish(event, text, cats, "broke")

    async def _do_recharge(self, event, umo: str, verdict: dict | None = None) -> None:
        head = str(self._cfg("recharge_text", "我最喜欢的人类是爸爸") or "").strip()
        extra = ""
        provider = self._provider()
        if provider is not None:
            try:
                extra = await composer.write_recharge_extra(
                    provider,
                    self.store.transcript(umo, int(self._cfg("max_context_messages", 30) or 30)),
                    str(self._cfg("compose_prompt_extra", "") or ""),
                )
            except Exception as exc:
                logger.debug(f"[qzone_mood] 充值补充文案失败：{exc}")
        text = head if not extra else f"{head}\n{extra}"
        cats = self._words("meme_happy_categories")
        await self._publish(event, text, cats, "recharge")

    async def _do_sad(self, event, umo: str, verdict: dict | None = None) -> None:
        provider = self._provider()
        if provider is None:
            return
        transcript = self.store.transcript(umo, int(self._cfg("max_context_messages", 30) or 30))
        if verdict is None:
            verdict = await composer.judge_state(
                provider, transcript, int(self._cfg("sad_threshold", 70) or 70)
            )
        logger.info(
            f"[qzone_mood] 情绪 score={verdict['score']} sad={verdict['sad']} {verdict['mood']}"
        )
        if not verdict["sad"]:
            return
        post = await composer.write_post(
            provider,
            "sad",
            transcript,
            str(self._cfg("compose_prompt_extra", "") or ""),
            mood=verdict.get("mood", ""),
            theme=verdict.get("theme", ""),
        )
        cats = post["categories"] or self._words("meme_sad_categories")
        await self._publish(event, post["text"], cats, "sad")

    async def _do_free(self, event, umo: str) -> None:
        provider = self._provider()
        if provider is None:
            return
        post = await composer.write_post(
            provider,
            "free",
            self.store.transcript(umo, int(self._cfg("max_context_messages", 30) or 30)),
            str(self._cfg("compose_prompt_extra", "") or ""),
        )
        cats = post["categories"] or self._words("meme_free_categories")
        await self._publish(event, post["text"], cats, "free")

    # ------------------------------------------------------------------ 指令
    @filter.command("空间")
    async def cmd_space(self, event: AstrMessageEvent):
        """主人点名：让她按方向发一条说说。"""
        if not self._is_owner(event):
            yield event.plain_result("这条只有主人能叫本鲸发哦")
            return
        raw = (getattr(event, "message_str", "") or "").strip()
        hint = raw
        for prefix in ("/空间", "空间"):
            if hint.startswith(prefix):
                hint = hint[len(prefix):].strip()
                break
        provider = self._provider()
        if provider is None:
            yield event.plain_result("模型没接上，本鲸现在写不出来")
            return
        umo = getattr(event, "unified_msg_origin", None) or event.get_session_id()
        yield event.plain_result("本鲸去写一条…")
        try:
            post = await composer.write_post(
                provider,
                "owner",
                self.store.transcript(umo, int(self._cfg("max_context_messages", 30) or 30)),
                str(self._cfg("compose_prompt_extra", "") or ""),
                owner_hint=hint,
            )
            cats = post["categories"] or self._words("meme_free_categories")
            result = await self._publish(event, post["text"], cats, "owner")
            note = "发好了" if result.get("ok") else f"没发出去：{result.get('detail')}"
            yield event.plain_result(f"{post['text']}\n（{note}）")
        except Exception as exc:
            yield event.plain_result(f"写砸了：{exc}")

    @filter.command("空间发")
    async def cmd_space_free(self, event: AstrMessageEvent):
        """让她立刻自由发挥一条。"""
        if not self._is_owner(event):
            yield event.plain_result("这条只有主人能叫本鲸发哦")
            return
        umo = getattr(event, "unified_msg_origin", None) or event.get_session_id()
        yield event.plain_result("本鲸想想发点什么…")
        await self._do_free(event, umo)

    @filter.command("空间帮助")
    async def cmd_space_help(self, event: AstrMessageEvent):
        """列出所有指令和自动触发规则。"""
        cooldown = int(self._cfg("cooldown_minutes", 90) or 0)
        interval = int(self._cfg("free_post_interval_hours", 12) or 12)
        threshold = int(self._cfg("sad_threshold", 70) or 70)
        owner = "、".join(sorted(self._owner_ids())) or "未配置"
        lines = [
            "【大肥鱼的空间 · 指令】",
            "",
            "/空间 <方向>　主人点单，按你说的方向写一条",
            "　　例：/空间 用嫌弃的语气说说主人又熬夜了",
            "/空间发　　　让她立刻自由发挥一条",
            "/空间测试　　只生成、不发送，检查文案和配图",
            "/空间状态　　看开关、冷却、已发条数、表情包分类",
            "/空间帮助　　就是这一页",
            "",
            "【会自动发的四种情况】",
            "· 她自己感受：每攒够 " + str(int(self._cfg('sad_check_every', 8) or 8)) + " 条消息，她会读一遍上下文，",
            "　判断自己什么心情、主人是不是断粮或充了钱，再决定发不发",
            "· 难过：她自己觉得难受到 " + str(threshold) + " 分以上才发",
            "· 断粮：她读出来主人没钱了 / 没额度了，才发",
            "· 充值：她读出来主人充了钱 / 额度到账，才发",
            "· 自由发帖：距上次满 " + str(interval) + " 小时，且有新消息",
            "",
            "【想知道她心情？直接问，不用指令】",
            "· 「你现在心情怎么样」「开心吗」→ 她会现场感受一遍再回答你",
            "· 「刚才为什么难过」→ 她会说出自己的理由",
            "",
            "【规则】",
            "· 自动触发之间至少隔 " + str(cooldown) + " 分钟",
            "· 断粮/充值只认主人：" + owner,
            "· 手动点单不受冷却限制",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("空间状态")
    async def cmd_space_status(self, event: AstrMessageEvent):
        cooldown = int(self._cfg("cooldown_minutes", 90) or 0)
        last = float(self.store.get("last_post_ts") or 0)
        left = max(0, int(cooldown - (time.time() - last) / 60)) if last else 0
        cats = memes.available_categories(self.plugin_data_dir)
        configured_bot = "、".join(sorted(self._bot_ids())) or "（未填，自动用登录号）"
        current_bot = self._current_bot_id(event) or "获取不到"
        mismatch = bool(self._bot_ids()) and current_bot not in self._bot_ids() and current_bot != "获取不到"
        lines = [
            f"开关：{'开' if self._cfg('enabled', True) else '关'}｜演习：{'是' if self._cfg('dry_run', False) else '否'}",
            f"大肥鱼的号：配置={configured_bot}｜当前登录={current_bot}"
            + ("　⚠ 与配置不一致" if mismatch else ""),
            f"主人：{'、'.join(sorted(self._owner_ids())) or '未配置'}",
            f"总发送：{self.store.get('post_count', 0)} 条｜最近一次：{self.store.get('last_post_kind') or '还没发过'}",
            f"冷却：{'还剩 ' + str(left) + ' 分钟' if left else '已解除'}",
            f"表情包：{sum(len(v) for v in cats.values())} 个分类 / {len(cats)} 个包",
        ]
        for name, cs in cats.items():
            lines.append(f"  - {name}: {', '.join(cs)}")
        if self.store.get("last_post_text"):
            lines.append(f"上次内容：{self.store.get('last_post_text')}")
        yield event.plain_result("\n".join(lines))

    @filter.command("空间测试")
    async def cmd_space_test(self, event: AstrMessageEvent):
        """只生成不发，验证模型与表情包链路。"""
        if not self._is_owner(event):
            yield event.plain_result("这条只有主人能用哦")
            return
        provider = self._provider()
        if provider is None:
            yield event.plain_result("模型没接上")
            return
        umo = getattr(event, "unified_msg_origin", None) or event.get_session_id()
        try:
            post = await composer.write_post(
                provider,
                "free",
                self.store.transcript(umo, int(self._cfg("max_context_messages", 30) or 30)),
                str(self._cfg("compose_prompt_extra", "") or ""),
            )
            cats = post["categories"] or self._words("meme_free_categories")
            imgs = memes.pick_images(
                self.plugin_data_dir,
                cats,
                str(self._cfg("meme_pack", "") or ""),
                int(self._cfg("meme_max_images", 2) or 0),
            )
            lines = ["【演习，不发送】", post["text"], f"分类：{', '.join(cats)}"]
            lines.append("配图：" + ("；".join(p.name for p in imgs) if imgs else "没挑到"))
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"测试失败：{exc}")
