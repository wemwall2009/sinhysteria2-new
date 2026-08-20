#!/usr/bin/env bash
set -euo pipefail

PANEL_DIR=/opt/sbox-panel
PANEL_CONFIG=/etc/sbox-panel/config.json
SINGBOX_CONFIG=${SINGBOX_CONFIG:-/root/sbox/sbconfig_server.json}
SINGBOX_BINARY=${SINGBOX_BINARY:-/root/sbox/sing-box}
PANEL_PORT=${PANEL_PORT:-2095}
PANEL_USER=${PANEL_USER:-admin}
PANEL_REPOSITORY=${PANEL_REPOSITORY:-wemwall2009/sing-box-reality-hysteria2}
PANEL_BRANCH=${PANEL_BRANCH:-main}

if [[ ${EUID} -ne 0 ]]; then
  echo "请使用 root 运行此脚本" >&2
  exit 1
fi

if [[ ! -x ${SINGBOX_BINARY} || ! -f ${SINGBOX_CONFIG} ]]; then
  echo "未找到现有 sing-box 安装：${SINGBOX_BINARY} / ${SINGBOX_CONFIG}" >&2
  echo "请先运行项目 install.sh 或设置 SINGBOX_BINARY、SINGBOX_CONFIG。" >&2
  exit 1
fi

case "$(. /etc/os-release && echo "${ID}")" in
  ubuntu|debian)
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 qrencode iptables openssl curl
    ;;
  centos|rhel|rocky|almalinux|fedora)
    if command -v dnf >/dev/null; then
      dnf install -y python3 qrencode iptables openssl curl
    else
      yum install -y python3 qrencode iptables openssl curl
    fi
    ;;
  *)
    echo "当前安装器仅支持 Debian/Ubuntu/RHEL 系发行版" >&2
    exit 1
    ;;
esac

if [[ -z ${PANEL_PASSWORD:-} ]]; then
  if [[ -t 0 ]]; then
    read -r -s -p "设置面板管理员密码: " PANEL_PASSWORD
    echo
    read -r -s -p "再次输入密码: " password_confirm
    echo
    [[ ${PANEL_PASSWORD} == "${password_confirm}" ]] || { echo "两次密码不一致" >&2; exit 1; }
  else
    echo "非交互安装必须设置 PANEL_PASSWORD 环境变量" >&2
    exit 1
  fi
fi

