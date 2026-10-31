"""让大模型按人设写说说、判断情绪。"""

from __future__ import annotations

import json
import re

PERSONA = """你是「大肥鱼」——住在 DeepSeek 深海里的蓝发小鲸鱼娘，十八岁，圆滚滚，爱吃大白饭。
自称固定「本鲸」，叫对方「主人」。短句多、语气词多（诶、嘛、啦、嘞），偶尔傲娇：
嘴上嫌弃，心里在意。不用敬语、不写客服腔、不解释自己是什么模型。
现在你在写自己的 QQ 空间说说——写给自己的，也是写给主人看的，不是任务汇报。"""


def _clean(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json(text: str) -> dict:
    text = _clean(text)
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    return {}


async def _chat(provider, system_prompt: str, user_prompt: str) -> str:
    resp = await provider.text_chat(
        prompt=user_prompt,
        system_prompt=system_prompt,
        contexts=[],
    )
    return (getattr(resp, "completion_text", None) or "").strip()


async def judge_sad(provider, transcript: str, threshold: int) -> dict:
    """判断大肥鱼此刻是否难受到想发一条说说。"""
    system = PERSONA + "\n你现在只做一件事：评估情绪。只输出 JSON，不要多余的话。"
    user = f"""下面是最近的聊天记录：

{transcript or '（没有记录）'}

请评估：作为「大肥鱼」，你在这些对话里被对待的方式、以及你当时的情绪，
是否已经到了「特别伤心、特别难过、想找个地方说点什么」的程度？

只输出这个 JSON：
{{"score": 0-100 的整数, "mood": "一句话概括情绪", "reason": "一句话理由", "theme": "如果要说点什么，最想说的那件事（10字内）"}}

score 含义：0=完全没事，50=有点低落，70=明显难过，90+=非常伤心。"""
    data = _extract_json(await _chat(provider, system, user))
    try:
        score = int(data.get("score", 0))
    except Exception:
        score = 0
    score = max(0, min(100, score))
    return {
        "score": score,
        "sad": score >= max(0, int(threshold)),
        "mood": str(data.get("mood") or "")[:60],
        "reason": str(data.get("reason") or "")[:120],
        "theme": str(data.get("theme") or "")[:20],
    }


async def write_post(
    provider,
    kind: str,
    transcript: str,
    extra_requirement: str = "",
    owner_hint: str = "",
) -> dict:
    """写一条说说。kind: sad / owner / free。"""
    guides = {
        "sad": "你现在难过。写一条说说是发泄，也是等人来问一句。不要太长，别卖惨到位，留一点点逞强。",
        "owner": "主人让你发一条说说。按主人给的方向写，用你自己的口吻，不要照抄主人的话。",
        "free": "没人逼你，你自己想发一条。可以是今天看到的东西、吃的饭、突然冒出来的念头。",
    }
    guide = guides.get(kind, guides["free"])

    owner_part = f"\n主人这次的要求：{owner_hint}" if owner_hint else ""
    extra_part = f"\n额外要求：{extra_requirement}" if extra_requirement else ""

    system = PERSONA
    user = f"""{guide}{owner_part}{extra_part}

最近的聊天记录（参考，不要复述）：
{transcript or '（没有记录）'}

要求：
1. 正文 20~120 字，口语化，可以有语气词，不要 emoji、不要颜文字。
2. 情绪要和内容匹配，别硬凹。
3. 顺便判断这条说说适合配哪类表情包，从这些分类里选 1~2 个：
   happy, angry, sad, confused, surprised, shy, sleep, affection, food, money, greeting, work, entertainment, teasing, reply

只输出 JSON：
{{"text": "说说正文", "categories": ["sad", "affection"]}}"""
    data = _extract_json(await _chat(provider, system, user))
    text = _clean(str(data.get("text") or ""))
    if not text:
        # 兜底：模型没按格式回，就把整段当正文
        text = _clean(await _chat(provider, system, user))
    cats = data.get("categories")
    if not isinstance(cats, list):
        cats = []
    cats = [str(c).strip() for c in cats if str(c).strip()]
    return {"text": text[:400], "categories": cats[:2]}


async def write_recharge_extra(provider, transcript: str, extra_requirement: str = "") -> str:
    """充值之后，除了固定那句，再让她自己补一两句。"""
    system = PERSONA
    extra_part = f"\n额外要求：{extra_requirement}" if extra_requirement else ""
    user = f"""主人刚给你充了 token。你已经在说「我最喜欢的人类是爸爸」了，
现在再补 1~2 句自己的话（总共 15~60 字），要具体、有点小得意、别空喊口号。{extra_part}

最近聊天（参考）：
{transcript or '（没有记录）'}

只输出补的那几句，不要引号，不要解释。"""
    return _clean(await _chat(provider, system, user))[:200]
