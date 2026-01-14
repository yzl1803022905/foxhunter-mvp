#!/bin/bash
# FoxHunter MVP - Ubuntu 22.04 一键部署脚本

set -e  # 遇到错误立即退出

echo "=================================================="
echo "  FoxHunter MVP Ubuntu 22.04 自动部署脚本"
echo "=================================================="

# 颜色输出
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 1. 更新系统并安装基础软件
log_info "步骤 1/8: 更新系统包..."
sudo apt update
sudo apt upgrade -y

log_info "步骤 2/8: 安装基础工具..."
sudo apt install -y python3 python3-pip git curl wget screen

# 2. 安装 Docker
log_info "步骤 3/8: 安装 Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    rm get-docker.sh
    log_warn "Docker 已安装，需要重新登录才能免 sudo 使用 docker"
else
    log_info "Docker 已安装，跳过"
fi

# 3. 启动 TimescaleDB
log_info "步骤 4/8: 启动 TimescaleDB 容器..."
if sudo docker ps -a | grep -q radio_db; then
    log_warn "检测到已存在的 radio_db 容器，停止并删除..."
    sudo docker stop radio_db 2>/dev/null || true
    sudo docker rm radio_db 2>/dev/null || true
fi

sudo docker run -d --name radio_db \
  -e POSTGRES_PASSWORD=password \
  -e POSTGRES_DB=foxhunter \
  -p 5432:5432 \
  -v /opt/radio_db_data:/var/lib/postgresql/data \
  --restart unless-stopped \
  timescale/timescaledb:latest-pg14

log_info "等待数据库启动（10秒）..."
sleep 10

# 4. 创建数据库表
log_info "步骤 5/8: 创建数据库表..."
sudo docker exec radio_db psql -U postgres -d foxhunter -c "
CREATE TABLE IF NOT EXISTS hfdl_logs (
  id SERIAL PRIMARY KEY,
  time TIMESTAMPTZ NOT NULL,
  frequency INTEGER NOT NULL,
  station_id TEXT NOT NULL,
  flight_id TEXT,
  aircraft_reg TEXT,
  lat DOUBLE PRECISION,
  lon DOUBLE PRECISION,
  message JSONB,
  snr DOUBLE PRECISION
);
" && log_info "数据库表创建成功" || log_error "数据库表创建失败"

# 5. 安装 dumphfdl 编译依赖
log_info "步骤 6/8: 安装 dumphfdl 编译依赖..."
sudo apt install -y build-essential cmake \
  libglib2.0-dev libliquid-dev libfftw3-dev \
  libconfig-dev libjansson-dev libsndfile1-dev

# 6. 编译安装 dumphfdl
log_info "步骤 7/8: 编译安装 dumphfdl..."
if ! command -v dumphfdl &> /dev/null; then
    cd /tmp
    if [ ! -d "dumphfdl" ]; then
        git clone https://github.com/szpajder/dumphfdl.git
    fi
    cd dumphfdl
    mkdir -p build && cd build
    cmake ..
    make -j$(nproc)
    sudo make install
    sudo ldconfig
    
    # 验证安装
    if dumphfdl --version &> /dev/null; then
        log_info "dumphfdl 安装成功: $(dumphfdl --version | head -1)"
    else
        log_error "dumphfdl 安装失败"
        exit 1
    fi
else
    log_info "dumphfdl 已安装，跳过"
fi

# 7. 克隆项目
log_info "步骤 8/8: 克隆 FoxHunter 项目..."
cd ~
if [ ! -d "foxhunter-mvp" ]; then
    git clone --recursive https://github.com/yzl1803022905/foxhunter-mvp.git
    cd foxhunter-mvp
else
    log_warn "项目目录已存在，拉取最新代码..."
    cd foxhunter-mvp
    git pull
    git submodule update --init --recursive
fi

# 8. 安装 Python 依赖
log_info "安装 Python 依赖..."
pip3 install psycopg2-binary numpy

# 9. 测试数据库连接
log_info "测试数据库连接..."
python3 -c "import psycopg2; conn = psycopg2.connect(host='localhost', port=5432, user='postgres', password='password', dbname='foxhunter'); print('✅ 数据库连接成功'); conn.close()" || {
    log_error "数据库连接测试失败，请检查 Docker 容器状态"
    exit 1
}

# 10. 创建 systemd 服务
log_info "创建 systemd 服务..."
SERVICE_FILE="/etc/systemd/system/foxhunter.service"
sudo tee $SERVICE_FILE > /dev/null <<EOF
[Unit]
Description=FoxHunter HFDL Monitor
After=network.target docker.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/foxhunter-mvp
ExecStart=/usr/bin/python3 $HOME/foxhunter-mvp/mvp_hunter.py
Restart=on-failure
RestartSec=10
Environment="PATH=/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable foxhunter

echo ""
echo "=================================================="
echo -e "${GREEN}✅ 部署完成！${NC}"
echo "=================================================="
echo ""
echo "下一步操作："
echo ""
echo "1. 启动服务："
echo "   sudo systemctl start foxhunter"
echo ""
echo "2. 查看运行日志："
echo "   sudo journalctl -u foxhunter -f"
echo ""
echo "3. 查看数据库内容："
echo "   sudo docker exec radio_db psql -U postgres -d foxhunter -c 'SELECT COUNT(*) FROM hfdl_logs;'"
echo ""
echo "4. 手动运行（用于测试）："
echo "   cd ~/foxhunter-mvp"
echo "   python3 mvp_hunter.py"
echo ""
echo "=================================================="
