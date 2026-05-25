#!/usr/bin/env python3
"""
docker-updater - Docker 容器自动更新工具

单次模式：
  updater [容器名...]

持久监控模式：
  updater watch [--interval N] [--window HH:MM-HH:MM] [容器名...]

不传容器名时操作所有运行中容器（自动排除自身）。
镜像地址自动从容器当前配置读取，拉取后对比镜像 ID，有变化才重建。

通过 /var/run/docker.sock 调用 Docker API，精确区分用户显式传入的参数
与 Dockerfile 中定义的默认值，避免旧值污染新容器。
"""

import argparse
import http.client
import json
import os
import signal
import socket
import sys
import time
import urllib.parse


# ------------------------------------------
# Docker API 客户端
# ------------------------------------------

def _decode_json(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"message": raw.decode(errors="replace")}


class DockerClient:
    def __init__(self, sock="/var/run/docker.sock"):
        self.sock = sock

    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.sock)
        return s

    def _request(self, method, path, body=None):
        conn = http.client.HTTPConnection("localhost")
        conn.sock = self._connect()
        data = json.dumps(body).encode() if body is not None else b""
        headers = {
            "Host": "localhost",
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
        }
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, _decode_json(raw)
        finally:
            conn.close()

    def list_containers(self):
        """返回所有运行中容器的列表"""
        status, data = self._request("GET", "/containers/json")
        if status != 200:
            raise RuntimeError(f"List containers failed {status}: {data}")
        return data

    def inspect_container(self, name):
        status, data = self._request("GET", f"/containers/{urllib.parse.quote(name, safe='')}/json")
        if status == 404:
            raise RuntimeError(f"Container not found: {name}")
        if status != 200:
            raise RuntimeError(f"Docker API error {status}: {data}")
        return data

    def inspect_image(self, image):
        status, data = self._request("GET", f"/images/{urllib.parse.quote(image, safe=':@/')}/json")
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"Docker API error {status}: {data}")
        return data

    def pull_image(self, image):
        """拉取镜像，流式输出进度，返回拉取后的本地镜像 ID"""
        ref, tag = _parse_image_ref(image)
        path = f"/images/create?fromImage={urllib.parse.quote(ref, safe='')}&tag={urllib.parse.quote(tag, safe='')}"

        conn = http.client.HTTPConnection("localhost")
        conn.sock = self._connect()
        try:
            conn.request("POST", path, body=b"", headers={
                "Host": "localhost",
                "Content-Length": "0",
            })
            resp = conn.getresponse()

            if resp.status != 200:
                body = resp.read().decode(errors="replace")
                raise RuntimeError(f"Pull failed with HTTP {resp.status}: {body}")

            last_error = None
            buf = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode(errors="replace").strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        obj = json.loads(line)
                        if "error" in obj:
                            last_error = obj["error"]
                        elif "status" in obj:
                            log(f"  {obj.get('status', '')} {obj.get('progress', '')}".rstrip())
                    except json.JSONDecodeError:
                        pass

            if last_error:
                raise RuntimeError(f"Pull error: {last_error}")
        finally:
            conn.close()

        # 返回拉取后的镜像 ID，用于与旧镜像对比
        info = self.inspect_image(image)
        if not info:
            raise RuntimeError(f"Pulled image but inspect failed: {image}")
        return info["Id"]

    def stop_container(self, name, timeout=30):
        path = f"/containers/{urllib.parse.quote(name, safe='')}/stop?t={timeout}"
        status, _ = self._request("POST", path)
        if status not in (204, 304):
            raise RuntimeError(f"Stop failed: {status}")

    def remove_container(self, name):
        status, data = self._request("DELETE", f"/containers/{urllib.parse.quote(name, safe='')}")
        if status not in (204,):
            raise RuntimeError(f"Remove failed {status}: {data}")

    def create_container(self, name, config):
        path = f"/containers/create?name={urllib.parse.quote(name, safe='')}"
        status, data = self._request("POST", path, config)
        if status not in (201,):
            raise RuntimeError(f"Create failed {status}: {data}")
        return data["Id"]

    def start_container(self, container_id):
        status, _ = self._request("POST", f"/containers/{container_id}/start")
        if status not in (204, 304):
            raise RuntimeError(f"Start failed: {status}")


