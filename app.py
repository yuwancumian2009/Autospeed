"""
autospeed — 家庭宽带自动测速面板（重写版）

相对原版修复：
  P0-1  节点名恒为"未知"：原版读 CLI 不存在的 sponsor 字段，改读 name/location
  P0-2  联通/电信/移动关键词恒失效：改为在 name+location+country 上匹配，
        并新增"仅国内节点"策略 + 自由关键词
  P0-3  重试静默降级 + 照记数据：新增严格模式，节点不可用即失败告警，
        绝不把"另一个节点的成绩"当成配置节点的成绩入库
  P0-4  死节点不剔除：新增 node_health 表 + 黑名单 + 健康预筛
  P0-5  企业微信 Secret 明文渲染进 HTML：改为只写不读
  P0-6  空 Cron 不生效：改为空值/关闭开关 = 真正暂停任务
  P1    微信失败原因被吞 / 无效图片地址 / 同步阻塞 / sqlite 并发 /
        节点列表无缓存 / 前端选择不还原
  P2    不存 result_url / 无鉴权 / 无健康检查 / 无数据清理
新增：
  * 三种可插拔后端：ookla / http(国内直连) / librespeed
  * 地区守卫：实际节点国家与期望不符时打标 + 告警
  * 异步执行 + 进度查询，前端不再干等
  * 可选的访问令牌鉴权
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests
import urllib3
from flask import Flask, Response, jsonify, render_template, request, send_file

from backends import (BackendError, DEFAULT_HTTP_TARGETS, build_backend,
                      split_url, tcp_latency_ms)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

app = Flask(__name__)

DATA_DIR = "data" if os.path.isdir("data") else "."
DB_FILE = os.path.join(DATA_DIR, "autospeed.db")
CHART_FILE = os.path.join(DATA_DIR, "chart.png")

TS_FMT = "%Y-%m-%d %H:%M:%S"


def log(msg):
    """统一日志输出（容器里走 stdout，docker logs 可见）。"""
    print(f"[{datetime.now().strftime(TS_FMT)}] {msg}", flush=True)

DEFAULT_SETTINGS = {
    # 调度
    "cron": "0 */6 * * *",
    "cron_enabled": "1",
    # 后端与节点
    "backend": "ookla",
    "mode": "closest",            # closest | cn | region | fixed | keyword
    "mode_migrated_from": "",     # 旧策略迁移标记，前端提示后可清除
    "server_id": "",
    "server_keyword": "",
    "server_region": "asia",      # region 模式用：REGION_PRESETS 里的代号
    "expected_country": "CN",
    "strict_mode": "1",
    "max_retries": "1",
    "retry_delay": "10",
    # 国内直连后端
    "http_targets": "\n".join(
        f"{t['name']}|{t['url']}|{t.get('note','')}" for t in DEFAULT_HTTP_TARGETS),
    "http_upload_url": "",
    "http_connections": "8",
    "http_duration": "10",
    # LibreSpeed 后端
    "librespeed_servers": "",
    # 通知
    "wecom_corpid": "", "wecom_secret": "", "wecom_agentid": "",
    "wecom_proxy": "", "external_url": "",
    "notify_on_success": "1", "notify_on_fail": "1",
    "speed_alert_below": "0",
    # 其它
    "tls_insecure": "0",
    "retention_days": "365",
    "auth_token": "",
}

SECRET_KEYS = {"wecom_secret", "auth_token"}
BOOL_KEYS = {"cron_enabled", "strict_mode", "notify_on_success",
             "notify_on_fail", "tls_insecure"}


# ==========================================================================
# 数据库
# ==========================================================================

def db():
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS results
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp TEXT, download REAL, upload REAL, ping REAL,
                  server_name TEXT, server_id TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)""")
    # P3: 时间范围查询走索引（历史表会越来越大）
    c.execute("CREATE INDEX IF NOT EXISTS idx_results_ts ON results(timestamp)")
    c.execute("""CREATE TABLE IF NOT EXISTS node_health
                 (key TEXT PRIMARY KEY, backend TEXT, server_id TEXT,
                  name TEXT, location TEXT, country TEXT,
                  last_ok TEXT, last_fail TEXT, fail_count INTEGER DEFAULT 0,
                  ok_count INTEGER DEFAULT 0, last_latency REAL,
                  last_down REAL, last_error TEXT, blacklist_until TEXT)""")
    # 老库补列
    cols = {r[1] for r in c.execute("PRAGMA table_info(results)")}
    for name, decl in [
        ("backend", "TEXT DEFAULT ''"), ("server_location", "TEXT DEFAULT ''"),
        ("server_country", "TEXT DEFAULT ''"), ("result_url", "TEXT DEFAULT ''"),
        ("warn", "TEXT DEFAULT ''"), ("isp", "TEXT DEFAULT ''"),
        ("duration", "REAL DEFAULT 0"), ("trigger", "TEXT DEFAULT ''"),
    ]:
        if name not in cols:
            c.execute(f"ALTER TABLE results ADD COLUMN {name} {decl}")
    for k, v in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    migrate_legacy_mode(c)
    conn.commit()
    conn.close()


