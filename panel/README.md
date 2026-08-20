# sing-box 节点管理面板

该面板为本项目现有的 Reality / Hysteria2 一键脚本提供多客户管理：

- 创建独立的 VLESS Reality 或 Hysteria2 节点
- 设置到期时间和总流量额度
- 达到时间或流量上限后自动从 sing-box 运行配置停用
- 查看累计流量、实时速度和最近在线状态
- 启用、关闭、编辑、重置流量、删除客户
- 导出节点链接和本地生成的二维码
- sing-box 配置写入前自动执行 `sing-box check`，失败时回滚

## 安装

先使用仓库根目录的 `install.sh` 安装 Reality + Hysteria2，然后运行：

```bash
cd panel
chmod +x install-panel.sh
./install-panel.sh
```

自动化安装：

```bash
PANEL_USER=admin \
PANEL_PASSWORD='change-this-password' \
PANEL_PORT=2095 \
SERVER_ADDRESS=203.0.113.10 \
./install-panel.sh
```

安装器默认尝试使用 Let’s Encrypt 为公网 IP/域名申请受信任证书。申请需要 TCP 80 空闲且云安全组已放行；申请失败时自动回退为自签名证书。可显式关闭：

```bash
PANEL_LETSENCRYPT=false ./install-panel.sh
```

已有受信任证书时，可以传入：

```bash
PANEL_CERT_FILE=/path/fullchain.pem \
PANEL_KEY_FILE=/path/privkey.pem \
./install-panel.sh
```

## 工作方式

面板为每个客户创建独立入站端口，并以 `iptables` / `ip6tables` 计数器统计该端口的上下行流量。计数规则只统计流量，不改变防火墙的放行策略。面板每 10 秒把增量写入 SQLite；达到配额或到期后，从运行配置中移除对应入站并热重载 sing-box。

面板数据保存在 `/var/lib/sbox-panel/panel.db`，配置位于 `/etc/sbox-panel/config.json`。每次修改 sing-box 配置前都会备份到 `/root/sbox/sbconfig_server.json.panel-backup`。

## 注意

- 云厂商安全组必须允许面板创建的节点端口，默认范围为 `20000-50000`。
- Let’s Encrypt IP 证书是短周期证书，安装器会配置 acme.sh 自动续期；生产环境仍建议使用域名证书或受信任的反向代理。
- Hysteria2 统计依赖服务器系统的 `iptables` 兼容层；安装器会自动安装。
