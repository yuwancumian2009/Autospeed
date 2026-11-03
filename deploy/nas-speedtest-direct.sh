#!/bin/sh
# 测速容器直连策略路由
#
# 目的：autospeed 容器的所有测速流量必须走主路由 192.168.1.1，不经过
# 192.168.1.10 上的 clash 透明代理，避免被「漏网之鱼」规则代理到境外节点
# （那样测出来的就不是家里宽带的速度了）。
#
# 为什么不用 clash 的 SRC-IP 规则：
#   容器跑在 docker bridge 上，出站会被 NAT 成 NAS 主机 IP 192.168.1.109。
#   在路由器 conntrack 里实测「来自 192.168.16.x 的连接数 = 0」，
#   说明 clash 根本看不到容器 IP，写 SRC-IP-CIDR,192.168.16.0/20 不会命中。
#   而把整个 192.168.1.109 设成直连会连累 MoviePilot 等依赖代理的服务。
#   所以在主机侧按源网段打 fwmark，用独立路由表分流。
#
# 参考同机已有的 /usr/local/bin/nas-bt-policy.sh 写法。
LOG=/var/log/nas-speedtest-direct.log
log(){ echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null; }

TABLE_ID=102
MARK_HEX=0x2
MARK_DEC=2
DEV=enp4s0-ovs
GW=192.168.1.1

# 网段动态取，容器网络重建后子网可能变，别写死
SUBNET=$(docker network inspect speedtest-web_default \
  --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null)
[ -z "$SUBNET" ] && SUBNET=192.168.16.0/20

ip route replace default via "$GW" dev "$DEV" table "$TABLE_ID" 2>/dev/null
ip rule add fwmark "$MARK_HEX" table "$TABLE_ID" priority 45 2>/dev/null || true

iptables -t mangle -C PREROUTING -s "$SUBNET" -j MARK --set-mark "$MARK_DEC" 2>/dev/null || \
  iptables -t mangle -A PREROUTING -s "$SUBNET" -j MARK --set-mark "$MARK_DEC"

log "applied: subnet=$SUBNET mark=$MARK_HEX table=$TABLE_ID gw=$GW dev=$DEV"
echo "subnet=$SUBNET mark=$MARK_HEX table=$TABLE_ID"
