#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code / Codex CLI PreToolUse hook: 手表远程批准 (watch remote approval).

链路:
  stdin(JSON)
    -> 经机场代理 POST 触发 Pushcut 通知
    -> 我在 iPhone / Apple Watch 上点 Allow / Deny
    -> Pushcut 云端后台 POST 到 ntfy 的 topic (allow / deny)
    -> 本脚本经代理从 ntfy stream 读到结果
    -> 输出 permissionDecision 给 Claude Code

设计原则:
  * 只用 Python 3 标准库,不引第三方依赖。
  * 所有出网请求显式走 HTTPS_PROXY(机场本地代理)。
  * 配置全部从环境变量读,绝不硬编码密钥。
  * 任何配置缺失 / 异常 / 超时,一律 fail-safe 成 "ask"(退回终端正常弹窗),
    绝不让 hook 崩溃卡住 Claude Code。
"""

import json
import os
import re
import socket
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


# ---------- 配置:全部来自环境变量 ----------
PUSHCUT_KEY = os.environ.get("PUSHCUT_KEY", "").strip()
PUSHCUT_NOTIF = os.environ.get("PUSHCUT_NOTIF", "claude").strip() or "claude"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

# 代理:优先 HTTPS_PROXY,其次大小写/HTTP 变体,形如 http://127.0.0.1:7890
PROXY = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("http_proxy")
    or ""
).strip()

# 等待手表回执的总时长(秒)。必须 < .claude/settings.json 里 hook 的 timeout(300)。
try:
    APPROVE_WAIT = int(float(os.environ.get("APPROVE_WAIT", "240")))
except ValueError:
    APPROVE_WAIT = 240

# 超时(没等到回执)时按什么决策返回:allow / deny / ask(默认 ask 最安全)。
TIMEOUT_DECISION = os.environ.get("APPROVE_TIMEOUT_DECISION", "ask").strip().lower()
if TIMEOUT_DECISION not in ("allow", "deny", "ask"):
    TIMEOUT_DECISION = "ask"

# 是否由 hook 动态注入 Allow/Deny 两个后台按钮(需要 Pushcut Pro)。
# "1"(默认):你只需在 Pushcut app 里建一条名为 PUSHCUT_NOTIF 的空通知即可,
#             按钮和它指向的 ntfy URL 都由 hook 在 API 调用里带上。
# "0":不注入,改用你在 app 里手动配好的 action。
DYNAMIC_ACTIONS = os.environ.get("PUSHCUT_DYNAMIC_ACTIONS", "1").strip() != "0"

# 指定通知发给哪些 Pushcut 设备(设备名见 GET /v1/devices)。逗号分隔,例如 "iPhone,watch"。
# 留空 = 用 Pushcut 默认(发给所有设备)。直接点名 watch 可绕过「iPhone 没锁屏就不镜像到表」
# 的 Apple 规则,确保手表能直接收到。
PUSHCUT_DEVICES = [
    d.strip() for d in os.environ.get("PUSHCUT_DEVICES", "").split(",") if d.strip()
]

# 通知声音。Pushcut 不带 sound 时会被当成静默通知,手表/手机不会震动提醒。
# 设成 "default" 让它正式提醒(手表会震)。想只震不响可用 "vibrateOnly";
# 其它可选值见 Pushcut(system/subtle/question/jobDone/problem/loud 等)。设 "none" 则不带。
PUSHCUT_SOUND = os.environ.get("PUSHCUT_SOUND", "default").strip()

# 是否把通知标记为「限时通知 / Time-Sensitive」。开启后:① 能冲破 iPhone 的专注模式/勿扰;
# ② Apple 会更积极地把它推到 Apple Watch——这是绕过「iPhone 在用时通知只显示在手机、
# 不上手表」这条系统路由规则的最有效手段(纯 API 侧能做的极限)。审批本就是时效操作,
# 默认开启;设 PUSHCUT_TIME_SENSITIVE=0 可关。
TIME_SENSITIVE = os.environ.get("PUSHCUT_TIME_SENSITIVE", "1").strip() != "0"

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

# 每个 agent 的展示预设。配图都是贴边裁好的矮横幅(777x243 透明背景,通知里图矮、
# 批准按钮不用下滑),走 jsDelivr CDN(国内可访问),锁定到具体 commit 防止失效。
_CDN = "https://cdn.jsdelivr.net/gh/ghy196830-del/agent-watch-approve"
_AGENT_PRESETS = {
    "claude": {
        "title": "🦀 Claude 待批准",
        # Claude Code 官方像素螃蟹 Clawd,动图(48 帧,会眨眼挪腿;手表上只显示静帧)。
        # 同仓库另有透明静态 PNG 兜底:.../assets/clawd-crab.png。
        "image": _CDN + "@53b1672aff4f18f8e3581f83f92f079f3031d6e4/assets/clawd-crab.gif",
    },
    "codex": {
        "title": "🤖 Codex 待批准",
        # GPT 结猫(用户自选吉祥物:OpenAI 结 logo 化身小猫),透明背景,与螃蟹同规格横幅。
        # 同仓库另有 ChatGPT 官方圆角图标备选:.../assets/gpt-logo.png。
        "image": _CDN + "@45aa8e4deb6d68b33ac03206e27aebb8c8a8ab89/assets/gpt-cat.png",
    },
}
_PRESET = _AGENT_PRESETS[AGENT]

# 通知配图(Pushcut 的 image 字段:公开图片 URL,或 Pushcut app 里存的图片名)。
# 默认按上面识别出的 agent 取预设(claude=螃蟹动图,codex=GPT 图标)。
# 设成 "none"/空 则不带图;想统一换图设 PUSHCUT_IMAGE=你的图片URL(对两个 agent 都生效)。
PUSHCUT_IMAGE = os.environ.get("PUSHCUT_IMAGE", _PRESET["image"]).strip()

# 通知正文最多显示多少字符,超出截断。手表屏幕小,默认压到 80 字,一行就够;
# 想再短/再长设环境变量 WATCH_DESC_MAX。
try:
    DESC_MAX = max(20, int(os.environ.get("WATCH_DESC_MAX", "80")))
except ValueError:
    DESC_MAX = 80

# 触发 Pushcut 通知的重试次数。经实测国内机场到 api.pushcut.io 的 TLS 握手会偶发
# SSLEOFError,重试几下基本就能成功(瞬时失败很快,不会拖很久)。
try:
    PUSHCUT_RETRIES = max(1, int(os.environ.get("PUSHCUT_RETRIES", "12")))
except ValueError:
    PUSHCUT_RETRIES = 12

# 单次触发 Pushcut 的超时(秒)。设短一点,这样卡住的连接能尽快失败、转下一次重试,
# 减少“通知迟迟不到”的体感延迟。SSLEOFError 本身是秒级失败,不受此影响。
# 默认 3s:实测国内机场到 api.pushcut.io,成功的握手 ~1.8s、失败的要卡满 ~5s 才报错,
# 约一半会失败;把超时压到 3s(成功留足余量)能在失败连接卡满 5s 之前就掐掉去重试,
# 省下 ~2s/次。想再宽松/严格设环境变量 PUSHCUT_TIMEOUT。
try:
    PUSHCUT_TIMEOUT = max(3, int(os.environ.get("PUSHCUT_TIMEOUT", "3")))
except ValueError:
    PUSHCUT_TIMEOUT = 3

NTFY_BASE = "https://ntfy.sh/"
# 通知名做 URL 转义,避免名字里有空格/特殊字符时拼坏 URL。
PUSHCUT_URL = "https://api.pushcut.io/v1/notifications/" + urllib.parse.quote(
    PUSHCUT_NOTIF, safe=""
)

# 读 ntfy stream 时单次 socket 超时;要大于 ntfy 的 keepalive(默认 ~45s),
# 这样正常等待期间不会被误判成断线。它只是断线兜底,不是总超时。
STREAM_READ_TIMEOUT = 60

# 当前 hook 事件名(main 里从输入 JSON 的 hook_event_name 读出)。决定 emit() 的输出格式:
#   * Claude 的 PreToolUse            -> permissionDecision 格式
#   * Codex 的 PermissionRequest      -> decision.behavior 格式(Codex 专属事件,只在
#     Codex 自己要弹审批时触发;hook 不回应 = 走 Codex 正常审批流程,等同 ask)
HOOK_EVENT = ""


# ---------- 危险命令过滤:只把"真要命"的操作推到手表 ----------
# WATCH_DANGER_ONLY=1 时,凡是没命中危险模式的操作,直接返回 "ask"(退回 agent 正常
# 流程),不打扰手表。这样日常命令安静,只有真正危险的操作才震你手表。
DANGER_ONLY = os.environ.get("WATCH_DANGER_ONLY", "0").strip() == "1"

# danger-only 模式下,非危险操作怎么处理:
#   ask(默认)= 退回 Claude Code 正常流程(终端可能还会问你 yes)
#   allow      = 直接自动放行,既不弹手表也不在终端问(危险才上手表、其余全自动)
#   deny       = 直接拒绝
NONDANGER_DECISION = os.environ.get("WATCH_NONDANGER_DECISION", "ask").strip().lower()
if NONDANGER_DECISION not in ("allow", "deny", "ask"):
    NONDANGER_DECISION = "ask"

_DEFAULT_DANGER_PATTERNS = [
    r"\brm\s+-",                                       # rm 带任意 flag(rm -rf / rm -r ...)
    r"\bsudo\b",
    r"\bgit\s+push\b.*(--force|-f|\s\+)",              # 强推
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[a-z]*f",
    r"\bdd\b.*\bif=",
    r"\bmkfs\b",
    r"\bof=/dev/",
    r">\s*/dev/sd",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\b(kill|pkill|killall)\b",
    r"\bchmod\s+(-r\s+|.*\b777\b)",
    r"\bchown\s+-r\b",
    r":\(\)\s*\{",                                     # fork 炸弹
    r"\b(drop|truncate)\s+(table|database)\b",
    r"\bdelete\s+from\b",
    r"(curl|wget)\b.*\|\s*(sudo\s+)?(sh|bash|zsh)\b",  # curl ... | sh
    r"\bnpm\s+publish\b",
    r"\bdocker\b.*\b(rm|prune|down)\b",
    r"\bterraform\s+destroy\b",
    r"\bkubectl\s+delete\b",
    r"\bremove-item\b.*-recurse.*-force",             # PowerShell
    r"\bdel\b\s+/[sf]",                                # cmd del /s /f
    r"\bformat\b\s+[a-z]:",                            # format C:
]
# WATCH_DANGER_REGEX 整体覆盖默认清单;WATCH_DANGER_EXTRA(换行分隔)在默认基础上追加。
_danger_override = os.environ.get("WATCH_DANGER_REGEX", "").strip()
_danger_extra = [p for p in os.environ.get("WATCH_DANGER_EXTRA", "").split("\n") if p.strip()]
_danger_sources = [_danger_override] if _danger_override else (_DEFAULT_DANGER_PATTERNS + _danger_extra)
_DANGER_RE = []
for _p in _danger_sources:
    try:
        _DANGER_RE.append(re.compile(_p, re.IGNORECASE))
    except re.error:
        pass


def is_dangerous(text):
    """text 命中任一危险模式则返回 True。"""
    if not text:
        return False
    for rx in _DANGER_RE:
        if rx.search(text):
            return True
    return False


# ---------- 受保护脚本路径:对它们的「写操作」强制上手表 ----------
# 背景:Claude Code 对 .claude/ 里的文件(hook 脚本、settings)有一道写死的终端确认,
# hook 的 allow / permissions.allow / 连 --dangerously-skip-permissions 都压不住(见
# github issue #41615),所以「改 .claude/ 里的 hook」没法走手表。对策:把 hook 脚本搬到
# .claude/ 之外,再用这里的规则——凡是**写类工具**动到这些路径,一律当危险、路由到手表。
# 逗号分隔的子串(大小写不敏感),命中即算;留空(默认)= 关闭。本机用 WATCH_PROTECT_PATHS
# 设成 "watch-hooks"(脚本新家目录名),这样改 C:\Users\ghy19\watch-hooks\*.py 就走手表。
PROTECT_PATHS = [
    p.strip().lower()
    for p in os.environ.get("WATCH_PROTECT_PATHS", "").split(",")
    if p.strip()
]
# 只对「写类」工具应用受保护路径规则:Read/Glob 等只读工具即使 file_path 命中也不拦
# (否则每次读 hook 脚本都震你手表)。
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def hits_protected_path(tool_name, tool_input):
    """写类工具动到受保护路径 -> True(该走手表);其它一律 False。"""
    if not PROTECT_PATHS or tool_name not in _WRITE_TOOLS:
        return False
    if not isinstance(tool_input, dict):
        return False
    fp = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "").lower()
    return bool(fp) and any(p in fp for p in PROTECT_PATHS)


# ---------- 会被 Claude Code 强制弹终端的路径:hook 拦不住,只能提醒 ----------
# Claude Code 对 .claude/ 里的 settings / hook 文件有一道写死的终端确认,hook 返回 allow
# 也压不住(github issue #41615),连 --dangerously-skip-permissions 都没用。对这类操作,
# hook 改变不了「必须去终端点」的事实,但它仍会先触发(#41615:"Hook fires but prompt
# still shows"),所以能**给手表/手机推一条无按钮提醒**:"去终端确认",让你不至于对着
# 静默卡住的终端发呆。匹配子串(大小写不敏感,逗号分隔)。
# 默认只点名 .claude 下的 settings/hooks —— **特意不写成整个 ".claude"**,否则会误伤
# ~/.claude/projects/**/memory/(记忆文件,根本不弹终端)和搬出去的 watch-hooks。
# 想加别的强制终端路径用 WATCH_TERMINAL_FORCED_PATHS 覆盖;设空串则关闭整套提醒。
TERMINAL_FORCED_PATHS = [
    p.strip().lower()
    for p in os.environ.get(
        "WATCH_TERMINAL_FORCED_PATHS",
        ".claude\\settings.json,.claude/settings.json,"
        ".claude\\settings.local.json,.claude/settings.local.json,"
        ".claude\\hooks,.claude/hooks",
    ).split(",")
    if p.strip()
]


def is_terminal_forced(tool_name, tool_input):
    """该操作会被 Claude Code 强制弹终端(hook 拦不住)-> True(只提醒、不做手表审批)。"""
    if not TERMINAL_FORCED_PATHS or not isinstance(tool_input, dict):
        return False
    if tool_name in _WRITE_TOOLS:
        text = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    elif tool_name in ("Bash", "PowerShell"):
        text = str(tool_input.get("command") or "")
    else:
        return False
    text = text.lower()
    return bool(text) and any(p in text for p in TERMINAL_FORCED_PATHS)


# ---------- 危险操作 -> 简短中文确认标签 ----------
# 推到手表的正文不再堆原始命令(一长串 + D 盘全路径看着累),而是一句「让我干什么」的
# 确认:命中的危险类别给一个中文标签(下表),再附上操作目标的「文件/文件夹名」(只取
# basename,去掉盘符和长目录)。每项 = (正则, 标签),命令命中第一条就用它的标签。
# 下表覆盖了 _DEFAULT_DANGER_PATTERNS 的每一条;若你用 WATCH_DANGER_REGEX / _EXTRA
# 自定义了别的危险模式,这里匹配不到标签时,describe() 会优雅退回「截断后的简短命令」。
_DANGER_LABELS = [
    (r"\bremove-item\b.*-recurse.*-force", "🗑️ 删除文件夹"),
    (r"\bdel\b\s+/[sf]", "🗑️ 删除文件"),
    (r"\brm\s+-", "🗑️ 删除文件/目录"),
    (r"\bformat\b\s+[a-z]:", "💽 格式化磁盘"),
    (r"\bmkfs\b", "💽 格式化文件系统"),
    (r"\bdd\b.*\bif=", "💽 dd 写盘"),
    (r"\bof=/dev/", "💽 写入磁盘设备"),
    (r">\s*/dev/sd", "💽 写入磁盘设备"),
    (r"\bsudo\b", "🔑 sudo 提权执行"),
    (r"\bgit\s+push\b.*(--force|-f|\s\+)", "⚠️ git 强制推送"),
    (r"\bgit\s+reset\s+--hard\b", "↩️ git 丢弃改动(reset --hard)"),
    (r"\bgit\s+clean\s+-[a-z]*f", "🧹 git 清理未跟踪文件"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "🔌 关机/重启"),
    (r"\b(kill|pkill|killall)\b", "🛑 结束进程"),
    (r"\bchmod\s+(-r\s+|.*\b777\b)", "🔓 修改权限(chmod)"),
    (r"\bchown\s+-r\b", "👤 修改属主(chown -R)"),
    (r":\(\)\s*\{", "💥 疑似 fork 炸弹"),
    (r"\b(drop|truncate)\s+(table|database)\b", "🗄️ 删表/清库"),
    (r"\bdelete\s+from\b", "🗄️ 删除数据(DELETE FROM)"),
    (r"(curl|wget)\b.*\|\s*(sudo\s+)?(sh|bash|zsh)\b", "📥 下载并执行脚本"),
    (r"\bnpm\s+publish\b", "📦 发布 npm 包"),
    (r"\bdocker\b.*\b(rm|prune|down)\b", "🐳 删除 docker 资源"),
    (r"\bterraform\s+destroy\b", "💥 terraform 销毁资源"),
    (r"\bkubectl\s+delete\b", "☸️ kubectl 删除资源"),
]
_DANGER_LABEL_RE = []
for _p, _lab in _DANGER_LABELS:
    try:
        _DANGER_LABEL_RE.append((re.compile(_p, re.IGNORECASE), _lab))
    except re.error:
        pass

# 像路径的 token:X:\... 盘符路径,或任何含 / 或 \ 的串。用来从命令里抠出「目标」。
_PATH_TOKEN_RE = re.compile(r"""[A-Za-z]:[\\/][^\s"']*|[^\s"']*[\\/][^\s"']*""")


def danger_label(text):
    """命中的危险类别 -> 简短中文标签;没命中返回 None。"""
    if not text:
        return None
    for rx, lab in _DANGER_LABEL_RE:
        if rx.search(text):
            return lab
    return None


def short_target(command):
    """从命令里抽一个『目标』给确认用:取最后一个像路径的 token,只留 basename
    (去掉盘符 / 长目录,手表上只看到文件/文件夹名)。抽不到返回空串。"""
    if not command:
        return ""
    tokens = _PATH_TOKEN_RE.findall(command)
    if not tokens:
        return ""
    raw = tokens[-1].strip().strip("\"'")
    base = os.path.basename(raw.rstrip("/\\"))
    return base or raw


def emit(decision, reason):
    """向 stdout 输出 hook JSON 并 exit 0。decision ∈ allow | deny | ask。

    输出格式按事件区分:
      * Codex 的 PermissionRequest:用 decision.behavior 格式;其中「ask=什么都不输出」
        (Codex 收不到决定就走它自己的正常审批流程,语义上等同 Claude 的 ask)。
      * 其余(Claude 的 PreToolUse 等):permissionDecision 格式。
    """
    if HOOK_EVENT == "PermissionRequest":
        if decision == "ask":
            sys.exit(0)
        beh = (
            {"behavior": "allow"}
            if decision == "allow"
            else {"behavior": "deny", "message": reason}
        )
        out = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": beh}}
    else:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }
    # 显式以 UTF-8 字节写出,避免 Windows 控制台 codepage(如 GBK/cp936)把
    # permissionDecisionReason 里的中文编错,导致 Claude Code 读到乱码。
    payload = json.dumps(out, ensure_ascii=False).encode("utf-8")
    try:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    except Exception:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        sys.stdout.flush()
    sys.exit(0)


def make_opener():
    """构造显式带机场代理的 opener;没有代理就直连(并屏蔽系统代理设置)。"""
    if PROXY:
        proxy_handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    else:
        proxy_handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(proxy_handler)


# 工具名 -> 中文动词前缀,让正文一眼是中文(命令本身是代码,没法翻译,只能带原文)。
_CN_VERB = {
    "Bash": "运行命令:",
    "PowerShell": "运行命令:",
    "Write": "写入文件:",
    "Edit": "编辑文件:",
    "MultiEdit": "批量编辑:",
    "NotebookEdit": "编辑笔记本:",
}


def describe(tool_name, tool_input):
    """拼一句简短的中文确认正文(给手表看「在让我批准什么」,不堆原始命令/长路径)。

    Bash/PowerShell:不回显整条命令,而是「危险动作标签 + 目标文件名」,
        例如『🗑️ 删除文件夹:.watch_danger_test』。命中不到标签(自定义危险正则)
        才退回截断后的简短命令兜底。
    Write/Edit/MultiEdit/NotebookEdit:只显示 basename(同样不带长路径)。
    其它工具:理论上非危险、走不到这里(已在 main 里放行),兜底给个 JSON 摘要。
    """
    ti = tool_input if isinstance(tool_input, dict) else {}

    # Codex 的文件修改工具 apply_patch:tool_input.command 是补丁全文(很长),
    # 不回显,只抽出补丁里涉及的文件名(*** Add/Update/Delete File: 行)。
    if tool_name == "apply_patch":
        cmd = str(ti.get("command", ""))
        files = re.findall(r"\*\*\*\s*(?:Add|Update|Delete) File:\s*(.+)", cmd)
        names = "、".join(
            os.path.basename(f.strip().strip("\"'").rstrip("/\\")) for f in files[:3] if f.strip()
        )
        desc = ("📝 修改文件:" + names) if names else "📝 应用代码补丁"
        if len(desc) > DESC_MAX:
            desc = desc[: DESC_MAX - 1] + "…"
        return desc

    if tool_name in ("Bash", "PowerShell"):
        cmd = " ".join(str(ti.get("command", "")).split())  # 压平空白/换行
        label = danger_label(cmd)
        if label:
            tgt = short_target(cmd)
            desc = (label + ":" + tgt) if tgt else label
        else:
            desc = (_CN_VERB.get(tool_name, "操作:") + cmd) if cmd else "运行命令"
        if len(desc) > DESC_MAX:
            desc = desc[: DESC_MAX - 1] + "…"
        return desc

    if tool_name in ("Write", "Edit", "MultiEdit"):
        body = os.path.basename(str(ti.get("file_path", "")).rstrip("/\\"))
    elif tool_name == "NotebookEdit":
        body = os.path.basename(str(ti.get("notebook_path", "")).rstrip("/\\"))
    else:
        try:
            body = json.dumps(ti, ensure_ascii=False)
        except Exception:
            body = str(ti)
    body = " ".join(body.split())  # 压平空白/换行成一行
    # 受保护路径的写操作:用 🛡️ 标签点明「这是在改 hook 脚本」,而不是普通「编辑文件」。
    fp_full = str(ti.get("file_path") or ti.get("notebook_path") or "").lower()
    if PROTECT_PATHS and tool_name in _WRITE_TOOLS and fp_full and any(p in fp_full for p in PROTECT_PATHS):
        prefix = "🛡️ 改 hook 脚本:"
    else:
        prefix = _CN_VERB.get(tool_name, "操作:")
    desc = (prefix + body) if body else prefix.rstrip(":")
    if len(desc) > DESC_MAX:
        desc = desc[: DESC_MAX - 1] + "…"
    return desc


def build_actions():
    """动态生成 Allow/Deny 两个按钮:用「后台 web 请求」GET 方式 publish 到 ntfy。

    关键:必须用 urlBackgroundOptions(后台 web 请求),不能用普通 url(=打开链接/打开
    app)——watchOS 不支持「打开 app / 跑快捷指令」类动作,只支持后台 web 请求。
    用 GET(ntfy 支持 /publish?message=xxx)可避开 urlBackgroundOptions 里 httpBody
    偶发不生效的问题。返回 None 表示不注入(改用 app 里手配的 action)。
    """
    if not DYNAMIC_ACTIONS or not NTFY_TOPIC:
        return None
    base = NTFY_BASE + urllib.parse.quote(NTFY_TOPIC, safe="") + "/publish?message="
    return [
        {"name": "✅ 允许", "url": base + "allow", "urlBackgroundOptions": {"httpMethod": "GET"}},
        {"name": "❌ 拒绝", "url": base + "deny", "urlBackgroundOptions": {"httpMethod": "GET"}},
    ]


def send_pushcut(opener, title, text, with_actions=True, retries=None):
    """经代理 POST 触发 Pushcut 通知;对瞬时网络/TLS 失败自动重试。

    with_actions=False:不带 Allow/Deny 按钮,纯提醒(用于「去终端确认」这种无需回执的通知)。
    retries:本次最多尝试几次(默认用 PUSHCUT_RETRIES);提醒类通知传小一点,避免拖慢终端。
    4xx(如通知不存在=404、key 无效=401)是配置问题,重试也没用,直接抛出 ->
    上层会 fail-safe 成 ask。
    """
    payload = {"title": title, "text": text}
    if with_actions:
        actions = build_actions()
        if actions:
            payload["actions"] = actions
    if PUSHCUT_DEVICES:
        payload["devices"] = PUSHCUT_DEVICES
    if PUSHCUT_SOUND and PUSHCUT_SOUND.lower() != "none":
        payload["sound"] = PUSHCUT_SOUND
    if PUSHCUT_IMAGE and PUSHCUT_IMAGE.lower() != "none":
        payload["image"] = PUSHCUT_IMAGE
    if TIME_SENSITIVE:
        payload["isTimeSensitive"] = True
    body = json.dumps(payload).encode("utf-8")

    last = None
    n = retries or PUSHCUT_RETRIES
    for attempt in range(n):
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
            # 4xx(429 限流除外)是配置错误,重试无意义,立刻上抛
            if 400 <= e.code < 500 and e.code != 429:
                raise
            last = e
        except Exception as e:
            last = e
        if attempt < n - 1:
            time.sleep(0.3)
    if last is not None:
        raise last


def wait_for_decision(opener, since_ts, deadline):
    """从 ntfy stream 读 allow/deny。读到返回 'allow'/'deny';到 deadline 返回 None。

    用 since=since_ts(发通知前记下的 t0)防竞态:即使秒点、回执比订阅先到,
    重连/订阅时 ntfy 会把 t0 之后的消息一并回放,不会漏。
    """
    url = NTFY_BASE + NTFY_TOPIC + "/json?since=" + str(since_ts)
    while time.monotonic() < deadline:
        try:
            resp = opener.open(url, timeout=STREAM_READ_TIMEOUT)
        except Exception:
            time.sleep(1)  # 打开失败,稍后重连(不超过 deadline)
            continue
        try:
            while time.monotonic() < deadline:
                try:
                    raw = resp.readline()
                except socket.timeout:
                    break  # 本次连接静默太久,跳出去重连
                except Exception:
                    break
                if not raw:
                    break  # 服务端关闭连接,重连
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                if obj.get("event") != "message":
                    continue  # 只认 message,忽略 open / keepalive
                msg = (obj.get("message") or "").strip().lower()
                if msg in ("allow", "approve", "yes", "ok"):
                    return "allow"
                if msg in ("deny", "block", "no"):
                    return "deny"
                # 其它内容忽略,继续等
        finally:
            try:
                resp.close()
            except Exception:
                pass
    return None


def main():
    global HOOK_EVENT
    # 1) 读 stdin JSON;解析失败 -> ask 退化
    #    以二进制读 + utf-8-sig 解码,顺手吃掉可能的 BOM(某些 shell 管道会加),
    #    再剥掉前后空白,最大化兼容性。
    try:
        raw_bytes = sys.stdin.buffer.read()
        raw = raw_bytes.decode("utf-8-sig", "replace").strip()
        data = json.loads(raw) if raw else {}
    except Exception:
        emit("ask", "watch-approve: 无法解析 hook 输入,退回正常审批。")

    # 调试留痕(需 WATCH_DEBUG_DUMP=1):本次 hook 的原始输入覆盖写到
    # %TEMP%/watch_approve_last_input_<agent>.json,排查「hook 触发没有/传了什么」直接看文件。
    if os.environ.get("WATCH_DEBUG_DUMP", "").strip() == "1":
        try:
            import tempfile
            with open(os.path.join(tempfile.gettempdir(), "watch_approve_last_input_%s.json" % AGENT), "wb") as _f:
                _f.write(("agent=%s\n" % AGENT).encode("utf-8") + raw_bytes)
        except Exception:
            pass
    if not isinstance(data, dict):
        data = {}

    tool_name = data.get("tool_name", "Tool")
    tool_input = data.get("tool_input", {})
    HOOK_EVENT = str(data.get("hook_event_name") or "")

    # 2) 关键配置缺失 -> ask 退化(不报错卡住)
    if not PUSHCUT_KEY or not NTFY_TOPIC:
        emit("ask", "watch-approve: 缺少 PUSHCUT_KEY 或 NTFY_TOPIC,退回正常审批。")

    desc = describe(tool_name, tool_input)

    # Codex 的 PermissionRequest:只在 Codex 自己判定「这事需要人批准」(提权/出沙箱/联网等)
    # 时才触发,频率低且都是真正的权限边界 -> 不做危险过滤,一律上手表让你点。
    # 正文优先用 Codex 给的人话描述(tool_input.description),没有再用 describe() 的结果。
    is_perm_request = HOOK_EVENT == "PermissionRequest"
    if is_perm_request and isinstance(tool_input, dict):
        _d = " ".join(str(tool_input.get("description") or "").split())
        if _d:
            desc = _d if len(_d) <= DESC_MAX else _d[: DESC_MAX - 1] + "…"

    # 0) 会被 Claude Code 强制弹终端的操作(改 .claude/ 里的 settings 等):它的终端确认
    #    hook 压不住(issue #41615),所以这里**不做手表审批、不等回执**,只给手表/手机推一条
    #    无按钮的「去终端确认」提醒,然后返回 ask 让终端照常弹。你就不会对着静默终端发呆。
    #    重试数压到 5(默认 12 会拖慢终端弹窗出现);推送失败也吞掉,绝不卡住。
    if (not is_perm_request) and is_terminal_forced(tool_name, tool_input):
        try:
            send_pushcut(make_opener(), "⚠️ 需去终端确认", desc, with_actions=False, retries=5)
        except Exception:
            pass
        emit("ask", "watch-approve: 该操作会被 Claude Code 强制要求终端确认,已推提醒到手表/手机。")

    # danger-only 模式:不危险的操作按 NONDANGER_DECISION 处理(不打扰手表)。
    # 注意:matcher 现在是 "*"(命中所有工具,见 settings.json),所以这里只把
    # **真正能造成破坏的字段**(shell 命令 / 写入的文件路径)拿去做危险匹配;
    # 像 Read/Glob/Grep/WebSearch 这类没有 command/file_path 的工具,match_text 为空
    # -> 判为非危险 -> 直接放行。**绝不**再去扫 desc(整段 JSON),否则搜索词里
    # 出现 "rm -" 之类会误判成危险、白白震你手表。
    if DANGER_ONLY and not is_perm_request:
        match_text = ""
        if isinstance(tool_input, dict):
            match_text = str(
                tool_input.get("command")
                or tool_input.get("file_path")
                or tool_input.get("notebook_path")
                or ""
            )
        if not is_dangerous(match_text) and not hits_protected_path(tool_name, tool_input):
            if NONDANGER_DECISION == "allow":
                emit("allow", "watch-approve: 非危险操作,自动放行(未打扰手表)。")
            elif NONDANGER_DECISION == "deny":
                emit("deny", "watch-approve: 非危险操作,按配置拒绝。")
            else:
                emit("ask", "watch-approve: 非危险操作,退回正常审批(未打扰手表)。")

    opener = make_opener()

    # 3) 防竞态:先记 t0,再发通知,订阅用 since=t0
    t0 = int(time.time())
    deadline = time.monotonic() + APPROVE_WAIT

    title = _PRESET["title"]
    text = desc if desc else "(无详情)"
    try:
        send_pushcut(opener, title, text)
    except urllib.error.HTTPError as e:
        hint = ""
        if e.code == 404:
            hint = "(通知 '%s' 不存在?去 Pushcut app 建一条同名通知)" % PUSHCUT_NOTIF
        elif e.code in (401, 403):
            hint = "(PUSHCUT_KEY 无效?)"
        emit("ask", "watch-approve: Pushcut 返回 HTTP %s%s,退回正常审批。" % (e.code, hint))
    except Exception as e:
        emit(
            "ask",
            "watch-approve: 推送 Pushcut 失败(%s,可能是代理/网络),退回正常审批。"
            % type(e).__name__,
        )

    # 4) 等手表回执
    try:
        decision = wait_for_decision(opener, t0, deadline)
    except Exception:
        decision = None

    if decision == "allow":
        emit("allow", "watch-approve: 已在手表上批准。")
    elif decision == "deny":
        emit("deny", "watch-approve: 已在手表上拒绝。")
    else:
        emit(
            TIMEOUT_DECISION,
            "watch-approve: %ss 内无回应,按超时策略返回 %s。"
            % (APPROVE_WAIT, TIMEOUT_DECISION),
        )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # 兜底:任何意外都 fail-safe 成 ask,绝不崩溃
        try:
            emit("ask", "watch-approve: 未知异常(%s),退回正常审批。" % type(e).__name__)
        except Exception:
            sys.exit(0)
