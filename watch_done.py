#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code / Codex CLI Stop hook: 任务完成提醒 (task-done notification).

对话任务结束(agent 这一轮回完)时,经机场代理 POST 触发一条 Pushcut 通知,
在 iPhone / Apple Watch 上显示「任务已完成」+ 螃蟹配图。
纯提醒:**没有按钮、不需回复**,看一眼即可。

设计原则(与 watch_approve.py 一致):
  * 只用 Python 3 标准库,不引第三方依赖。
  * 所有出网请求显式走 HTTPS_PROXY(机场本地代理)。
  * 配置全部从环境变量读,绝不硬编码密钥。
  * fire-and-forget:任何配置缺失 / 异常 / 超时,一律静默 exit 0,
    既不阻塞 Claude Code,也绝不触发 Stop 循环(永不输出 decision=block)。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request




# ---------- 兜底配置文件 watch.env ----------
# hook 进程的环境变量取决于「谁启动了 agent」:Claude Code 会把 settings.json 的 env
# 注入自己进程(hook 继承得到);但 Codex 不会把 config.toml 里 shell_environment_policy
# 的 env 传给 hook(那只作用于 shell 工具,见 codex-rs/hooks/engine/command_runner.rs)。
# 为了让脚本在任何宿主下都拿得到配置,这里读脚本同目录的 watch.env(KEY=VALUE 每行一条,
# # 开头是注释),【只填补缺失的环境变量】——真实环境变量永远优先。路径可用 WATCH_ENV_FILE 覆盖。
def _load_env_file():
    path = os.environ.get("WATCH_ENV_FILE", "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "watch.env"
    )
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_env_file()


# ---------- 配置:全部来自环境变量(与审批 hook 共用同一套) ----------
PUSHCUT_KEY = os.environ.get("PUSHCUT_KEY", "").strip()
PUSHCUT_NOTIF = os.environ.get("PUSHCUT_NOTIF", "claude").strip() or "claude"

# 代理:优先 HTTPS_PROXY,其次大小写/HTTP 变体,形如 http://127.0.0.1:7890
PROXY = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("http_proxy")
    or ""
).strip()

# 指定通知发给哪些 Pushcut 设备(设备名见 GET /v1/devices)。逗号分隔,例如 "iPhone,watch"。
# 留空 = 用 Pushcut 默认(发给所有设备)。
PUSHCUT_DEVICES = [
    d.strip() for d in os.environ.get("PUSHCUT_DEVICES", "").split(",") if d.strip()
]

# 完成提醒的声音。Pushcut 不带 sound 会被当成静默通知,手表/手机不震。
# 默认用 "jobDone"——Pushcut 内置的「任务完成」提示音,语义正好;设 "none" 则不带。
DONE_SOUND = os.environ.get(
    "WATCH_DONE_SOUND", os.environ.get("PUSHCUT_SOUND", "jobDone")
).strip()

# ---------- 识别是哪个 agent 在调用:claude(默认)还是 codex ----------
# 同一份脚本同时服务 Claude Code 和 Codex CLI,两边通知用不同的标题和配图区分。
# 优先级:命令行 --agent(在 hooks 接线处显式声明,最可靠)> 环境变量 WATCH_AGENT > claude。
# Codex 侧 ~/.codex/hooks.json 的 command 带 "--agent codex";Claude 侧 settings.json 不带。
def _detect_agent():
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--agent" and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
        if a.startswith("--agent="):
            return a.split("=", 1)[1].strip().lower()
    return os.environ.get("WATCH_AGENT", "").strip().lower()


AGENT = _detect_agent()
if AGENT not in ("claude", "codex"):
    AGENT = "claude"

# 每个 agent 的展示预设(标题/正文/配图;配图与审批 hook 同款,jsDelivr 锁定 commit)。
_CDN = "https://cdn.jsdelivr.net/gh/ghy196830-del/agent-watch-approve"
_AGENT_PRESETS = {
    "claude": {
        "title": "🦀 任务已完成",
        "text": "Claude 已完成当前任务",
        "image": _CDN + "@53b1672aff4f18f8e3581f83f92f079f3031d6e4/assets/clawd-crab.gif",
    },
    "codex": {
        "title": "🤖 任务已完成",
        "text": "Codex 已完成当前任务",
        "image": _CDN + "@45aa8e4deb6d68b33ac03206e27aebb8c8a8ab89/assets/gpt-cat.png",
    },
}
_PRESET = _AGENT_PRESETS[AGENT]

# 通知配图,按 agent 取预设(claude=螃蟹动图,codex=GPT 图标),与审批 hook 一致。
# 设 "none"/空 则不带图;想统一换图设 PUSHCUT_IMAGE=你的图片URL。
PUSHCUT_IMAGE = os.environ.get("PUSHCUT_IMAGE", _PRESET["image"]).strip()