def migrate_legacy_mode(c):
    """旧版 mode 取值（telecom/unicom/mobile）在 Ookla 侧已无法实现。

    改写为 cn 并在 settings 留下标记，让前端显式提示，
    避免像旧版那样静默降级到"就近节点"（实测会落到境外节点）。
    """
    row = c.execute("SELECT value FROM settings WHERE key='mode'").fetchone()
    if row and row[0] in LEGACY_MODES:
        old = row[0]
        c.execute("REPLACE INTO settings (key, value) VALUES ('mode', 'cn')")
        c.execute("REPLACE INTO settings (key, value) VALUES ('mode_migrated_from', ?)",
                  (old,))
        log(f"settings: 旧策略 mode={old} 已失效，自动改为 cn（国内优先）")


def get_setting(key, default=None):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return row[0]


def set_setting(key, value):
    conn = db()
    conn.execute("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def all_settings():
    conn = db()
    d = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    conn.close()
    for k, v in DEFAULT_SETTINGS.items():
        d.setdefault(k, v)
    return d


def public_settings():
    """给前端的设置：密钥类只回报是否已配置，绝不回传明文。"""
    s = all_settings()
    out = {k: v for k, v in s.items() if k not in SECRET_KEYS}
    for k in SECRET_KEYS:
        out[k + "_set"] = bool(s.get(k))
    return out


# ==========================================================================
# 节点健康
# ==========================================================================

def health_key(backend, server_id):
    return f"{backend}|{server_id}"


def mark_ok(res):
    conn = db()
    conn.execute("""REPLACE INTO node_health
        (key, backend, server_id, name, location, country, last_ok,
         ok_count, last_latency, last_down, last_error, blacklist_until)
        VALUES (?,?,?,?,?,?,?, COALESCE((SELECT ok_count FROM node_health WHERE key=?),0)+1,?,?, '', NULL)""",
                 (health_key(res["backend"], res["server_id"]), res["backend"],
                  res["server_id"], res["server_name"], res["server_location"],
                  res["server_country"], datetime.now().strftime(TS_FMT),
                  health_key(res["backend"], res["server_id"]),
                  res.get("ping"), res.get("download")))
    conn.commit()
    conn.close()


def mark_fail(backend, server_id, name, location, country, err, blacklist_minutes=30):
    k = health_key(backend, server_id)
    now = datetime.now()
    until = (now + timedelta(minutes=blacklist_minutes)).strftime(TS_FMT)
    conn = db()
    conn.execute("""REPLACE INTO node_health
        (key, backend, server_id, name, location, country, last_fail, fail_count,
         ok_count, last_error, blacklist_until)
        VALUES (?,?,?,?,?,?,?, COALESCE((SELECT fail_count FROM node_health WHERE key=?),0)+1,
                COALESCE((SELECT ok_count FROM node_health WHERE key=?),0), ?, ?)""",
                 (k, backend, server_id, name, location, country,
                  now.strftime(TS_FMT), k, k, str(err)[:300], until))
    conn.commit()
    conn.close()


def blacklisted_ids(backend):
    now = datetime.now().strftime(TS_FMT)
    conn = db()
    rows = conn.execute("""SELECT server_id FROM node_health
        WHERE backend=? AND blacklist_until IS NOT NULL AND blacklist_until > ?""",
                        (backend, now)).fetchall()
    conn.close()
    return {r[0] for r in rows}


def node_health_rows():
    conn = db()
    rows = conn.execute("SELECT * FROM node_health ORDER BY last_ok DESC").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM node_health LIMIT 1").description] \
        if rows else ["key", "backend", "server_id", "name", "location", "country",
                      "last_ok", "last_fail", "fail_count", "ok_count",
                      "last_latency", "last_down", "last_error", "blacklist_until"]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


# ==========================================================================
# 节点列表（带缓存）
# ==========================================================================

_srv_cache = {"t": 0, "data": [], "err": ""}
_srv_lock = threading.Lock()
SRV_TTL = 600


def ookla_servers(force=False):
    with _srv_lock:
        if not force and _srv_cache["data"] and time.time() - _srv_cache["t"] < SRV_TTL:
            return _srv_cache["data"], _srv_cache["err"]
        try:
            data = build_backend({"backend": "ookla"}).list_servers()
            _srv_cache.update(t=time.time(), data=data, err="")
        except BackendError as e:
            _srv_cache.update(t=time.time(), err=str(e))
            if not _srv_cache["data"]:
                _srv_cache["data"] = []
        return _srv_cache["data"], _srv_cache["err"]


# ==========================================================================
# 节点选择
# ==========================================================================

CN_HINTS = ("china", "中国", "beijing", "shanghai", "guangzhou", "shenzhen",
            "hangzhou", "nanjing", "chengdu", "wuhan", "xian", "tianjin",
            "suzhou", "kunshan", "zhengzhou", "changsha", "qingdao", "hefei")

# 旧版策略名 → 新策略。Ookla 早已不下发运营商字段，旧的 unicom/telecom/mobile
# 在旧代码里恒匹配失败后静默降级到蒙古节点。这里显式迁移到"国内优先"，
# 并在 intent.label 里说明，避免再次静默漂移。
LEGACY_MODES = {"unicom": "中国联通", "telecom": "中国电信", "mobile": "中国移动"}

