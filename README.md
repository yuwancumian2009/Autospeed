# Autospeed · 家庭宽带测速面板

定时跑 Ookla Speedtest，记录下行 / **上行** / 延迟，推送到企业微信，并在网页上看历史曲线。

Vue3 + Element Plus 前端，Flask + APScheduler 后端，SQLite 存储，单容器部署。

## 功能

- **三种测速后端**
  - `Ookla`（推荐）— 官方 CLI，下行 / 上行 / 延迟三项齐全，节点池由 Ookla 下发
  - `国内直连` — 下行走国内镜像源，上行由 Ookla 兜底补测（国内无可用公开上传端点）
  - `LibreSpeed` — 自建或可用的 LibreSpeed 实例
- **五种节点策略**
  - 按地区（10 个预置地区，带实时可用节点数，无节点的自动置灰）
  - 自动：就近节点
  - 自动：仅国内节点
  - 固定：指定节点 ID
  - 关键词：按名称 / 地区匹配
- **节点健康表** — 记录每个节点的成功 / 失败次数与最近延迟，坏节点自动拉黑跳过
- **定时调度** — 标准 5 段 Cron，可在页面上改，支持暂停
- **企业微信推送** — 成功 / 失败分别可控，支持 forwarding proxy
- **历史与图表** — 分页表格、趋势曲线、CSV 导出、按天数自动清理
- **访问令牌** — 设置后全站需 `?token=` 或 cookie；密钥类配置永不回传前端

## 快速开始

```bash
docker compose up -d
```

打开 `http://<宿主机IP>:5000`。

### docker-compose.yml

默认用**端口映射**方式，直接通过宿主机 IP + 端口访问，无需 macvlan：

```yaml
services:
  autospeed:
    image: yuwancumian2009/autospeed:latest
    container_name: autospeed
    restart: always
    ports:
      - "5000:5000"
    volumes:
      - ./data:/app/data
      - ./ookla-config:/root/.config/ookla
      - /etc/localtime:/etc/localtime:ro
    environment:
      - TZ=Asia/Shanghai
```

> **为什么不用 macvlan**：macvlan 需要给容器单独分配一个局域网 IP，还要在宿主机上
> 配置对应网段和物理网卡，换机器就容易失效。端口映射用宿主机 IP + 端口访问，
> 更简单也更不容易出错。

### 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `TZ` | 时区，影响定时任务和记录时间 | `Asia/Shanghai` |

## 配置项

首次启动会自动建库并写入默认值，全部可在「系统」页修改。

| 键 | 说明 | 默认 |
|---|---|---|
| `backend` | 测速后端：`ookla` / `http` / `librespeed` | `ookla` |
| `mode` | 节点策略：`closest` / `cn` / `region` / `fixed` / `keyword` | `closest` |
| `server_region` | `region` 模式用，地区代号 | `asia` |
| `server_id` | `fixed` 模式用的节点 ID | 空 |
| `server_keyword` | `keyword` 模式用的关键词 | 空 |
| `cron` | 5 段标准 Cron | `0 */6 * * *` |
| `cron_enabled` | 定时任务开关 | `1` |
| `strict_mode` | 严格模式：节点不符合预期直接失败告警（关闭则降级继续） | `0` |
| `expected_country` | 期望节点国家，`strict_mode` 开启时生效 | 空 |
| `notify_on_success` | 成功也推送 | `1` |
| `notify_on_fail` | 失败推送 | `1` |
| `retention_days` | 历史保留天数，`0` 表示不清理 | `90` |
| `speed_alert_below` | 下行低于此值时告警（Mbps） | `50` |
| `auth_token` | 访问令牌，留空则免登录 | 空 |
| `wecom_corpid` | 企业微信企业 ID | 空 |
| `wecom_secret` | 企业微信应用 Secret | 空 |
| `wecom_agentid` | 企业微信应用 AgentId | 空 |
| `wecom_proxy` | 企业微信 forwarding proxy | 空 |
| `external_url` | 推送里附带的面板外网地址 | 空 |

## 企业微信推送

用的不是标准 HTTP CONNECT 代理。若填了 `wecom_proxy`，调用时会把
`https://qyapi.weixin.qq.com` 换成代理地址、路径保持不变：

```
http://<proxy-host>:<port>/cgi-bin/gettoken?...
```

代理调用失败会自动回退直连 `qyapi.weixin.qq.com` 并记录日志。

## 关于上行测速

**国内没有任何可用的公开上传端点。** 实测过的全部失败：

| 类型 | 结果 |
|---|---|
| LibreSpeed 实例（江苏电信 / 南大 / 浙大 / 中科大 / 重大 / 清华 / 北邮 / 华科 / 深大） | 全部不可用 |
| 阿里云 OSS / 腾讯 COS / 七牛 | 404 / 400 |
| 测速网 API | DNS 解析失败 |
| Cloudflare `__up` | 唯一能通，但仅 0.62–3.03 Mbps，走国际出口会严重误导 |

所以**默认后端必须是 `ookla`**。选「国内直连」时下行走国内镜像，上行仍由 Ookla
兜底补测，并在记录里用 `upload_source` 标明来源。

## 已知限制

- Ookla 下发的节点池较小（实测 10 个左右），且会抖动；坏节点自动拉黑跳过。
- Ookla 返回的 `country` 是**国家全称**（`China` / `South Korea` / `Mongolia`），
  不是 ISO 码，代码里用 `COUNTRY_ALIASES` 统一归一。
- 若本地无可用节点，就近策略可能落到境外节点，此时数据反映的是**国际出口带宽**，
  不是本地宽带的理论上限。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 面板首页 |
| GET | `/healthz` | 健康检查（免鉴权） |
| GET | `/api/boot` | 首屏数据（设置 + 最近记录 + 下次定时） |
| GET | `/api/servers` | 可用节点列表 |
| GET | `/api/backends` | 后端列表 |
| GET | `/api/node_health` | 节点健康表 |
| GET | `/api/history` | 按时间范围的历史 |
| GET | `/api/results` | 分页记录 |
| POST | `/api/run` | 手动触发一次测速 |
| GET | `/api/job/<id>` | 任务状态 |
| POST | `/api/settings` | 修改设置 |
| POST | `/api/test_wechat` | 测试企业微信推送 |
| POST | `/api/selftest` | 自检 |
| POST | `/api/prune` | 按保留天数清理历史 |
| GET | `/api/export.csv` | 导出 CSV |
| GET | `/chart.png` | 趋势图 PNG |

## 发布镜像

`.github/workflows/release.yml` 在**发布 Release** 时自动构建并推送到 Docker Hub
`yuwancumian2009/autospeed`（需仓库 secrets `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`）。

```bash
git tag v1.1.0 && git push origin v1.1.0
gh release create v1.1.0 --generate-notes
```

## 许可

个人自用项目。
