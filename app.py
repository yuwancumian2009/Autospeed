import os
import json
import sqlite3
import subprocess
import time
import io
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import requests

# 必须设置 Matplotlib 使用非交互模式后台绘图，避免没有 X 服务报错
import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

app = Flask(__name__)
DB_FILE = "data/autospeed.db" if os.path.exists("data") else "autospeed.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS results
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp TEXT, download REAL, upload REAL, ping REAL,
                  server_name TEXT, server_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    
    defaults = {
        'cron': '0 */6 * * *', 'mode': 'closest', 'server_id': '',
        'wecom_corpid': '', 'wecom_secret': '', 'wecom_agentid': '', 'wecom_proxy': '',
        'external_url': '' # 新增：用于微信卡片图片显示的外部/公网基准地址
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def generate_7day_chart_image():
    """在后台生成近七天数据的双Y轴折线图图片"""
    threshold = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT timestamp, download, upload, ping FROM results WHERE timestamp >= ? ORDER BY timestamp ASC", (threshold,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return None

    timestamps = [datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") for r in rows]
    downloads = [r[1] for r in rows]
    uploads = [r[2] for r in rows]
    pings = [r[3] for r in rows]

    # 创建绘图对象
    fig, ax1 = plt.figure(figsize=(10, 5)), plt.gca()
    plt.title('网络测速近 7 天趋势')

    # 配置第一个 Y 轴 (左侧：速率 Mbps)
    ax1.set_xlabel('时间')
    ax1.set_ylabel('速率 (Mbps)', color='blue')
    ax1.plot(timestamps, downloads, color='green', label='下行 (Mbps)', linewidth=2)
    ax1.plot(timestamps, uploads, color='blue', label='上行 (Mbps)', linewidth=2)
    ax1.tick_params(axis='y', labelcolor='blue')
    ax1.grid(True, linestyle='--', alpha=0.5)

    # 配置第二个 Y 轴 (右侧：延迟 ms)
    ax2 = ax1.twinx()
    ax2.set_ylabel('延迟 (ms)', color='orange')
    ax2.plot(timestamps, pings, color='orange', label='延迟 (ms)', linestyle='--')
    ax2.tick_params(axis='y', labelcolor='orange')

    # X 轴时间格式化
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    fig.autofmt_xdate() # 自动旋转日期标记

    # 合并两个 Y 轴的图例
    lines, labels = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines + lines2, labels + labels2, loc='upper left')

    # 将图片保存到内存缓冲区
    img_buf = io.BytesIO()
    plt.savefig(img_buf, format='png', bbox_inches='tight')
    plt.close(fig) # 必须关闭释放内存
    img_buf.seek(0)
    return img_buf

def send_wechat_news_msg(result_text, chart_exists=False):
    """发送企业微信图文卡片（News）消息"""
    corpid = get_setting('wecom_corpid')
    secret = get_setting('wecom_secret')
    agentid = get_setting('wecom_agentid')
    proxy = get_setting('wecom_proxy')
    external_url = get_setting('external_url') # 获取公网基准地址
    
    if not corpid or not secret or not agentid:
        print("未配置企业微信，跳过通知推送。")
        return False

    base_url = proxy.rstrip('/') if proxy else "https://qyapi.weixin.qq.com"
    
    try:
        token_url = f"{base_url}/cgi-bin/gettoken?corpid={corpid}&corpsecret={secret}"
        resp = requests.get(token_url, timeout=10).json()
        if resp.get('errcode') != 0: return False
        access_token = resp.get('access_token')
        
        # 核心修改：如果是 News 消息，外部必须能访问此网络地址
        # 如果没有填写公网基准地址，我们将尝试使用容器IP（此时外网裂图，描述正确）
        # external_url 示例：http://myname.ddns.net:5000 或 http://192.168.1.111:5000
        image_base = external_url.rstrip('/') if external_url else "http://speedtest-local.com" 
        pic_url = f"{image_base}/chart.png?t={int(time.time())}" # 加上时间戳防止微信缓存
        
        send_url = f"{base_url}/cgi-bin/message/send?access_token={access_token}"
        
        # 构造图文卡片消息体
        payload = {
            "touser": "@all",
            "msgtype": "news",
            "agentid": int(agentid),
            "news": {
                "articles": [
                    {
                        "title": "私人网络测速结果报告",
                        "description": result_text, # 具体的通知文字放在描述里
                        "url": external_url if external_url else "", # 点击卡片可以跳转到外部WebUI
                        "picurl": pic_url if chart_exists else "" # 如果有趋势图，显示它
                    }
                ]
            },
            "safe": 0
        }
        res = requests.post(send_url, json=payload, timeout=10).json()
        if res.get('errcode') == 0: return True
        else:
            print(f"企业微信 News 通知发送失败: {res}")
            return False
    except Exception as e:
        print(f"企业微信推送 News 发生异常: {e}")
        return False

# ... 保留 get_servers_list, get_target_server_id 函数 ...
def get_servers_list():
    try:
        result = subprocess.run(["speedtest", "-L", "-f", "json"], capture_output=True, text=True, timeout=15)
        return json.loads(result.stdout).get('servers', [])
    except Exception as e:
        print(f"获取节点列表失败: {e}")
        return []

def get_target_server_id(mode, fixed_id):
    if mode == 'fixed' and fixed_id: return fixed_id
    if mode == 'closest': return None 
    
    servers = get_servers_list()
    keyword_map = {
        'telecom': ['telecom', '电信', 'chinanet', 'ct'],
        'unicom': ['unicom', '联通', 'cucc'],
        'mobile': ['mobile', '移动', 'cmcc']
    }
    keywords = keyword_map.get(mode, [])
    
    for srv in servers:
        name = (str(srv.get('name', '')) + str(srv.get('sponsor', ''))).lower()
        for kw in keywords:
            if kw in name: return str(srv.get('id'))
    return None

def run_speedtest(server_id=None, max_retries=2):
    cmd = ["speedtest", "--accept-license", "--accept-gdpr", "-f", "json"]
    if server_id:
        cmd.extend(["-s", str(server_id)])
        
    for attempt in range(max_retries + 1):
        print(f"[{datetime.now()}] 开始执行测速 (第 {attempt + 1} 次尝试)...")
        error_reason = "未知错误"
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                error_reason = f"命令执行失败\n详细日志: {result.stderr.strip()}"
                raise Exception("命令错误")
                
            data = json.loads(result.stdout)
            
            download_mbps = round(data['download']['bandwidth'] * 8 / 1000000, 2)
            upload_mbps = round(data['upload']['bandwidth'] * 8 / 1000000, 2)
            ping_ms = round(data['ping']['latency'], 2)
            srv_name = f"{data['server'].get('sponsor', '未知')} - {data['server'].get('location', '未知')}"
            srv_id = str(data['server']['id'])
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("INSERT INTO results (timestamp, download, upload, ping, server_name, server_id) VALUES (?, ?, ?, ?, ?, ?)",
                      (timestamp, download_mbps, upload_mbps, ping_ms, srv_name, srv_id))
            conn.commit()
            conn.close()
            
            success_msg = f"✅ 测速成功\n\n下行：{download_mbps} Mbps\n上行：{upload_mbps} Mbps\n延迟：{ping_ms} ms\n节点：{srv_name}"
            # 修改：调用图文消息发送函数，并检查是否具备趋势图数据
            send_wechat_news_msg(success_msg, chart_exists=True)
            return True
            
        except Exception as e:
            if attempt < max_retries:
                time.sleep(10)
            else:
                fail_msg = f"❌ 测速失败 (已重试 {max_retries} 次)\n⚠️ 原因：{error_reason}"
                # 修改：失败通知依然采用简单的图文卡片（裂图描述错误）或可以降级为普通text通知
                send_wechat_news_msg(fail_msg, chart_exists=False)
                return False

# ... 保留 scheduled_job, scheduler, update_scheduler 函数 ...
def scheduled_job():
    mode = get_setting('mode')
    server_id = get_setting('server_id')
    target_id = get_target_server_id(mode, server_id)
    run_speedtest(target_id)

scheduler = BackgroundScheduler()

def update_scheduler():
    cron_expr = get_setting('cron') or "0 */6 * * *"
    try:
        scheduler.add_job(
            func=scheduled_job, trigger=CronTrigger.from_crontab(cron_expr), 
            id='speedtest_job', replace_existing=True
        )
    except Exception as e:
        print(f"Cron 错误: {e}")

@app.route('/')
def index():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM results ORDER BY id DESC LIMIT 100")
    rows = c.fetchall()
    c.execute("SELECT key, value FROM settings")
    settings = dict(c.fetchall())
    conn.close()
    return render_template('index.html', results=rows, settings=settings)

@app.route('/api/servers')
def api_servers():
    servers = []
    for s in get_servers_list():
        name = s.get('name', s.get('sponsor', '未知节点'))
        location = s.get('location', '未知地区')
        servers.append({'id': s.get('id'), 'display': f"[{location}] {name}"})
    return jsonify(servers)

# ... 保留 api_history 函数供 WebUI ECharts 使用 ...
@app.route('/api/history')
def get_history():
    timeframe = request.args.get('timeframe', '7')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if timeframe == 'all':
        c.execute("SELECT timestamp, download, upload, ping FROM results ORDER BY timestamp ASC")
    else:
        try:
            days = int(timeframe)
            threshold = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("SELECT timestamp, download, upload, ping FROM results WHERE timestamp >= ? ORDER BY timestamp ASC", (threshold,))
        except ValueError:
            c.execute("SELECT timestamp, download, upload, ping FROM results ORDER BY timestamp ASC LIMIT 100")
    rows = c.fetchall()
    conn.close()
    return jsonify({
        'timestamps': [r[0] for r in rows],
        'downloads': [r[1] for r in rows],
        'uploads': [r[2] for r in rows],
        'pings': [r[3] for r in rows]
    })

@app.route('/api/run', methods=['POST'])
def trigger_test():
    data = request.json
    target_id = get_target_server_id(data.get('mode', 'closest'), data.get('server_id', ''))
    success = run_speedtest(target_id)
    return jsonify({"status": "success" if success else "error"})

@app.route('/api/settings', methods=['POST'])
def save_settings():
    data = request.json
    keys = ['cron', 'mode', 'server_id', 'wecom_corpid', 'wecom_secret', 'wecom_agentid', 'wecom_proxy', 'external_url']
    for key in keys:
        if key in data: set_setting(key, data[key])
    update_scheduler()
    return jsonify({"status": "success"})

@app.route('/api/test_wechat', methods=['POST'])
def test_wechat():
    data = request.json
    for key in ['wecom_corpid', 'wecom_secret', 'wecom_agentid', 'wecom_proxy', 'external_url']:
        if key in data: set_setting(key, data[key])
    # 测试消息也尝试发送 News 卡片
    success = send_wechat_news_msg("🔔 私人网络测速面板：这是测试消息。外部设置正确即可看到趋势图！", chart_exists=True)
    return jsonify({"status": "success" if success else "error"})

# --- 新增专用绘图图片 API 用于微信访问 ---
@app.route('/chart.png')
def serve_chart():
    """提供后端动态生成的近七天趋势图 PNG 图片"""
    img_buf = generate_7day_chart_image()
    if img_buf:
        # 指定 mimetype 为 png，直接输出二进制图片流
        return send_file(img_buf, mimetype='image/png', as_attachment=False)
    else:
        return "No data for chart", 404

if __name__ == '__main__':
    init_db()
    update_scheduler()
    scheduler.start()
    app.run(host='0.0.0.0', port=5000)