# ------------------------------------------
# 镜像名解析
# ------------------------------------------

def _parse_image_ref(image: str) -> tuple:
    """
    将镜像地址拆分为 (ref, tag)。

    需要处理以下格式：
      - ubuntu                              → (ubuntu, latest)
      - ubuntu:22.04                        → (ubuntu, 22.04)
      - registry.example.com/app            → (registry.example.com/app, latest)
      - registry.example.com/app:1.0        → (registry.example.com/app, 1.0)
      - registry.example.com:5000/app:1.0   → (registry.example.com:5000/app, 1.0)

    只看最后路径段是否含冒号，避免把 registry 端口号误认为 tag。
    """
    last_slash = image.rfind("/")
    last_segment = image[last_slash + 1:]

    if ":" in last_segment:
        colon_pos = image.rfind(":")
        return image[:colon_pos], image[colon_pos + 1:]
    else:
        return image, "latest"


# ------------------------------------------
# 运行参数提取
# ------------------------------------------

def diff_env(container_env: list, image_env: list) -> list:
    """
    对比容器和镜像的环境变量，返回用户显式传入的部分。
    规则：
    - 镜像中不存在的 KEY=VAL → 用户新增，保留
    - 镜像中存在且值相同    → 来自 Dockerfile ENV，丢弃
    - 镜像中存在但值不同    → 用户覆盖，保留
    """
    image_env_map = {}
    for item in (image_env or []):
        k, _, v = item.partition("=")
        image_env_map[k] = v

    user_envs = []
    for item in (container_env or []):
        k, _, v = item.partition("=")
        if k not in image_env_map:
            user_envs.append(item)
        elif image_env_map[k] != v:
            user_envs.append(item)

    return user_envs


def diff_map(container_map: dict, image_map: dict) -> dict:
    """对比 dict 类型配置（如 Labels），返回用户显式新增/覆盖的部分。"""
    result = {}
    for k, v in (container_map or {}).items():
        if not image_map or k not in image_map or image_map.get(k) != v:
            result[k] = v
    return result


def _diff_config_value(container_cfg: dict, image_cfg: dict, key: str):
    """
    容器配置与镜像默认值不同才保留，避免旧镜像默认值污染新镜像。
    仅当容器值等于镜像默认值时才丢弃（包括均为 None/空的情况）；
    用户显式设为空值（如清空 Cmd）也会被保留。
    """
    cv = container_cfg.get(key)
    iv = image_cfg.get(key)
    # 两侧都是空（None/[]/""），视为"未设置"，丢弃
    def _empty(v):
        return v is None or v == "" or v == [] or v == {}
    if _empty(cv) and _empty(iv):
        return None
    # 值相同，来自镜像默认，丢弃
    if cv == iv:
        return None
    return cv