# Ookla 的 -L -f json 里 country 是**全称**（"China"/"South Korea"），不是 ISO 码。
# 旧代码按 == "CN" 比对，导致国内节点判定恒失败、地区守卫误报。
COUNTRY_ALIASES = {
    "CN": "CN", "CHINA": "CN", "CHN": "CN", "中国": "CN", "中国大陆": "CN", "大陆": "CN",
    "HK": "HK", "HONGKONG": "HK", "HONG KONG": "HK", "香港": "HK",
    "TW": "TW", "TAIWAN": "TW", "台湾": "TW",
    "MO": "MO", "MACAO": "MO", "MACAU": "MO", "澳门": "MO",
    "US": "US", "USA": "US", "UNITED STATES": "US", "美国": "US",
    "KR": "KR", "KOREA": "KR", "SOUTH KOREA": "KR", "韩国": "KR",
    "JP": "JP", "JAPAN": "JP", "日本": "JP",
    "MN": "MN", "MONGOLIA": "MN", "蒙古": "MN",
    "SG": "SG", "SINGAPORE": "SG", "新加坡": "SG",
    "MY": "MY", "MALAYSIA": "MY", "马来西亚": "MY",
    "TH": "TH", "THAILAND": "TH", "泰国": "TH",
    "VN": "VN", "VIETNAM": "VN", "越南": "VN",
    "RU": "RU", "RUSSIA": "RU", "俄罗斯": "RU",
}


def norm_country(v):
    """把各种写法的国家名归一到 ISO 风格短码；未知的原样返回（大写）。"""
    key = (v or "").strip().upper()
    return COUNTRY_ALIASES.get(key, key)


def is_cn_server(s):
    """国内节点判定：国家字段归一后为 CN，或名称/地名命中国内关键词。"""
    if norm_country(s.get("country")) == "CN":
        return True
    blob = ((s.get("name") or "") + " " + (s.get("location") or "")).lower()
    return any(h in blob for h in CN_HINTS)


# 「按地区」模式的预置策略：(代号, 显示名, 归一化国家码集合)。
# 用 norm_country() 归一后的短码匹配，所以 Ookla 下发的 "CHINA"/"South Korea"
# 这类全称也能正确落位，不必让用户自己猜该填什么关键词。
REGION_PRESETS = [
    ("cn",    "中国大陆",  ("CN",)),
    ("hktw",  "港澳台",    ("HK", "TW", "MO")),
    ("jp",    "日本",      ("JP",)),
    ("kr",    "韩国",      ("KR",)),
    ("sg",    "新加坡",    ("SG",)),
    ("mn",    "蒙古",      ("MN",)),
    ("asia",  "亚洲（全部）", ("CN", "HK", "TW", "MO", "JP", "KR", "SG",
                              "MY", "TH", "VN", "PH", "ID", "IN")),
    ("na",    "北美（美/加）", ("US", "CA")),
    ("eu",    "欧洲",      ("GB", "DE", "FR", "NL", "RU", "SE", "CH", "IT",
                            "ES", "PL", "FI", "NO", "DK", "IE", "AT", "BE",
                            "CZ", "RO", "UA", "PT", "GR", "HU")),
    ("oce",   "大洋洲",    ("AU", "NZ")),
]
REGION_MAP = {code: (label, set(codes)) for code, label, codes in REGION_PRESETS}


def fastest_by_latency(cands):
    """在候选节点里挑 TCP 延迟最低的；全不可达时退回第一个（可能为 None）。"""
    best, best_lat = None, None
    for s in cands:
        lat = tcp_latency_ms(s["host"], s["port"], timeout=2.5) if s["host"] else None
        if lat is None:
            continue
        if best_lat is None or lat < best_lat:
            best, best_lat = s, lat
    return best if best is not None else (cands[0] if cands else None)


