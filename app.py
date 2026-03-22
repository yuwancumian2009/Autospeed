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
import urllib3

# 禁用 requests 使用 verify=False 时的系统警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 设置 Matplotlib 使用非交互模式后台绘图，避免报错
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
        'external_url': ''
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
    """在后台生成近七天数据的双Y轴折线图图片 (全英文标识，避免方框)"""
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

    fig, ax1 = plt.subplots(figsize=(10, 5))
    plt.title('Network Speed Trends (Last 7 Days)')

    # 左侧 Y 轴 (速率 Mbps)
    ax1.set_xlabel('Time')
    ax1.set_ylabel('Speed (Mbps)', color='blue')
    ax1.plot(timestamps, downloads, color='green', label='Download (Mbps)', linewidth=2)
    ax1.plot(timestamps, uploads, color='blue', label='Upload (Mbps)', linewidth=2)
    ax1.tick_params(axis='y', labelcolor='blue')
    ax1.grid(True, linestyle='--', alpha=0.5)

    # 右侧 Y 轴 (延迟 ms)
    ax2 = ax1.twinx()
    ax2.set_ylabel('Latency (ms)', color='orange')
    ax2.plot(timestamps, pings, color='orange', label='Latency (ms)', linestyle='--')
    ax2.tick_params(axis='y', labelcolor='orange')

    # X 轴时间格式化
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    fig.autofmt_xdate()

    # 图例
    lines, labels = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines + lines2, labels + labels2, loc='upper left')

    img_buf = io.BytesIO()
    plt.savefig(img_buf, format='png', bbox_inches='tight')
    plt.close(fig) 
    img_buf.seek(0)
    return img_buf

def send_wechat_news_msg(result_text, chart_exists=False):
    """发送企业微信图文卡片（带有防御性代理异常捕获）"""
    corpid = get_setting('wecom_corpid')
    secret = get_setting('wecom_secret')
    agentid = get_setting('wecom_agentid')
    proxy = get_setting('wecom_proxy')
    external_url = get_setting('external_url')
    
    if not corpid or not secret or not agentid:
        print("未配置企业微信，跳过通知推送。")
        return False

    base_url = proxy.rstrip('/') if proxy else "https://qyapi.weixin.qq.com"
    
    try:
        token_url = f"{base_url}/cgi-bin/gettoken?corpid={corpid}&corpsecret={secret}"
        resp = requests.get(token_url, timeout=10, verify=False)
        
        try:
            resp_data = resp.json()
        except ValueError:
            print(f"❌ 致命错误：代理服务器({base_url}) 未返回有效的 JSON 数据！")
            print(f"HTTP 状态码: {resp.status_code}")
            print(f"代理返回的原始内容 (前200字): {resp.text[:200]}")
            return False

        if resp_data.get('errcode') != 0: 
            print(f"获取 Token 失败，微信接口返回: {resp_data}")
            return False
            
        access_token = resp_data.get('access_token')
        
        image_base = external_url.rstrip('/') if external_url else "http://speedtest-local.com" 
        pic_url = f"{image_base}/chart.png?t={int(time.time())}"
        
        send_url = f"{base_url}/cgi-bin/message/send?access_token={access_token}"
        
        payload = {
            "touser": "@all",
            "msgtype": "news",
            "agentid": int(agentid),
            "news": {
                "articles": [
                    {
                        "title": "私人网络测速结果报告",
                        "description": result_text,
                        "url": external_url if external_url else "",
                        "picurl": pic_url if chart_exists else ""
                    }
                ]
            },
            "safe": 0
        }
        res = requests.post(send_url, json=payload, timeout=10, verify=False)
        
        try:
            res_data = res.json()
        except ValueError:
            print(f"❌ 致命错误：通过代理发送消息时，未返回有效的 JSON 数据！")
            print(f"HTTP 状态码: {res.status_code}")
            print(f"代理返回的原始内容 (前200字): {res.text[:200]}")
            return False

        if res_data.get('errcode') == 0: 
            return True
        else:
            print(f"企业微信 News 通知发送失败: {res_data}")
            return False
            
    except Exception as e:
        print(f"网络请求发生异常 (请检查代理地址是否联通): {e}")
        return False

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
    for attempt in range(max_retries + 1):
        print(f"[{datetime.now()}] 开始执行测速 (第 {attempt + 1} 次尝试)...")
        error_reason = "未知错误"
        
        cmd = ["speedtest", "--accept-license", "--accept-gdpr", "-f", "json"]
        
        # 失败重试时，自动放弃指定的超时节点，交由官方就近寻找
        use_server_id = server_id
        if attempt > 0 and server_id:
            print("上一次指定的节点可能超时，重试时将自动降级为寻找其他最优节点...")
            use_server_id = None
            
        if use_server_id:
            cmd.extend(["-s", str(use_server_id)])
            
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            
            if result.returncode != 0:
                # 提取干净的官方 JSON 报错原因
                clean_errors = []
                for line in result.stderr.strip().split('\n'):
                    try:
                        log_data = json.loads(line)
                        if log_data.get('level') == 'error':
                            clean_errors.append(log_data.get('message', '未知错误'))
                    except:
                        pass
                
                if clean_errors:
                    error_reason = " | ".join(clean_errors)
                else:
                    error_reason = "节点连接超时或无响应"
                    
                raise Exception("执行非零返回")
                
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                error_reason = "返回数据异常，节点可能拒绝服务"
                raise Exception("JSON解析失败")
                
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
            send_wechat_news_msg(success_msg, chart_exists=True)
            return True
            
        except Exception as e:
            print(f"测速失败: {e}\n原因: {error_reason}")
            if attempt < max_retries:
                print("休眠 10 秒后准备重试...")
                time.sleep(10)
            else:
                fail_msg = f"❌ 测速失败 (已重试 {max_retries} 次)\n⚠️ 原因：{error_reason}"
                send_wechat_news_msg(fail_msg, chart_exists=False)
                return False

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
    # 扩大查询范围，配合前端分页组件
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
    success = send_wechat_news_msg("🔔 私人网络测速面板：这是测试消息。外部设置正确即可看到趋势图！", chart_exists=True)
    return jsonify({"status": "success" if success else "error"})

@app.route('/chart.png')
def serve_chart():
    img_buf = generate_7day_chart_image()
    if img_buf:
        return send_file(img_buf, mimetype='image/png', as_attachment=False)
    else:
        return "No data for chart", 404

if __name__ == '__main__':
    init_db()
    update_scheduler()
    scheduler.start()
    app.run(host='0.0.0.0', port=5000)
