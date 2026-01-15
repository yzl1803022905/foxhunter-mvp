import subprocess
import json
import time
import os
import datetime
import random
import sys
import psycopg2
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ================= 配置区域 =================

# 1. 数据库配置 (对应您之前 Docker 启动的参数)
DB_CONFIG = {
    "dbname": "foxhunter",
    "user": "postgres",
    "password": "password",
    "host": "localhost",
    "port": "5432"
}

# 2. 目标频率清单 (HFDL 常用频率，单位 Hz)
# 21982kHz(巴林/中东), 13312kHz(全球通用), 17919kHz(旧金山), 11312kHz(大西洋)
TARGET_FREQS = [21982000, 13312000, 17919000, 11312000]

# 3. KiwiSDR 节点池 (!!! 请务必修改这里 !!!)
# 去 rx.kiwisdr.com 找几个绿色的、欧洲或中东的节点 IP 填进去
# 格式示例: {"host": "sdr.hu", "port": 8073}
# (精选欧洲节点，长期在线)
KIWI_NODES = [
    # --- 英国 (Hack Green 核掩体，非常著名的节点) ---
    {"host": "hackgreensdr.org", "port": 8073},
    
    # --- 瑞典 (SK3W 竞赛台，北欧接收效果极佳) ---
    {"host": "sk3w.se", "port": 8073},
    
    # --- 德国 (Bavaria / 拜仁州) ---
    {"host": "sdr-bayern.spdns.de", "port": 8073},
    
    # --- 荷兰 (Zierikzee，低地国家接收 HFDL 很好) ---
    {"host": "21886.proxy.kiwisdr.com", "port": 8073},
]

# ================= 核心功能函数 =================