def select_target(backend, cfg):
    """
    返回 (server_arg, intent) —— server_arg 传给 backend.run()，intent 记录"本意"。
    intent = {"mode","server_id","label"}，用于事后比对是否发生了漂移。
    """
    mode = (cfg.get("mode") or "closest").strip()
    legacy_note = ""
    if mode in LEGACY_MODES:
        legacy_note = (f"旧策略「{LEGACY_MODES[mode]}」已失效"
                       f"（Ookla 不再下发运营商字段），已改为国内优先")
        mode = "cn"
    strict = str(cfg.get("strict_mode")) in ("1", "true", "True")

    def tag(d):
        if legacy_note:
            d["label"] = f"{legacy_note}；{d['label']}"
        return d

    if backend.name != "ookla":
        intent = {"mode": mode, "server_id": "", "label": "自动选择最优目标"}
        if backend.name == "http":
            # HttpBackend.run 接受目标名
            if mode == "fixed" and cfg.get("server_id"):
                return cfg["server_id"], tag({"mode": "fixed",
                                              "server_id": cfg["server_id"],
                                              "label": f"指定目标 {cfg['server_id']}"})
            return "", tag(intent)
        if backend.name == "librespeed":
            if mode == "fixed" and cfg.get("server_id"):
                return cfg["server_id"], tag({"mode": "fixed",
                                              "server_id": cfg["server_id"],
                                              "label": f"指定服务器 {cfg['server_id']}"})
            return "", tag(intent)

    # ---- Ookla ----
    if mode == "closest":
        return None, tag({"mode": mode, "server_id": "", "label": "就近节点（不指定）"})


    servers, err = ookla_servers()
    if not servers:
        msg = "无法获取节点列表" + (f"：{err}" if err else "")
        if strict:
            raise BackendError(msg + "（严格模式：不降级）")
        return None, {"mode": mode, "server_id": "", "label": msg + "，已降级为就近节点"}

    bl = blacklisted_ids("ookla")

    if mode == "fixed":
        sid = str(cfg.get("server_id") or "").strip()
        if not sid:
            if strict:
                raise BackendError("指定节点模式但未填写节点 ID（严格模式：不降级）")
            return None, {"mode": mode, "server_id": "", "label": "未填节点 ID，已降级"}
        hit = next((s for s in servers if s["id"] == sid), None)
        if hit is None:
            msg = (f"节点 {sid} 不在当前可用池中（Ookla 只允许池内节点）。"
                   f"当前池 {len(servers)} 个，国内节点 "
                   f"{sum(1 for s in servers if s['country']=='CN')} 个")
            if strict:
                raise BackendError(msg + "（严格模式：不降级）")
            return None, {"mode": mode, "server_id": sid, "label": msg + "，已降级为就近节点"}
        return sid, tag({"mode": mode, "server_id": sid,
                         "label": f"指定 {hit['name']} ({hit['location']})"})

    if mode == "cn":
        cands = [s for s in servers if is_cn_server(s) and s["id"] not in bl]
        if not cands:
            msg = (f"当前节点池里没有可用的国内节点（共 {len(servers)} 个，来自："
                   + "、".join(sorted({norm_country(s.get("country")) or "??" for s in servers}))
                   + "）")
            if strict:
                raise BackendError(msg + "（严格模式：不降级）")
            return None, {"mode": mode, "server_id": "", "label": msg + "，已降级"}
        best = fastest_by_latency(cands)
        return best["id"], tag({"mode": mode, "server_id": best["id"],
                                "label": f"国内最优 {best['name']} ({best['location']})"})

    if mode == "region":
        code = (cfg.get("server_region") or "").strip().lower()
        preset = REGION_MAP.get(code)
        if preset is None:
            msg = f"未选择有效地区「{code or '(空)'}」"
            if strict:
                raise BackendError(msg + "（严格模式：不降级）")
            return None, tag({"mode": mode, "server_id": "",
                              "label": msg + "，已降级为就近节点"})
        rlabel, codes = preset
        cands = [s for s in servers
                 if norm_country(s.get("country")) in codes and s["id"] not in bl]
        if not cands:
            have = "、".join(sorted({norm_country(s.get("country")) or "??" for s in servers}))
            msg = f"当前节点池里没有「{rlabel}」的可用节点（池内地区：{have}）"
            if strict:
                raise BackendError(msg + "（严格模式：不降级）")
            return None, tag({"mode": mode, "server_id": "",
                              "label": msg + "，已降级为就近节点"})
        best = fastest_by_latency(cands)
        return best["id"], tag({"mode": mode, "server_id": best["id"],
                                "label": f"{rlabel}最优 {best['name']} ({best['location']})"})

    if mode == "keyword":
        kw = (cfg.get("server_keyword") or "").strip().lower()
        if not kw:
            if strict:
                raise BackendError("关键词模式但未填写关键词（严格模式：不降级）")
            return None, {"mode": mode, "server_id": "", "label": "未填关键词，已降级"}
        hit = next((s for s in servers
                    if kw in (s["name"] + " " + s["location"] + " " + s["country"]).lower()
                    and s["id"] not in bl), None)
        if hit is None:
            msg = f"节点池中没有匹配「{kw}」的可用节点"
            if strict:
                raise BackendError(msg + "（严格模式：不降级）")
            return None, {"mode": mode, "server_id": "", "label": msg + "，已降级"}
        return hit["id"], tag({"mode": mode, "server_id": hit["id"],
                               "label": f"关键词命中 {hit['name']} ({hit['location']})"})

    # 兜底：不认识的策略也明确说出来，不静默伪装成正常就近
    return None, tag({"mode": mode, "server_id": "",
                      "label": f"未知策略「{mode}」，已回退就近节点"})


def evaluate_warning(res, cfg, intent):
    """地区守卫 + 漂移检测。返回告警文案（空串表示正常）。"""
    warns = []
    exp = norm_country(cfg.get("expected_country"))
    cc = norm_country(res.get("server_country"))
    if exp and cc and cc != exp:
        warns.append(f"节点地区不符：期望 {exp}，实际 {cc}")
    if exp and not cc:
        warns.append(f"节点地区未知（期望 {exp}）")
    if intent.get("mode") == "fixed" and intent.get("server_id") \
            and str(res.get("server_id")) != str(intent["server_id"]):
        warns.append(f"实际节点与指定节点不一致（{intent['server_id']} → {res.get('server_id')}）")
    thr = float(cfg.get("speed_alert_below") or 0)
    if thr > 0 and (res.get("download") or 0) < thr:
        warns.append(f"下行 {res.get('download')} Mbps 低于阈值 {thr:g} Mbps")
    # 上行缺失必须显式暴露：静默记 None 会让人以为测过了
    if res.get("upload") is None:
        if res.get("backend") == "http":
            warns.append("上行未测（国内镜像不接受上传，Ookla 兜底亦失败）")
        else:
            warns.append("上行未测（后端未返回上行数据）")
    return "；".join(warns)


# ==========================================================================
# 图表
# ==========================================================================

_chart_lock = threading.Lock()


