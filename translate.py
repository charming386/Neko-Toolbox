# -*- coding: utf-8 -*-
"""translate.py —— 翻译工具（阿里云百炼 Qwen-MT 机器翻译专用模型）。

几个刻意的决定：

1. 只用 OpenAI 兼容端点 https://dashscope.aliyuncs.com/compatible-mode/v1。
   `translation_options` 不是 OpenAI 标准参数，直接放请求体顶层（cURL 形态），
   不依赖 openai SDK，全程 urllib，打包体积不变。

2. 风格和领域**都走 `translation_options.domains`**，`content` 里只放原文。
   以前按官方 8.8 的做法把风格提示词拼在 content 前面，结果模型会把
   "Translate the following text into formal academic prose…" 这段指令
   当成正文一起翻译出来（用户看到的「本报告采用正式的学术文体，适用于同行评议
   期刊…」就是它），偶尔还会吐出 terms 的内部结构，严重时整段译文只剩一句
   领域描述。domains 是请求元信息，模型只拿它当背景调语体，不会写进译文。
   代价：domains 里必须写**描述性短语**（「正式学术散文：精确术语、无人称客观
   语气」），不能写祈使句（「请翻译成正式学术散文」）。

3. 领域 = translation_options.domains（英文领域描述）+ terms（术语表）。
   domains 官方明确「只支持英文」，所以内置描述都写成英文。
   terms 只发「原文里真的出现、且 source ≠ target」的条目。

4. 不流式、不带记忆（多轮）。Qwen-MT 本身就不支持多轮，这里也不发 stream。

5. 输入上限定 12000 **单位**：中文一字算 2 单位、英文一字符算 1 单位。
   这样中英文手感一致，也让中文（token 贵 4 倍）自然少进一点。
   算法和实测数据见 MAX_UNITS 注释。
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QKeySequence, QShortcut,
                           QTextCursor, QTextDocument)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
                               QPushButton, QSizeGrip, QSplitter, QTextBrowser, QTextEdit,
                               QVBoxLayout, QWidget)

import store
import theme
import trhistory
from widgets import ConfirmDialog, SoftButton, shadow

API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
CONSOLE_URL = "https://bailian.console.aliyun.com/cn-beijing/model/settings/api-key"

# Qwen-MT 的输入上限是 8192 token（含提示词、术语、领域描述）。
#
# 输入上限不用「字符数」计，改用一个**按显示宽度折算的「单位」**：
#   中日韩文字 / 全角标点 / emoji = 2 单位，其余（英文、数字、ASCII）= 1 单位。
# 理由：中文一个字在屏幕上顶两个英文字符宽，而且 token 也贵得多（0.64 vs 0.15）。
# 用一个统一尺度，中文和英文的「手感」才对得上 —— 12000 单位 = 6000 汉字 = 12000 英文。
#
# 实测（qwen-mt-flash / plus，translation_options 走 compatible-mode）：
#   普通中文      10000 字符 → 6597 tokens （0.66）
#   英文          10000 字符 → 1458 tokens （0.15）
#   emoji 混排    10000 字符 → 6785 tokens （0.68）
#   数字 0-9                     1.007 token/字符   ← 最贵的
#   数字+符号交替（报表串）        1.17  token/字符
#   密集标点数字   8000 字符 → 7513 tokens （0.94），9000 字符就超了
#
# 12000 单位对应：6000 汉字 ≈ 4000 tokens、12000 英文 ≈ 1800 tokens、
# 6000 汉字混 6000 英文 ≈ 4900 tokens —— 全都安全。
# 唯一还会撞线的是「全是数字符号」的报表（12000 单位 ≈ 12000 tokens），
# 所以在发送前仍用 estimate_tokens() 预检一次，估到会超就提前拦住。
MAX_UNITS = 12000
TOKEN_LIMIT = 8192
# 发送前的预检阈值（estimate_tokens 的结果超过它就提前拦下，不等阿里云报 400）。
# estimate_tokens 本身偏保守（密集标点估 0.95 token/字符，实测 0.94），
# 所以 7800 相当于「估算已撞到 8192 的 95%」——这时真实值基本也爆了。
BODY_TOKEN_BUDGET = 7800


def text_units(s):
    """按「显示宽度」折算单位：中日韩文字 / 全角标点 / emoji 各算 2，其余算 1。

    这就是 East Asian Width 里的宽字符判断（和终端里 `wcwidth` 一个口径）。
    """
    n = 0
    for ch in s or "":
        o = ord(ch)
        if (0x1100 <= o <= 0x115F            # 谚文字母
                or 0x2E80 <= o <= 0xA4CF      # 中日韩部首、假名、汉字
                or 0xAC00 <= o <= 0xD7A3      # 谚文音节
                or 0xF900 <= o <= 0xFAFF      # 兼容汉字
                or 0xFE30 <= o <= 0xFE6F      # 中文标点（竖排/兼容形式）
                or 0xFF00 <= o <= 0xFF60      # 全角 ASCII
                or 0xFFE0 <= o <= 0xFFE6      # 全角符号
                or 0x1F000 <= o <= 0x1FAFF    # emoji / 表情
                or 0x20000 <= o <= 0x3FFFD):  # 扩展 B 及以后的汉字
            n += 2
        else:
            n += 1
    return n


def fit_units(s, budget):
    """把 s 截到不超过 budget 个单位的**最长前缀**（不会把汉字劈一半）。"""
    if budget <= 0:
        return ""
    n = 0
    for i, ch in enumerate(s):
        w = text_units(ch)
        if n + w > budget:
            return s[:i]
        n += w
    return s

# 三档 → 模型（都不开流式、不开记忆）
LEVELS = [
    ("fast", "快速", "qwen-mt-lite", "最省最快，适合聊天、短句"),
    ("normal", "普通", "qwen-mt-flash", "通用首选，速度与质量平衡"),
    ("high", "高级", "qwen-mt-plus", "质量最好，适合论文、合同、技术文档"),
]
LEVEL_MAP = {k: (label, model, hint) for k, label, model, hint in LEVELS}

# 语种：先给 lite 也支持的 31 种，再补常用的几种（只有 flash/plus 支持）
LANGS = [
    ("auto", "自动检测"),
    ("zh", "简体中文"), ("en", "英语"), ("ja", "日语"), ("ko", "韩语"),
    ("zh_tw", "繁体中文"), ("ru", "俄语"), ("es", "西班牙语"), ("fr", "法语"),
    ("de", "德语"), ("pt", "葡萄牙语"), ("it", "意大利语"), ("th", "泰语"),
    ("vi", "越南语"), ("id", "印尼语"), ("ms", "马来语"), ("ar", "阿拉伯语"),
    ("hi", "印地语"), ("he", "希伯来语"), ("ur", "乌尔都语"), ("bn", "孟加拉语"),
    ("pl", "波兰语"), ("nl", "荷兰语"), ("tr", "土耳其语"), ("km", "高棉语"),
    ("cs", "捷克语"), ("sv", "瑞典语"), ("hu", "匈牙利语"), ("da", "丹麦语"),
    ("fi", "芬兰语"), ("tl", "他加禄语"), ("fa", "波斯语"),
    ("uk", "乌克兰语"), ("el", "希腊语"), ("ro", "罗马尼亚语"), ("my", "缅甸语"),
    ("ta", "泰米尔语"), ("yue", "粤语"), ("nb", "书面挪威语"), ("cy", "威尔士语"),
]
LANG_NAME = dict(LANGS)

# 内置风格：(名称, 英文描述式短语)。
#
# ⚠️ 必须是**描述式名词/形容词短语**，不能写 "Translate the following text into …"
# 这种祈使句，理由见 translate_text() 的注释。
DEFAULT_STYLE = "默认（不加风格）"
BUILTIN_STYLES = [
    (DEFAULT_STYLE, ""),
    ("日常交流", "Casual, natural everyday conversation between friends: relaxed, "
                 "idiomatic, the way people actually talk."),
    ("学术论文", "Formal academic prose for a peer-reviewed journal: precise technical "
                 "vocabulary, impersonal objective register, no contractions or "
                 "colloquialisms."),
    ("正式场合", "Polished formal language for an official occasion or a formal "
                 "statement: precise and courteous."),
    ("商务邮件", "A clear professional business email: concise, polite, action-oriented."),
    ("技术文档", "Clear technical documentation: consistent terminology, short "
                 "declarative sentences, identifiers / code / numbers / units kept "
                 "exactly as written."),
    ("口语字幕", "Natural spoken-style subtitles: short, easy to read at a glance, "
                 "no literary flourishes."),
]

DEFAULT_DOMAIN = "通用"
# 内置领域：(名称, 英文描述式短语, 术语表)
# 同样只写「这是什么领域」，不写「请使用…」。
BUILTIN_DOMAINS = [
    (DEFAULT_DOMAIN, "", []),
    ("计算机", "Computer software and IT: software development, system operations and "
               "hardware. Standard computing terminology, code identifiers / commands / "
               "file paths left unchanged.",
     [("服务器", "server"), ("数据库", "database"), ("接口", "API endpoint"),
      ("部署", "deployment"), ("缓存", "cache"), ("并发", "concurrency"),
      ("容器", "container"), ("负载均衡", "load balancing"), ("日志", "log"),
      ("算法", "algorithm"), ("字段", "field"), ("实例", "instance")]),
    ("医学", "Clinical medicine: standard medical terminology as used in "
             "peer-reviewed literature and clinical records.",
     [("患者", "patient"), ("诊断", "diagnosis"), ("治疗", "treatment"),
      ("症状", "symptom"), ("临床", "clinical"), ("不良反应", "adverse reaction"),
      ("适应证", "indication"), ("疗效", "efficacy"), ("剂量", "dosage"),
      ("手术", "surgery"), ("预后", "prognosis"), ("病理", "pathology")]),
    ("法律", "Legal instruments such as contracts: standard common-law terminology, "
             "formal precise register, every obligation and condition preserved.",
     [("甲方", "Party A"), ("乙方", "Party B"), ("不可抗力", "Force Majeure"),
      ("违约责任", "liability for breach of contract"), ("仲裁", "arbitration"),
      ("管辖", "jurisdiction"), ("赔偿", "compensation"), ("生效", "take effect"),
      ("附件", "annex"), ("保密义务", "confidentiality obligation")]),
    ("机械工程", "Mechanical engineering and manufacturing technical writing: standard "
                 "engineering terminology, dimensions / tolerances / units kept exactly "
                 "as given.",
     [("公差", "tolerance"), ("轴承", "bearing"), ("装配", "assembly"),
      ("扭矩", "torque"), ("转速", "rotational speed"), ("淬火", "quenching"),
      ("疲劳强度", "fatigue strength"), ("液压", "hydraulic"), ("模具", "mould"),
      ("表面粗糙度", "surface roughness")]),
    ("金融", "Finance and investment: standard financial terminology, all figures and "
             "currency units preserved exactly.",
     [("净利润", "net profit"), ("现金流", "cash flow"), ("资产负债率", "debt-to-asset ratio"),
      ("市盈率", "price-earnings ratio"), ("收益率", "rate of return"),
      ("信用证", "letter of credit"), ("对冲", "hedging"), ("估值", "valuation"),
      ("抵押", "collateral"), ("摊销", "amortization")]),
    ("跨境电商", "Cross-border e-commerce: wording commonly seen on international "
                 "marketplaces, product model numbers and certifications unchanged.",
     [("爆款", "best seller"), ("询盘", "inquiry"), ("报关", "customs declaration"),
      ("物流", "logistics"), ("退换货", "returns and exchanges"),
      ("库存", "inventory"), ("起订量", "minimum order quantity"),
      ("样品", "sample"), ("FOB", "FOB"), ("贴牌", "OEM")]),
]

ERROR_HINTS = {
    "Arrearage": "账户欠费或额度用尽，请到百炼控制台充值",
    "insufficient": "账户额度不足（或超出套餐限额）",
    "InvalidApiKey": "API Key 无效，请到设置里重新填写",
    "Unauthorized": "API Key 无效或已失效",
    "invalid_request_error": "请求被拒绝，可能是语种代码不被该档位模型支持",
    "Range of input length": "输入太长，超过了 8192 token 上限",
}


def estimate_tokens(text):
    """粗估这段文字会占多少 prompt token（界面提示 + 发送前预检）。

    权重按**字符类别分别实测**得来（qwen-mt-flash，每类各 4000 字符单测）：

      | 类别                | 实测 token/字符 |
      |---------------------|-----------------|
      | 数字 0-9            | 1.007           |
      | ASCII 标点 / 符号    | 0.60            |
      | 中日韩文字 / emoji   | 0.64            |
      | ASCII 字母          | 0.15 ~ 0.23     |
      | 空白                | 0.00            |

    但「数字和符号交替出现」时比各自单价更贵：`1,234.56; 7.89%; 98.6°C`
    这类报表串实测折合 **1.17 token/字符**（分词器把它们切得更碎）。
    所以数字+符号占比超过 40% 时，这一块按 1.10 算（偏高一点，宁可早拦）。

    ⚠️ 这里最容易踩的坑是**数字**：它几乎是 1 token/字符，比汉字还贵。
    早期版本把数字并进「其他 = 0.18」那一档，于是报表/参数类的密集数字
    被低估 5 倍，预检形同虚设（实测 8000 字符要 7513 token，估算只给 3509）。
    """
    if not text:
        return 0
    cjk = digit = symbol = letter = 0
    for ch in text:
        o = ord(ch)
        if ch.isascii():
            if ch.isdigit():
                digit += 1
            elif ch.isalpha():
                letter += 1
            elif not ch.isspace():
                symbol += 1
        elif (0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x9FFF
                or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF
                or 0xFF00 <= o <= 0xFFEF or o >= 0x1F000):
            cjk += 1
        elif not ch.isspace():
            symbol += 1          # 全角标点以外的其他符号
    dense = digit + symbol
    body = dense + letter + cjk
    if body and dense / float(body) >= 0.40:
        # 数字符号主导（报表、参数表、代码）：按混合密度整体算
        return int(cjk * 0.66 + letter * 0.24 + dense * 1.10) + 1
    return int(cjk * 0.66 + digit * 1.00 + symbol * 0.62 + letter * 0.24) + 1


def level_model(level):
    return LEVEL_MAP.get(level, LEVEL_MAP["normal"])[1]


def style_prompt(style_name):
    """风格名 → 英文描述式短语。自定义风格优先。

    自定义风格里如果还留着老版本的 `{text_to_translate}` 占位符，
    这里顺手去掉 —— 那个占位符已经没有意义了（正文不再拼进提示词）。
    """
    for name, prompt in BUILTIN_STYLES:
        if name == style_name:
            return prompt
    for it in store.tr_custom_styles():
        if it["name"] == style_name:
            p = it.get("prompt", "") or ""
            return p.replace("{text_to_translate}", " ").replace("{text}", " ")
    return ""


def domain_def(domain_name):
    """领域名 → (domains 英文描述, terms 列表)。自定义领域优先。"""
    for it in store.tr_custom_domains():
        if it["name"] == domain_name:
            return it.get("domains", ""), it.get("terms", [])
    for name, desc, terms in BUILTIN_DOMAINS:
        if name == domain_name:
            return desc, [{"source": s, "target": t} for s, t in terms]
    return "", []


def all_style_names():
    names = [n for n, _p in BUILTIN_STYLES]
    for it in store.tr_custom_styles():
        if it["name"] not in names:
            names.append(it["name"])
    return names


def all_domain_names():
    names = [n for n, _d, _t in BUILTIN_DOMAINS]
    for it in store.tr_custom_domains():
        if it["name"] not in names:
            names.append(it["name"])
    return names


# ============================================================ 网络

class TranslateError(Exception):
    pass


def _friendly(err_body, status):
    """把阿里云的报错翻译成人话。"""
    code = ""
    msg = ""
    try:
        j = json.loads(err_body)
        e = j.get("error") or j
        code = str(e.get("code") or e.get("type") or "")
        msg = str(e.get("message") or "")
    except Exception:
        msg = err_body or ""
    for k, friendly in ERROR_HINTS.items():
        if k.lower() in (code + " " + msg).lower():
            return friendly + ("（%s）" % code if code and code not in friendly else "")
    if status == 401 or status == 403:
        return "API Key 无效或没有权限（HTTP %d）" % status
    if status == 429:
        return "请求太频繁或额度用尽（HTTP 429），稍后再试"
    if status >= 500:
        return "服务端出错了（HTTP %d），稍后重试" % status
    short = msg or ("HTTP %d" % status)
    return short[:160]


def _clean_hint(s):
    """把提示词整理成一句「描述式短语」，塞进 domains。

    为什么不能直接拼进 content：
      content 是这个模型的**待翻译正文**。以前把风格提示词拼在正文前面
      （官方 8.8 那种 `{text_to_translate}` 玩法），只要提示词里带
      "Translate the following text into formal academic prose…" 这种祈使句，
      模型就会把提示词本身当正文一起输出 —— 用户看到的
      「本报告采用正式的学术文体，适用于同行评议期刊…」就是这么来的；
      更糟的是它偶尔还会把 terms 的内部结构（`{"src": …}`）一起吐出来，
      甚至整段译文只剩一句领域描述。

      domains 就安全得多：它是 `translation_options` 里的**元信息**，
      模型只拿它当背景调整语体，**永远不会把它写进译文**。
      代价是它必须是描述性的（「正式学术散文：精确术语、无人称客观语气」），
      而不是命令句（「请翻译成正式学术散文」）。所以内置的都改写成名词短语了。

    这里做两件小事：去掉换行（拼成一个 domains）和多余标点。
    """
    s = " ".join((s or "").split())
    return s.strip(" .;；。")


# 译文里不该出现的「元描述」开头。只在**译文目标语言不是中文**时才用来清理：
# 模型偶尔会把英文提示词翻成中文复述一遍（"本报告采用正式的学术文体…"），
# 那一行显然不是译文内容。反过来，目标是中文时这句可能是真正的译文，不能删。
_META_STARTS = (
    "本报告", "本文", "以下译文", "以下为译文", "以下是译文", "译文如下",
    "根据您的要求", "根据要求", "根据上述", "请使用", "请注意", "说明：", "注：",
    "the following text", "translation:", "translated text:", "note:",
)

# 模型解释「我做了什么」的英文句子。它把 terms 当成「双语词典词条」时会这么说。
# 只在前两行、行内有这些词、且**原文里没有这些词**时才删（原文真讲词典就不动）。
_META_MARKERS = (
    "bilingual dictionary", "translation pairs", "as requested",
    "here is the translation", "here's the translation",
    "i have translated", "the translated text is", "below is the translation",
)


def _strip_echo(out, hints, dst=None, src_text=""):
    """兜底：把译文里混进来的「提示词回声」和内部结构剥掉。

    正常情况不该出现（domains 不会被写进译文），但模型偶尔仍会把背景描述
    复述一遍，或者把 terms 的 JSON 结构吐出来。这里做保守清理：
      1. 干掉 `{"src": ...}` / `{"tgt": ...}` 这类内部结构行；
      2. 开头几行如果跟某段 hint 高度重合（说明是回声），整行删掉；
      3. 目标语不是中文、而开头却冒出中文的「元描述」句，也删掉；
      4. 开头几行出现「我用了双语词典词条」这类英文说明，删掉。

    只删「明确是脏数据」的东西，正常译文一个字都不动。
    """
    if not out:
        return out
    lines = out.split("\n")
    kept = []
    hint_keys = []
    for h in hints or []:
        # 取 hint 里较长的片段做指纹，太短容易误伤正文
        for seg in re.split(r"[:：,，;；.]", h):
            seg = seg.strip()
            if len(seg) >= 18:
                hint_keys.append(seg.lower())

    # 目标语是中文时，中文元描述可能是真译文，别碰
    allow_cn_meta_strip = (dst or "") not in ("zh", "zh_tw", "yue")
    src_low = (src_text or "").lower()

    for idx, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            kept.append(ln)
            continue
        low = s.lower()
        # 1) 内部结构泄露
        if re.search(r'\{\s*"(src|tgt|source|target)"\s*:', low):
            continue
        if low in ('{"src"', "```json") or low.startswith("```json"):
            continue
        # 2) 提示词回声：整行或大部分内容来自 hint
        if hint_keys:
            hit = sum(1 for k in hint_keys if k in low)
            ratio = hit / float(len(hint_keys))
            if ratio >= 0.5 or any(low.startswith(k[:24]) for k in hint_keys):
                continue
        # 3) 中文元描述混进了非中文译文（只看开头三行，避免误删正文）
        if allow_cn_meta_strip and idx < 3 and any(s.startswith(w) for w in _META_STARTS):
            continue
        # 4) 英文「我做了什么」式说明（原文里没这些词才删）
        if (idx < 2 and len(s) < 320
                and any(m in low for m in _META_MARKERS)
                and not any(m in src_low for m in _META_MARKERS)):
            continue
        kept.append(ln)
    cleaned = "\n".join(kept).strip()
    # 万一全删光了就退回原文，总比给个空白强
    return cleaned or out.strip()


def translate_text(text, src, dst, level, style, domain, api_key, timeout=120):
    """同步调用。返回 (译文, usage dict, 耗时秒)。失败抛 TranslateError。

    输入侧只用两个口子：
      * `content` —— **只放原文**，一个字的额外提示都不加；
      * `translation_options.domains` —— 风格 + 领域合成一句英文描述。
        同时给出时用「风格在前、领域在后」，两段都保留，实测互不打架。
    """
    if not api_key:
        raise TranslateError("还没有填 API Key，请到「设置 → 翻译」里填一个")
    model = level_model(level)

    opts = {"source_lang": src or "auto", "target_lang": dst or "en"}

    hints = []
    sp = _clean_hint(style_prompt(style))
    if sp:
        hints.append(sp)
    desc, terms = domain_def(domain)
    desc = _clean_hint(desc)
    if desc:
        hints.append(desc)
    if hints:
        # 结尾补个句点，让模型读起来是「一段背景说明」而不是半截话
        opts["domains"] = ". ".join(hints) + "."

    if terms:
        # 只传真正在原文里出现的术语：terms 会算进输入 token，全塞进去纯属浪费。
        # 另外 target 为空 / 跟 source 一样的条目直接跳过 —— 那种「公差 → tolerance」
        # 式的占位条目发给模型只会让它犯迷糊。
        hit = [t for t in terms
               if t.get("source") and t.get("target")
               and t["source"] != t["target"]
               and t["source"] in text]
        if hit:
            opts["terms"] = [{"source": t["source"], "target": t["target"]} for t in hit]

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": text}],   # ← 只有原文
        "stream": False,                    # 明确不开流式
        "translation_options": opts,        # compatible-mode 下直接放顶层
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API_URL, data=data, headers={
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise TranslateError(_friendly(e.read().decode("utf-8", "replace"), e.code))
    except urllib.error.URLError as e:
        raise TranslateError("连不上阿里云（%s），检查网络或代理" % getattr(e, "reason", e))
    except Exception as e:
        raise TranslateError("请求失败：%s" % e)
    cost = time.time() - t0
    try:
        j = json.loads(body)
        out = j["choices"][0]["message"]["content"]
        usage = j.get("usage") or {}
    except Exception:
        raise TranslateError("返回格式不认识：%s" % body[:160])
    # 输出侧兜底：剥掉可能混进来的提示词回声 / terms 结构
    out = _strip_echo(out, hints, dst, text)
    return out.strip(), usage, cost


class _Bridge(QObject):
    """跨线程投递用的信号桥。

    子线程里绝不能直接用 QTimer.singleShot 回 UI 线程：那个定时器属于调用它的线程，
    子线程没有事件循环，回调永远不触发（表现就是界面一直卡在「翻译中…」）。
    信号走 Qt 的队列连接才能安全地回到 UI 线程。
    """
    ok = Signal(object, object, float)      # 译文 / usage / 耗时
    fail = Signal(str)


class TranslateWorker(threading.Thread):
    """发请求的线程。QThread 也行，但这里纯 IO，用 threading 更省事。"""

    def __init__(self, bridge, **kw):
        super().__init__(daemon=True)
        self._b, self._kw = bridge, kw

    def run(self):
        try:
            out, usage, cost = translate_text(**self._kw)
            self._b.ok.emit(out, usage, cost)
        except TranslateError as e:
            self._b.fail.emit(str(e))
        except Exception as e:
            self._b.fail.emit("翻译失败：%s" % e)


def _n(v):
    """千分位（历史窗口在定义处用它，跟下面的 _fmt_num 等价）。"""
    return "{:,}".format(int(v or 0))


def _similar(a, b):
    """两段文字是否几乎一样（用于「翻完跟原文一样」的兜底提示）。"""
    a = re.sub(r"\s+", "", a or "")
    b = re.sub(r"\s+", "", b or "")
    if not a or not b:
        return False
    return a == b or (a in b and len(a) / len(b) > 0.9)


# 只区分中文 / 英文；判不出来的（日文混排、代码、数字为主）一律返回 None，
# 不做任何自动切换，免得平白改掉用户设好的目标语言。
SCRIPT_MIN_RATIO = 0.80


def detect_lang(text):
    """按字符构成判断语言。返回 (语言, 占比)：中文→'zh'，英文→'en'，判断不了→(None, 0)。

    只认中 / 英。日文（有假名）、韩文（有谚文）、代码、纯数字一律返回 None，
    宁可不动用户的设置，也不要猜错后把目标语言改乱。
    """
    if not text:
        return None, 0.0
    cjk = latin = kana = hangul = 0
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:
            kana += 1                     # 平假名 / 片假名 → 日文
        elif 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
            hangul += 1                   # 谚文 → 韩文
        elif (0x3400 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF):
            cjk += 1                      # 汉字（含扩展 A、兼容区）
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            latin += 1
    total = cjk + latin + kana + hangul
    if total < 4:                         # 样本太短不做判断
        return None, 0.0
    # 有假名 / 谚文就说明不是纯中文，交给用户自己选，别乱猜
    if (kana + hangul) / total >= 0.10:
        return None, 0.0
    if cjk / total >= SCRIPT_MIN_RATIO:
        return "zh", cjk / total
    if latin / total >= SCRIPT_MIN_RATIO:
        return "en", latin / total
    return None, 0.0


# ============================================================ 受限输入框

class LimitedEditor(QPlainTextEdit):
    """超过上限就不再让输入，粘贴会自动截断。光标不会乱跳。

    上限按 **单位** 算而不是字符数：一个汉字算 2 单位、一个英文字符算 1 单位，
    这样中英文混排时长度手感一致（详见 MAX_UNITS 的注释）。
    """

    hit_limit = Signal()
    pasted = Signal(str)        # 真正粘进来的那段文字（用来判语言）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.max_units = MAX_UNITS

    def _remaining(self):
        return max(0, self.max_units - text_units(self.toPlainText()))

    def keyPressEvent(self, e):
        # 只拦「会插入字符」的按键，退格 / 删除 / 方向键一律放行
        if e.text() and len(e.text()) == 1 and e.key() not in (
                Qt.Key_Backspace, Qt.Key_Delete, Qt.Key_Return, Qt.Key_Enter):
            room = self._remaining()
            if room <= 0 or text_units(e.text()) > room:
                self.hit_limit.emit()
                e.accept()
                return
        super().keyPressEvent(e)

    def insertFromMimeData(self, src):
        if not src.hasText():
            super().insertFromMimeData(src)
            return
        text = src.text()
        room = self._remaining()
        if text_units(text) > room:
            got = fit_units(text, room)
            if got:
                self.insertPlainText(got)
            self.hit_limit.emit()
        else:
            got = text
            super().insertFromMimeData(src)
        if got:
            self.pasted.emit(got)


# ============================================================ 大窗口预览（纯文本）

def _centre(win, host):
    scr = QApplication.primaryScreen()
    avail = scr.availableGeometry() if scr else None
    w, h = win.width(), win.height()
    if host is not None and host.isVisible():
        g = host.frameGeometry()
        x, y = g.center().x() - w // 2, g.top() + theme.px(30)
    elif avail:
        x, y = avail.center().x() - w // 2, avail.top() + theme.px(40)
    else:
        return
    if avail:
        x = max(avail.left() + 8, min(x, avail.right() - w - 8))
        y = max(avail.top() + 8, min(y, avail.bottom() - h - 8))
    win.move(x, y)


class TextPreviewWindow(QDialog):
    """译文大窗口：可拉伸、可缩放、可搜索（和 JSON 转 MD 的预览一个手感）。

    多一个「MD 格式查看」开关：译文里常常带着 `#`、`**`、`|表格|` 这些 Markdown
    标记（原文是 Markdown、模型也照原样搬过来），纯文本看着全是符号；
    切到 MD 模式就用和 JSON转MD 同一套渲染器排出来。
    """

    MIN_ZOOM, MAX_ZOOM, STEP = 0.6, 2.4, 0.1

    def __init__(self, parent, text="", zoom=1.0):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._text = text
        self._zoom = zoom
        self._drag = None
        self._md_mode = False        # False = 纯文本，True = 按 Markdown 渲染

        scr = QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else None
        w, h = theme.px(1000), theme.px(760)
        if avail:
            w = min(w, int(avail.width() * 0.92))
            h = min(h, int(avail.height() * 0.9))
        self.resize(w, h)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        shell.addWidget(card)
        shadow(card, 48, QColor(30, 70, 105, 90), 8)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(theme.px(16), theme.px(14), theme.px(16), theme.px(12))
        lay.setSpacing(theme.px(8))

        head = QHBoxLayout()
        head.setSpacing(theme.px(8))
        t = QLabel("译文预览")
        t.setObjectName("H1")
        head.addWidget(t)
        self.lbl_zoom = QLabel("100%")
        self.lbl_zoom.setAlignment(Qt.AlignCenter)
        self.lbl_zoom.setStyleSheet(
            f"color:{theme.TEXT_MUTED}; font-size:{theme.px(11.5)}px; background:transparent;"
            f"min-width:{theme.px(40)}px;")
        head.addStretch(1)
        # MD 格式查看：可勾选，勾上就按 Markdown 渲染（再点一下回纯文本）
        self.btn_md = QPushButton("MD 格式")
        self.btn_md.setFixedSize(theme.px(72), theme.px(26))
        self.btn_md.setCheckable(True)
        self.btn_md.setCursor(Qt.PointingHandCursor)
        self.btn_md.setToolTip("按 Markdown 渲染查看（译文里带 #、**、表格时更好读）")
        self.btn_md.setStyleSheet(
            f"QPushButton{{background:rgba(255,255,255,0.75); color:{theme.PRIMARY_PRESS};"
            f"border:1px solid {theme.BORDER}; border-radius:8px; padding:0px;"
            f"font-size:{theme.px(11.5)}px;}}"
            f"QPushButton:hover{{background:#ffffff; border-color:{theme.PRIMARY};}}"
            f"QPushButton:checked{{background:{theme.PRIMARY}; color:#ffffff;"
            f"border-color:{theme.PRIMARY};}}")
        self.btn_md.clicked.connect(self.toggle_md)
        head.addWidget(self.btn_md)
        head.addWidget(self._mini("-", self.zoom_out))
        head.addWidget(self.lbl_zoom)
        head.addWidget(self._mini("+", self.zoom_in))
        rst = self._mini("重置", self.zoom_reset)
        rst.setFixedWidth(theme.px(52))
        head.addWidget(rst)
        x = self._mini("✕", self.close)
        x.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:8px;padding:0px;"
            "color:#7d9cb5;font-size:13px;}"
            "QPushButton:hover{background:#f0a0aa;color:#ffffff;}")
        head.addWidget(x)
        lay.addLayout(head)

        self.browser = QTextBrowser()
        self.browser.setObjectName("Preview")
        self.browser.setOpenExternalLinks(True)
        self.browser.setFocusPolicy(Qt.StrongFocus)

        from jsonmd import FindBar, make_search_btn
        self.find_bar = FindBar(self.browser)
        self.btn_find = make_search_btn()
        head.insertWidget(2, self.find_bar)
        head.insertWidget(3, self.btn_find)
        self.btn_find.clicked.connect(self._open_find)
        self.find_bar.closed.connect(lambda: self.btn_find.setChecked(False))
        lay.addWidget(self.browser, 1)

        sc = QShortcut(QKeySequence.Find, self)
        sc.setContext(Qt.WidgetWithChildrenShortcut)
        sc.activated.connect(self._open_find)

        grip = QHBoxLayout()
        grip.setContentsMargins(0, 0, 0, 0)
        grip.addStretch(1)
        grip.addWidget(QSizeGrip(card))
        lay.addLayout(grip)

        self.set_text(text)
        _centre(self, parent)

    def _mini(self, text, slot):
        b = QPushButton(text)
        b.setFixedSize(theme.px(34), theme.px(26))
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton{{background:rgba(255,255,255,0.75); color:{theme.PRIMARY_PRESS};"
            f"border:1px solid {theme.BORDER}; border-radius:8px; padding:0px;"
            f"font-size:{theme.px(12)}px;}}"
            f"QPushButton:hover{{background:#ffffff; border-color:{theme.PRIMARY};}}")
        b.clicked.connect(slot)
        return b

    def set_text(self, text):
        self._text = text or ""
        self._apply()

    def _apply(self):
        sb = self.browser.verticalScrollBar()
        ratio = sb.value() / float(max(1, sb.maximum()))
        if self._md_mode:
            # 按 Markdown 渲染：复用 JSON转MD 那套模板。
            # 字号交给 build_html 的 zoom 参数，这里**不要再 setFont** ——
            # 两边都乘一遍就是双重缩放，切模式时字会突然变大变小。
            from jsonmd import build_html
            self.browser.setHtml(build_html(self._text, self._zoom))
        else:
            f = QFont(theme.FONT_UI)
            f.setPointSizeF(theme.pt(11) * self._zoom)
            self.browser.setFont(f)
            self.browser.setPlainText(self._text)
        sb.setValue(int(round(ratio * sb.maximum())))
        if hasattr(self, "find_bar"):
            self.find_bar.refresh()
        self.lbl_zoom.setText("%d%%" % round(self._zoom * 100))

    def toggle_md(self):
        """纯文本 ↔ Markdown 渲染。"""
        self._md_mode = not self._md_mode
        self.btn_md.setChecked(self._md_mode)
        self.btn_md.setText("纯文本" if self._md_mode else "MD 格式")
        self.btn_md.setToolTip("切回纯文本查看" if self._md_mode
                               else "按 Markdown 渲染查看（译文里带 #、**、表格时更好读）")
        # 切模式时回到顶部，别让上次的滚动比例套到完全不同的排版上
        self.browser.verticalScrollBar().setValue(0)
        self._apply()

    def set_md_mode(self, on):
        if bool(on) != self._md_mode:
            self.toggle_md()

    def set_zoom(self, z):
        z = max(self.MIN_ZOOM, min(self.MAX_ZOOM, round(z, 2)))
        if abs(z - self._zoom) < 0.001:
            return
        self._zoom = z
        self._apply()

    def zoom_in(self):
        self.set_zoom(self._zoom + self.STEP)

    def zoom_out(self):
        self.set_zoom(self._zoom - self.STEP)

    def zoom_reset(self):
        self.set_zoom(1.0)

    def _open_find(self):
        self.find_bar.open()
        self.btn_find.setChecked(True)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.set_zoom(self._zoom + (self.STEP if e.angleDelta().y() > 0 else -self.STEP))
            e.accept()
            return
        super().wheelEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() <= theme.px(48):
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        super().mouseReleaseEvent(e)


# ============================================================ 翻译历史

class HistoryDetailWindow(QDialog):
    """单条历史：左原文、右译文，可复制，也能一键载回翻译页。"""

    def __init__(self, parent, entry, data, on_reuse=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._entry = entry or {}
        self._data = data or {}
        self._on_reuse = on_reuse
        self._drag = None

        scr = QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else None
        w, h = theme.px(1060), theme.px(760)
        if avail:
            w = min(w, int(avail.width() * 0.92))
            h = min(h, int(avail.height() * 0.9))
        self.resize(w, h)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        shell.addWidget(card)
        shadow(card, 48, QColor(30, 70, 105, 90), 8)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(theme.px(16), theme.px(14), theme.px(16), theme.px(12))
        lay.setSpacing(theme.px(8))

        head = QHBoxLayout()
        head.setSpacing(theme.px(8))
        t = QLabel("翻译记录")
        t.setObjectName("H1")
        head.addWidget(t)

        e = self._entry
        src_name = LANG_NAME.get(e.get("src") or "", e.get("src") or "?")
        dst_name = LANG_NAME.get(e.get("dst") or "", e.get("dst") or "?")
        info = "%s　%s → %s" % (trhistory.human_time(e.get("ts") or 0),
                                src_name, dst_name)
        lb = QLabel(info)
        lb.setStyleSheet(f"color:{theme.TEXT_MUTED}; font-size:{theme.px(11)}px;"
                         "background:transparent;")
        head.addWidget(lb)
        bits = []
        if e.get("level"):
            bits.append(LEVEL_MAP.get(e["level"], ("", "", ""))[1])
        if e.get("style") and e["style"] != DEFAULT_STYLE:
            bits.append(e["style"])
        if e.get("domain") and e["domain"] != DEFAULT_DOMAIN:
            bits.append(e["domain"])
        if bits:
            tag = QLabel("　".join(bits))
            tag.setStyleSheet(
                f"color:{theme.PRIMARY_PRESS}; background:{theme.PRIMARY_SOFT};"
                f"border-radius:{theme.px(8)}px; padding:{theme.px(1)}px {theme.px(7)}px;"
                f"font-size:{theme.px(10.5)}px;")
            head.addWidget(tag)
        head.addStretch(1)

        self.btn_copy_src = self._mini("复制原文", self._copy_src)
        self.btn_copy_src.setFixedWidth(theme.px(72))
        self.btn_copy_out = self._mini("复制译文", self._copy_out)
        self.btn_copy_out.setFixedWidth(theme.px(72))
        reuse = self._mini("载回翻译页", self._reuse)
        reuse.setFixedWidth(theme.px(88))
        head.addWidget(self.btn_copy_src)
        head.addWidget(self.btn_copy_out)
        head.addWidget(reuse)
        x = self._mini("✕", self.close)
        x.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:8px;padding:0px;"
            "color:#7d9cb5;font-size:13px;}"
            "QPushButton:hover{background:#f0a0aa;color:#ffffff;}")
        head.addWidget(x)
        lay.addLayout(head)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(theme.px(10))
        split.addWidget(self._side("原文", self._data.get("src_text", ""), "DetailLeft"))
        split.addWidget(self._side("译文", self._data.get("out_text", ""), "DetailRight"))
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([theme.px(500), theme.px(500)])
        lay.addWidget(split, 1)

        grip = QHBoxLayout()
        grip.setContentsMargins(0, 0, 0, 0)
        grip.addWidget(self._foot_label())
        grip.addStretch(1)
        grip.addWidget(QSizeGrip(card))
        lay.addLayout(grip)

        _centre(self, parent)

    def _foot_label(self):
        e = self._entry
        bits = ["%s 字符 → %s 字符" % (_n(e.get("chars") or 0),
                                       _n(len(self._data.get("out_text") or "")))]
        if e.get("tokens"):
            bits.append("%s tokens" % _n(e["tokens"]))
        if e.get("cost"):
            bits.append("%.1fs" % e["cost"])
        lb = QLabel("　·　".join(bits))
        lb.setStyleSheet(f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px;"
                         "background:transparent;")
        return lb

    def _side(self, title, text, obj):
        card = QFrame()
        card.setObjectName("CardSoft")
        v = QVBoxLayout(card)
        v.setContentsMargins(theme.px(10), theme.px(8), theme.px(10), theme.px(8))
        v.setSpacing(theme.px(6))
        h = QLabel(title)
        h.setStyleSheet(f"color:{theme.TEXT}; font-size:{theme.px(12.5)}px;"
                        "font-weight:700; background:transparent;")
        v.addWidget(h)
        br = QTextBrowser()
        br.setObjectName("Preview")
        br.setPlainText(text or "")
        v.addWidget(br, 1)
        card._browser = br
        return card

    def _mini(self, text, slot):
        b = QPushButton(text)
        b.setFixedHeight(theme.px(26))
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton{{background:rgba(255,255,255,0.75); color:{theme.PRIMARY_PRESS};"
            f"border:1px solid {theme.BORDER}; border-radius:8px; padding:0px;"
            f"font-size:{theme.px(11.5)}px;}}"
            f"QPushButton:hover{{background:#ffffff; border-color:{theme.PRIMARY};}}")
        b.clicked.connect(slot)
        return b

    def _copy_src(self):
        QApplication.clipboard().setText(self._data.get("src_text", ""))
        self.btn_copy_src.setText("已复制")

    def _copy_out(self):
        QApplication.clipboard().setText(self._data.get("out_text", ""))
        self.btn_copy_out.setText("已复制")

    def _reuse(self):
        if self._on_reuse:
            self._on_reuse(self._data.get("src_text", ""))
        self.close()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() <= theme.px(48):
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        super().mouseReleaseEvent(e)


class HistoryWindow(QDialog):
    """翻译历史列表。点一条 → 另开窗口看原文+译文。"""

    def __init__(self, parent, on_reuse=None, on_toast=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._on_reuse = on_reuse
        self._on_toast = on_toast
        self._drag = None
        self._detail = None
        self._items = []

        scr = QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else None
        w, h = theme.px(760), theme.px(620)
        if avail:
            w = min(w, int(avail.width() * 0.86))
            h = min(h, int(avail.height() * 0.86))
        self.resize(w, h)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        shell.addWidget(card)
        shadow(card, 44, QColor(30, 70, 105, 90), 8)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(theme.px(16), theme.px(14), theme.px(16), theme.px(12))
        lay.setSpacing(theme.px(8))

        head = QHBoxLayout()
        head.setSpacing(theme.px(8))
        t = QLabel("翻译历史")
        t.setObjectName("H1")
        head.addWidget(t)
        self.lbl_info = QLabel("")
        self.lbl_info.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px; background:transparent;")
        head.addWidget(self.lbl_info)
        head.addStretch(1)
        self.btn_open_dir = self._mini("打开文件夹", self._open_dir)
        self.btn_clear = self._mini("清空", self._clear)
        head.addWidget(self.btn_open_dir)
        head.addWidget(self.btn_clear)
        x = self._mini("✕", self.close)
        x.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:8px;padding:0px;"
            "color:#7d9cb5;font-size:13px;}"
            "QPushButton:hover{background:#f0a0aa;color:#ffffff;}")
        head.addWidget(x)
        lay.addLayout(head)

        self.lst = QListWidget()
        self.lst.setObjectName("HistoryList")
        self.lst.setCursor(Qt.PointingHandCursor)
        self.lst.setSpacing(theme.px(4))
        self.lst.itemClicked.connect(self._open_item)
        self.lst.itemActivated.connect(self._open_item)
        lay.addWidget(self.lst, 1)

        tip = QLabel("历史存在系统临时目录（%%TEMP%% 下），重启电脑后会自动清空。"
                     "最多保留 %d 条 / %d MB。"
                     % (trhistory.MAX_ITEMS, trhistory.MAX_BYTES // 1048576))
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10)}px;"
                          "background:transparent;")
        lay.addWidget(tip)

        grip = QHBoxLayout()
        grip.setContentsMargins(0, 0, 0, 0)
        grip.addStretch(1)
        grip.addWidget(QSizeGrip(card))
        lay.addLayout(grip)

        self.reload()
        _centre(self, parent)

    def _mini(self, text, slot):
        b = QPushButton(text)
        b.setFixedHeight(theme.px(26))
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton{{background:rgba(255,255,255,0.75); color:{theme.PRIMARY_PRESS};"
            f"border:1px solid {theme.BORDER}; border-radius:8px; padding:0px {theme.px(10)}px;"
            f"font-size:{theme.px(11.5)}px;}}"
            f"QPushButton:hover{{background:#ffffff; border-color:{theme.PRIMARY};}}")
        b.clicked.connect(slot)
        return b

    def reload(self):
        self._items = trhistory.load_all()
        self.lst.clear()
        if not self._items:
            it = QListWidgetItem("还没有翻译记录 —— 翻完一次就会自动存一条在这里")
            it.setFlags(Qt.NoItemFlags)
            it.setForeground(QColor(theme.TEXT_FAINT))
            self.lst.addItem(it)
            self.lbl_info.setText("0 条")
            return
        for e in self._items:
            item = QListWidgetItem("")
            item.setData(Qt.UserRole, e.get("id"))
            item.setSizeHint(QSize(0, theme.px(52)))
            self.lst.addItem(item)
            self.lst.setItemWidget(item, self._row_widget(e))
        self.lbl_info.setText("%d 条 · 共 %s" % (len(self._items),
                                                trhistory.human_size(trhistory.total_bytes())))

    def _row_widget(self, e):
        """两行：上面是原文摘要（正常色），下面是「时间 · 方向 · 档位 · 风格 · 领域」（浅色）。"""
        w = QWidget()
        w.setAttribute(Qt.WA_TransparentForMouseEvents, True)   # 点击穿透给列表项
        v = QVBoxLayout(w)
        v.setContentsMargins(theme.px(12), theme.px(6), theme.px(12), theme.px(6))
        v.setSpacing(theme.px(3))

        src_name = LANG_NAME.get(e.get("src") or "", e.get("src") or "?")
        dst_name = LANG_NAME.get(e.get("dst") or "", e.get("dst") or "?")
        bits = [trhistory.human_time(e.get("ts") or 0),
                "%s → %s" % (src_name, dst_name)]
        if e.get("level"):
            bits.append(LEVEL_MAP.get(e["level"], ("", "", ""))[1])
        if e.get("style") and e["style"] != DEFAULT_STYLE:
            bits.append(e["style"])
        if e.get("domain") and e["domain"] != DEFAULT_DOMAIN:
            bits.append(e["domain"])

        t1 = QLabel(e.get("preview") or "(无摘要)")
        t1.setStyleSheet(f"color:{theme.TEXT}; font-size:{theme.px(11.5)}px;"
                         "background:transparent;")
        t1.setToolTip(e.get("preview") or "")
        t2 = QLabel("　·　".join(bits))
        t2.setStyleSheet(f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10)}px;"
                         "background:transparent;")
        v.addWidget(t1)
        v.addWidget(t2)
        return w

    def _open_item(self, item):
        item_id = item.data(Qt.UserRole)
        if not item_id:
            return
        data = trhistory.load_item(item_id)
        if not data:
            if self._on_toast:
                self._on_toast("这条记录的文件已经不在了", "warn")
            self.reload()
            return
        entry = next((e for e in self._items if e.get("id") == item_id), {})
        if self._detail is not None:
            try:
                self._detail.close()
            except Exception:
                pass
        self._detail = HistoryDetailWindow(self.window(), entry, data,
                                           on_reuse=self._on_reuse)
        self._detail.show()
        self._detail.raise_()
        self._detail.activateWindow()

    def _open_dir(self):
        p = trhistory.dir_path()
        try:
            os.makedirs(p, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(p)          # Windows
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(p))
        except Exception as e:
            if self._on_toast:
                self._on_toast("打不开目录：%s" % e, "warn")

    def _clear(self):
        if not self._items:
            return
        dlg = ConfirmDialog(self, "清空翻译历史？",
                            "会删掉全部 %d 条记录，删了就找不回来了。" % len(self._items),
                            ok_text="清空", danger=True)
        if dlg.exec() != QDialog.Accepted:
            return
        trhistory.clear_all()
        self.reload()
        if self._on_toast:
            self._on_toast("翻译历史已清空", "success")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() <= theme.px(48):
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        super().mouseReleaseEvent(e)


# ============================================================ 工具页面

class TranslatePage(QWidget):
    """左：原文 + 控制条；右：译文。点「翻译」才发请求（不做实时翻译省 token）。"""

    def __init__(self, on_toast=None, parent=None):
        super().__init__(parent)
        self._on_toast = on_toast
        self._out = ""
        self._worker = None
        self._busy = False
        self._big = None
        self._hist = None
        self._t0 = 0.0
        self._tick = QTimer(self)
        self._tick.setInterval(200)
        self._tick.timeout.connect(self._on_tick)
        self._bridge = _Bridge(self)
        self._bridge.ok.connect(self._show_result)
        self._bridge.fail.connect(self._show_error)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, theme.px(6), 0, 0)
        lay.setSpacing(theme.px(10))

        lay.addWidget(self._build_control_bar())

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(theme.px(10))
        split.addWidget(self._build_input_card())
        split.addWidget(self._build_output_card())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([theme.px(430), theme.px(560)])
        lay.addWidget(split, 1)

        self._load_prefs()
        self._update_counter()

    # ---------- 顶部控制条 ----------
    def _build_control_bar(self):
        bar = QFrame()
        bar.setObjectName("CardSoft")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(theme.px(12), theme.px(8), theme.px(12), theme.px(8))
        bl.setSpacing(theme.px(8))

        bl.addWidget(self._tag("风格"))
        self.cmb_style = QComboBox()
        self.cmb_style.setMinimumWidth(theme.px(112))
        self.cmb_style.setCursor(Qt.PointingHandCursor)
        self.cmb_style.currentTextChanged.connect(self._on_style_changed)
        bl.addWidget(self.cmb_style)

        bl.addSpacing(theme.px(4))
        bl.addWidget(self._tag("领域"))
        self.cmb_domain = QComboBox()
        self.cmb_domain.setMinimumWidth(theme.px(104))
        self.cmb_domain.setCursor(Qt.PointingHandCursor)
        self.cmb_domain.currentTextChanged.connect(self._on_domain_changed)
        bl.addWidget(self.cmb_domain)

        bl.addSpacing(theme.px(4))
        bl.addWidget(self._tag("档位"))
        self.level_group = QButtonGroup(self)
        self.level_group.setExclusive(True)
        self.level_btns = {}
        for key, label, _m, tip in LEVELS:
            b = QPushButton(label)
            b.setObjectName("Seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(theme.px(28))
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, k=key: self._on_level(k))
            self.level_group.addButton(b)
            self.level_btns[key] = b
            bl.addWidget(b)

        bl.addStretch(1)
        self.lbl_model = QLabel("")
        self.lbl_model.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px; background:transparent;")
        bl.addWidget(self.lbl_model)
        return bar

    def _tag(self, text):
        lb = QLabel(text)
        lb.setStyleSheet(
            f"color:{theme.TEXT_MUTED}; font-size:{theme.px(11.5)}px; background:transparent;")
        return lb

    # ---------- 左：原文 ----------
    def _build_input_card(self):
        card = QFrame()
        card.setObjectName("Card")
        shadow(card)
        ll = QVBoxLayout(card)
        ll.setContentsMargins(theme.px(14), theme.px(12), theme.px(14), theme.px(12))
        ll.setSpacing(theme.px(8))

        head = QHBoxLayout()
        head.setSpacing(theme.px(6))
        h = QLabel("原文")
        h.setStyleSheet(f"color:{theme.TEXT}; font-size:{theme.px(13)}px; font-weight:700;"
                        "background:transparent;")
        head.addWidget(h)

        self.cmb_src = QComboBox()
        self.cmb_src.setFixedWidth(theme.px(92))
        self.cmb_src.setCursor(Qt.PointingHandCursor)
        self.cmb_src.currentIndexChanged.connect(self._on_lang_changed)
        head.addWidget(self.cmb_src)

        btn_swap = QPushButton("⇄")
        btn_swap.setFixedSize(theme.px(26), theme.px(24))
        btn_swap.setCursor(Qt.PointingHandCursor)
        btn_swap.setToolTip("互换源语言和目标语言")
        # padding 必须归零：全局 QPushButton 的 9px 18px 内边距会把字挤没
        btn_swap.setStyleSheet(
            f"QPushButton{{background:transparent; border:1px solid transparent;"
            f"border-radius:7px; color:{theme.TEXT_MUTED}; padding:0px;"
            f"font-size:{theme.px(13)}px;}}"
            f"QPushButton:hover{{border-color:{theme.BORDER}; background:#ffffff;"
            f"color:{theme.PRIMARY_PRESS};}}")
        btn_swap.clicked.connect(self.swap_langs)
        head.addWidget(btn_swap)

        self.cmb_dst = QComboBox()
        self.cmb_dst.setFixedWidth(theme.px(92))
        self.cmb_dst.setCursor(Qt.PointingHandCursor)
        self.cmb_dst.currentIndexChanged.connect(self._on_lang_changed)
        head.addWidget(self.cmb_dst)

        head.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px; background:transparent;")
        head.addWidget(self.lbl_count)
        ll.addLayout(head)

        self.editor = LimitedEditor()
        self.editor.setObjectName("Code")
        self.editor.max_units = MAX_UNITS
        self.editor.setPlaceholderText(
            "把要翻译的文字粘到这里…\n\n"
            "这里不做实时翻译：写完点「翻译」，免得每个字都发一次请求。\n"
            "单次最多 %d 单位（中文一个字算 2 单位，英文一个字符算 1 单位）。"
            % MAX_UNITS)
        f = QFont(theme.FONT_UI)
        f.setPointSizeF(theme.pt(11))
        self.editor.setFont(f)
        self.editor.setMinimumHeight(theme.px(200))
        self.editor.textChanged.connect(self._update_counter)
        self.editor.hit_limit.connect(self._on_hit_limit)
        self.editor.pasted.connect(self._on_pasted)
        ll.addWidget(self.editor, 1)

        row = QHBoxLayout()
        row.setSpacing(theme.px(8))
        self.btn_do = self._btn("翻译", self.start, primary=True)
        self.btn_do.setMinimumWidth(theme.px(78))
        row.addWidget(self.btn_do)
        self.btn_paste = self._btn("粘贴", self.paste_clipboard)
        self.btn_clear = self._btn("清空", self.clear_all)
        row.addWidget(self.btn_paste)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        self.lbl_hint = QLabel("")
        self.lbl_hint.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px; background:transparent;")
        row.addWidget(self.lbl_hint)
        ll.addLayout(row)
        return card

    # ---------- 右：译文 ----------
    def _build_output_card(self):
        card = QFrame()
        card.setObjectName("Card")
        shadow(card)
        rl = QVBoxLayout(card)
        rl.setContentsMargins(theme.px(14), theme.px(12), theme.px(14), theme.px(12))
        rl.setSpacing(theme.px(8))

        rh = QHBoxLayout()
        rh.setSpacing(theme.px(8))
        h = QLabel("译文")
        h.setStyleSheet(f"color:{theme.TEXT}; font-size:{theme.px(13)}px; font-weight:700;"
                        "background:transparent;")
        rh.addWidget(h)
        self.lbl_state = QLabel("等待翻译…")
        self.lbl_state.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px; background:transparent;")
        rh.addWidget(self.lbl_state)
        rh.addStretch(1)

        self.browser = QTextBrowser()
        self.browser.setObjectName("Preview")

        from jsonmd import FindBar, make_search_btn
        self.find_bar = FindBar(self.browser)
        self.btn_find = make_search_btn()
        rh.insertWidget(2, self.find_bar)
        rh.insertWidget(rh.indexOf(self.lbl_state) + 1, self.btn_find)
        self.btn_find.clicked.connect(self._toggle_find)
        self.find_bar.closed.connect(self._on_find_closed)

        self.btn_copy = self._btn("复制", self.copy_out)
        self.btn_big = self._btn("大窗口预览", self.open_big, primary=True)
        self.btn_copy.setEnabled(False)
        self.btn_big.setEnabled(False)
        rh.addWidget(self.btn_copy)
        rh.addWidget(self.btn_big)
        rl.addLayout(rh)

        rl.addWidget(self.browser, 1)

        sc = QShortcut(QKeySequence.Find, self)
        sc.setContext(Qt.WidgetWithChildrenShortcut)
        sc.activated.connect(self._open_find)

        foot = QHBoxLayout()
        foot.setSpacing(theme.px(8))
        self.btn_send_wc = self._btn("统计字数", self.send_to_wordcount)
        self.btn_send_wc.setEnabled(False)
        foot.addWidget(self.btn_send_wc)
        self.lbl_usage = QLabel("")
        self.lbl_usage.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px; background:transparent;")
        foot.addWidget(self.lbl_usage)
        foot.addStretch(1)
        # 历史始终可点：没有译文的时候也要能翻旧账
        self.btn_history = self._btn("历史", self.open_history)
        self.btn_history.setToolTip("以前的翻译记录（存在系统临时目录，关机会自动清空）")
        foot.addWidget(self.btn_history)
        rl.addLayout(foot)

        self._render_empty()
        return card

    def _btn(self, text, slot, primary=False):
        b = QPushButton(text)
        b.setObjectName("MiniPrimary" if primary else "Mini")
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(theme.px(32))
        b.clicked.connect(slot)
        return b

    # ---------- 偏好 ----------
    def _load_prefs(self):
        # 整个填充过程必须屏蔽信号：addItem / setCurrentIndex 都会触发
        # currentIndexChanged，此时对侧下拉框还没建好，会读到 None 并把
        # 错误的值写回 settings —— 表现就是「明明存的是中译英，打开变成
        # 自动检测→简体中文」，然后中文翻成中文、原样返回。
        for w in (self.cmb_src, self.cmb_dst):
            w.blockSignals(True)
        try:
            for key, label in LANGS:
                self.cmb_src.addItem(label, key)
                if key != "auto":
                    self.cmb_dst.addItem(label, key)
            src = store.tr_get("src", "zh") or "zh"
            dst = store.tr_get("dst", "en") or "en"
            self._select(self.cmb_src, src)
            self._select(self.cmb_dst, dst if dst != "auto" else "en")
            # 两边撞成同一种语言时给个能用的默认，避免翻出来跟原文一样
            if (self.cmb_src.currentData() == self.cmb_dst.currentData()
                    and self.cmb_src.currentData() != "auto"):
                self._select(self.cmb_dst, "en" if self.cmb_src.currentData() != "en" else "zh")
        finally:
            for w in (self.cmb_src, self.cmb_dst):
                w.blockSignals(False)

        self._reload_styles(store.tr_get("style", DEFAULT_STYLE))
        self._reload_domains(store.tr_get("domain", DEFAULT_DOMAIN))
        lvl = store.tr_get("level", "normal")
        if lvl not in self.level_btns:
            lvl = "normal"
        self.level_btns[lvl].setChecked(True)
        self._refresh_model_label()
        # 校正后的结果落回配置，下次打开就是对的
        self._on_lang_changed()

    @staticmethod
    def _select(cmb, code):
        i = cmb.findData(code)
        if i >= 0:
            cmb.setCurrentIndex(i)

    def _reload_styles(self, want=None):
        want = want or self.cmb_style.currentText() or store.tr_get("style", DEFAULT_STYLE)
        self.cmb_style.blockSignals(True)
        self.cmb_style.clear()
        self.cmb_style.addItems(all_style_names())
        i = self.cmb_style.findText(want)
        self.cmb_style.setCurrentIndex(i if i >= 0 else 0)
        self.cmb_style.blockSignals(False)
        self._refresh_style_tip()

    def _reload_domains(self, want=None):
        want = want or self.cmb_domain.currentText() or store.tr_get("domain", DEFAULT_DOMAIN)
        self.cmb_domain.blockSignals(True)
        self.cmb_domain.clear()
        self.cmb_domain.addItems(all_domain_names())
        i = self.cmb_domain.findText(want)
        self.cmb_domain.setCurrentIndex(i if i >= 0 else 0)
        self.cmb_domain.blockSignals(False)
        self._refresh_domain_tip()

    def _refresh_style_tip(self):
        sp = style_prompt(self.cmb_style.currentText())
        self.cmb_style.setToolTip(sp[:300] if sp else "不加任何风格约束，按原样翻译")

    def _refresh_domain_tip(self):
        desc, terms = domain_def(self.cmb_domain.currentText())
        if not desc and not terms:
            self.cmb_domain.setToolTip("不使用领域提示，也不带术语")
        else:
            self.cmb_domain.setToolTip("术语 %d 条\n%s" % (len(terms), desc[:220]))

    def _refresh_model_label(self):
        _label, model, _hint = LEVEL_MAP[self.current_level()]
        self.lbl_model.setText(model)

    def current_level(self):
        for k, b in self.level_btns.items():
            if b.isChecked():
                return k
        return "normal"

    def _on_style_changed(self, text):
        store.tr_set("style", text)
        self._refresh_style_tip()

    def _on_domain_changed(self, text):
        store.tr_set("domain", text)
        self._refresh_domain_tip()

    def _on_level(self, key):
        store.tr_set("level", key)
        self._refresh_model_label()
        self._update_counter()

    def _on_lang_changed(self, *_):
        # 源 / 目标撞车时自动把目标挪到另一个：不拦的话中文会被「翻译」成
        # 中文，看着像翻译失败（尤其 source=auto 时模型检测出中文→中文）。
        s, d = self.cmb_src.currentData(), self.cmb_dst.currentData()
        if s == d and s != "auto":
            alt = "en" if s != "en" else "zh"
            self.cmb_dst.blockSignals(True)
            self._select(self.cmb_dst, alt)
            self.cmb_dst.blockSignals(False)
            d = alt
        store.tr_set("src", s)
        store.tr_set("dst", d)
        self._update_counter()

    def swap_langs(self):
        s, d = self.cmb_src.currentData(), self.cmb_dst.currentData()
        # 互换期间不能触发 currentIndexChanged：先改源再改目标时，
        # 第一次改动就会把「目标语」写成当时的旧源语，第二次再改又覆盖回去。
        for w in (self.cmb_src, self.cmb_dst):
            w.blockSignals(True)
        try:
            if s == "auto":
                # 自动检测时互换没意义：把目标语搬去当源语，目标语给个合理默认
                self._select(self.cmb_src, d)
                self._select(self.cmb_dst, "zh" if d != "zh" else "en")
            else:
                self._select(self.cmb_src, d)
                self._select(self.cmb_dst, s)
            if (self.cmb_src.currentData() == self.cmb_dst.currentData()
                    and self.cmb_src.currentData() != "auto"):
                self._select(self.cmb_dst, "en" if s != "en" else "zh")
        finally:
            for w in (self.cmb_src, self.cmb_dst):
                w.blockSignals(False)
        self._on_lang_changed()

    # ---------- 输入 ----------
    def set_text(self, text, auto_detect=True):
        """外部送进来的原文（JSON转MD 的「送去翻译」走这里）。

        只负责填进输入框，**不会自动翻译** —— 翻译要花 token，得用户自己确认。
        填完顺手做一次语言检测（和粘贴一个待遇），省得粘中文却设着中译中。
        """
        text = text or ""
        if not text:
            return
        if text_units(text) > MAX_UNITS:
            text = fit_units(text, MAX_UNITS)
            self.editor.setPlainText(text)
            self._on_hit_limit()
        else:
            self.editor.setPlainText(text)
        self.editor.moveCursor(QTextCursor.Start)
        if auto_detect:
            # 只拿真正放进去的那段判语言，别用被截掉的完整原文
            self._on_pasted(text)
        self._update_counter()

    def _update_counter(self):
        text = self.editor.toPlainText()
        n = len(text)
        u = text_units(text)
        est = estimate_tokens(text)
        self.lbl_count.setText("%d / %d 单位" % (u, MAX_UNITS))
        over = u > MAX_UNITS or est > BODY_TOKEN_BUDGET
        near = u >= MAX_UNITS * 0.9 or est >= BODY_TOKEN_BUDGET * 0.9
        self.lbl_count.setStyleSheet(
            f"color:{'#c06a72' if over else (theme.WARN if near else theme.TEXT_FAINT)};"
            f"font-size:{theme.px(10.5)}px; background:transparent;")
        self.lbl_count.setToolTip(
            "中文 / 日文 / emoji 每个算 2 单位，英文、数字、符号每个算 1 单位。\n"
            "本段 %d 个字符 → %d 单位。" % (n, u))
        self.btn_do.setEnabled(not self._busy and bool(n))
        if n:
            self.lbl_hint.setText("≈%d tokens" % est)
        else:
            self.lbl_hint.setText("")

    def _on_hit_limit(self):
        self._toast("最多 %d 单位（中文一字 2 单位、英文一字符 1 单位）" % MAX_UNITS, "warn")

    def _on_pasted(self, text):
        """粘贴后立刻判一次语言：粘进来的内容跟目标语言同种，就把目标语言调过去。

        只在中 / 英之间判断，且要求脚本占比 ≥80%；判不出来的（日韩、代码、
        纯数字）保持不动。用户之后仍可手动改回来 —— 这里只是「猜一次」。
        """
        lang, ratio = detect_lang(text)
        if not lang:
            return
        dst = self.cmb_dst.currentData()
        if lang != dst:
            return
        # 粘进来的就是目标语言 → 可能要「反向翻」，把目标换掉
        new_dst = "en" if lang == "zh" else "zh"
        self.cmb_dst.blockSignals(True)
        self._select(self.cmb_dst, new_dst)
        self.cmb_dst.blockSignals(False)
        # 只在「新目标语撞上了源语」时才动源语（否则会把用户手选的源语冲掉）。
        # 不处理这种情况的话，_on_lang_changed 发现两边相同又会把目标语翻回来，
        # 自动纠正自己把自己抵掉了。
        if self.cmb_src.currentData() == new_dst:
            self.cmb_src.blockSignals(True)
            self._select(self.cmb_src, "auto")
            self.cmb_src.blockSignals(False)
        self._on_lang_changed()
        name = LANG_NAME.get(new_dst, new_dst)
        self._toast("检测到%s内容，目标语言已自动改为%s（可手动修改）"
                    % ("中文" if lang == "zh" else "英文", name), "info")

    def paste_clipboard(self):
        txt = QApplication.clipboard().text()
        if not txt.strip():
            self._toast("剪贴板里没有文字", "warn")
            return
        room = self.editor._remaining()
        if text_units(txt) > room:
            got = fit_units(txt, room)
            if got:
                self.editor.insertPlainText(got)
            self._on_hit_limit()
        else:
            got = txt
            self.editor.insertPlainText(got)
        self.editor.setFocus()
        # insertPlainText 不走 insertFromMimeData，这里要自己补一次语言检测
        if got:
            self._on_pasted(got)

    def clear_all(self):
        self.editor.clear()
        self._out = ""
        self._render_empty()
        self._set_state("等待翻译…", None)
        self.lbl_usage.setText("")
        self.btn_copy.setEnabled(False)
        self.btn_big.setEnabled(False)
        self.btn_send_wc.setEnabled(False)

    # ---------- 翻译 ----------
    def start(self):
        if self._busy:
            return
        text = self.editor.toPlainText().strip()
        if not text:
            self._toast("先粘贴或输入要翻译的文字", "warn")
            return
        if text_units(text) > MAX_UNITS:
            self._toast("超过 %d 单位上限了，删掉一些再翻" % MAX_UNITS, "warn")
            return
        # 单位没超不代表 token 没超：数字和符号几乎 1 token/字符，
        # 「一万字的报表」比「一万字的文章」贵一倍多。这里先估算一次，
        # 会撞 8192 就当场拦下并告诉用户该减到多少，别等阿里云返回 400。
        est = estimate_tokens(text)
        if est > BODY_TOKEN_BUDGET:
            keep = max(1, int(text_units(text) * BODY_TOKEN_BUDGET / float(est)))
            self._toast("这段内容 token 太密（≈%d，上限 8192），请删到约 %d 单位再翻"
                        % (est, keep), "warn")
            self._set_state("输入过长（≈%d tokens）" % est, True)
            return
        key = store.tr_api_key()
        if not key:
            self._toast("还没填 API Key，到「设置 → 翻译」里填一个", "warn")
            self._set_state("缺少 API Key", True)
            return

        src = self.cmb_src.currentData() or "auto"
        dst = self.cmb_dst.currentData() or "en"
        if src == dst and src != "auto":
            self._toast("源语言和目标语言一样，换一个吧", "warn")
            return
        # auto 时不做拦截，但翻完如果跟原文一模一样，多半是语种撞了，提示一下
        self._same_src_dst_hint = (src == "auto")

        self._busy = True
        self.btn_do.setEnabled(False)
        self.btn_do.setText("翻译中…")
        self._t0 = time.time()
        self._tick.start()
        self._set_state("正在翻译…", None)
        self.lbl_usage.setText("")

        api_kw = dict(text=text, src=src, dst=dst, level=self.current_level(),
                      style=self.cmb_style.currentText(),
                      domain=self.cmb_domain.currentText(), api_key=key)
        self._worker = TranslateWorker(self._bridge, **api_kw)
        self._worker.start()

    def _on_tick(self):
        if self._busy:
            self._set_state("正在翻译… %.1fs" % (time.time() - self._t0), None)

    def _show_result(self, out, usage, cost):
        self._finish()
        self._out = out or ""
        self.browser.setPlainText(self._out)
        self.find_bar.refresh()
        n = len(self._out)
        total = int((usage or {}).get("total_tokens") or 0)
        if total:
            store.add_tr_token_used(total)
        bits = ["%d 字符" % n, "%.1fs" % cost]
        if total:
            bits.append("%d tokens" % total)
        self._set_state("完成 · " + " · ".join(bits), False)
        self.lbl_usage.setText(("本次 %d tokens　" % total if total else "") +
                               "累计 %s" % _fmt_num(store.tr_get("token_used", 0)))
        self.btn_copy.setEnabled(bool(self._out))
        self.btn_big.setEnabled(bool(self._out))
        self.btn_send_wc.setEnabled(bool(self._out))

        # 存一条历史：原文 + 译文，放系统临时目录（关机自动清掉）
        try:
            trhistory.save(self.editor.toPlainText(), self._out, {
                "src": self.cmb_src.currentData(),
                "dst": self.cmb_dst.currentData(),
                "level": self.current_level(),
                "style": self.cmb_style.currentText(),
                "domain": self.cmb_domain.currentText(),
                "tokens": total,
                "cost": cost,
            })
        except Exception:
            pass          # 历史存不下来不该影响「翻译成功」这件事

        # 自动检测时最常见的一个「翻译无效」：模型判断原文就是目标语，
        # 于是原样返回。这里给一句明确提示，别让人误以为功能坏了。
        if getattr(self, "_same_src_dst_hint", False):
            src_text = self.editor.toPlainText().strip()
            if self._out and _similar(self._out, src_text):
                self._set_state("译文与原文几乎相同 —— 原文可能就是目标语，换个目标语言试试", True)

    def _finish(self):
        self._busy = False
        self._tick.stop()
        self.btn_do.setText("翻译")
        self.btn_do.setEnabled(bool(self.editor.toPlainText()))

    def _show_error(self, msg):
        self._finish()
        self._out = ""
        self._render_empty(msg, error=True)
        self._set_state(msg[:52], True)

    def _render_empty(self, text="", error=False):
        color = "#c06a72" if error else "#a8c0d2"
        self.browser.setHtml(
            '<div style="color:%s; font-size:%dpx; margin-top:%dpx;">%s</div>'
            % (color, theme.px(12), theme.px(30),
               _esc(text or "译文会显示在这里")))

    def _set_state(self, text, error):
        text = text if len(text) <= 60 else text[:58] + "…"
        color = "#c06a72" if error else (theme.TEXT_MUTED if error is False
                                         else theme.TEXT_FAINT)
        self.lbl_state.setText(text)
        self.lbl_state.setStyleSheet(
            f"color:{color}; font-size:{theme.px(10.5)}px; background:transparent;")

    # ---------- 输出操作 ----------
    def copy_out(self):
        if not self._out:
            return
        QApplication.clipboard().setText(self._out)
        self._toast("译文已复制", "success")

    def open_big(self):
        if not self._out:
            return
        if self._big is None:
            self._big = TextPreviewWindow(self.window(), self._out)
        else:
            self._big.set_text(self._out)
        self._big.show()
        self._big.raise_()
        self._big.activateWindow()

    def send_to_wordcount(self):
        if self._out and getattr(self, "_on_wordcount", None):
            self._on_wordcount(self._out)

    # ---------- 历史 ----------
    def open_history(self):
        """打开历史列表（非模态，这样点条目弹出的详情窗口才能正常交互）。"""
        if getattr(self, "_hist", None) is None:
            self._hist = HistoryWindow(self.window(), on_reuse=self.set_text,
                                       on_toast=self._toast)
        else:
            self._hist.reload()
        self._hist.show()
        self._hist.raise_()
        self._hist.activateWindow()

    def _open_find(self):
        self.lbl_state.setVisible(False)
        self.btn_copy.setVisible(False)
        self.btn_big.setVisible(False)
        self.find_bar.open()
        self.btn_find.setChecked(True)

    def _toggle_find(self):
        if self.find_bar.isVisible():
            self.find_bar.close_bar()
        else:
            self._open_find()

    def _on_find_closed(self):
        self.lbl_state.setVisible(True)
        self.btn_copy.setVisible(True)
        self.btn_big.setVisible(True)
        self.btn_find.setChecked(False)

    def _toast(self, msg, kind="info"):
        if self._on_toast:
            self._on_toast(msg, kind)

    # ---------- 设置变更后同步 ----------
    def apply_settings(self):
        """设置里改了风格 / 领域 / Key：刷新下拉，保留当前选择。"""
        self._reload_styles(store.tr_get("style", DEFAULT_STYLE))
        self._reload_domains(store.tr_get("domain", DEFAULT_DOMAIN))


def _fmt_num(n):
    return "{:,}".format(int(n or 0))


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ============================================================ 设置面板

class TranslationSettings(QWidget):
    """翻译设置：API Key、风格、领域（含术语）、控制台入口。"""

    def __init__(self, on_change=None, parent=None):
        super().__init__(parent)
        self._on_change = on_change

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        from PySide6.QtWidgets import QScrollArea
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(theme.px(2), theme.px(2), theme.px(10), theme.px(8))
        lay.setSpacing(theme.px(12))

        lay.addWidget(self._section("API Key"))
        row = QHBoxLayout()
        row.setSpacing(theme.px(8))
        self.edit_key = QLineEdit()
        self.edit_key.setEchoMode(QLineEdit.Password)
        self.edit_key.setPlaceholderText("sk-…（阿里云百炼的 API Key）")
        self.edit_key.setText(store.tr_api_key())
        self.edit_key.editingFinished.connect(self._save_key)
        row.addWidget(self.edit_key, 1)
        self.btn_eye = QPushButton("显示")
        self.btn_eye.setObjectName("Mini")
        self.btn_eye.setFixedHeight(theme.px(30))
        self.btn_eye.setCursor(Qt.PointingHandCursor)
        self.btn_eye.setCheckable(True)
        self.btn_eye.clicked.connect(self._toggle_eye)
        row.addWidget(self.btn_eye)
        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("MiniPrimary")
        self.btn_save.setFixedHeight(theme.px(30))
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self._save_key)
        row.addWidget(self.btn_save)
        lay.addLayout(row)

        tip = QLabel("Key 只保存在本机 data/settings.json（已在 .gitignore 里），不会上传。"
                     "百炼没有开放余额查询接口，所以这里只提供控制台入口。")
        tip.setWordWrap(True)
        lay.addWidget(self._hint(tip))

        row2 = QHBoxLayout()
        row2.setSpacing(theme.px(8))
        b_console = QPushButton("打开百炼控制台 · API Key")
        b_console.setObjectName("Mini")
        b_console.setFixedHeight(theme.px(30))
        b_console.setCursor(Qt.PointingHandCursor)
        b_console.clicked.connect(lambda: self._open(CONSOLE_URL))
        row2.addWidget(b_console)
        b_bill = QPushButton("查看用量与账单")
        b_bill.setObjectName("Mini")
        b_bill.setFixedHeight(theme.px(30))
        b_bill.setCursor(Qt.PointingHandCursor)
        b_bill.clicked.connect(
            lambda: self._open("https://bailian.console.aliyun.com/cn-beijing/model/usage"))
        row2.addWidget(b_bill)
        b_test = QPushButton("测试连通")
        b_test.setObjectName("Mini")
        b_test.setFixedHeight(theme.px(30))
        b_test.setCursor(Qt.PointingHandCursor)
        b_test.clicked.connect(self._test)
        row2.addWidget(b_test)
        row2.addStretch(1)
        self.lbl_test = QLabel("")
        self.lbl_test.setStyleSheet(
            f"color:{theme.TEXT_MUTED}; font-size:{theme.px(11)}px; background:transparent;")
        row2.addWidget(self.lbl_test)
        lay.addLayout(row2)

        self.lbl_used = QLabel("")
        lay.addWidget(self._hint(self.lbl_used))

        # ---- 风格 ----
        lay.addWidget(self._section("翻译风格"))
        lay.addWidget(self._hint_text(
            "风格改变的是语体（口语 / 学术 / 正式…）。内置的不能改，自定义的写在下面。\n"
            "⚠️ 请用**英文的描述性短语**写，不要写成「请把下面翻译成…」这种命令句 —— "
            "提示词会被当作背景说明交给模型，命令句容易让它把指令本身也翻出来。\n"
            "例：Formal academic prose: precise vocabulary, impersonal register, "
            "no contractions."))

        self.style_box = QVBoxLayout()
        self.style_box.setSpacing(theme.px(6))
        lay.addLayout(self.style_box)
        b_add_style = QPushButton("＋ 添加自定义风格")
        b_add_style.setObjectName("Mini")
        b_add_style.setFixedHeight(theme.px(30))
        b_add_style.setCursor(Qt.PointingHandCursor)
        b_add_style.clicked.connect(lambda: self._edit_style(None))
        holder = QHBoxLayout()
        holder.addWidget(b_add_style)
        holder.addStretch(1)
        lay.addLayout(holder)

        # ---- 领域 ----
        lay.addWidget(self._section("翻译领域"))
        lay.addWidget(self._hint_text(
            "领域会同时改两样东西：domains（给模型的领域说明，官方只支持英文）"
            "和 terms（术语表，中文→译文对照）。术语只会把原文里真出现的那几条发出去，"
            "不会白占 token。"))

        self.domain_box = QVBoxLayout()
        self.domain_box.setSpacing(theme.px(6))
        lay.addLayout(self.domain_box)
        b_add_domain = QPushButton("＋ 添加自定义领域")
        b_add_domain.setObjectName("Mini")
        b_add_domain.setFixedHeight(theme.px(30))
        b_add_domain.setCursor(Qt.PointingHandCursor)
        b_add_domain.clicked.connect(lambda: self._edit_domain(None))
        holder2 = QHBoxLayout()
        holder2.addWidget(b_add_domain)
        holder2.addStretch(1)
        lay.addLayout(holder2)

        lay.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        self._refresh()

    # ---- 小工具 ----
    def _section(self, text):
        lb = QLabel(text)
        lb.setStyleSheet(
            f"color:{theme.TEXT}; font-size:{theme.px(13)}px; font-weight:700;"
            "background:transparent; margin-top:2px;")
        return lb

    def _hint(self, lb):
        lb.setWordWrap(True)
        lb.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px; background:transparent;")
        return lb

    def _hint_text(self, text):
        return self._hint(QLabel(text))

    def _open(self, url):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(url))

    # ---- Key ----
    def _toggle_eye(self):
        on = self.btn_eye.isChecked()
        self.edit_key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        self.btn_eye.setText("隐藏" if on else "显示")

    def _save_key(self):
        store.save_tr_api_key(self.edit_key.text())
        self.lbl_test.setText("已保存")
        self.lbl_test.setStyleSheet(
            f"color:{theme.SUCCESS}; font-size:{theme.px(11)}px; background:transparent;")

    def _test(self):
        key = self.edit_key.text().strip()
        if not key:
            self.lbl_test.setText("先填 Key")
            return
        store.save_tr_api_key(key)
        self.lbl_test.setText("正在测试…")
        self.lbl_test.setStyleSheet(
            f"color:{theme.TEXT_MUTED}; font-size:{theme.px(11)}px; background:transparent;")
        if getattr(self, "_tb", None) is None:
            self._tb = _Bridge(self)
            self._tb.ok.connect(self._test_ok)
            self._tb.fail.connect(self._test_fail)
        TranslateWorker(self._tb, text="你好", src="zh", dst="en",
                        level="fast", style=DEFAULT_STYLE, domain=DEFAULT_DOMAIN,
                        api_key=key).start()

    def _test_ok(self, out, usage, cost):
        self.lbl_test.setText("连通 ✓ %.1fs" % cost)
        self.lbl_test.setStyleSheet(
            f"color:{theme.SUCCESS}; font-size:{theme.px(11)}px; background:transparent;")

    def _test_fail(self, msg):
        self.lbl_test.setText(msg[:40])
        self.lbl_test.setStyleSheet(
            f"color:{theme.DANGER}; font-size:{theme.px(11)}px; background:transparent;")

    # ---- 列表 ----
    def _refresh(self):
        self.lbl_used.setText("本地累计消耗：%s tokens（按接口返回的 usage 累加）"
                              % _fmt_num(store.tr_get("token_used", 0)))

        while self.style_box.count():
            it = self.style_box.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for name, _p in BUILTIN_STYLES:
            self.style_box.addWidget(self._row(name, "内置", locked=True))
        for it in store.tr_custom_styles():
            self.style_box.addWidget(self._row(
                it["name"], (it.get("prompt") or "（空提示词）").replace("\n", " ")[:60],
                on_edit=lambda i=it: self._edit_style(i)))

        while self.domain_box.count():
            it = self.domain_box.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for name, desc, terms in BUILTIN_DOMAINS:
            extra = "内置 · 术语 %d 条" % len(terms) if terms else "内置"
            self.domain_box.addWidget(self._row(name, extra, locked=True))
        for it in store.tr_custom_domains():
            desc = "自定义 · 术语 %d 条" % len(it.get("terms") or [])
            self.domain_box.addWidget(self._row(
                it["name"], desc, on_edit=lambda i=it: self._edit_domain(i)))

    def _row(self, name, detail, locked=False, on_edit=None):
        w = QFrame()
        w.setObjectName("CardSoft")
        h = QHBoxLayout(w)
        h.setContentsMargins(theme.px(10), theme.px(6), theme.px(8), theme.px(6))
        h.setSpacing(theme.px(8))
        t = QLabel(name)
        t.setStyleSheet(f"color:{theme.TEXT}; font-size:{theme.px(12)}px;"
                        "background:transparent;")
        h.addWidget(t)
        d = QLabel(detail)
        d.setStyleSheet(f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px;"
                        "background:transparent;")
        h.addWidget(d, 1)
        if on_edit:
            b = QPushButton("编辑")
            b.setObjectName("Mini")
            b.setFixedHeight(theme.px(26))
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda: on_edit())
            h.addWidget(b)
        if not locked:
            x = QPushButton("删除")
            x.setObjectName("Mini")
            x.setFixedHeight(theme.px(26))
            x.setCursor(Qt.PointingHandCursor)
            x.clicked.connect(lambda: self._delete(name))
            h.addWidget(x)
        return w

    def _delete(self, name):
        d = ConfirmDialog.ask(self, "删除「%s」？" % name, "删掉后已经选中的会回到默认项。")
        if not d:
            return
        styles = [s for s in store.tr_custom_styles() if s["name"] != name]
        store.save_tr_custom_styles(styles)
        domains = [s for s in store.tr_custom_domains() if s["name"] != name]
        store.save_tr_custom_domains(domains)
        if store.tr_get("style") == name:
            store.tr_set("style", DEFAULT_STYLE)
        if store.tr_get("domain") == name:
            store.tr_set("domain", DEFAULT_DOMAIN)
        self._refresh()
        self._changed()

    # ---- 编辑对话框 ----
    def _edit_style(self, item):
        dlg = StyleDialog(self, item)
        if dlg.exec() == QDialog.Accepted:
            self._refresh()
            self._changed()

    def _edit_domain(self, item):
        dlg = DomainDialog(self, item)
        if dlg.exec() == QDialog.Accepted:
            self._refresh()
            self._changed()

    def _changed(self):
        if self._on_change:
            self._on_change()


class StyleDialog(QDialog):
    """新增 / 编辑一个自定义风格。"""

    def __init__(self, parent, item=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setMinimumWidth(theme.px(560))
        self._old = (item or {}).get("name")

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        shell.addWidget(card)
        shadow(card, 40, QColor(30, 70, 105, 80), 6)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(theme.px(22), theme.px(18), theme.px(22), theme.px(16))
        lay.setSpacing(theme.px(10))

        t = QLabel("自定义翻译风格" if item else "添加翻译风格")
        t.setObjectName("H1")
        lay.addWidget(t)

        lay.addWidget(self._lb("名称"))
        self.edit_name = QLineEdit((item or {}).get("name", ""))
        self.edit_name.setPlaceholderText("比如：法律合同、小红书文案")
        lay.addWidget(self.edit_name)

        lay.addWidget(self._lb("风格描述（建议英文，写成描述而不是命令）"))
        self.edit_prompt = QPlainTextEdit((item or {}).get("prompt", ""))
        self.edit_prompt.setObjectName("Code")
        self.edit_prompt.setPlaceholderText(
            "写成「这是什么语体」，不要写「请把下面翻译成…」：\n\n"
            "✅ Formal legal contract language: standard common-law terminology, "
            "precise and impersonal, no colloquialisms.\n\n"
            "✅ Casual social-media copy: short punchy sentences, playful tone, emoji ok.\n\n"
            "❌ Please translate the following text into a casual style…\n"
            "   （命令句会让模型把指令本身也翻出来）")
        self.edit_prompt.setMinimumHeight(theme.px(180))
        lay.addWidget(self.edit_prompt)

        tip = QLabel("提示：译文直接来自模型，提示词只是背景说明；"
                     "写中文也能用，但英文描述效果更稳。")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px;"
                          "background:transparent;")
        lay.addWidget(tip)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setMinimumHeight(theme.px(36))
        cancel.setMinimumWidth(theme.px(88))
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = SoftButton("保存", kind="primary")
        ok.setMinimumHeight(theme.px(36))
        ok.setMinimumWidth(theme.px(104))
        ok.clicked.connect(self._save)
        row.addWidget(ok)
        lay.addLayout(row)

    def _lb(self, text):
        lb = QLabel(text)
        lb.setStyleSheet(f"color:{theme.TEXT_MUTED}; font-size:{theme.px(11.5)}px;"
                         "background:transparent;")
        return lb

    def _save(self):
        name = self.edit_name.text().strip()
        if not name:
            return
        builtin = [n for n, _p in BUILTIN_STYLES]
        items = store.tr_custom_styles()
        items = [i for i in items if i["name"] != self._old and i["name"] != name]
        items.append({"name": name, "prompt": self.edit_prompt.toPlainText().strip()})
        store.save_tr_custom_styles(items)
        if self._old and self._old != name:
            if store.tr_get("style") == self._old:
                store.tr_set("style", name)
        if name in builtin:
            # 撞了内置名：内置优先，这里提示一下但也留着（改名后可生效）
            pass
        self.accept()


class DomainDialog(QDialog):
    """新增 / 编辑一个自定义领域：领域描述 + 术语表。"""

    def __init__(self, parent, item=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setMinimumWidth(theme.px(620))
        self.setMinimumHeight(theme.px(560))
        self._old = (item or {}).get("name")

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        shell.addWidget(card)
        shadow(card, 40, QColor(30, 70, 105, 80), 6)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(theme.px(22), theme.px(18), theme.px(22), theme.px(16))
        lay.setSpacing(theme.px(9))

        t = QLabel("自定义翻译领域" if item else "添加翻译领域")
        t.setObjectName("H1")
        lay.addWidget(t)

        lay.addWidget(self._lb("名称"))
        self.edit_name = QLineEdit((item or {}).get("name", ""))
        self.edit_name.setPlaceholderText("比如：半导体、医疗器械注册")
        lay.addWidget(self.edit_name)

        lay.addWidget(self._lb("领域描述（官方要求英文，写成「这是什么领域」的短语）"))
        self.edit_desc = QPlainTextEdit((item or {}).get("domains", ""))
        self.edit_desc.setObjectName("Code")
        self.edit_desc.setPlaceholderText(
            "Semiconductor manufacturing technical writing: standard industry "
            "terminology, process node numbers kept unchanged.")
        self.edit_desc.setMinimumHeight(theme.px(80))
        lay.addWidget(self.edit_desc)

        lay.addWidget(self._lb("术语表（每行一条：原文<Tab>或=译文）"))
        self.edit_terms = QPlainTextEdit()
        self.edit_terms.setObjectName("Code")
        self.edit_terms.setPlaceholderText("光刻胶\tphotoresist\n晶圆 = wafer\n良率\tproduction yield")
        self.edit_terms.setMinimumHeight(theme.px(150))
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPointSizeF(theme.pt(10))
        self.edit_terms.setFont(f)
        lay.addWidget(self.edit_terms)
        for term in (item or {}).get("terms") or []:
            self.edit_terms.appendPlainText("%s\t%s" % (term.get("source", ""),
                                                        term.get("target", "")))

        lay.addWidget(self._hint("提示：术语会算进输入 token，只放这一个领域真正常用的，"
                                 "几十条就够；用不到的术语不会发出去。"))

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setMinimumHeight(theme.px(36))
        cancel.setMinimumWidth(theme.px(88))
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = SoftButton("保存", kind="primary")
        ok.setMinimumHeight(theme.px(36))
        ok.setMinimumWidth(theme.px(104))
        ok.clicked.connect(self._save)
        row.addWidget(ok)
        lay.addLayout(row)

    def _lb(self, text):
        lb = QLabel(text)
        lb.setStyleSheet(f"color:{theme.TEXT_MUTED}; font-size:{theme.px(11.5)}px;"
                         "background:transparent;")
        return lb

    def _hint(self, text):
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setStyleSheet(f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px;"
                         "background:transparent;")
        return lb

    def _save(self):
        name = self.edit_name.text().strip()
        if not name:
            return
        terms = []
        for line in self.edit_terms.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            if "\t" in line:
                s, t = line.split("\t", 1)
            elif "=" in line:
                s, t = line.split("=", 1)
            elif "|" in line:
                s, t = line.split("|", 1)
            else:
                continue
            s, t = s.strip(), t.strip()
            if s:
                terms.append({"source": s, "target": t})
        items = store.tr_custom_domains()
        items = [i for i in items if i["name"] != self._old and i["name"] != name]
        items.append({"name": name, "domains": self.edit_desc.toPlainText().strip(),
                      "terms": terms})
        store.save_tr_custom_domains(items)
        if self._old and self._old != name and store.tr_get("domain") == self._old:
            store.tr_set("domain", name)
        self.accept()
