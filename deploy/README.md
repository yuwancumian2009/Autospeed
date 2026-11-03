# 部署配套：让测速流量绕开透明代理

## 问题

NAS 上的默认路由指向 `192.168.1.10`（跑 clash 的软路由）。容器出站被 docker
NAT 成 NAS 主机 IP，clash 侧看到的源 IP 全是 `192.168.1.109`，于是境外测速
节点走「漏网之鱼」被代理到海外节点 —— 测出来的是代理的吞吐，不是家里宽带。

实测证据（改造前）：

```
容器内 api.ipify.org  → 13.250.252.3   （AWS 新加坡，被代理）
容器内 ip.3322.net    → 121.17.207.32  （家庭宽带，直连）
路由器 conntrack 里来自 192.168.16.x 的连接数 = 0
```

## 为什么不在 clash 里写规则

`SRC-IP-CIDR,192.168.16.0/20,DIRECT` 不会命中：容器流量到 clash 时源 IP 已被
NAT 成 `192.168.1.109`。而把整个 `192.168.1.109` 设为直连会连累 MoviePilot
等依赖代理的服务。

## 做法

主机侧按源网段打 fwmark，用独立路由表把这份流量送去主路由 `192.168.1.1`，
根本不进 clash。与同机已有的 `nas-bt-policy.sh` 是同一套路。

```sh
install -m 755 deploy/nas-speedtest-direct.sh /usr/local/bin/
install -m 644 deploy/nas-speedtest-direct.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now nas-speedtest-direct
```

## 验证

```sh
# 规则就位
ip rule list | grep 0x2              # from all fwmark 0x2 lookup 102
ip route show table 102              # default via 192.168.1.1 dev enp4s0-ovs

# 出口已是家庭 IP，境外站点直连不通（说明确实没走代理）
docker exec autospeed curl -s https://api.ipify.org        # 空/失败
docker exec autospeed curl -s http://ip.3322.net           # 121.17.207.32

# Ookla 选点会变：被代理时选香港，直连后按真实 IP 归属选点
```

注意：绕开代理后 Ookla 的测速节点从香港变为蒙古（`121.17.207.32` 的 IP 库
归属误判），国际路由质量差时上下行数字会波动，这是线路实况。