def generate_chart(days=7):
    if days == "all" or days is None:
        sql, args = "SELECT timestamp, download, upload, ping FROM results ORDER BY timestamp ASC", ()
        title = "Network Speed Trend (All)"
    else:
        thr = (datetime.now() - timedelta(days=int(days))).strftime(TS_FMT)
        sql = ("SELECT timestamp, download, upload, ping FROM results "
               "WHERE timestamp >= ? ORDER BY timestamp ASC")
        args = (thr,)
        title = f"Network Speed Trend (Last {days} Days)"
    conn = db()
    rows = conn.execute(sql, args).fetchall()
    conn.close()

    with _chart_lock:
        fig, ax1 = plt.subplots(figsize=(10, 5))
        if not rows:
            ax1.text(0.5, 0.5, "Waiting for data...", ha="center", va="center",
                     fontsize=18, color="gray")
            ax1.axis("off")
        else:
            ts = [datetime.strptime(r[0], TS_FMT) for r in rows]
            ax1.set_title(title)
            ax1.set_xlabel("Time")
            ax1.set_ylabel("Speed (Mbps)")
            ax1.plot(ts, [r[1] for r in rows], color="green", label="Download", linewidth=2)
            ax1.plot(ts, [r[2] if r[2] is not None else float("nan") for r in rows],
                     color="blue", label="Upload", linewidth=2)
            ax1.grid(True, linestyle="--", alpha=0.5)
            ax2 = ax1.twinx()
            ax2.set_ylabel("Latency (ms)")
            ax2.plot(ts, [r[3] for r in rows], color="orange", label="Latency",
                     linestyle="--")
            l1, la1 = ax1.get_legend_handles_labels()
            l2, la2 = ax2.get_legend_handles_labels()
            ax1.legend(l1 + l2, la1 + la2, loc="upper left")
            ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
            fig.autofmt_xdate()
        plt.savefig(CHART_FILE, format="png", bbox_inches="tight")
        plt.close(fig)


# ==========================================================================
# 企业微信
# ==========================================================================

def send_wecom(title, body, with_chart=False, timeout=15):
    """返回 (ok, detail)。detail 是面向用户的原因说明。"""
    cfg = all_settings()
    corpid, secret, agentid = cfg.get("wecom_corpid"), cfg.get("wecom_secret"), cfg.get("wecom_agentid")
    proxy = (cfg.get("wecom_proxy") or "").strip()
    external_url = (cfg.get("external_url") or "").strip()
    insecure = str(cfg.get("tls_insecure")) in ("1", "true", "True")
    if not (corpid and secret and agentid):
        return False, "未配置企业微信（corpid/secret/agentid 必填）"
    try:
        int(agentid)
    except ValueError:
        return False, f"agentid 必须是数字，当前为「{agentid}」"

    if external_url and not external_url.startswith(("http://", "https://")):
        external_url = "http://" + external_url
    base = proxy.rstrip("/") if proxy else "https://qyapi.weixin.qq.com"

    try:
        r = requests.get(f"{base}/cgi-bin/gettoken",
                         params={"corpid": corpid, "corpsecret": secret},
                         timeout=timeout, verify=not insecure)
    except requests.RequestException as e:
        return False, f"获取 access_token 网络失败：{e}"
    try:
        d = r.json()
    except ValueError:
        return False, f"获取 access_token 返回非 JSON（HTTP {r.status_code}）：{r.text[:120]}"
    if d.get("errcode") != 0:
        return False, f"获取 access_token 失败：errcode={d.get('errcode')} {d.get('errmsg')}"
    token = d.get("access_token")

    article = {"title": title, "description": body, "url": external_url}
    # 只有外部地址可访问时才带图片，避免把无效地址发给微信
    if with_chart and external_url and os.path.exists(CHART_FILE):
        article["picurl"] = f"{external_url.rstrip('/')}/chart.png?t={int(time.time())}"

    payload = {"touser": "@all", "msgtype": "news", "agentid": int(agentid),
               "news": {"articles": [article]}, "safe": 0}
    try:
        res = requests.post(f"{base}/cgi-bin/message/send",
                            params={"access_token": token}, json=payload,
                            timeout=timeout, verify=not insecure)
    except requests.RequestException as e:
        return False, f"发送消息网络失败：{e}"
    try:
        rd = res.json()
    except ValueError:
        return False, f"发送返回非 JSON（HTTP {res.status_code}）：{res.text[:120]}"
    if rd.get("errcode") == 0:
        return True, "发送成功"
    return False, f"发送失败：errcode={rd.get('errcode')} {rd.get('errmsg')}"


# ==========================================================================
# 测速主流程
# ==========================================================================

