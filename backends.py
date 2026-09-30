"""
autospeed 测速后端。

三种后端统一返回同一形状的 dict：
    {
      "backend": "ookla" | "http" | "librespeed",
      "download": float,          # Mbps
      "upload": float | None,     # Mbps, None = 该后端无法测量
      "ping": float,              # ms
      "server_id": str,
      "server_name": str,
      "server_location": str,
      "server_country": str,      # ISO 国家码, 可能为空
      "server_host": str,
      "isp": str | None,
      "packet_loss": float | None,
      "result_url": str | None,
      "extra": dict,
    }

设计要点：
  * 每个后端失败时抛 BackendError，由调用方决定"严格模式"还是"降级"，
    绝不在这里偷偷换节点。
  * 所有网络测量都带超时上限，不会挂死。
"""
from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import random
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 自签/代理场景允许放开校验，但默认严格校验
_SSL_CTX = ssl.create_default_context()
_SSL_INSECURE = ssl.create_default_context()
_SSL_INSECURE.check_hostname = False
_SSL_INSECURE.verify_mode = ssl.CERT_NONE


class BackendError(Exception):
    """测速失败，message 面向用户可读。"""


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------

def _ctx(insecure: bool):
    return _SSL_INSECURE if insecure else _SSL_CTX


def split_url(url: str):
    p = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    https = p.scheme != "http"
    host = p.hostname or ""
    port = p.port or (443 if https else 80)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    return https, host, port, path


def _connect(https: bool, host: str, port: int, timeout: float, insecure: bool = False):
    if https:
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=_ctx(insecure))
    return http.client.HTTPConnection(host, port, timeout=timeout)


def tcp_latency_ms(host: str, port: int, timeout: float = 3.0, samples: int = 3):
    """纯 TCP 建连延迟，用于节点健康预筛。"""
    ok, best = 0, None
    for _ in range(samples):
        t0 = time.perf_counter()
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            dt = (time.perf_counter() - t0) * 1000
            ok += 1
            best = dt if best is None else min(best, dt)
        except OSError:
            pass
    if not ok:
        return None
    return round(best, 1)


# --------------------------------------------------------------------------
# 后端 A：Ookla speedtest CLI
# --------------------------------------------------------------------------