# 是否标记为「限时通知 / Time-Sensitive」(冲破专注模式 + 更积极推到 Apple Watch)。
# 与审批 hook 共用同一个开关 PUSHCUT_TIME_SENSITIVE,默认开启;设为 0 关闭。
TIME_SENSITIVE = os.environ.get("PUSHCUT_TIME_SENSITIVE", "1").strip() != "0"

# 通知标题 / 正文,默认按 agent 取预设,可用环境变量覆盖。
DONE_TITLE = os.environ.get("WATCH_DONE_TITLE", "").strip() or _PRESET["title"]
DONE_TEXT = os.environ.get("WATCH_DONE_TEXT", _PRESET["text"]).strip()

# 触发 Pushcut 的重试次数与单次超时(秒)。国内机场到 api.pushcut.io 的 TLS 握手会偶发
# 失败,重试几下基本就能成功;完成提醒不阻塞主流程,重试次数比审批略少即可。
try:
    PUSHCUT_RETRIES = max(1, int(os.environ.get("PUSHCUT_RETRIES", "8")))
except ValueError:
    PUSHCUT_RETRIES = 8
try:
    PUSHCUT_TIMEOUT = max(3, int(os.environ.get("PUSHCUT_TIMEOUT", "6")))
except ValueError:
    PUSHCUT_TIMEOUT = 6

# 通知名做 URL 转义,避免名字里有空格/特殊字符时拼坏 URL。
PUSHCUT_URL = "https://api.pushcut.io/v1/notifications/" + urllib.parse.quote(
    PUSHCUT_NOTIF, safe=""
)


def make_opener():
    """构造显式带机场代理的 opener;没有代理就直连(并屏蔽系统代理设置)。"""
    if PROXY:
        proxy_handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    else:
        proxy_handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(proxy_handler)


def send_pushcut(opener, title, text):
    """经代理 POST 触发 Pushcut 通知(无 actions = 无按钮);瞬时网络/TLS 失败自动重试。

    4xx(通知不存在=404、key 无效=401 等)是配置问题,重试无意义,直接 return。
    """
    payload = {"title": title}
    if text:
        payload["text"] = text
    if PUSHCUT_DEVICES:
        payload["devices"] = PUSHCUT_DEVICES
    if DONE_SOUND and DONE_SOUND.lower() != "none":
        payload["sound"] = DONE_SOUND
    if PUSHCUT_IMAGE and PUSHCUT_IMAGE.lower() != "none":
        payload["image"] = PUSHCUT_IMAGE
    if TIME_SENSITIVE:
        payload["isTimeSensitive"] = True
    # 关键:不带 actions 字段 -> 通知上没有任何按钮,纯展示、不需回复。
    body = json.dumps(payload).encode("utf-8")

    for attempt in range(PUSHCUT_RETRIES):
        try:
            req = urllib.request.Request(
                PUSHCUT_URL,
                data=body,
                method="POST",
                headers={"API-Key": PUSHCUT_KEY, "Content-Type": "application/json"},
            )
            with opener.open(req, timeout=PUSHCUT_TIMEOUT) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            # 4xx(429 限流除外)是配置错误,重试无意义,直接放弃(完成提醒不报错卡人)
            if 400 <= e.code < 500 and e.code != 429:
                return
        except Exception:
            pass
        if attempt < PUSHCUT_RETRIES - 1:
            time.sleep(0.3)


def main():
    # Stop hook 的 stdin 是一段 JSON(含 session_id / stop_hook_active 等)。
    # 这里其实不依赖它的内容,读出来只为兼容管道;解析失败也无所谓。
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace").strip()
        data = json.loads(raw) if raw else {}
    except Exception:
        raw = ""
        data = {}

    # 调试留痕(需 WATCH_DEBUG_DUMP=1):本次 hook 输入覆盖写到
    # %TEMP%/watch_done_last_input_<agent>.json,排查「Stop hook 触发没有」直接看文件。
    if os.environ.get("WATCH_DEBUG_DUMP", "").strip() == "1":
        try:
            import tempfile
            with open(
                os.path.join(tempfile.gettempdir(), "watch_done_last_input_%s.json" % AGENT), "w",
                encoding="utf-8",
            ) as _f:
                _f.write(raw)
        except Exception:
            pass
    if not isinstance(data, dict):
        data = {}

    # 缺关键配置就静默退出(完成提醒是锦上添花,绝不打断 Claude Code)。
    if not PUSHCUT_KEY:
        return

    opener = make_opener()
    send_pushcut(opener, DONE_TITLE, DONE_TEXT)
    # 不输出任何 JSON、正常 exit 0 -> Claude Code 正常结束,不会触发 Stop 循环。


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # 兜底:任何意外都静默吞掉,绝不影响主流程。
        pass
    sys.exit(0)