def save_result(res, trigger, warn):
    conn = db()
    conn.execute("""INSERT INTO results
        (timestamp, download, upload, ping, server_name, server_id,
         backend, server_location, server_country, result_url, warn, isp,
         duration, trigger)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (res.get("timestamp") or datetime.now().strftime(TS_FMT),
                  res["download"], res["upload"],
                  res["ping"], f"{res['server_name']} - {res['server_location']}",
                  res["server_id"], res["backend"], res["server_location"],
                  res["server_country"], res.get("result_url") or "", warn,
                  res.get("isp") or "", res.get("duration") or 0, trigger))
    conn.commit()
    conn.close()


def run_speedtest(trigger="manual", progress=None):
    """执行一次测速。返回 (res | None, detail_str)。"""
    def p(msg):
        if progress:
            progress(msg)
        log(msg)

    cfg = all_settings()
    backend = build_backend(cfg)
    attempts = max(1, int(cfg.get("max_retries") or 0) + 1)
    delay = max(0, int(cfg.get("retry_delay") or 0))

    try:
        target, intent = select_target(backend, cfg)
    except BackendError as e:
        msg = f"❌ 测速未执行\n\n后端：{backend.label}\n⚠️ 原因：{e}"
        p(str(e))
        if str(cfg.get("notify_on_fail")) in ("1", "true", "True"):
            ok, detail = send_wecom("网络测速失败", msg, with_chart=False)
            p(f"失败告警发送：{detail}")
        return None, str(e)

    p(f"后端={backend.label} 目标={target or '自动'} 意图={intent['label']}")

    last_err = ""
    res = None
    for i in range(attempts):
        t0 = time.perf_counter()
        try:
            res = backend.run(target)
            res["duration"] = round(time.perf_counter() - t0, 2)
            break
        except BackendError as e:
            last_err = str(e)
            p(f"第 {i+1}/{attempts} 次失败：{last_err}")
            if backend.name == "ookla" and target:
                s = next((x for x in ookla_servers()[0] if x["id"] == str(target)), None)
                mark_fail("ookla", str(target), (s or {}).get("name", ""),
                          (s or {}).get("location", ""), (s or {}).get("country", ""),
                          last_err)
            if i < attempts - 1 and delay:
                time.sleep(delay)

    if res is None:
        msg = (f"❌ 测速失败（已重试 {attempts - 1} 次）\n\n后端：{backend.label}\n"
               f"目标：{intent['label']}\n⚠️ 原因：{last_err}")
        if str(cfg.get("notify_on_fail")) in ("1", "true", "True"):
            ok, detail = send_wecom("网络测速失败", msg, with_chart=False)
            p(f"失败告警发送：{detail}")
        return None, last_err

    # 统一时间戳：任务返回值、DB 记录、节点健康三处保持一致
    res.setdefault("timestamp", datetime.now().strftime(TS_FMT))
    res["timestamp"] = res.get("timestamp") or datetime.now().strftime(TS_FMT)
    warn = evaluate_warning(res, cfg, intent)
    mark_ok(res)
    save_result(res, trigger, warn)

    if str(cfg.get("notify_on_success")) in ("1", "true", "True"):
        try:
            generate_chart(7)
            has_chart = True
        except Exception as e:  # noqa: BLE001
            p(f"生成图表失败：{e}")
            has_chart = False
        lines = [f"⬇️ 下行：{res['download']} Mbps"]
        if res.get("upload") is not None:
            lines.append(f"⬆️ 上行：{res['upload']} Mbps")
        lines.append(f"📶 延迟：{res['ping']} ms")
        lines.append(f"🖥 后端：{backend.label}")
        lines.append(f"📍 节点：{res['server_name']} - {res['server_location']}"
                     + (f" [{res['server_country']}]" if res.get("server_country") else ""))
        if warn:
            lines.append(f"⚠️ {warn}")
        if res.get("result_url"):
            lines.append(f"🔗 {res['result_url']}")
        ok, detail = send_wecom("私人网络测速结果报告", "\n".join(lines), with_chart=has_chart)
        p(f"结果推送：{detail}")

    summary = (f"下行 {res['download']} Mbps / 上行 "
               f"{res['upload'] if res.get('upload') is not None else 'N/A'} Mbps / "
               f"延迟 {res['ping']} ms @ {res['server_name']}")
    return res, (warn or summary)


# ==========================================================================
# 异步任务
# ==========================================================================

JOBS = {}
# RLock：start_job 持锁期间会调用 new_job，非重入 Lock 会自锁死
JOB_LOCK = threading.RLock()
CURRENT = {"running": False, "id": None}


def _new_job_locked():
    """调用方必须已持有 JOB_LOCK。"""
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"id": jid, "state": "running", "log": [], "result": None,
                 "detail": "", "started": datetime.now().strftime(TS_FMT)}
    # 只保留最近 20 条
    if len(JOBS) > 20:
        for k in sorted(JOBS, key=lambda x: JOBS[x]["started"])[:-20]:
            JOBS.pop(k, None)
    return jid


def _job_worker(jid, trigger):
    def progress(msg):
        with JOB_LOCK:
            j = JOBS.get(jid)
            if j:
                j["log"].append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
    try:
        res, detail = run_speedtest(trigger, progress)
        with JOB_LOCK:
            JOBS[jid].update(state="done" if res else "error",
                             result=res, detail=detail)
    except Exception as e:  # noqa: BLE001
        with JOB_LOCK:
            JOBS[jid].update(state="error", detail=f"内部异常：{e}")
    finally:
        with JOB_LOCK:
            # 保留 id：前端刷新页面后仍能查到最近一次结果
            CURRENT["running"] = False


def start_job(trigger="manual"):
    with JOB_LOCK:
        if CURRENT["running"]:
            return None, "已有测速任务在执行中"
        jid = _new_job_locked()
        CURRENT["running"] = True
        CURRENT["id"] = jid   # 仅在此处替换，完成后保留供查询
    threading.Thread(target=_job_worker, args=(jid, trigger), daemon=True).start()
    return jid, ""


def scheduled_job():
    start_job("scheduled")


# ==========================================================================
# 调度器
# ==========================================================================

from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402

scheduler = BackgroundScheduler()
JOB_ID = "speedtest_job"


def apply_schedule():
    """把设置里的 cron 真正应用到调度器。返回状态文案。"""
    cfg = all_settings()
    enabled = str(cfg.get("cron_enabled")) in ("1", "true", "True")
    expr = (cfg.get("cron") or "").strip()
    job = scheduler.get_job(JOB_ID)

    if not enabled or not expr:
        if job:
            scheduler.remove_job(JOB_ID)
        return "已暂停（未启用或 Cron 为空）"
    try:
        trig = CronTrigger.from_crontab(expr)
    except Exception as e:  # noqa: BLE001
        return f"Cron 表达式无效，任务未变更：{e}"
    try:
        if job:
            scheduler.reschedule_job(JOB_ID, trigger=trig)   # 不重建，避免重置执行时间
        else:
            scheduler.add_job(scheduled_job, trig, id=JOB_ID,
                              max_instances=1, coalesce=True, misfire_grace_time=300)
    except Exception as e:  # noqa: BLE001
        return f"应用调度失败：{e}"
    return "已启用"


def next_run_text():
    job = scheduler.get_job(JOB_ID)
    if not job or not job.next_run_time:
        return "未调度"
    return job.next_run_time.strftime(TS_FMT)


# ==========================================================================
# 鉴权
# ==========================================================================

def _token_ok():
    tok = (all_settings().get("auth_token") or "").strip()
    if not tok:
        return True
    supplied = (request.headers.get("X-Auth-Token")
                or request.args.get("token")
                or request.cookies.get("as_token") or "")
    return supplied == tok


@app.before_request
def _guard():
    if request.path in ("/healthz",):
        return None
    if _token_ok():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"status": "error", "message": "未授权：请提供访问令牌"}), 401
    return Response("未授权：请在 URL 后加 ?token=你的令牌", 401, mimetype="text/plain")


@app.after_request
def _remember_token(resp):
    """带 ?token= 访问过之后写 cookie，后续 API 调用不用再带。"""
    tok = request.args.get("token")
    if tok and (all_settings().get("auth_token") or "").strip() == tok:
        resp.set_cookie("as_token", tok, max_age=90 * 24 * 3600,
                        httponly=False, samesite="Lax")
    return resp


# ==========================================================================
# 路由
# ==========================================================================

PAGE_SIZE = 10


def boot_payload():
    conn = db()
    rows = conn.execute("SELECT * FROM results ORDER BY id DESC LIMIT ?",
                        (PAGE_SIZE,)).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM results LIMIT 1").description]
    total = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    conn.close()
    return {
        "settings": public_settings(),
        "results": [dict(zip(cols, r)) for r in rows],
        "total": total,
        "next_run": next_run_text(),
        "sched_state": apply_state_text(),
    }


@app.route("/")
def index():
    return render_template("index.html", boot=boot_payload())


@app.route("/api/boot")
def api_boot():
    return jsonify(boot_payload())


def apply_state_text():
    return "运行中" if scheduler.get_job(JOB_ID) else "已暂停"


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "time": datetime.now().strftime(TS_FMT)})


@app.route("/api/servers")
def api_servers():
    force = request.args.get("refresh") == "1"
    servers, err = ookla_servers(force=force)
    bl = blacklisted_ids("ookla")
    out = [{"id": s["id"],
            "display": f"[{s['country'] or '??'}] {s['name']} - {s['location']}",
            "country": s["country"], "norm_country": norm_country(s.get("country")),
            "blacklisted": s["id"] in bl,
            "host": s["host"], "port": s["port"]} for s in servers]
    # 每个预置地区在当前池里有多少可用（未拉黑）节点，供前端下拉直接标注
    avail = [s for s in servers if s["id"] not in bl]
    regions = [{"code": code, "label": label,
                "count": sum(1 for s in avail
                             if norm_country(s.get("country")) in codes)}
               for code, label, codes in REGION_PRESETS]
    return jsonify({"servers": out, "error": err,
                    "count": len(out),
                    "regions": regions,
                    # 注意：Ookla 下发的是 "CHINA" 全称，必须走 is_cn_server 归一；
                    # 直接比对 == "CN" 会恒为 0（老版本就踩了这个坑）。
                    "cn_count": sum(1 for s in servers if is_cn_server(s))})


@app.route("/api/backends")
def api_backends():
    cfg = all_settings()
    cur = (cfg.get("backend") or "ookla").lower()
    return jsonify([
        {"id": "ookla", "label": "Ookla", "current": cur == "ookla",
         "desc": "官方 CLI，下行+上行+延迟三项齐全（推荐）；节点池由 Ookla 下发，"
                 "国内节点常缺失，会自动跳过故障节点"},
        {"id": "http", "label": "国内直连", "current": cur == "http",
         "desc": "下行走国内镜像（贴近日常体验）；上行由 Ookla 兜底补测"
                 "（国内无公开上传端点，也可自行配置上传地址）"},
        {"id": "librespeed", "label": "LibreSpeed", "current": cur == "librespeed",
         "desc": "自建或可用的 LibreSpeed 实例，三项齐全"},
    ])


@app.route("/api/node_health")
def api_node_health():
    return jsonify(node_health_rows())


@app.route("/api/history")
def api_history():
    tf = request.args.get("timeframe", "7")
    conn = db()
    if tf == "all":
        rows = conn.execute("SELECT timestamp, download, upload, ping FROM results "
                            "ORDER BY timestamp ASC").fetchall()
    else:
        try:
            days = int(tf)
        except ValueError:
            days = 7
        thr = (datetime.now() - timedelta(days=days)).strftime(TS_FMT)
        rows = conn.execute("SELECT timestamp, download, upload, ping FROM results "
                            "WHERE timestamp >= ? ORDER BY timestamp ASC", (thr,)).fetchall()
    conn.close()
    return jsonify({"timestamps": [r[0] for r in rows],
                    "downloads": [r[1] for r in rows],
                    "uploads": [r[2] for r in rows],
                    "pings": [r[3] for r in rows]})


@app.route("/api/results")
def api_results():
    """服务端分页：返回 {rows, total}，前端翻页不再受"只加载最近 N 条"限制。"""
    try:
        limit = int(request.args.get("limit", PAGE_SIZE))
    except ValueError:
        limit = PAGE_SIZE
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0
    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    rows = conn.execute("SELECT * FROM results ORDER BY id DESC LIMIT ? OFFSET ?",
                        (limit, offset)).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM results LIMIT 1").description]
    conn.close()
    return jsonify({"rows": [dict(zip(cols, r)) for r in rows], "total": total})


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(silent=True) or {}
    # 允许临时覆盖节点选择
    for k in ("mode", "server_id", "server_keyword", "server_region",
              "backend", "strict_mode"):
        if k in data and data[k] != "":
            set_setting(k, data[k])
    jid, err = start_job("manual")
    if not jid:
        return jsonify({"status": "error", "message": err}), 409
    return jsonify({"status": "success", "job": jid})


@app.route("/api/job/<jid>")
def api_job(jid):
    with JOB_LOCK:
        j = JOBS.get(jid)
        if not j:
            return jsonify({"status": "error", "message": "任务不存在"}), 404
        return jsonify(j)


@app.route("/api/job")
def api_job_current():
    """最近一次任务（含已完成），并附带是否仍在执行。"""
    with JOB_LOCK:
        cur = CURRENT["id"]
        if not cur or cur not in JOBS:
            return jsonify({"state": "idle", "running": False})
        return jsonify(dict(JOBS[cur], running=CURRENT["running"]))


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json(silent=True) or {}
    # 先校验 Cron，非法则整体拒绝 —— 旧版会把错误表达式存库、却让旧任务继续跑，
    # 界面还提示"保存成功"，用户以为改了其实没改。
    if "cron" in data:
        expr = (data.get("cron") or "").strip()
        if expr:
            try:
                CronTrigger.from_crontab(expr)
            except Exception as e:  # noqa: BLE001
                return jsonify({"status": "error",
                                "message": f"Cron 表达式无效，未保存任何改动：{e}"}), 400
    for k, v in data.items():
        if k in DEFAULT_SETTINGS:
            if k in SECRET_KEYS and (v is None or str(v).strip() == ""):
                continue          # 留空 = 不改动已保存的密钥
            set_setting(k, "" if v is None else str(v))
    state = apply_schedule()
    return jsonify({"status": "success", "schedule": state, "next_run": next_run_text()})


@app.route("/api/test_wechat", methods=["POST"])
def api_test_wechat():
    data = request.get_json(silent=True) or {}
    for k, v in data.items():
        if k in DEFAULT_SETTINGS:
            if k in SECRET_KEYS and (v is None or str(v).strip() == ""):
                continue
            set_setting(k, "" if v is None else str(v))
    try:
        generate_chart(7)
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "message": f"生成图表失败：{e}"})
    ok, detail = send_wecom("测速面板测试消息",
                            "🔔 这是一条测试消息。能收到即代表企业微信配置正确。",
                            with_chart=True)
    return jsonify({"status": "success" if ok else "error", "message": detail})


@app.route("/api/selftest", methods=["POST"])
def api_selftest():
    """不动数据库，只验证当前配置能不能选到节点、能不能连通。"""
    cfg = all_settings()
    out = {"backend": cfg.get("backend"), "steps": []}
    try:
        backend = build_backend(cfg)
        target, intent = select_target(backend, cfg)
        out["steps"].append({"ok": True, "msg": f"节点选择：{intent['label']}"})
        if backend.name == "ookla":
            servers, err = ookla_servers()
            out["pool"] = {"count": len(servers),
                           "cn": sum(1 for s in servers if is_cn_server(s)),
                           "countries": sorted({norm_country(s.get("country")) or "??"
                                                for s in servers})}
            if err:
                out["steps"].append({"ok": False, "msg": f"节点列表：{err}"})
        elif backend.name == "http":
            t, lat = backend.pick_target()
            out["steps"].append({"ok": True,
                                 "msg": f"目标连通：{t['name']} TCP {lat} ms"})
    except BackendError as e:
        out["steps"].append({"ok": False, "msg": str(e)})
    return jsonify(out)


@app.route("/api/prune", methods=["POST"])
def api_prune():
    days = int(all_settings().get("retention_days") or 0)
    if days <= 0:
        return jsonify({"status": "success", "message": "保留天数为 0，未清理"})
    thr = (datetime.now() - timedelta(days=days)).strftime(TS_FMT)
    conn = db()
    n = conn.execute("DELETE FROM results WHERE timestamp < ?", (thr,)).rowcount
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": f"已清理 {n} 条（早于 {thr}）"})


@app.route("/api/export.csv")
def api_export():
    conn = db()
    rows = conn.execute("SELECT * FROM results ORDER BY id DESC").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM results LIMIT 1").description]
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=autospeed.csv"})


@app.route("/chart.png")
def serve_chart():
    if os.path.exists(CHART_FILE):
        return send_file(CHART_FILE, mimetype="image/png")
    return "No chart yet", 404


init_db()
if __name__ == "__main__":
    apply_schedule()
    scheduler.start()
    app.run(host="0.0.0.0", port=5000, threaded=True)
else:
    # gunicorn 等场景下也要起调度
    apply_schedule()
    scheduler.start()
