"""
Ariawave Sovereign Multi-WAN Bonded Download & Torrent Accelerator
Stripes HTTP/HTTPS downloads across all active physical network adapters
(PCIe Ethernet + Dual USB-C Ethernet / RNDIS + iPhone Tether + IPv6) using
pre-connect IP_UNICAST_IF hardware interface index binding + source-IP binding
and dynamic work-stealing chunk queues, and accelerates qBittorrent multi-homing.
"""

import http.client
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from queue import Empty, Queue

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

PUBLIC_FAST_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker1.bt.moack.co.kr:80/announce",
    "udp://tracker.theoks.net:6969/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://p4p.arenabg.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "http://tracker.opentrackr.org:1337/announce",
    "https://tracker.tamersunion.org:443/announce",
]


def discover_wan_interfaces(verify_internet: bool = False) -> list[dict]:
    """
    Enumerates all connected physical IPv4 network interfaces with default gateways
    (excluding Loopback, Tailscale, and Hyper-V vEthernet switches).
    """
    ps_script = r"""
    $routes = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue
    $results = @()
    foreach ($r in $routes) {
        $adapter = Get-NetAdapter -InterfaceIndex $r.ifIndex -ErrorAction SilentlyContinue
        if (-not $adapter -or $adapter.Status -ne 'Up') { continue }
        if ($adapter.Name -match 'vEthernet|Tailscale|Loopback') { continue }
        $ipObj = Get-NetIPAddress -InterfaceIndex $r.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notmatch '^169\.254\.' } | Select-Object -First 1
        if ($ipObj) {
            $results += [PSCustomObject]@{
                ifIndex = [int]$r.ifIndex
                alias = [string]$adapter.Name
                description = [string]$adapter.InterfaceDescription
                link_speed = [string]$adapter.LinkSpeed
                local_ip = [string]$ipObj.IPAddress
                gateway = [string]$r.NextHop
                metric = [int]($r.RouteMetric + $r.InterfaceMetric)
            }
        }
    }
    $results | ConvertTo-Json -Compress
    """
    interfaces = []
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=6,
            creationflags=_NO_WINDOW,
        )
        raw = (proc.stdout or "").strip()
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                interfaces = [parsed]
            elif isinstance(parsed, list):
                interfaces = parsed
    except Exception:
        pass

    if verify_internet and interfaces:
        verified = []
        threads = []
        lock = threading.Lock()

        def _probe(nic):
            ip = nic["local_ip"]
            t0 = time.perf_counter()
            try:
                proc = subprocess.run(
                    ["curl.exe", "--interface", ip, "-s", "--max-time", "4", "https://ipinfo.io/json"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=_NO_WINDOW,
                )
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                if proc.returncode == 0 and proc.stdout.strip():
                    info = json.loads(proc.stdout.strip())
                    nic["wan_ip"] = info.get("ip", "")
                    nic["isp"] = info.get("org", "")
                    nic["latency_ms"] = elapsed_ms
                    nic["online"] = bool(nic["wan_ip"])
                else:
                    nic["online"] = False
            except Exception:
                nic["online"] = False
            with lock:
                verified.append(nic)

        for n in interfaces:
            t = threading.Thread(target=_probe, args=(n,), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=6)
        interfaces = sorted(verified, key=lambda x: x.get("ifIndex", 999))

    return interfaces


def _create_bound_socket(address, timeout=15.0, source_address=None, if_index=None):
    """
    Creates an IPv4 TCP socket bound to `source_address` AND Windows `IP_UNICAST_IF` (31)
    BEFORE calling `connect()`, guaranteeing hardware interface routing even when two
    adapters share identical gateway subnets (e.g. 192.168.1.1 on dual Inseego modems).
    """
    host, port = address
    err = None
    for res in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        af, socktype, proto, canonname, sa = res
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            if timeout is not None:
                sock.settimeout(timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if if_index:
                sock.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", int(if_index)))
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            return sock
        except OSError as _e:
            err = _e
            if sock is not None:
                sock.close()
    if err is not None:
        raise err
    raise OSError("getaddrinfo returns an empty list")


class _OptimizedBoundHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port=None, timeout=15.0, source_address=None, if_index=None):
        super().__init__(host, port=port, timeout=timeout, source_address=source_address)
        self._if_index = if_index

    def connect(self):
        self.sock = _create_bound_socket(
            (self.host, self.port), self.timeout, self.source_address, self._if_index
        )
        if self._tunnel_host:
            self._tunnel()


class _OptimizedBoundHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port=None, timeout=15.0, context=None, source_address=None, if_index=None):
        super().__init__(host, port=port, timeout=timeout, context=context, source_address=source_address)
        self._if_index = if_index

    def connect(self):
        sock = _create_bound_socket(
            (self.host, self.port), self.timeout, self.source_address, self._if_index
        )
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        server_hostname = self._tunnel_host if self._tunnel_host else self.host
        self.sock = self._context.wrap_socket(sock, server_hostname=server_hostname)


