import subprocess
import json
import time
import os
import datetime
import random
import sys
import psycopg2
import threading
import glob

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
TARGET_FREQS = [21982000, 13312000, 17919000, 11312000]

# 3. KiwiSDR 节点池 (已移除失效节点)
KIWI_NODES = [
    {"host": "hackgreensdr.org", "port": 8073},
    {"host": "sk3w.se", "port": 8073},
    {"host": "sdr-bayern.spdns.de", "port": 8073},
    {"host": "kiwisdr.briata.org", "port": 8073},
]

# ================= 核心功能函数 =================

def get_db_connection():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except Exception as e:
        print(f"[!] 数据库连接失败: {e}")
        return None

def record_audio(host, port, freq, duration=15):
    timestamp = int(time.time())
    base_filename = f"rec_{timestamp}_{freq}"
    expected_filename = os.path.join(BASE_DIR, f"{base_filename}_FoxHunter.wav")
    print(f"[*] [{host}] 正在监听 {freq/1000} kHz ...")
    python_cmd = sys.executable
    recorder_path = os.path.join(BASE_DIR, "kiwiclient", "kiwirecorder.py")
    if not os.path.exists(recorder_path):
        print(f"[!] 错误: 找不到 {recorder_path}")
        return None
    cmd = [
        python_cmd, recorder_path,
        "-s", host, "-p", str(port),
        "-f", str(freq / 1000), "-m", "iq",
        "--station=FoxHunter", "--tlimit", str(duration),
        "--filename", base_filename,
        "--connect-timeout", "30", "--socket-timeout", "20", "--quiet"
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=BASE_DIR)
        if os.path.exists(expected_filename):
            return expected_filename
        pattern = os.path.join(BASE_DIR, f"{base_filename}*.wav")
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
        print(f"[-] 录音文件未生成")
        return None
    except subprocess.CalledProcessError as e:
        if e.stderr:
            print(f"[-] 录音出错: {e.stderr.decode()[:200]}")
        return None
    except FileNotFoundError:
        return None

def decode_audio_native(wav_path, freq):
    if not wav_path:
        return []
    print(f"[*] 正在解码...")
    freq_khz = int(freq / 1000)
    try:
        sox_proc = subprocess.Popen(
            ["sox", wav_path, "-t", "raw", "-e", "signed-integer", "-b", "16", "-c", "2", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        dumphfdl_proc = subprocess.Popen(
            ["dumphfdl", "--iq-file", "-", "--sample-rate", "11999",
             "--sample-format", "CS16", "--centerfreq", str(freq_khz),
             str(freq_khz), "--output", "decoded:json:file:path=-"],
            stdin=sox_proc.stdout, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8"
        )
        sox_proc.stdout.close()
        stdout, _ = dumphfdl_proc.communicate(timeout=30)
        msgs = []
        for line in stdout.splitlines():
            if line.strip().startswith("{"):
                try:
                    data = json.loads(line)
                    if "hfdl" in data:
                        msgs.append(data)
                except json.JSONDecodeError:
                    continue
        return msgs
    except Exception as e:
        print(f"[!] 解码出错: {e}")
        return []

def save_logs(conn, host, freq, msgs):
    if not msgs:
        return 0
    cursor = conn.cursor()
    count = 0
    for msg in msgs:
        try:
            hfdl = msg.get("hfdl", {})
            lpdu = hfdl.get("lpdu", {})
            perf = hfdl.get("perf", {})
            flight_id = lpdu.get("flight_id", "")
            ac_reg = lpdu.get("src", {}).get("id", "")
            if not ac_reg and "dst" in lpdu:
                ac_reg = lpdu.get("dst", {}).get("id", "")
            snr = float(perf.get("snr", 0))
            lat, lon = None, None
            if "pos" in lpdu:
                lat = float(lpdu["pos"].get("lat"))
                lon = float(lpdu["pos"].get("lon"))
            sql = """INSERT INTO hfdl_logs 
                (time, frequency, station_id, flight_id, aircraft_reg, lat, lon, message, snr)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"""
            cursor.execute(sql, (datetime.datetime.now(), freq, host, flight_id, ac_reg, lat, lon, json.dumps(hfdl), snr))
            count += 1
        except:
            continue
    conn.commit()
    cursor.close()
    return count

def worker_loop(target_node, target_freq, archive_dir):
    host = target_node["host"]
    port = target_node["port"]
    freq = target_freq
    while True:
        print(f"\n=== [WORKER] 扫描 {host} @ {freq/1000:.1f} kHz ===")
        wav_file = record_audio(host, port, freq, duration=10)
        if not wav_file:
            time.sleep(5)
            continue
        msgs = decode_audio_native(wav_file, freq)
        if msgs:
            conn = get_db_connection()
            if conn:
                count = save_logs(conn, host, freq, msgs)
                conn.close()
                if count > 0:
                    print(f"[+] {host} @ {freq/1000:.1f} kHz 入库: {count} 条")
                    new_path = os.path.join(archive_dir, os.path.basename(wav_file))
                    if os.path.exists(new_path):
                        os.remove(new_path)
                    os.rename(wav_file, new_path)
                else:
                    try: os.remove(wav_file)
                    except: pass
            else:
                time.sleep(10)
        else:
            print(f"[-] {host} @ {freq/1000:.1f} kHz 无有效信号")
            try: os.remove(wav_file)
            except: pass
        time.sleep(random.randint(5, 10))

def main():
    print("=== FoxHunter MVP (Linux Native版) 启动 ===")
    conn = get_db_connection()
    if not conn:
        return
    conn.close()
    archive_dir = os.path.join(BASE_DIR, "safe_store")
    if not os.path.exists(archive_dir):
        os.makedirs(archive_dir)
    for target_node in KIWI_NODES:
        for target_freq in TARGET_FREQS:
            t = threading.Thread(target=worker_loop, args=(target_node, target_freq, archive_dir), daemon=True)
            t.start()
            print(f"[+] 已启动 worker: {target_node['host']} @ {target_freq/1000:.1f} kHz")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[!] 停止运行")

if __name__ == "__main__":
    main()