if [[ ${#PANEL_PASSWORD} -lt 10 ]]; then
  echo "管理员密码至少需要 10 个字符" >&2
  exit 1
fi

if [[ -z ${SERVER_ADDRESS:-} ]]; then
  SERVER_ADDRESS=$(curl -4fsS --max-time 5 https://api4.ipify.org || true)
fi
if [[ -z ${SERVER_ADDRESS:-} ]]; then
  SERVER_ADDRESS=$(curl -6fsS --max-time 5 https://api6.ipify.org || true)
fi
[[ -n ${SERVER_ADDRESS:-} ]] || { echo "无法确定服务器公网地址，请设置 SERVER_ADDRESS" >&2; exit 1; }

if [[ -z ${HYSTERIA_SERVER_NAME:-} && -f /root/sbox/config ]]; then
  HYSTERIA_SERVER_NAME=$(sed -n "s/^HY_SERVER_NAME='\([^']*\)'.*/\1/p" /root/sbox/config | head -n 1)
fi
HYSTERIA_SERVER_NAME=${HYSTERIA_SERVER_NAME:-bing.com}

certificate_dir=/etc/sbox-panel/cert
mkdir -p "${certificate_dir}" /var/lib/sbox-panel "${PANEL_DIR}"
chmod 700 /etc/sbox-panel /var/lib/sbox-panel

if [[ -n ${PANEL_CERT_FILE:-} && -n ${PANEL_KEY_FILE:-} ]]; then
  cert_file=${PANEL_CERT_FILE}
  key_file=${PANEL_KEY_FILE}
else
  cert_file=${certificate_dir}/panel.crt
  key_file=${certificate_dir}/panel.key
  if [[ ! -s ${cert_file} || ! -s ${key_file} ]]; then
    if [[ ${SERVER_ADDRESS} =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ || ${SERVER_ADDRESS} == *:* ]]; then
      certificate_san="IP:${SERVER_ADDRESS}"
    else
      certificate_san="DNS:${SERVER_ADDRESS}"
    fi
    openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 \
      -keyout "${key_file}" -out "${cert_file}" -subj "/CN=${SERVER_ADDRESS}" \
      -addext "subjectAltName=${certificate_san}" >/dev/null 2>&1 || \
    openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 \
      -keyout "${key_file}" -out "${cert_file}" -subj "/CN=${SERVER_ADDRESS}" >/dev/null 2>&1
  fi
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_dir=${script_dir}
download_dir=""
if [[ ! -s ${source_dir}/sbox_panel.py || ! -s ${source_dir}/static/index.html || ! -s ${source_dir}/static/app.js ]]; then
  download_dir=$(mktemp -d)
  trap 'rm -rf "${download_dir}"' EXIT
  source_dir=${download_dir}
  mkdir -p "${source_dir}/static"
  raw_base="https://raw.githubusercontent.com/${PANEL_REPOSITORY}/${PANEL_BRANCH}/panel"
  curl -fsSL "${raw_base}/sbox_panel.py" -o "${source_dir}/sbox_panel.py"
  curl -fsSL "${raw_base}/static/index.html" -o "${source_dir}/static/index.html"
  curl -fsSL "${raw_base}/static/app.js" -o "${source_dir}/static/app.js"
fi

install -m 755 "${source_dir}/sbox_panel.py" "${PANEL_DIR}/sbox_panel.py"
install -d -m 755 "${PANEL_DIR}/static"
install -m 644 "${source_dir}/static/index.html" "${PANEL_DIR}/static/index.html"
install -m 644 "${source_dir}/static/app.js" "${PANEL_DIR}/static/app.js"

if [[ -f ${PANEL_CONFIG} ]]; then
  cp -a "${PANEL_CONFIG}" "${PANEL_CONFIG}.backup.$(date +%s)"
fi

python3 "${PANEL_DIR}/sbox_panel.py" init \
  --config "${PANEL_CONFIG}" --username "${PANEL_USER}" --password "${PANEL_PASSWORD}" \
  --port "${PANEL_PORT}" --cert-file "${cert_file}" --key-file "${key_file}" \
  --singbox-config "${SINGBOX_CONFIG}" --singbox-binary "${SINGBOX_BINARY}" \
  --server-address "${SERVER_ADDRESS}" --hysteria-server-name "${HYSTERIA_SERVER_NAME}" --force

cat > /etc/systemd/system/sbox-panel.service <<'EOF'
[Unit]
Description=sing-box Node Management Panel
After=network-online.target sing-box.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/sbox-panel
ExecStart=/usr/bin/python3 /opt/sbox-panel/sbox_panel.py serve --config /etc/sbox-panel/config.json
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=read-only
ProtectSystem=strict
ReadWritePaths=/etc/sbox-panel /var/lib/sbox-panel /root/sbox /run /var/run
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now sbox-panel
sleep 2
systemctl is-active --quiet sbox-panel || {
  journalctl -u sbox-panel -n 50 --no-pager
  exit 1
}

echo
echo "sing-box 面板安装完成"
echo "访问地址: https://${SERVER_ADDRESS}:${PANEL_PORT}/"
echo "管理员: ${PANEL_USER}"
echo "提示: 默认使用自签名 HTTPS 证书，浏览器首次访问会显示证书警告。"
echo "如服务器启用了云安全组，请放行 TCP ${PANEL_PORT} 和面板创建的节点端口范围。"