def extract_run_config(container: dict, image: dict) -> dict:
    """
    从容器和镜像的 inspect 信息中提取重建容器所需的配置。
    返回符合 Docker API /containers/create 的 body 格式。
    """
    host_cfg = container.get("HostConfig", {})
    container_cfg = container.get("Config", {})
    image_cfg = (image or {}).get("Config", {})

    user_env = diff_env(
        container_cfg.get("Env", []),
        image_cfg.get("Env", []),
    )

    # 差分 Cmd / Entrypoint / 其他 Config：与镜像默认不同才保留
    user_cmd = _diff_config_value(container_cfg, image_cfg, "Cmd")
    user_ep = _diff_config_value(container_cfg, image_cfg, "Entrypoint")
    user_labels = diff_map(container_cfg.get("Labels") or {}, image_cfg.get("Labels") or {})

    # ExposedPorts 只保留用户发布端口对应项，避免旧镜像 EXPOSE 污染新镜像
    exposed_ports = {p: {} for p in (host_cfg.get("PortBindings") or {}).keys()}

    config = {
        "Image": None,
        "Env": user_env,
        "Cmd": user_cmd,
        "Entrypoint": user_ep,
        "Labels": user_labels,
        "ExposedPorts": exposed_ports,
        "User": _diff_config_value(container_cfg, image_cfg, "User"),
        "WorkingDir": _diff_config_value(container_cfg, image_cfg, "WorkingDir"),
        "Tty": container_cfg.get("Tty", False),
        "OpenStdin": container_cfg.get("OpenStdin", False),
        "StdinOnce": container_cfg.get("StdinOnce", False),
        "HostConfig": {
            "Binds":            host_cfg.get("Binds") or [],
            "Mounts":           host_cfg.get("Mounts") or [],
            "PortBindings":     host_cfg.get("PortBindings") or {},
            "PublishAllPorts":  host_cfg.get("PublishAllPorts", False),
            "NetworkMode":      host_cfg.get("NetworkMode", ""),
            "RestartPolicy":    host_cfg.get("RestartPolicy", {"Name": "no", "MaximumRetryCount": 0}),
            "Devices":          host_cfg.get("Devices") or [],
            "CapAdd":           host_cfg.get("CapAdd") or [],
            "CapDrop":          host_cfg.get("CapDrop") or [],
            "Sysctls":          host_cfg.get("Sysctls") or {},
            "ExtraHosts":       host_cfg.get("ExtraHosts") or [],
            "Links":            host_cfg.get("Links") or [],
            "VolumesFrom":      host_cfg.get("VolumesFrom") or [],
            "Dns":              host_cfg.get("Dns") or [],
            "DnsOptions":       host_cfg.get("DnsOptions") or [],
            "DnsSearch":        host_cfg.get("DnsSearch") or [],
            "LogConfig":        host_cfg.get("LogConfig") or {},
            "Privileged":       host_cfg.get("Privileged", False),
            "ReadonlyRootfs":   host_cfg.get("ReadonlyRootfs", False),
            "Tmpfs":            host_cfg.get("Tmpfs") or {},
            "SecurityOpt":      host_cfg.get("SecurityOpt") or [],
            "GroupAdd":         host_cfg.get("GroupAdd") or [],
            "PidMode":          host_cfg.get("PidMode", ""),
            "IpcMode":          host_cfg.get("IpcMode", ""),
            "UsernsMode":       host_cfg.get("UsernsMode", ""),
            "CgroupnsMode":     host_cfg.get("CgroupnsMode", ""),
            "ShmSize":          host_cfg.get("ShmSize", 0),
            "Ulimits":          host_cfg.get("Ulimits") or [],
            "MemorySwappiness": host_cfg.get("MemorySwappiness"),
            "Memory":           host_cfg.get("Memory", 0),
            "NanoCpus":         host_cfg.get("NanoCpus", 0),
            "AutoRemove":       host_cfg.get("AutoRemove", False),
            "Init":             host_cfg.get("Init"),
        },
        "NetworkingConfig": _build_network_config(container),
    }

    return config


def _build_network_config(container: dict) -> dict:
    """提取用户自定义网络的 EndpointsConfig，跳过 bridge/host/none 内置网络。"""
    builtin_networks = {"bridge", "host", "none"}

    networks = container.get("NetworkSettings", {}).get("Networks", {})
    endpoints = {}
    for net_name, net_info in networks.items():
        if net_name in builtin_networks:
            continue
        endpoints[net_name] = {
            "IPAMConfig": net_info.get("IPAMConfig"),
            "Aliases":    net_info.get("Aliases") or [],
        }
    return {"EndpointsConfig": endpoints} if endpoints else {}


# ------------------------------------------
# 日志工具
# ------------------------------------------

