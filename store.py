# -*- coding: utf-8 -*-
"""数据存取：data/recorded.json（每次覆盖）与 data/presets.json（多条）。

存放位置：始终放在程序所在目录（exe 旁边 / 源码目录）下的 data/，
绝不写进 PyInstaller 的临时解压目录，否则关机就丢。
"""
import json
import os
import sys
import time


def _app_dir():
    """程序所在目录。打包成 onefile exe 时取 exe 所在目录，而不是临时解压目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


ROOT = _app_dir()
DATA_DIR = os.path.join(ROOT, "data")
RECORDED = os.path.join(DATA_DIR, "recorded.json")
PRESETS = os.path.join(DATA_DIR, "presets.json")
SETTINGS = os.path.join(DATA_DIR, "settings.json")


def resource(*parts):
    """只读资源（图标、背景图）的路径。

    onefile 打包后资源被解压到 sys._MEIPASS 临时目录，只读没问题；
    可写数据（上面的 DATA_DIR）才必须放在 exe 旁边。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)




def ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj):
    ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ----------------------------------------------------------- 当前记录

def save_recorded(windows):
    payload = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "windows": [
            {
                "title": w.get("title", ""),
                "active_tab": w.get("active_tab", -1),
                "tabs": [{"path": t["path"], "name": t.get("name", "")} for t in w["tabs"]],
            }
            for w in windows
        ],
    }
    _write(RECORDED, payload)
    return payload


def load_recorded():
    data = _read(RECORDED, None)
    if not data or not data.get("windows"):
        return None
    return data


def clear_recorded():
    """清除记录：写入空结构而不是删除文件。

    删除文件在部分环境下会被安全策略拦截，写空更稳，也便于再次保存时覆盖。
    """
    _write(RECORDED, {"captured_at": "", "windows": []})


# ----------------------------------------------------------- 预设

def load_presets():
    data = _read(PRESETS, None)
    if not data or not isinstance(data.get("presets"), list):
        return {"presets": []}
    return data


def save_presets(data):
    _write(PRESETS, data)


def add_preset(name, windows):
    data = load_presets()
    pid = int(time.time() * 1000)
    data["presets"].append({
        "id": pid,
        "name": name,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "replace": False,          # 应用前是否先关闭现有文件夹（替换模式）
        "windows": [
            {
                "active_tab": w.get("active_tab", -1),
                "tabs": [{"path": t["path"], "name": t.get("name", "")} for t in w["tabs"]],
            }
            for w in windows
        ],
    })
    save_presets(data)
    return pid


def update_preset(pid, name=None, windows=None):
    data = load_presets()
    for p in data["presets"]:
        if p["id"] == pid:
            if name is not None:
                p["name"] = name
            if windows is not None:
                p["windows"] = [
                    {
                        "active_tab": w.get("active_tab", -1),
                        "tabs": [{"path": t["path"], "name": t.get("name", "")} for t in w["tabs"]],
                    }
                    for w in windows
                ]
            save_presets(data)
            return True
    return False


def set_preset_replace(pid, flag):
    """开关某个预设的「替换模式」：应用前先关掉现有文件夹。"""
    data = load_presets()
    for p in data["presets"]:
        if p["id"] == pid:
            p["replace"] = bool(flag)
            save_presets(data)
            return True
    return False


def is_replace(pid):
    p = get_preset(pid)
    return bool(p.get("replace", False)) if p else False


def delete_preset(pid):
    data = load_presets()
    before = len(data["presets"])
    data["presets"] = [p for p in data["presets"] if p["id"] != pid]
    if len(data["presets"]) != before:
        save_presets(data)
        return True
    return False


# ----------------------------------------------------------- 设置

WC_DEFAULT = ["words", "chars", "lines", "paras", "cjk", "cjksym", "en_words", "en_chars"]


def load_settings():
    data = _read(SETTINGS, None)
    if not isinstance(data, dict):
        return {}
    return data


def save_settings(data):
    _write(SETTINGS, data)


# JSON 转 Markdown：正文候选键（按顺序取第一个有内容的）
JM_KEYS = ["markdown", "md", "content", "body", "article", "text"]


def jm_keys():
    data = load_settings()
    j = data.get("jsonmd") or {}
    got = j.get("keys")
    if not isinstance(got, list) or not got:
        return list(JM_KEYS)
    out = [k.strip() for k in got if isinstance(k, str) and k.strip()]
    return out or list(JM_KEYS)


def jm_exclude():
    data = load_settings()
    j = data.get("jsonmd") or {}
    got = j.get("exclude")
    if not isinstance(got, list):
        return []
    return [k.strip() for k in got if isinstance(k, str) and k.strip()]


