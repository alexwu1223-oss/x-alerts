#!/usr/bin/env python3
"""x-alerts: push new posts from chosen X accounts into a 企业微信 group.

Runs on GitHub Actions (see .github/workflows/x-alerts.yml). Settings come from repo secrets:
  TW_AUTH_TOKEN, TW_CT0   cookies of a spare X account
  WATCH_ACCOUNTS          usernames, comma separated
  WECOM_WEBHOOK           企业微信 group bot webhook URL
  python xalerts.py          check once (what the schedule runs)
  python xalerts.py --test   read X once and send a test message
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

os.environ.setdefault("TWS_TELEMETRY", "0")  # turn off twscrape's anonymous usage stats
os.environ.setdefault("TWS_LOG_LEVEL", "WARNING")
try:  # browser-like TLS fingerprint if available
    import curl_cffi  # noqa: F401

    os.environ.setdefault("TWS_HTTP_BACKEND", "curl")
except Exception:
    pass

import httpx  # noqa: E402
from loguru import logger as _loguru  # noqa: E402
from twscrape import API, NoAccountError, gather  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA, exist_ok=True)
STATE_FILE = os.path.join(DATA, "state.json")
DB_FILE = os.path.join(DATA, "accounts.db")

MAX_AGE = 6 * 3600         # never push posts older than this
MAX_PER_ACCOUNT = 5        # per account per run; extra ones are summarized
NO_DATA_ALERT = 30 * 60    # alert after this long without any data from X
ALERT_EVERY = 6 * 3600     # repeat the same alert at most this often
SEEN_KEEP = 800

log = logging.getLogger("x-alerts")

# ------------------------------------------------------------------ logging (public logs: hide names/links)
QUIET = os.environ.get("X2W_QUIET", "1").strip() != "0"
_HIDE: list[str] = []


def redact(s: str) -> str:
    if not QUIET:
        return s
    s = re.sub(r"https?://\S+", "<url>", s)
    s = re.sub(r"@[A-Za-z0-9_]{1,15}", "@***", s)
    s = re.sub(r"(?<!\d)\d{6,}(?!\d)", "<id>", s)
    for w in _HIDE:
        s = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(w) + r"(?![A-Za-z0-9_])", "***", s, flags=re.I)
    return s


class _Redact(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), ()
        return True


def setup_logging() -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    h.addFilter(_Redact())
    log.handlers[:] = [h]
    log.setLevel(logging.INFO)
    _loguru.remove()
    _loguru.add(lambda m: log.warning("[twscrape] %s", str(m).rstrip()), level="WARNING", format="{message}")


def say(s: str = "") -> None:
    print(redact(s), flush=True)


# ------------------------------------------------------------------ config
class ConfigError(Exception):
    pass


@dataclass
class Config:
    auth_token: str
    ct0: str
    accounts: list
    list_id: str
    webhook: str
    retweets: bool
    replies: bool
    quotes: bool
    keywords: list
    exclude: list
    images: bool
    mention_all: bool
    heartbeat: int
    interval: int

    def summary(self) -> str:
        return f"推特列表 {self.list_id}" if self.list_id else "、".join("@" + a for a in self.accounts)


def _env(k: str) -> str:
    return os.environ.get(k, "").strip()


def _bool(k: str, default: bool) -> bool:
    v = _env(k).lower()
    return default if not v else v in ("1", "true", "yes", "on")


def _list(k: str, split_space: bool = False) -> list[str]:
    return [x.strip() for x in re.split(r"[,，;；\s]+" if split_space else r"[,，;；\r\n]+", _env(k)) if x.strip()]


def load_config() -> Config:
    errs = []
    tok, ct0 = _env("TW_AUTH_TOKEN"), _env("TW_CT0")
    if not tok or not ct0:
        errs.append("Secrets TW_AUTH_TOKEN and TW_CT0 are required (cookies of the spare X account)")
    accounts: list[str] = []
    for a in _list("WATCH_ACCOUNTS", split_space=True):
        m = re.search(r"(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,15})", a)
        u = (m.group(1) if m else a).lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", u):
            errs.append(f"WATCH_ACCOUNTS: '{a}' is not a valid X username")
        elif u.lower() not in (x.lower() for x in accounts):
            accounts.append(u)
    lm = re.search(r"(\d{5,})", _env("WATCH_LIST_ID"))
    list_id = lm.group(1) if lm else ""
    if not accounts and not list_id:
        errs.append("Secret WATCH_ACCOUNTS is required (usernames separated by commas)")
    hook = _env("WECOM_WEBHOOK")
    if hook and not hook.startswith("http") and re.fullmatch(r"[0-9a-fA-F-]{20,}", hook):
        hook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + hook
    if not hook.startswith("https://qyapi.weixin.qq.com/"):
        errs.append("Secret WECOM_WEBHOOK is required: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...")
    try:
        hb = int(_env("HEARTBEAT_HOUR") or -1)
    except ValueError:
        hb = -1
    if errs:
        raise ConfigError("Configuration problems:\n  - " + "\n  - ".join(errs))
    _HIDE[:] = [*accounts, list_id] if list_id else accounts
    return Config(tok, ct0, accounts, list_id, hook,
                  retweets=_bool("INCLUDE_RETWEETS", False), replies=_bool("INCLUDE_REPLIES", False),
                  quotes=_bool("INCLUDE_QUOTES", True), keywords=_list("KEYWORDS"), exclude=_list("EXCLUDE_KEYWORDS"),
                  images=_bool("SEND_IMAGES", True), mention_all=_bool("WECOM_MENTION_ALL", False),
                  heartbeat=hb if 0 <= hb <= 23 else -1, interval=90)


# ------------------------------------------------------------------ state (persisted by actions/cache)
class State:
    def __init__(self):
        self.data: dict = {"seen": {}, "uids": {}, "alerts": {}, "pushed": [], "meta": {}}
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            for k in self.data:
                if isinstance(loaded.get(k), type(self.data[k])):
                    self.data[k] = loaded[k]
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("state.json unreadable, starting fresh: %s", e)

    @property
    def meta(self) -> dict:
        return self.data["meta"]

    def initialized(self, key: str) -> bool:
        return key in self.data["seen"]

    def seen(self, key: str) -> set:
        return set(self.data["seen"].get(key, []))

    def add_seen(self, key: str, ids) -> None:
        lst = self.data["seen"].setdefault(key, [])
        have = set(lst)
        lst += [i for i in ids if i not in have]
        del lst[: max(0, len(lst) - SEEN_KEEP)]

    def unsee(self, key: str, tid: int) -> None:
        self.data["seen"][key] = [x for x in self.data["seen"].get(key, []) if x != tid]

    def save(self) -> None:
        cutoff = time.time() - 2 * 86400
        self.data["pushed"] = [t for t in self.data["pushed"] if t > cutoff]
        with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)


# ------------------------------------------------------------------ formatting
def clip_bytes(s: str, n: int) -> str:
    b = s.encode("utf-8")
    return s if len(b) <= n else b[: max(0, n - 3)].decode("utf-8", "ignore") + "…"


def clean_text(tw) -> str:
    t = tw.rawContent or ""
    for link in getattr(tw, "links", None) or []:
        if link and getattr(link, "tcourl", None) and getattr(link, "url", None):
            t = t.replace(link.tcourl, link.url)
    t = re.sub(r"\s*https://t\.co/\w+", "", t)  # leftover t.co links are media placeholders
    return html.unescape(t).strip()


def _reply_target(tw) -> str:
    if getattr(tw, "inReplyToScreenName", None):
        return tw.inReplyToScreenName
    ru = getattr(tw, "inReplyToUser", None)
    return ru.username if ru else ""


def classify(tw) -> str:
    if getattr(tw, "retweetedTweet", None):
        return "retweet"
    if getattr(tw, "inReplyToTweetId", None):
        ru = getattr(tw, "inReplyToUser", None)
        if (ru and ru.id == tw.user.id) or _reply_target(tw).lower() == tw.user.username.lower():
            return "thread"
        return "reply"
    return "quote" if getattr(tw, "quotedTweet", None) else "original"


def should_push(tw, cfg: Config) -> bool:
    kind = classify(tw)
    if (kind == "retweet" and not cfg.retweets) or (kind == "reply" and not cfg.replies) \
            or (kind == "quote" and not cfg.quotes):
        return False
    parts = [clean_text(tw)]
    for sub in (getattr(tw, "retweetedTweet", None), getattr(tw, "quotedTweet", None)):
        if sub:
            parts.append(clean_text(sub))
    text = "\n".join(parts).lower()
    if cfg.keywords and not any(k.lower() in text for k in cfg.keywords):
        return False
    return not any(k.lower() in text for k in cfg.exclude)


@dataclass
class Message:
    md: str
    plain: str
    photos: list
    url: str


def format_tweet(tw, prefix: str = "") -> Message:
    kind = classify(tw)
    u = tw.user
    main = tw.retweetedTweet if kind == "retweet" else tw
    action = {"original": "发推", "thread": "发推（推文串）", "reply": f"回复了 @{_reply_target(tw) or '?'}",
              "quote": "引用转发", "retweet": f"转推了 @{main.user.username}"}[kind]
    body = clean_text(main) or "（无文字）"
    q = getattr(main, "quotedTweet", None)
    q_line = f"@{q.user.username}：{clip_bytes(clean_text(q) or '（无文字）', 600)}" if q else ""
    media = getattr(main, "media", None)
    photos = [p.url for p in (media.photos if media else [])][:4]
    n_video = (len(media.videos) + len(media.animated)) if media else 0
    tags = ([f"图片×{len(photos)}"] if photos else []) + (["视频"] if n_video else [])
    note = f"[{' '.join(tags)}]" if tags else ""
    when = tw.date.astimezone().strftime("%m-%d %H:%M")
    url = main.url

    md = [f"{prefix}**{u.displayname}** <font color=\"comment\">@{u.username}</font> {action}", "",
          clip_bytes(body, 2600)]
    if q_line:
        md += ["", "> " + q_line.replace("\n", " ")]
    if note:
        md.append(f"<font color=\"comment\">{note}</font>")
    md += ["", f"[查看原推]({url})  <font color=\"comment\">{when}</font>"]

    head = f"{prefix}{u.displayname}(@{u.username}) {action}"
    tail = (f"\n{note}" if note else "") + f"\n{url}\n{when}"
    mid = body + (f"\n【引用】{q_line}" if q_line else "")
    plain = f"{head}\n{clip_bytes(mid, max(200, 1990 - len((head + tail).encode())))}{tail}"
    return Message(clip_bytes("\n".join(md), 4000), clip_bytes(plain, 2040), photos, url)


# ------------------------------------------------------------------ 企业微信 group bot
class WeCom:
    def __init__(self, url: str, mention_all: bool, client: httpx.AsyncClient):
        self.url, self.mention_all, self.client = url, mention_all, client
        self.sent: deque = deque()

    async def _throttle(self) -> None:  # official limit: 20 messages per minute per bot
        while True:
            now = time.monotonic()
            while self.sent and now - self.sent[0] > 60:
                self.sent.popleft()
            if len(self.sent) < 19:
                self.sent.append(now)
                return
            await asyncio.sleep(60 - (now - self.sent[0]) + 0.5)

    async def _post(self, payload: dict) -> bool:
        for attempt in range(3):
            await self._throttle()
            try:
                data = (await self.client.post(self.url, json=payload, timeout=20)).json()
            except Exception as e:
                log.warning("WeCom network error (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(3 * (attempt + 1))
                continue
            code = data.get("errcode")
            if code == 0:
                return True
            if code == 45009:
                log.warning("WeCom rate limit, waiting 60 s")
                await asyncio.sleep(61)
                continue
            if code == 93000:
                log.error("WeCom webhook is invalid, or the bot was removed from the group (93000)")
            else:
                log.error("WeCom error: errcode=%s errmsg=%s", code, data.get("errmsg"))
            return False
        return False

    async def send_text(self, text: str) -> bool:
        return await self._post({"msgtype": "text", "text": {"content": clip_bytes(text, 2040)}})

    async def send_tweet(self, msg: Message, images: list) -> bool:
        if self.mention_all:
            ok = await self._post({"msgtype": "text", "text": {"content": msg.plain, "mentioned_list": ["@all"]}})
        else:
            ok = await self._post({"msgtype": "markdown", "markdown": {"content": msg.md}})
        if ok:
            for img in images:
                await self._post({"msgtype": "image", "image": {
                    "base64": base64.b64encode(img).decode(), "md5": hashlib.md5(img).hexdigest()}})
        return ok


async def download_photos(urls: list, client: httpx.AsyncClient) -> list:
    out = []
    for u in urls:
        for size in ("medium", "small"):
            try:
                r = await client.get(f"{u.split('?')[0]}?name={size}", timeout=30)
                if r.status_code == 200 and 0 < len(r.content) <= 2_000_000:
                    out.append(r.content)
                    break
            except Exception as e:
                log.warning("image download failed: %s", e)
                break
    return out


# ------------------------------------------------------------------ X
ACCOUNT = "spare"


class Twitter:
    def __init__(self, cfg: Config, wait_timeout: float | None = None):
        self.api = API(pool=DB_FILE, raise_when_no_account=True, wait_timeout=wait_timeout, wait_interval=2)

    async def login(self, cfg: Config) -> None:
        await self.api.pool.add_account_cookies(ACCOUNT, f"auth_token={cfg.auth_token}; ct0={cfg.ct0}")

    async def status(self) -> tuple[bool, str]:
        for a in await self.api.pool.accounts_info():
            if a["username"] == ACCOUNT:
                return bool(a["active"]), a.get("error_msg") or ""
        return False, ""

    async def user_id(self, name: str, state: State) -> int | None:
        key = name.lower()
        if key not in state.data["uids"]:
            u = await self.api.user_by_login(name)
            if not u:
                return None
            state.data["uids"][key] = u.id
        return int(state.data["uids"][key])

    async def user_tweets(self, uid: int) -> list:
        return await gather(self.api.user_tweets(uid, limit=20))

    async def list_tweets(self, list_id: str) -> list:
        return await gather(self.api.list_timeline(int(list_id), limit=20))


def explain_inactive(err: str) -> str:
    e = (err or "").lower()
    if "326" in e or "88" in e or "denied" in e:
        return "小号可能被推特限制/封禁了"
    return "小号 cookie 已失效（退出登录或过期了）"


# ------------------------------------------------------------------ one run
class Run:
    def __init__(self, cfg: Config, state: State, http: httpx.AsyncClient):
        self.cfg, self.state, self.http = cfg, state, http
        self.bot = WeCom(cfg.webhook, cfg.mention_all, http)
        self.tw = Twitter(cfg)
        self.account_dead = False
        self.last_ok = float(state.meta.get("last_ok") or time.time())

    async def alert(self, key: str, text: str, repeat: float = ALERT_EVERY) -> None:
        if time.time() - self.state.data["alerts"].get(key, 0) < repeat:
            return
        log.warning("alert: %s", text.split("\n")[0])
        if await self.bot.send_text(text):
            self.state.data["alerts"][key] = time.time()

    async def announce_if_changed(self) -> None:
        sig = hashlib.md5(json.dumps([self.cfg.list_id, sorted(a.lower() for a in self.cfg.accounts)]).encode()).hexdigest()
        if self.state.meta.get("announced") != sig and await self.bot.send_text(
                f"✅ 推特监控已开启\n监控对象：{self.cfg.summary()}\n大约每 5～15 分钟检查一次，有新推文会自动推送到这里。"):
            self.state.meta["announced"] = sig

    async def handle_no_account(self) -> None:
        active, err = await self.tw.status()
        if active:
            log.info("X rate-limited or unreachable this time; will retry next run")
            return
        self.account_dead = True
        await self.alert("account", f"⚠️ 推特监控暂停：{explain_inactive(err)}。\n"
                                    "请用浏览器重新登录推特小号，把新的 auth_token 和 ct0 更新到 GitHub 仓库 "
                                    "Settings → Secrets → Actions 里的 TW_AUTH_TOKEN 和 TW_CT0，下一轮会自动恢复。")

    async def sources(self) -> list:
        if self.cfg.list_id:
            return [(f"list:{self.cfg.list_id}", None, lambda: self.tw.list_tweets(self.cfg.list_id))]
        out = []
        for name in self.cfg.accounts:
            uid = await self.tw.user_id(name, self.state)
            if uid is None:
                await self.alert(f"nouser:{name.lower()}",
                                 f"⚠️ 推特监控：找不到用户 @{name}，请检查 WATCH_ACCOUNTS 里的用户名。", 24 * 3600)
                continue
            out.append((f"user:{uid}", uid, (lambda u=uid: self.tw.user_tweets(u))))
        return out

    def pick_new(self, key: str, tweets: list, uid: int | None) -> tuple[list, int]:
        if uid is not None:
            tweets = [t for t in tweets if t.user.id == uid]
        if not self.state.initialized(key):  # first time: remember what exists, push nothing
            self.state.add_seen(key, [t.id for t in tweets])
            return [], 0
        seen, now = self.state.seen(key), time.time()
        fresh = [t for t in tweets if t.id not in seen]
        self.state.add_seen(key, [t.id for t in fresh])
        cand = sorted((t for t in fresh if now - t.date.timestamp() <= MAX_AGE and should_push(t, self.cfg)),
                      key=lambda t: t.id)
        return cand[-MAX_PER_ACCOUNT:], max(0, len(cand) - MAX_PER_ACCOUNT)

    async def push(self, tw) -> bool:
        msg = format_tweet(tw)
        imgs = await download_photos(msg.photos, self.http) if (self.cfg.images and msg.photos) else []
        ok = await self.bot.send_tweet(msg, imgs)
        if ok:
            log.info("pushed %s", msg.url)
            self.state.data["pushed"].append(time.time())
        return ok

    async def cycle(self) -> None:
        if self.account_dead:
            return
        got = False
        try:
            srcs = await self.sources()
        except NoAccountError:
            await self.handle_no_account()
            return
        for i, (key, uid, fetch) in enumerate(srcs):
            if i:
                await asyncio.sleep(random.uniform(1.5, 3.5))
            try:
                tweets = await fetch()
            except NoAccountError:
                await self.handle_no_account()
                break
            except Exception as e:
                log.warning("fetch failed: %s: %s", type(e).__name__, e)
                continue
            if not tweets:
                continue
            got = True
            new, skipped = self.pick_new(key, tweets, uid)
            fails = self.state.meta.setdefault("fails", {})
            for tw in new:
                if await self.push(tw):
                    continue
                n = fails[str(tw.id)] = fails.get(str(tw.id), 0) + 1
                if n < 3:  # retry next run, at most 3 times
                    self.state.unsee(key, tw.id)
            if skipped:
                name = new[0].user.username if new else "?"
                await self.bot.send_text(f"（@{name} 还有 {skipped} 条较早的新推文未逐条推送，可去主页查看）")
            self.state.save()
        if len(self.state.meta.get("fails", {})) > 200:
            self.state.meta["fails"] = {}
        if got:
            self.last_ok = time.time()
            if self.state.meta.get("nodata_alerted"):
                self.state.meta["nodata_alerted"] = False
                await self.bot.send_text("✅ 推特监控已恢复正常。")
        elif not self.account_dead and time.time() - self.last_ok > NO_DATA_ALERT \
                and not self.state.meta.get("nodata_alerted"):
            self.state.meta["nodata_alerted"] = True
            await self.alert("nodata", f"⚠️ 推特监控：已经 {int((time.time() - self.last_ok) / 60)} 分钟拿不到推特数据。\n"
                                       "常见原因：小号被推特临时限流，或推特改了接口。程序会继续自动重试。")
        self.state.meta["last_ok"] = self.last_ok

    async def heartbeat(self) -> None:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self.cfg.heartbeat == now.hour and self.state.meta.get("heartbeat") != today:
            n = sum(1 for t in self.state.data["pushed"] if t > time.time() - 86400)
            if await self.bot.send_text(f"💓 推特监控运行正常，过去 24 小时推送了 {n} 条。"):
                self.state.meta["heartbeat"] = today


async def run_once(loop_minutes: float) -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        say(f"✗ {e}")
        return 1
    state = State()
    started = time.time()
    deadline = started + max(0.0, min(loop_minutes, 10.0)) * 60
    rounds = 0
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        r = Run(cfg, state, http)
        try:
            await r.tw.login(cfg)
            await r.announce_if_changed()
            while True:
                try:
                    await r.cycle()
                    await r.heartbeat()
                except Exception as e:
                    log.exception("round failed: %s", e)
                rounds += 1
                state.save()
                wait = max(cfg.interval, 8 * len(cfg.accounts))
                if time.time() + wait > deadline:
                    break
                await asyncio.sleep(wait)
        finally:
            state.save()
    pushed = sum(1 for t in state.data["pushed"] if t >= started)
    status = "spare account problem (alert sent)" if r.account_dead else (
        "ok" if time.time() - r.last_ok < 600 else "no data from X")
    log.info("done: %d round(s), pushed %d new post(s), status: %s", rounds, pushed, status)
    return 0


async def run_test() -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        say(f"✗ {e}")
        return 1
    say(f"[1/3] Configuration OK: watching {len(cfg.accounts)} account(s), sending to the 企业微信 group")
    say("[2/3] Reading X with the spare account …")
    state = State()
    tw = Twitter(cfg, wait_timeout=20)
    try:
        await tw.login(cfg)
        if cfg.list_id:
            tweets = await tw.list_tweets(cfg.list_id)
        else:
            uid = await tw.user_id(cfg.accounts[0], state)
            if uid is None:
                active, err = await tw.status()
                say(f"  ✗ {explain_inactive(err)}  {err}" if not active else
                    f"  ✗ X user @{cfg.accounts[0]} not found — check WATCH_ACCOUNTS")
                return 1
            state.save()
            tweets = [t for t in await tw.user_tweets(uid) if t.user.id == uid]
    except NoAccountError:
        active, err = await tw.status()
        say(f"  ✗ {explain_inactive(err)}  {err}\n    → log in to the spare account again and update the "
            "TW_AUTH_TOKEN / TW_CT0 secrets" if not active else "  ✗ X is rate-limiting or unreachable; try again later")
        return 1
    except Exception as e:
        say(f"  ✗ Reading X failed: {type(e).__name__}: {e}")
        return 1
    if not tweets:
        say("  ✗ X returned no posts (the cookies may be invalid)")
        return 1
    say(f"  ✓ Read {len(tweets)} posts")
    say("[3/3] Sending a test message to the 企业微信 group …")
    sample = max(tweets, key=lambda t: t.id)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        bot = WeCom(cfg.webhook, cfg.mention_all, http)
        ok = await bot.send_text(f"🔧 测试消息：推特监控配置成功！\n监控对象：{cfg.summary()}\n下面是一条最新推文的推送效果示例 👇")
        msg = format_tweet(sample, prefix="[示例] ")
        ok = ok and await bot.send_tweet(msg, await download_photos(msg.photos, http) if cfg.images else [])
    say("  ✓ Sent — check the group. All checks passed ✅" if ok else "  ✗ Sending failed (see the error above)")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Push new X posts into a 企业微信 group")
    ap.add_argument("--test", action="store_true", help="read X once and send a test message")
    ap.add_argument("--once", action="store_true", help="check once and exit (default)")
    args = ap.parse_args()
    setup_logging()
    if args.test:
        sys.exit(asyncio.run(run_test()))
    try:
        loop = float(_env("LOOP_MINUTES") or 0)
    except ValueError:
        loop = 0.0
    sys.exit(asyncio.run(run_once(loop)))


if __name__ == "__main__":
    main()
