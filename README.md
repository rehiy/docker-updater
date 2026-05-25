# docker-updater

Docker 容器自动更新工具。

镜像地址自动从容器当前配置读取，拉取后对比镜像 ID，有变化才重建容器，无变化则跳过。重建时精确还原容器的运行参数（端口、卷、网络、环境变量等），并通过对比新旧镜像的默认值，避免 Dockerfile 中的默认配置污染新容器。

## 使用方式

### 单次更新

```bash
# 更新所有运行中容器（自动排除自身）
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  rehiy/docker-updater

# 更新指定容器
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  rehiy/docker-updater app1 app2
```

### 持久监控模式

```bash
# 持续监控所有容器，每小时检查一次
docker run -d --name docker-updater \
  -v /var/run/docker.sock:/var/run/docker.sock \
  rehiy/docker-updater watch

# 持续监控指定容器，每 30 分钟检查一次
docker run -d --name docker-updater \
  -v /var/run/docker.sock:/var/run/docker.sock \
  rehiy/docker-updater watch --interval 1800 app1 app2

# 限定更新时间段（仅在凌晨 2~6 点更新）
docker run -d --name docker-updater \
  -v /var/run/docker.sock:/var/run/docker.sock \
  rehiy/docker-updater watch --interval 3600 --window 02:00-06:00

# 跨午夜时间段（22:00 到次日 06:00）
docker run -d --name docker-updater \
  -v /var/run/docker.sock:/var/run/docker.sock \
  rehiy/docker-updater watch --window 22:00-06:00
```

## 参数说明

```
docker-updater [container...]
docker-updater watch [--interval SECONDS] [--window HH:MM-HH:MM] [container...]
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `container...` | 容器名，可指定多个；不传则操作所有运行中容器（排除自身） | 全部容器 |
| `watch` | 进入持久监控模式，定期检查并更新 | — |
| `--interval / -i` | watch 模式检查间隔秒数 | `3600` |
| `--window / -w` | 允许更新的时间段，格式 `HH:MM-HH:MM`；支持跨午夜 | 不限制 |

## 工作原理

1. **拉取镜像** — 强制执行 `docker pull`，获取最新镜像 ID
2. **对比 ID** — 新旧镜像 ID 相同则跳过，有变化才继续
3. **提取配置** — 从旧容器还原运行参数，通过差分过滤掉来自 Dockerfile 的默认值：
   - `Env`：只保留用户显式传入或覆盖的变量
   - `Cmd` / `Entrypoint`：只保留与镜像默认值不同的部分
   - `Labels`：只保留用户自定义的标签
   - `HostConfig`：完整保留（端口、卷、网络、Restart 策略等）
4. **重建容器** — 停止旧容器 → 删除 → 用提取的配置创建并启动新容器

## 注意事项

- **`--rm` 容器**（`AutoRemove=true`）会被跳过，因为它停止后会被 Docker 自动删除，无法重建
- 持久监控模式下，updater 自身容器会被自动排除，不会更新自己
- 直接通过 `/var/run/docker.sock` 调用 Docker API，**无需安装 docker CLI**，也不依赖任何第三方库

## 构建镜像

```bash
# 在项目根目录执行
docker build -f build/docker-updater/Dockerfile -t rehiy/docker-updater .
```