def log(msg: str):
    """带时间戳的日志输出"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def sep(title=""):
    print("==========================================", flush=True)
    if title:
        print(f"  {title}", flush=True)
        print("==========================================", flush=True)


# ------------------------------------------
# 核心更新逻辑
# ------------------------------------------

def do_update(client: DockerClient, container_name: str, verbose: bool = False,
              _container: dict = None) -> bool:
    """
    对单个容器执行更新。
    返回 True 表示完成更新，False 表示跳过（镜像无变化或不适合更新），异常时抛出。
    verbose=True 时额外打印差分详情（单次模式使用）。
    _container: 已有的 inspect 结果，避免重复调用。
    """
    container = _container or client.inspect_container(container_name)
    image_name = container["Config"]["Image"]

    # AutoRemove 容器停止后会被 Docker 自动删除，无法正常重建，跳过
    if container.get("HostConfig", {}).get("AutoRemove", False):
        log(f"[warn] Skip {container_name}: AutoRemove=true, cannot recreate")
        return False

    # 获取当前镜像（pull 前），用于差分和 ID 对比
    current_image = client.inspect_image(image_name)
    old_image_id = current_image["Id"] if current_image else None

    if current_image is None:
        log(f"[warn] Current image not found locally, diff may be inaccurate")

    # 提取运行配置（以旧镜像为差分基准，必须在 pull 前完成）
    run_config = extract_run_config(container, current_image)

    if verbose:
        user_env = run_config.get("Env", [])
        user_cmd = run_config.get("Cmd")
        user_ep  = run_config.get("Entrypoint")
        if user_env:
            log("User-defined env vars:")
            for e in user_env:
                print(f"       {e}")
        else:
            log("No user-defined env vars detected")
        if user_cmd:
            log(f"User-defined cmd: {user_cmd}")
        if user_ep:
            log(f"User-defined entrypoint: {user_ep}")

    # 拉取镜像，获取新 ID
    log(f"Pulling image: {image_name}")
    new_image_id = client.pull_image(image_name)

    # 镜像无变化则跳过
    if new_image_id == old_image_id:
        log(f"Image unchanged, skip: {container_name}")
        return False

    log(f"Image updated ({_short_id(old_image_id)} → {_short_id(new_image_id)}), recreating: {container_name}")

    run_config["Image"] = image_name

    # 停止并删除旧容器
    if container["State"]["Running"]:
        log(f"Stopping container: {container_name}")
        client.stop_container(container_name)
    log(f"Removing container: {container_name}")
    client.remove_container(container_name)

    # 创建并启动新容器
    log(f"Creating container: {container_name}")
    cid = client.create_container(container_name, run_config)
    log(f"Starting container: {container_name}")
    client.start_container(cid)

    result = client.inspect_container(container_name)
    log(f"Done: {container_name} → {result['State']['Status']}")
    return True


def _short_id(image_id: str) -> str:
    if not image_id:
        return "unknown"
    # sha256:abc123... → abc123 前12位
    raw = image_id.split(":")[-1]
    return raw[:12]


# ------------------------------------------
# 更新时间窗口
# ------------------------------------------

def parse_update_window(window_str: str):
    """
    解析时间窗口字符串，返回 (start_time, end_time) 的 (hour, minute) 元组对。
    格式：HH:MM-HH:MM，如 "02:00-06:00"。
    解析失败返回 None。
    """
    window_str = window_str.strip()
    if not window_str:
        return None
    try:
        start_str, end_str = window_str.split("-", 1)
        sh, sm = int(start_str.split(":")[0]), int(start_str.split(":")[1])
        eh, em = int(end_str.split(":")[0]),   int(end_str.split(":")[1])
        if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
            raise ValueError("time out of range")
        return (sh, sm), (eh, em)
    except Exception:
        return None


def in_update_window(window) -> bool:
    """
    判断当前时间是否在允许更新的时间窗口内。
    window 为 None 表示不限制，始终返回 True。
    支持跨午夜区间，如 22:00-06:00。
    """
    if window is None:
        return True

    (sh, sm), (eh, em) = window
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    start = sh * 60 + sm
    end   = eh * 60 + em

    if start <= end:
        # 普通区间，如 02:00-06:00
        return start <= cur < end
    else:
        # 跨午夜区间，如 22:00-06:00
        return cur >= start or cur < end


# ------------------------------------------
# 容器列表解析
# ------------------------------------------

def get_self_container_id() -> str:
    """
    读取当前容器的短 ID（前12位）。
    支持 cgroup v1（/proc/self/cgroup）和 cgroup v2（/proc/self/mountinfo）。
    若不在容器内则返回空字符串。
    """
    # Docker 默认 hostname 为容器短 ID，优先使用
    try:
        hostname = socket.gethostname()
        if len(hostname) >= 12 and all(c in "0123456789abcdef" for c in hostname[:12].lower()):
            return hostname[:12]
    except Exception:
        pass

    # cgroup v1: 格式 "12:devices:/docker/<full-id>"
    try:
        with open("/proc/self/cgroup", "r") as f:
            for line in f:
                parts = line.strip().split("/")
                for i, part in enumerate(parts):
                    if part == "docker" and i + 1 < len(parts):
                        cid = parts[i + 1]
                        if len(cid) >= 12:
                            return cid[:12]
    except Exception:
        pass

    # cgroup v2 fallback: 从 /proc/self/mountinfo 中找 overlay 挂载点
    # 格式: "... /var/lib/docker/overlay2/<id>/merged ..."
    try:
        with open("/proc/self/mountinfo", "r") as f:
            for line in f:
                if "/docker/containers/" in line:
                    for part in line.split("/"):
                        if len(part) == 64 and all(c in "0123456789abcdef" for c in part):
                            return part[:12]
    except Exception:
        pass

    return ""


def resolve_containers(client: DockerClient, targets: list) -> list:
    """
    解析最终要操作的容器名列表：
    - targets 非空：直接使用
    - targets 为空：列出所有运行中容器，自动排除自身（避免更新自己导致中断）
    """
    if targets:
        return list(targets)

    self_id = get_self_container_id()
    running = client.list_containers()
    result = []
    for c in running:
        name = c["Names"][0].lstrip("/") if c.get("Names") else ""
        if not name:
            continue
        # 用容器 ID 前12位对比，排除自身
        cid = c.get("Id", "")[:12]
        if self_id and cid == self_id:
            log(f"Skip self: {name}")
            continue
        result.append(name)
    return result


# ------------------------------------------
# 单次更新模式
# ------------------------------------------

def cmd_update(targets: list):
    if not os.path.exists("/var/run/docker.sock"):
        print("[error] /var/run/docker.sock not found")
        sys.exit(1)

    client = DockerClient()
    container_names = resolve_containers(client, targets)

    if not container_names:
        log("No containers to update")
        return

    has_error = False

    for container_name in container_names:
        sep("docker-updater")
        print(f"  Container: {container_name}")
        sep()

        image_name = None
        try:
            container = client.inspect_container(container_name)
            image_name = container["Config"]["Image"]
            log(f"Current image: {image_name}")

            # 传入已有 container 数据，do_update 内不再重复 inspect
            changed = do_update(client, container_name, verbose=True, _container=container)
            if not changed:
                print()
                continue
        except RuntimeError as e:
            log(f"[error] {e}")
            if image_name:
                log(f"[warn] To restore manually: docker run -d --name {container_name} {image_name}")
            has_error = True
            continue

        result = client.inspect_container(container_name)
        print()
        sep("Update Complete!")
        print(f"  Container: {container_name}")
        print(f"  Image:     {image_name}")
        print(f"  Status:    {result['State']['Status']}")
        sep()

    if has_error:
        sys.exit(1)


# ------------------------------------------
# 持久监控模式
# ------------------------------------------

def cmd_watch(targets: list, interval: int, window):
    """
    持久监控模式。
    targets:  指定的容器名列表，为空则每轮动态列出所有运行中容器（排除自身）。
    interval: 检查间隔秒数。
    window:   更新时间窗口，None 表示不限制。
    """
    if not os.path.exists("/var/run/docker.sock"):
        print("[error] /var/run/docker.sock not found")
        sys.exit(1)

    client = DockerClient()

    # 注册 SIGTERM/SIGINT 优雅退出
    stop_event = {"flag": False}
    def _on_signal(signum, frame):
        log(f"Received signal {signum}, stopping...")
        stop_event["flag"] = True
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    sep("docker-updater watch")
    if targets:
        print(f"  Containers: {', '.join(targets)}")
    else:
        print(f"  Containers: all running containers (except self)")
    print(f"  Interval:   {interval}s")
    if window:
        (sh, sm), (eh, em) = window
        print(f"  Window:     {sh:02d}:{sm:02d} - {eh:02d}:{em:02d}")
    else:
        print(f"  Window:     (no restriction)")
    sep()

    while not stop_event["flag"]:
        if not in_update_window(window):
            # 窗口外最多 60 秒检查一次，避免 INTERVAL 很大时错过窗口开始时间
            sleep_seconds = min(interval, 60)
            next_check = time.strftime("%H:%M:%S", time.localtime(time.time() + sleep_seconds))
            log(f"Outside update window, skip. Next check ~{next_check}")
            for _ in range(sleep_seconds):
                if stop_event["flag"]:
                    break
                time.sleep(1)
            continue

        log("--- check start ---")

        try:
            check_list = resolve_containers(client, targets)

            if not check_list:
                log("No containers to check")
            else:
                updated = 0
                skipped = 0
                failed  = 0
                for name in check_list:
                    if stop_event["flag"]:
                        break
                    try:
                        changed = do_update(client, name)
                        if changed:
                            updated += 1
                        else:
                            skipped += 1
                    except RuntimeError as e:
                        log(f"[error] Failed to update {name}: {e}")
                        failed += 1

                log(f"--- check done: updated={updated}, skipped={skipped}, failed={failed} ---")

        except Exception as e:
            log(f"[error] Unexpected error: {e}")

        # 分段 sleep，保持对 stop_event 的响应
        for _ in range(interval):
            if stop_event["flag"]:
                break
            time.sleep(1)

    log("Stopped.")


# ------------------------------------------
# 入口
# ------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docker-updater",
        usage="docker-updater [container...] | docker-updater watch [options] [container...]",
        description="Docker 容器自动更新工具。镜像地址自动从容器配置读取，拉取后对比镜像 ID，有变化才重建。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  docker-updater                              更新所有运行中容器
  docker-updater app1 app2                    更新指定容器
  docker-updater watch                        持续监控所有容器（每小时检查一次）
  docker-updater watch app1 app2              持续监控指定容器
  docker-updater watch --interval 1800        每 30 分钟检查一次
  docker-updater watch --window 02:00-06:00   仅在凌晨 2~6 点更新
  docker-updater watch --window 22:00-06:00   跨午夜时间段
        """.strip(),
    )

    subparsers = parser.add_subparsers(dest="mode")

    # 单次更新子命令（默认）
    once = subparsers.add_parser(
        "once",
        help="单次更新容器（默认模式，可省略）",
        description="单次更新一个或多个容器，不传容器名则更新所有运行中容器（排除自身）。",
    )
    once.add_argument("containers", nargs="*", metavar="CONTAINER", help="容器名，可指定多个，不传则更新所有")

    # 持久监控子命令
    watch = subparsers.add_parser(
        "watch",
        help="持久监控并自动更新容器",
        description="定期拉取镜像并在有更新时重建容器，不传容器名则监控所有运行中容器（排除自身）。",
    )
    watch.add_argument("containers", nargs="*", metavar="CONTAINER", help="容器名，可指定多个，不传则监控所有")
    watch.add_argument(
        "--interval", "-i",
        type=int, default=3600, metavar="SECONDS",
        help="检查间隔秒数（默认 3600）",
    )
    watch.add_argument(
        "--window", "-w",
        default="", metavar="HH:MM-HH:MM",
        help="允许更新的时间段，如 02:00-06:00；支持跨午夜；不传则不限制",
    )

    return parser


def main():
    parser = _build_parser()

    # 兼容直接传容器名（不写 once 子命令）：将第一个非 watch/once 参数视为 once 模式
    raw = sys.argv[1:]
    if raw and raw[0] not in ("watch", "once", "--help", "-h"):
        raw = ["once"] + raw

    args = parser.parse_args(raw)

    # 未传任何子命令（如直接运行 docker-updater）→ once 模式、更新所有
    if args.mode is None:
        args.mode = "once"
        args.containers = []

    if args.mode == "watch":
        if args.interval <= 0:
            parser.error("--interval must be a positive integer")

        window = None
        if args.window:
            window = parse_update_window(args.window)
            if window is None:
                parser.error(f"--window invalid: {args.window!r}  (expected HH:MM-HH:MM)")

        cmd_watch(args.containers, args.interval, window)

    else:
        cmd_update(args.containers)


if __name__ == "__main__":
    main()