class OoklaBackend:
    name = "ookla"
    label = "Ookla"

    def __init__(self, binary: str = "speedtest", insecure: bool = False):
        self.binary = binary
        self.insecure = insecure

    # -- 内部 ------------------------------------------------------------
    def _base_cmd(self):
        # --accept-license/--accept-gdpr 必须带：容器重建后 ~/.config/ookla
        # 会丢失，不带这两个参数 -L 会直接失败（原版 BUG）
        return [self.binary, "--accept-license", "--accept-gdpr"]

    @staticmethod
    def _parse_error(stderr: str) -> str:
        msgs = []
        for line in (stderr or "").strip().splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("level") == "error":
                m = d.get("message", "")
                if m:
                    msgs.append(m)
        if msgs:
            return " | ".join(dict.fromkeys(msgs))
        tail = (stderr or "").strip().splitlines()
        return tail[-1] if tail else "无输出"

    def list_servers(self, timeout: float = 30.0):
        """返回 [{id, name, location, country, host, port, latency}]。"""
        cmd = self._base_cmd() + ["-L", "-f", "json"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BackendError(f"获取节点列表超时（>{timeout:.0f}s）")
        except FileNotFoundError:
            raise BackendError("容器内找不到 speedtest 可执行文件")
        if r.returncode != 0:
            raise BackendError("获取节点列表失败：" + self._parse_error(r.stderr))
        try:
            data = json.loads(r.stdout)
        except ValueError:
            raise BackendError("节点列表返回的不是合法 JSON（可能是 license 提示）")
        out = []
        for s in data.get("servers", []):
            out.append({
                "id": str(s.get("id")),
                "name": s.get("name") or "未知节点",
                # 原版把 name 和 sponsor 拼一起，而 CLI 根本没有 sponsor 字段
                "location": s.get("location") or "未知地区",
                "country": (s.get("country") or "").upper(),
                "host": s.get("host") or "",
                "port": int(s.get("port") or 8080),
                "latency": s.get("latency"),
            })
        return out

    def run(self, server_id=None, timeout: float = 180.0):
        cmd = self._base_cmd() + ["-f", "json"]
        if server_id:
            cmd += ["-s", str(server_id)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BackendError(f"测速超时（>{timeout:.0f}s）")
        if r.returncode != 0:
            raise BackendError(self._parse_error(r.stderr))
        try:
            data = json.loads(r.stdout)
        except ValueError:
            raise BackendError("测速返回的不是合法 JSON")

        srv = data.get("server") or {}
        res = data.get("result") or {}
        return {
            "backend": self.name,
            "download": round(float(data["download"]["bandwidth"]) * 8 / 1e6, 2),
            "upload": round(float(data["upload"]["bandwidth"]) * 8 / 1e6, 2),
            "ping": round(float(data["ping"]["latency"]), 2),
            "server_id": str(srv.get("id") or ""),
            # 原版读 srv['sponsor'] → 恒为"未知"
            "server_name": srv.get("name") or "未知节点",
            "server_location": srv.get("location") or "未知地区",
            "server_country": (srv.get("country") or "").upper(),
            "server_host": srv.get("host") or "",
            "isp": data.get("isp"),
            "packet_loss": data.get("packetLoss"),
            "result_url": res.get("url"),
            "extra": {"jitter": (data.get("ping") or {}).get("jitter"),
                      "interface": (data.get("interface") or {}).get("name"),
                      "externalIp": (data.get("interface") or {}).get("externalIp")},
        }


# --------------------------------------------------------------------------
# 后端 B：国内直连 HTTP 测速（镜像站多目标）
# --------------------------------------------------------------------------
# 国内 LibreSpeed 公共实例已大面积失效（含中科大实例，后端 500）、测速网接口
# 加密，因此国内模式改为直连国内镜像/CDN 测下行。
#
# 上行：实测国内已无可用公开上传端点，而国外 HTTP 端点在本网络下仅 1–3 Mbps
# （路由差，严重低报，Ookla 对同区域节点却可达 68 Mbps）。故上行交给 Ookla CLI
# （多流并发 + 运营商优质互联），下行仍走国内镜像 —— 即"国内下行 + Ookla 上行"
# 混合模式，保证国内直连也有真实可用的上行数值。

DEFAULT_HTTP_TARGETS = [
    {"name": "阿里云镜像", "url": "https://mirrors.aliyun.com/ubuntu/ls-lR.gz",
     "country": "CN", "note": "杭州"},
    {"name": "华为云镜像", "url": "https://mirrors.huaweicloud.com/ubuntu/ls-lR.gz",
     "country": "CN", "note": "贵阳/北京"},
    {"name": "腾讯云镜像", "url": "https://mirrors.cloud.tencent.com/ubuntu/ls-lR.gz",
     "country": "CN", "note": "深圳"},
]


class HttpBackend:
    name = "http"
    label = "国内直连"

    def __init__(self, targets=None, upload_url: str = "", insecure: bool = False,
                 connections: int = 8, duration: float = 10.0,
                 upload_connections: int = 4, upload_duration: float = 8.0,
                 ookla_binary: str = "speedtest", upload_via_ookla: bool = True):
        self.targets = [t for t in (targets or DEFAULT_HTTP_TARGETS) if t.get("url")]
        self.upload_url = (upload_url or "").strip()
        self.insecure = insecure
        self.connections = max(1, int(connections))
        self.duration = max(3.0, float(duration))
        self.upload_connections = max(1, int(upload_connections))
        self.upload_duration = max(3.0, float(upload_duration))
        # 上行兜底引擎：国内无可用公开上传端点，见类上方说明。
        self.ookla_binary = ookla_binary or "speedtest"
        self.upload_via_ookla = bool(upload_via_ookla)

    # -- 单项测量 --------------------------------------------------------
    def _ping(self, url: str, samples: int = 5, timeout: float = 5.0):
        https, host, port, path = split_url(url)
        lat = []
        conn = None
        try:
            conn = _connect(https, host, port, timeout, self.insecure)
            for _ in range(samples):
                try:
                    t0 = time.perf_counter()
                    conn.request("GET", path, headers={
                        "Range": "bytes=0-0", "User-Agent": UA,
                        "Accept": "*/*", "Connection": "keep-alive"})
                    resp = conn.getresponse()
                    resp.read(1)
                    lat.append((time.perf_counter() - t0) * 1000)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = _connect(https, host, port, timeout, self.insecure)
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
        if not lat:
            raise BackendError("延迟测量失败（连接被拒绝或超时）")
        lat.sort()
        return round(lat[len(lat) // 2], 2)

    def _content_length(self, url: str, timeout: float = 6.0):
        https, host, port, path = split_url(url)
        try:
            c = _connect(https, host, port, timeout, self.insecure)
            c.request("HEAD", path, headers={"User-Agent": UA})
            r = c.getresponse()
            n = int(r.getheader("Content-Length") or 0)
            c.close()
            return n
        except Exception:
            return 0

    def _download(self, url: str, timeout: float = 6.0):
        https, host, port, path = split_url(url)
        size = self._content_length(url, timeout) or 0
        chunk = 4 * 1024 * 1024
        deadline = time.perf_counter() + self.duration
        counter = [0]
        lock = threading.Lock()
        errors = []

        def worker(idx: int):
            local = 0
            offset = (idx * chunk) % max(size, chunk) if size else idx * chunk
            while time.perf_counter() < deadline:
                try:
                    c = _connect(https, host, port, timeout, self.insecure)
                    hdr = {"User-Agent": UA, "Accept": "*/*"}
                    if size:
                        end = min(offset + chunk - 1, size - 1)
                        hdr["Range"] = f"bytes={offset}-{end}"
                    c.request("GET", path, headers=hdr)
                    resp = c.getresponse()
                    if resp.status >= 400:
                        raise BackendError(f"HTTP {resp.status}")
                    while time.perf_counter() < deadline:
                        b = resp.read(262144)
                        if not b:
                            break
                        local += len(b)
                    c.close()
                except Exception as e:  # noqa: BLE001
                    errors.append(str(e))
                    break
                offset = (offset + self.connections * chunk) % max(size, chunk) if size \
                    else offset + self.connections * chunk
            with lock:
                counter[0] += local

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.connections) as ex:
            list(ex.map(worker, range(self.connections)))
        elapsed = max(time.perf_counter() - t0, 0.001)
        got = counter[0]
        if got <= 0:
            raise BackendError("下载测量失败：" + (errors[0] if errors else "无数据返回"))
        return round(got * 8 / elapsed / 1e6, 2), got, elapsed

    def _upload(self, timeout: float = 6.0):
        if not self.upload_url:
            return None
        https, host, port, path = split_url(self.upload_url)
        blk = 1 * 1024 * 1024
        payload = os.urandom(blk)
        deadline = time.perf_counter() + self.upload_duration
        counter = [0]
        lock = threading.Lock()
        errors = []

        def worker(_i: int):
            local = 0
            while time.perf_counter() < deadline:
                try:
                    c = _connect(https, host, port, timeout, self.insecure)
                    c.request("POST", path, body=payload, headers={
                        "User-Agent": UA,
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(blk)})
                    r = c.getresponse()
                    r.read()
                    c.close()
                    local += blk
                except Exception as e:  # noqa: BLE001
                    errors.append(str(e))
                    break
            with lock:
                counter[0] += local

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.upload_connections) as ex:
            list(ex.map(worker, range(self.upload_connections)))
        elapsed = max(time.perf_counter() - t0, 0.001)
        if counter[0] <= 0:
            raise BackendError("上传测量失败：" + (errors[0] if errors else "无数据返回"))
        return round(counter[0] * 8 / elapsed / 1e6, 2)

    def _upload_via_ookla(self, timeout: float = 150.0):
        """用 Ookla CLI 补测上行，返回 (Mbps, 节点名, 地区, 国家码) 或 None。

        国内镜像站只提供下载、不接受上传；国外 HTTP 端点在本网络下实测仅
        1–3 Mbps（单流 1.1、16 流 3.6），严重低报。Ookla 走多流 + 运营商
        优质互联，能得到接近真实宽带上行的数值，故作为上行兜底。
        """
        cmd = [self.ookla_binary, "--accept-license", "--accept-gdpr", "-f", "json"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            return None
        except Exception:  # noqa: BLE001
            return None
        if r.returncode != 0:
            return None
        try:
            data = json.loads(r.stdout)
        except ValueError:
            return None
        bw = (data.get("upload") or {}).get("bandwidth")
        if not bw:
            return None
        srv = data.get("server") or {}
        return (round(float(bw) * 8 / 1e6, 2),
                srv.get("name") or "", srv.get("location") or "",
                (srv.get("country") or "").upper())

    # -- 目标选择 --------------------------------------------------------
    def probe_target(self, t: dict, timeout: float = 4.0):
        https, host, port, _ = split_url(t["url"])
        return tcp_latency_ms(host, port, timeout=timeout)

    def _burst(self, url: str, seconds: float = 3.0, timeout: float = 6.0) -> float:
        """对单个目标做短促多连接下载，返回 Mbps；完全失败返回 -1.0。

        仅用于选点，不做正式计分，所以窗口很短。
        """
        https, host, port, path = split_url(url)
        deadline = time.perf_counter() + seconds
        counter = [0]
        lock = threading.Lock()

        def worker(_idx: int):
            local = 0
            while time.perf_counter() < deadline:
                try:
                    c = _connect(https, host, port, timeout, self.insecure)
                    c.request("GET", path,
                              headers={"User-Agent": UA, "Accept": "*/*"})
                    resp = c.getresponse()
                    if resp.status >= 400:
                        raise BackendError(f"HTTP {resp.status}")
                    while time.perf_counter() < deadline:
                        b = resp.read(262144)
                        if not b:
                            break
                        local += len(b)
                    c.close()
                except Exception:  # noqa: BLE001
                    break
            with lock:
                counter[0] += local

        n = min(4, self.connections)
        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(worker, range(n)))
        elapsed = max(time.perf_counter() - t0, 0.001)
        if counter[0] <= 0:
            return -1.0
        return round(counter[0] * 8 / elapsed / 1e6, 2)

    def pick_target(self, prefer: str = ""):
        """返回 (target, latency_ms)。

        先用延迟筛掉不可达的，再对候选做短促下载**按实测吞吐**取最快。

        不能只看延迟：实测阿里云镜像延迟最低（21ms）但吞吐仅 38Mbps，而华为云
        /腾讯云延迟略高却有 90+Mbps。只按延迟选会让"国内直连"的结果忽高忽低，
        看起来像网络抖动，其实是选错了站。
        """
        if not self.targets:
            raise BackendError("没有配置任何国内测速目标")
        if prefer:
            for t in self.targets:
                if prefer.lower() in t["name"].lower():
                    lat = self.probe_target(t)
                    if lat is not None:
                        return t, lat

        alive = []
        for t in self.targets:
            lat = self.probe_target(t)
            if lat is not None:
                alive.append((t, lat))
        if not alive:
            raise BackendError("所有国内目标均不可达：" +
                               "、".join(t["name"] for t in self.targets))
        if len(alive) == 1:
            return alive[0]

        scored = [(self._burst(t["url"]), lat, t) for t, lat in alive]
        # 吞吐降序；吞吐相同（含全失败 -1）时退回延迟升序
        scored.sort(key=lambda x: (-x[0], x[1]))
        _bw, lat, t = scored[0]
        return t, lat

    def run(self, target_name: str = "", timeout: float = 180.0):
        t, lat = self.pick_target(target_name)
        https, host, port, _ = split_url(t["url"])
        # 延迟用 TCP 建连往返（纯网络指标，不受服务端处理耗时干扰），
        # TTFB 另存到 extra 供参考。
        samples = [x for x in (tcp_latency_ms(host, port, timeout=4.0, samples=1)
                               for _ in range(5)) if x is not None]
        ping = round(sorted(samples)[len(samples) // 2], 2) if samples else lat
        try:
            ttfb = self._ping(t["url"])
        except BackendError:
            ttfb = None
        down, got, elapsed = self._download(t["url"])
        up = self._upload()
        # 上行兜底：镜像站不接受上传 → 改用 Ookla CLI 补测（见 _upload_via_ookla）
        upload_source = "http" if up is not None else ""
        ookla_up_srv = None
        if up is None and self.upload_via_ookla:
            got_up = self._upload_via_ookla()
            if got_up:
                up, o_name, o_loc, o_country = got_up
                upload_source = "ookla"
                ookla_up_srv = {"name": o_name, "location": o_loc, "country": o_country}
        return {
            "backend": self.name,
            "download": down,
            "upload": up,
            "ping": ping,
            "server_id": "http:" + t["name"],
            "server_name": t["name"],
            "server_location": t.get("note") or t["name"],
            "server_country": (t.get("country") or "CN").upper(),
            "server_host": urllib.parse.urlsplit(t["url"]).hostname or "",
            "isp": None,
            "packet_loss": None,
            "result_url": None,
            "extra": {"target_url": t["url"], "tcp_latency": lat, "ttfb": ttfb,
                      "bytes": got, "seconds": round(elapsed, 2),
                      "connections": self.connections,
                      "upload_configured": bool(self.upload_url),
                      "upload_source": upload_source,
                      "upload_server": ookla_up_srv},
        }


# --------------------------------------------------------------------------
# 后端 C：LibreSpeed（自建/找到可用实例时使用）
# --------------------------------------------------------------------------

class LibreSpeedBackend:
    name = "librespeed"
    label = "LibreSpeed"

    def __init__(self, servers=None, insecure: bool = False,
                 connections: int = 4, duration: float = 10.0):
        # servers: [{"name": "...", "url": "https://host[:port][/path]"}]
        self.servers = [s for s in (servers or []) if s.get("url")]
        self.insecure = insecure
        self.connections = max(1, int(connections))
        self.duration = max(3.0, float(duration))

    @staticmethod
    def _join(base: str, suffix: str):
        return base.rstrip("/") + "/" + suffix.lstrip("/")

    def _ping(self, base: str, samples: int = 5, timeout: float = 5.0):
        url = self._join(base, "backend/empty.php")
        https, host, port, path = split_url(url)
        lat, conn = [], None
        try:
            conn = _connect(https, host, port, timeout, self.insecure)
            for _ in range(samples):
                try:
                    t0 = time.perf_counter()
                    conn.request("GET", path, headers={"User-Agent": UA,
                                                       "Cache-Control": "no-cache"})
                    r = conn.getresponse()
                    r.read()
                    lat.append((time.perf_counter() - t0) * 1000)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = _connect(https, host, port, timeout, self.insecure)
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
        if not lat:
            raise BackendError("LibreSpeed 延迟测量失败")
        lat.sort()
        return round(lat[len(lat) // 2], 2)

    def _download(self, base: str, timeout: float = 8.0):
        https, host, port, path = split_url(self._join(base, "backend/garbage.php"))
        mb = 25
        deadline = time.perf_counter() + self.duration
        counter, lock, errors = [0], threading.Lock(), []

        def worker(_i: int):
            local = 0
            while time.perf_counter() < deadline:
                try:
                    c = _connect(https, host, port, timeout, self.insecure)
                    c.request("GET", path + f"?ckSize={mb}", headers={
                        "User-Agent": UA, "Accept": "*/*",
                        "Cache-Control": "no-cache"})
                    r = c.getresponse()
                    if r.status >= 400:
                        raise BackendError(f"HTTP {r.status}")
                    while time.perf_counter() < deadline:
                        b = r.read(262144)
                        if not b:
                            break
                        local += len(b)
                    c.close()
                except Exception as e:  # noqa: BLE001
                    errors.append(str(e))
                    break
            with lock:
                counter[0] += local

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.connections) as ex:
            list(ex.map(worker, range(self.connections)))
        elapsed = max(time.perf_counter() - t0, 0.001)
        if counter[0] <= 0:
            raise BackendError("LibreSpeed 下载失败：" + (errors[0] if errors else "无数据"))
        return round(counter[0] * 8 / elapsed / 1e6, 2)

    def _upload(self, base: str, timeout: float = 8.0):
        https, host, port, path = split_url(self._join(base, "backend/empty.php"))
        blk = 1 * 1024 * 1024
        payload = os.urandom(blk)
        deadline = time.perf_counter() + self.duration
        counter, lock, errors = [0], threading.Lock(), []

        def worker(_i: int):
            local = 0
            while time.perf_counter() < deadline:
                try:
                    c = _connect(https, host, port, timeout, self.insecure)
                    c.request("POST", path, body=payload, headers={
                        "User-Agent": UA,
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(blk)})
                    r = c.getresponse()
                    r.read()
                    c.close()
                    local += blk
                except Exception as e:  # noqa: BLE001
                    errors.append(str(e))
                    break
            with lock:
                counter[0] += local

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.connections) as ex:
            list(ex.map(worker, range(self.connections)))
        elapsed = max(time.perf_counter() - t0, 0.001)
        if counter[0] <= 0:
            raise BackendError("LibreSpeed 上传失败：" + (errors[0] if errors else "无数据"))
        return round(counter[0] * 8 / elapsed / 1e6, 2)

    def run(self, server_name: str = "", timeout: float = 180.0):
        if not self.servers:
            raise BackendError("未配置 LibreSpeed 服务器")
        cand = None
        if server_name:
            for s in self.servers:
                if server_name.lower() in (s.get("name") or "").lower():
                    cand = s
                    break
        if cand is None:
            best = None
            for s in self.servers:
                https, host, port, _ = split_url(s["url"])
                lat = tcp_latency_ms(host, port, timeout=3.0)
                if lat is not None and (best is None or lat < best[1]):
                    best = (s, lat)
            if best is None:
                raise BackendError("所有 LibreSpeed 服务器均不可达")
            cand = best[0]
        base = cand["url"]
        ping = self._ping(base)
        down = self._download(base)
        up = self._upload(base)
        https, host, port, _ = split_url(base)
        return {
            "backend": self.name,
            "download": down,
            "upload": up,
            "ping": ping,
            "server_id": "ls:" + (cand.get("name") or host),
            "server_name": cand.get("name") or host,
            "server_location": cand.get("note") or host,
            "server_country": (cand.get("country") or "").upper(),
            "server_host": host,
            "isp": None,
            "packet_loss": None,
            "result_url": None,
            "extra": {"base": base, "connections": self.connections},
        }


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------

def build_backend(cfg: dict):
    """根据配置构造后端实例。cfg 为 settings 字典。"""
    which = (cfg.get("backend") or "ookla").lower()
    insecure = str(cfg.get("tls_insecure") or "0") in ("1", "true", "True", "on")
    if which == "http":
        targets = cfg.get("http_targets")
        if isinstance(targets, str):
            targets = _parse_targets(targets)
        return HttpBackend(
            targets=targets, upload_url=cfg.get("http_upload_url") or "",
            insecure=insecure,
            connections=int(cfg.get("http_connections") or 8),
            duration=float(cfg.get("http_duration") or 10),
            ookla_binary=cfg.get("ookla_binary") or "speedtest",
        )
    if which == "librespeed":
        servers = cfg.get("librespeed_servers")
        if isinstance(servers, str):
            servers = _parse_targets(servers)
        return LibreSpeedBackend(servers=servers, insecure=insecure)
    return OoklaBackend(binary=cfg.get("ookla_binary") or "speedtest", insecure=insecure)


def _parse_targets(raw: str):
    """解析 "名称|url|备注" 每行一条；也兼容纯 url 列表。"""
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        url = ""
        name = ""
        note = ""
        for p in parts:
            if p.startswith("http://") or p.startswith("https://"):
                url = p
            elif not name:
                name = p
            else:
                note = p
        if not url:
            continue
        out.append({"name": name or urllib.parse.urlsplit(url).hostname or url,
                    "url": url, "note": note, "country": "CN"})
    return out