def get_db_connection():
    """获取数据库连接"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except Exception as e:
        print(f"[!] 数据库连接失败: {e}")
        print("    请检查 Docker 容器 'radio_db' 是否正在运行？")
        return None

def record_audio(host, port, freq, duration=15):
    """
    调用 kiwiclient 录制音频 (Windows 适配版)
    """
    timestamp = int(time.time())
    # 基础文件名
    base_filename = f"rec_{timestamp}_{freq}"
    # kiwiclient 会自动加上 _{freq_khz}_usb.wav 后缀
    expected_filename = os.path.join(BASE_DIR, f"{base_filename}_{int(freq/1000)}_usb.wav")

    print(f"[*] [{host}] 正在监听 {freq/1000} kHz ...")

    # 使用当前 Python 解释器执行 kiwiclient
    python_cmd = sys.executable

    # 检查 kiwiclient 路径
    recorder_path = os.path.join(BASE_DIR, "kiwiclient", "kiwirecorder.py")
    if not os.path.exists(recorder_path):
        print(f"[!] 错误: 找不到 {recorder_path}，请确认 kiwiclient 文件夹和脚本存在")
        return None

    cmd = [
        python_cmd, recorder_path,
        "-s", host,
        "-p", str(port),
        "-f", str(freq / 1000),  # 转换成 kHz
        "-m", "usb",             # HFDL 必须用 USB 模式
        "--station=FoxHunter",   # 伪装 ID
        "--tlimit", str(duration),
        "--filename", base_filename,
        "--resample", "16000",   # 重采样适合解码
        "--connect-timeout", "30",  # 连接超时 30 秒
        "--socket-timeout", "20",   # 数据传输超时 20 秒
        "--quiet"
    ]

    try:
        # 执行录音
        result = subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=BASE_DIR)
        
        if os.path.exists(expected_filename):
            return expected_filename
        else:
            print(f"[-] 录音文件未生成 (可能是节点忙或离线)")
            return None
    except subprocess.CalledProcessError as e:
        # 打印详细错误信息帮助调试
        if e.stderr:
            stderr_msg = e.stderr.decode('utf-8', errors='ignore')[:200]
            print(f"[-] 录音脚本出错: {stderr_msg}")
        else:
            print(f"[-] 录音脚本连接超时或出错")
        return None
    except FileNotFoundError:
        print(f"[!] 系统找不到 'python' 命令，请检查 Python 环境变量")
        return None

def decode_audio_docker(wav_path):
    """
    Windows Docker 专用解码函数
    通过 Docker 调用 dumphfdl 镜像处理本地文件
    """
    if not wav_path:
        return []

    print(f"[*] 正在调用 Docker 解码...")
    
    # 获取绝对路径 (Windows Docker 挂载必须用绝对路径)
    abs_path = os.path.abspath(wav_path)
    dir_path = os.path.dirname(abs_path)
    file_name = os.path.basename(abs_path)

    # 拼接 Docker 命令
    # -v 挂载: 将本机的 wav 所在目录挂载到容器内的 /data
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{dir_path}:/data",
        "ghcr.io/sdr-enthusiasts/docker-dumphfdl",
        "--output", "json",
        f"/data/{file_name}"  # 让容器读取挂载进去的文件
    ]

    try:
        # 运行 Docker 命令并捕获输出
        # Windows 上 capture_output 需要 Python 3.7+
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        
        msgs = []
        # 逐行解析输出
        for line in result.stdout.splitlines():
            try:
                # Docker 有时会输出非 JSON 的日志，跳过它们
                if not line.strip().startswith("{"):
                    continue
                    
                data = json.loads(line)
                # 只有包含 hfdl 协议数据的才是有效包
                if "hfdl" in data:
                    msgs.append(data)
            except json.JSONDecodeError:
                continue
        
        return msgs

    except FileNotFoundError:
        print("[!] 错误: 找不到 'docker' 命令。请确保已安装 Docker Desktop 并添加到 Path 环境变量")
        return []
    except Exception as e:
        print(f"[!] Docker 解码过程出错: {e}")
        return []

def decode_audio_native(wav_path, freq):
    """
    Linux 服务器版：直接调用本地 dumphfdl 可执行文件解码
    适用于已安装 dumphfdl 的环境
    
    Args:
        wav_path: WAV 文件路径
        freq: 频率（Hz）
    """
    if not wav_path:
        return []

    print(f"[*] 正在调用 dumphfdl 解码...")
    
    # dumphfdl 从 WAV 文件解码的命令格式
    # 需要指定频率（单位：kHz）
    freq_khz = int(freq / 1000)
    cmd = [
        "dumphfdl",
        "--iq-file", wav_path,
        "--centerfreq", str(freq_khz),  # 中心频率（kHz）
        str(freq_khz),  # 监听频率（kHz）
        "--output", "decoded:json:file:path=-"  # 输出到 stdout
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', timeout=30)
        
        msgs = []
        for line in result.stdout.splitlines():
            try:
                if not line.strip().startswith("{"):
                    continue
                data = json.loads(line)
                if "hfdl" in data:
                    msgs.append(data)
            except json.JSONDecodeError:
                continue
        
        # 如果有错误输出，打印出来帮助调试
        if result.stderr and not msgs:
            print(f"[DEBUG] dumphfdl stderr: {result.stderr[:200]}")
        
        return msgs

    except FileNotFoundError:
        print("[!] 错误: 找不到 'dumphfdl' 命令，请确认已安装")
        print("    安装方法: https://github.com/szpajder/dumphfdl")
        return []
    except subprocess.TimeoutExpired:
        print("[!] dumphfdl 解码超时（30秒）")
        return []
    except Exception as e:
        print(f"[!] dumphfdl 解码出错: {e}")
        return []

def save_logs(conn, host, freq, msgs):
    """数据清洗与入库"""
    if not msgs:
        return 0

    cursor = conn.cursor()
    count = 0
    
    for msg in msgs:
        try:
            hfdl = msg.get('hfdl', {})
            lpdu = hfdl.get('lpdu', {})
            perf = hfdl.get('perf', {})
            
            # 提取字段，使用 .get 防止报错
            flight_id = lpdu.get('flight_id', '')
            
            # 尝试多位置提取注册号
            ac_reg = lpdu.get('src', {}).get('id', '')
            if not ac_reg and 'dst' in lpdu:
                ac_reg = lpdu.get('dst', {}).get('id', '')

            snr = float(perf.get('snr', 0))
            
            # 提取位置
            lat = None
            lon = None
            if 'pos' in lpdu:
                lat = float(lpdu['pos'].get('lat'))
                lon = float(lpdu['pos'].get('lon'))

            # 插入 SQL
            sql = """
                INSERT INTO hfdl_logs 
                (time, frequency, station_id, flight_id, aircraft_reg, lat, lon, message, snr)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            now = datetime.datetime.now()
            
            cursor.execute(sql, (
                now, freq, host, flight_id, ac_reg, lat, lon, json.dumps(hfdl), snr
            ))
            count += 1
            
        except Exception:
            continue

    conn.commit()
    cursor.close()
    return count