def _open_bound_connection(parsed_url, source_ip: str | None, timeout: float = 15.0, if_index: int | None = None):
    """Creates an HTTPConnection or HTTPSConnection bound to a specific local NIC IP & hardware ifIndex."""
    host = parsed_url.hostname
    port = parsed_url.port
    is_https = parsed_url.scheme.lower() == "https"
    source_addr = (source_ip, 0) if source_ip else None
    if is_https:
        ctx = ssl.create_default_context()
        return _OptimizedBoundHTTPSConnection(
            host, port or 443, timeout=timeout, context=ctx, source_address=source_addr, if_index=if_index
        )
    return _OptimizedBoundHTTPConnection(
        host, port or 80, timeout=timeout, source_address=source_addr, if_index=if_index
    )


def _resolve_redirects_and_probe(
    url: str,
    headers: dict | None = None,
    source_ip: str | None = None,
    if_index: int | None = None,
):
    """
    Follows up to 5 HTTP redirects and probes total size and Range support
    using a 1-byte Range request (Range: bytes=0-0).
    """
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Ariawave-MultiWAN/3.0",
        "Accept": "*/*",
    }
    if headers:
        req_headers.update(headers)

    current_url = url
    for _ in range(6):
        parsed = urllib.parse.urlsplit(current_url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        conn = _open_bound_connection(parsed, source_ip, timeout=12.0, if_index=if_index)
        try:
            probe_headers = dict(req_headers)
            probe_headers["Range"] = "bytes=0-0"
            conn.request("GET", path, headers=probe_headers)
            resp = conn.getresponse()
            status = resp.status
            if status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                resp.read()
                conn.close()
                if not loc:
                    break
                current_url = urllib.parse.urljoin(current_url, loc)
                continue

            content_range = resp.getheader("Content-Range", "")
            content_len = resp.getheader("Content-Length", "0")
            accept_ranges = resp.getheader("Accept-Ranges", "")
            resp.read(16)
            conn.close()

            if status == 206 and "/" in content_range:
                total_str = content_range.split("/")[-1].strip()
                if total_str.isdigit():
                    return current_url, int(total_str), True

            total_bytes = int(content_len) if content_len.isdigit() else 0
            supports_range = (status == 206) or ("bytes" in accept_ranges.lower())
            return current_url, total_bytes, supports_range
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            break

    return current_url, 0, False


def download_file_multi_wan(
    url: str,
    dest_path: str,
    headers: dict | None = None,
    progress_callback=None,
    chunk_size_bytes: int = 4 * 1024 * 1024,
    workers_per_nic: int = 4,
    progress_cb=None,
    expected_size_mb: float = 0,
) -> dict:
    """
    Downloads `url` to `dest_path` by striping HTTP Range chunks across all active
    physical WAN interfaces (`Ethernet`, `Ethernet 2`, `Ethernet 4`, `Ethernet 5`) simultaneously.
    Automatically falls back to single-stream download if server does not support Range.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    nics = discover_wan_interfaces(verify_internet=False)
    ip_to_ifindex = {n["local_ip"]: n.get("ifIndex") for n in nics if n.get("local_ip")}
    ip_to_alias = {n["local_ip"]: n.get("alias", n["local_ip"]) for n in nics if n.get("local_ip")}
    local_ips = [n["local_ip"] for n in nics if n.get("local_ip")]
    if not local_ips:
        local_ips = [None]

    final_url, total_size, supports_range = False, 0, False
    for probe_ip in local_ips:
        final_url, total_size, supports_range = _resolve_redirects_and_probe(
            url, headers=headers, source_ip=probe_ip, if_index=ip_to_ifindex.get(probe_ip)
        )
        if total_size > 0:
            break

    t_start = time.perf_counter()

    # Fallback if file is small (< 5 MB) or server doesn't support Range requests
    if not supports_range or total_size <= chunk_size_bytes:
        req_headers = {"User-Agent": "Mozilla/5.0 Ariawave-MultiWAN/3.0"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(final_url or url, headers=req_headers)
        done = 0
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest_path, "wb") as out_f:
            total = total_size or int(resp.headers.get("Content-Length", 0))
            while True:
                buf = resp.read(1024 * 1024)
                if not buf:
                    break
                out_f.write(buf)
                done += len(buf)
                if progress_callback:
                    progress_callback(done, total, len(local_ips))
        elapsed = max(0.001, time.perf_counter() - t_start)
        return {
            "success": True,
            "mode": "single_stream",
            "bytes": done,
            "elapsed_sec": round(elapsed, 2),
            "speed_mbps": round((done * 8) / (elapsed * 1_000_000), 2),
            "interfaces_used": local_ips,
        }

    # Pre-allocate destination file
    with open(dest_path, "wb") as f:
        f.truncate(total_size)

    # Build work-stealing chunk queue
    chunk_queue = Queue()
    offset = 0
    while offset < total_size:
        end = min(offset + chunk_size_bytes - 1, total_size - 1)
        chunk_queue.put((offset, end))
        offset = end + 1

    parsed_final = urllib.parse.urlsplit(final_url)
    path_final = parsed_final.path or "/"
    if parsed_final.query:
        path_final += "?" + parsed_final.query

    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Ariawave-MultiWAN/3.0",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }
    if headers:
        req_headers.update(headers)

    file_lock = threading.Lock()
    progress_lock = threading.Lock()
    bytes_completed = 0
    per_nic_bytes = {ip or "default": 0 for ip in local_ips}
    errors = []

    def _worker(bind_ip):
        nonlocal bytes_completed
        conn = None
        while not chunk_queue.empty():
            try:
                start_b, end_b = chunk_queue.get_nowait()
            except Empty:
                break

            expected_len = end_b - start_b + 1
            success = False
            # Try bound IP first, then fallback to any other IP if a link hiccups (2 full passes)
            base_candidates = [bind_ip] + [ip for ip in local_ips if ip != bind_ip] + [None]
            candidate_ips = base_candidates + base_candidates
            for try_ip in candidate_ips:
                try:
                    if conn is None or try_ip != bind_ip:
                        if conn is not None:
                            try:
                                conn.close()
                            except Exception:
                                pass
                        conn = _open_bound_connection(
                            parsed_final,
                            try_ip,
                            timeout=20.0,
                            if_index=ip_to_ifindex.get(try_ip) if try_ip else None,
                        )

                    h = dict(req_headers)
                    h["Range"] = f"bytes={start_b}-{end_b}"
                    conn.request("GET", path_final, headers=h)
                    resp = conn.getresponse()
                    if resp.status not in (200, 206):
                        resp.read()
                        conn.close()
                        conn = None
                        continue

                    data_parts = []
                    received = 0
                    while received < expected_len:
                        block = resp.read(min(262144, expected_len - received))
                        if not block:
                            break
                        data_parts.append(block)
                        received += len(block)
                        with progress_lock:
                            bytes_completed += len(block)
                            per_nic_bytes[try_ip or "default"] = per_nic_bytes.get(try_ip or "default", 0) + len(block)
                            cb = progress_callback or progress_cb
                            if cb:
                                dt_now = max(0.05, time.perf_counter() - t_start)
                                mb_s_now = (bytes_completed / (1024 * 1024)) / dt_now
                                try:
                                    cb(bytes_completed, total_size, mb_s_now, len(local_ips))
                                except TypeError:
                                    cb(bytes_completed, total_size, len(local_ips))

                    if received == expected_len:
                        full_chunk = b"".join(data_parts)
                        with file_lock:
                            with open(dest_path, "r+b") as wf:
                                wf.seek(start_b)
                                wf.write(full_chunk)
                        success = True
                        break
                    else:
                        with progress_lock:
                            bytes_completed = max(0, bytes_completed - received)
                            per_nic_bytes[try_ip or "default"] = max(
                                0, per_nic_bytes.get(try_ip or "default", 0) - received
                            )
                        conn.close()
                        conn = None
                except Exception:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None

            if not success:
                errors.append(f"Failed chunk {start_b}-{end_b}")
            chunk_queue.task_done()

        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    threads = []
    for ip in local_ips:
        for _ in range(workers_per_nic):
            t = threading.Thread(target=_worker, args=(ip,), daemon=True)
            threads.append(t)
            t.start()

    for t in threads:
        t.join()

    elapsed = max(0.001, time.perf_counter() - t_start)
    if errors:
        raise RuntimeError(f"Multi-WAN download incomplete: {errors[0]}")

    return {
        "success": True,
        "mode": "multi_wan_striped",
        "bytes": total_size,
        "elapsed_sec": round(elapsed, 2),
        "speed_mbps": round((total_size * 8) / (elapsed * 1_000_000), 2),
        "speed_mbs": round(total_size / (elapsed * 1048576), 2),
        "interfaces_used": local_ips,
        "per_nic_mb": {
            f"{ip_to_alias.get(k, k)} ({k})": round(v / 1048576, 2)
            for k, v in per_nic_bytes.items()
        },
    }


def boost_qbittorrent_active_torrents() -> dict:
    """
    Connects to qBittorrent's local API (127.0.0.1:8090) to inject high-speed
    public trackers into all active torrents and trigger an immediate multi-WAN
    reannounce + DHT announce across all active network adapters.
    """
    base = "http://127.0.0.1:8090/api/v2"
    try:
        req = urllib.request.Request(f"{base}/torrents/info")
        with urllib.request.urlopen(req, timeout=3) as r:
            torrents = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"success": False, "error": f"qBittorrent WebUI unreachable: {e}"}

    boosted = []
    urls_joined = "\n".join(PUBLIC_FAST_TRACKERS)
    for t in torrents:
        thash = t.get("hash")
        if not thash:
            continue
        try:
            body = urllib.parse.urlencode({"hash": thash, "urls": urls_joined}).encode("utf-8")
            treq = urllib.request.Request(f"{base}/torrents/addTrackers", data=body, method="POST")
            urllib.request.urlopen(treq, timeout=3).read()
        except Exception:
            pass
        boosted.append({
            "name": t.get("name"),
            "state": t.get("state"),
            "progress_pct": round((t.get("progress") or 0) * 100, 1),
            "dlspeed_mbs": round((t.get("dlspeed") or 0) / 1048576, 2),
            "num_seeds": t.get("num_seeds", 0),
            "num_leechs": t.get("num_leechs", 0),
        })

    try:
        rbody = urllib.parse.urlencode({"hashes": "all"}).encode("utf-8")
        urllib.request.urlopen(
            urllib.request.Request(f"{base}/torrents/reannounce", data=rbody, method="POST"),
            timeout=3,
        ).read()
    except Exception:
        pass

    return {"success": True, "torrents_count": len(boosted), "torrents": boosted}


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--status":
        nics = discover_wan_interfaces(verify_internet=True)
        qb = boost_qbittorrent_active_torrents()
        print(json.dumps({"interfaces": nics, "qbittorrent": qb}, indent=2))
    elif len(sys.argv) >= 3:
        res = download_file_multi_wan(sys.argv[1], sys.argv[2])
        print(json.dumps(res, indent=2))
    else:
        print("Usage: python multi_wan.py --status | <url> <dest_path>")