def save_jm_keys(keys, exclude=None):
    data = load_settings()
    j = data.get("jsonmd") or {}
    j["keys"] = [k.strip() for k in keys if isinstance(k, str) and k.strip()]
    if exclude is not None:
        j["exclude"] = [k.strip() for k in exclude if isinstance(k, str) and k.strip()]
    data["jsonmd"] = j
    save_settings(data)


def wc_fields():
    """字数统计要显示的项（"words" 永远在）。"""
    data = load_settings()
    got = (data.get("wordcount") or {}).get("fields")
    if not isinstance(got, list) or not got:
        return list(WC_DEFAULT)
    out = [k for k in got if isinstance(k, str)]
    if "words" not in out:
        out.insert(0, "words")
    return out


def wc_paras_mode():
    """段落数口径：'enter' 按回车分段（默认） / 'blank' 按空行分段。"""
    data = load_settings()
    mode = (data.get("wordcount") or {}).get("paras_mode")
    return "blank" if mode == "blank" else "enter"


def save_wc_paras_mode(mode):
    data = load_settings()
    wc = data.get("wordcount") or {}
    wc["paras_mode"] = "blank" if mode == "blank" else "enter"
    data["wordcount"] = wc
    save_settings(data)


def save_wc_fields(keys):
    data = load_settings()
    wc = data.get("wordcount") or {}
    wc["fields"] = list(keys)
    data["wordcount"] = wc
    save_settings(data)


def get_preset(pid):
    for p in load_presets()["presets"]:
        if p["id"] == pid:
            return p
    return None


# ----------------------------------------------------------- 翻译（Qwen-MT）

TR_DEFAULTS = {
    "level": "normal",          # fast / normal / high
    "src": "zh",
    "dst": "en",
    "style": "日常交流",
    "domain": "通用",
    "token_used": 0,            # 累计消耗（本地估算，跨会话累加）
}


def _tr():
    data = load_settings()
    d = dict(TR_DEFAULTS)
    got = data.get("translate")
    if isinstance(got, dict):
        # api_key / styles / domains 不在 TR_DEFAULTS 里，必须单独放行，
        # 否则存取一圈下来 Key 就被过滤没了。
        keep = set(TR_DEFAULTS) | {"styles", "domains", "api_key"}
        d.update({k: v for k, v in got.items() if k in keep})
    if not isinstance(d.get("styles"), list):
        d["styles"] = []
    if not isinstance(d.get("domains"), list):
        d["domains"] = []
    return d


def _save_tr(d):
    data = load_settings()
    data["translate"] = d
    save_settings(data)


def tr_get(key, default=None):
    v = _tr().get(key, TR_DEFAULTS.get(key, default))
    if v is None:
        return TR_DEFAULTS.get(key, default)
    return v


def tr_set(key, value):
    d = _tr()
    d[key] = value
    _save_tr(d)


def tr_api_key():
    """API Key 只存在本地 data/settings.json（已在 .gitignore 里），不进代码、不进记忆。"""
    d = _tr()
    return d.get("api_key", "") or ""


def save_tr_api_key(key):
    d = _tr()
    d["api_key"] = (key or "").strip()
    _save_tr(d)


def tr_custom_styles():
    """自定义风格：[{"name": ..., "prompt": ...}]"""
    out = []
    for it in _tr().get("styles") or []:
        if isinstance(it, dict) and (it.get("name") or "").strip():
            out.append({"name": it["name"].strip(), "prompt": it.get("prompt", "")})
    return out


def save_tr_custom_styles(items):
    d = _tr()
    d["styles"] = [{"name": i["name"].strip(), "prompt": i.get("prompt", "")}
                   for i in items if (i.get("name") or "").strip()]
    _save_tr(d)


def tr_custom_domains():
    """自定义领域：[{"name": ..., "domains": ..., "terms": [{"source","target"}]}]"""
    out = []
    for it in _tr().get("domains") or []:
        if isinstance(it, dict) and (it.get("name") or "").strip():
            terms = []
            for t in it.get("terms") or []:
                if isinstance(t, dict) and (t.get("source") or "").strip():
                    terms.append({"source": t["source"].strip(),
                                  "target": (t.get("target") or "").strip()})
            out.append({"name": it["name"].strip(),
                        "domains": it.get("domains", ""), "terms": terms})
    return out


def save_tr_custom_domains(items):
    d = _tr()
    out = []
    for i in items:
        name = (i.get("name") or "").strip()
        if not name:
            continue
        terms = []
        for t in i.get("terms") or []:
            src = (t.get("source") or "").strip()
            if src:
                terms.append({"source": src, "target": (t.get("target") or "").strip()})
        out.append({"name": name, "domains": i.get("domains", ""), "terms": terms})
    d["domains"] = out
    _save_tr(d)


def add_tr_token_used(n):
    """累计用量（只用本地 usage 返回值累加，不发额外请求）。"""
    if not n:
        return
    d = _tr()
    d["token_used"] = int(d.get("token_used", 0) or 0) + int(n)
    _save_tr(d)