def worker_loop(target_node, target_freq, archive_dir):
    """单个 节点×频点 的工作线程，循环录音+解码+入库"""
    host = target_node['host']
    port = target_node['port']
    freq = target_freq

    while True:
        print(f"\n=== [WORKER] 扫描 {host} @ {freq/1000:.1f} kHz ===")

        # 1. 录音
        wav_file = record_audio(host, port, freq, duration=10)

        if not wav_file:
            wait_fail = 5
            print(f"[-] {host}:{freq/1000:.1f} kHz 连接失败或无数据，等待 {wait_fail} 秒再重试...")
            time.sleep(wait_fail)
            continue

        # 2. 调用解码器（根据平台自动选择）
        # Windows 使用 Docker，Linux 使用本地 dumphfdl
        if os.name == 'nt':  # Windows
            msgs = decode_audio_docker(wav_file)
        else:  # Linux/Unix
            msgs = decode_audio_native(wav_file, freq)

        # 3. 入库
        if msgs:
            conn = get_db_connection()
            if not conn:
                # 数据库连不上，保守处理：等待后重试
                print("[!] 数据库当前不可用，等待 10 秒后重试...")
                time.sleep(10)
            else:
                count = save_logs(conn, host, freq, msgs)
                conn.close()

                if count > 0:
                    print(f"[+] {host} @ {freq/1000:.1f} kHz 成功捕获并入库: {count} 条信号！")
                    # 移动有效文件到归档目录
                    new_path = os.path.join(archive_dir, os.path.basename(wav_file))
                    if os.path.exists(new_path):
                        os.remove(new_path)  # 防止重名
                    os.rename(wav_file, new_path)
                    print(f"[*] 音频已保存至: {new_path}")
                else:
                    # 虽然有数据包但没入库成功(可能是空包)，删掉
                    os.remove(wav_file)
        else:
            # 噪音，直接删除
            print(f"[-] {host} @ {freq/1000:.1f} kHz 无有效信号 (SNR过低或无数据)")
            try:
                os.remove(wav_file)
            except:
                pass

        sleep_time = random.randint(5, 10)
        print(f"[*] {host} @ {freq/1000:.1f} kHz 冷却 {sleep_time} 秒...\n")
        time.sleep(sleep_time)
# ================= 主程序循环 =================

def main():
    platform_name = "Windows Docker版" if os.name == 'nt' else "Linux Native版"
    print(f"=== FoxHunter MVP ({platform_name}) 启动 ===")
    print(f"[*] 检测到的平台: {os.name}")
    print(f"[*] 解码方式: {'Docker' if os.name == 'nt' else 'Native dumphfdl'}")
    
    # 0. 启动前检查数据库连通性
    conn = get_db_connection()
    if not conn:
        return
    conn.close()

    # 1. 创建音频归档目录
    archive_dir = os.path.join(BASE_DIR, "safe_store")
    if not os.path.exists(archive_dir):
        os.makedirs(archive_dir)

    # 2. 为每个 节点×频点 启动一个工作线程
    workers = []
    for target_node in KIWI_NODES:
        for target_freq in TARGET_FREQS:
            t = threading.Thread(target=worker_loop, args=(target_node, target_freq, archive_dir), daemon=True)
            t.start()
            workers.append(t)
            print(f"[+] 已启动 worker: {target_node['host']} @ {target_freq/1000:.1f} kHz")

    # 3. 主线程保持存活，方便 Ctrl+C 停止
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[!] 用户停止运行，worker 将自动退出 (daemon 线程)")

if __name__ == "__main__":
    main()