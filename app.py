from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import pandas as pd
import threading
import uuid
import time
import io
import traceback
from aps_engine import run_simulation

app = Flask(__name__)
CORS(app)

TASKS = {}

@app.route('/api/upload', methods=['POST'])
def upload_excel():
    if 'file' not in request.files:
        return jsonify({"error": "未接收到文件"}), 400
    file = request.files['file']
    try:
        df = pd.read_excel(file, dtype={'车号': str})
        df = df.fillna("")
        return jsonify({"data": df.to_dict(orient='records')})
    except Exception as e:
        return jsonify({"error": f"解析 Excel 失败: {str(e)}"}), 500

def background_calculation(task_id, df, config):
    def progress_cb(current_day, max_days):
        TASKS[task_id]['progress'] = min(int((current_day / max_days) * 100), 99)
        TASKS[task_id]['current_day'] = current_day

    try:
        start_time = time.time()
        df_rep, df_schedule = run_simulation(df, config, progress_callback=progress_cb)
        
        TASKS[task_id]['result'] = {
            "repair_plan": df_rep.to_dict(orient='records') if not df_rep.empty else [],
            "schedule_logs": df_schedule.to_dict(orient='records') if not df_schedule.empty else []
        }
        TASKS[task_id]['time_cost'] = round(time.time() - start_time, 2)
        TASKS[task_id]['progress'] = 100
        TASKS[task_id]['status'] = 'completed'

    except Exception as e:
        TASKS[task_id]['error'] = str(e)
        TASKS[task_id]['status'] = 'error'

@app.route('/api/start_sim', methods=['POST'])
def start_simulation():
    req = request.json
    data = req.get('data')
    config = req.get('config')
    
    task_id = str(uuid.uuid4())
    TASKS[task_id] = {'status': 'running', 'progress': 0, 'current_day': 0, 'result': None, 'error': None}
    
    try:
        df_input = pd.DataFrame(data)
        thread = threading.Thread(target=background_calculation, args=(task_id, df_input, config))
        thread.start()
        return jsonify({"task_id": task_id})
    except Exception as e:
        return jsonify({"error": f"启动任务失败: {str(e)}"}), 500

@app.route('/api/status/<task_id>', methods=['GET'])
def get_status(task_id):
    if task_id not in TASKS: return jsonify({"error": "找不到该任务"}), 404
    return jsonify(TASKS[task_id])

# 接口1：导出大架修计划
@app.route('/api/export_repair', methods=['POST'])
def export_repair():
    data = request.json
    df_rep = pd.DataFrame(data.get('repair_plan', []))
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_rep.to_excel(writer, index=False)
    output.seek(0)
    return send_file(output, download_name="output_repair_v18.xlsx", as_attachment=True)

# 接口2：导出每日纯净排班表 (逆向还原至原版格式)
@app.route('/api/export_schedule', methods=['POST'])
def export_schedule():
    data = request.json
    raw_logs = data.get('schedule_logs', [])
    
    df_schedule_rich = pd.DataFrame(raw_logs)
    df_schedule_export = pd.DataFrame()
    
    if not df_schedule_rich.empty:
        df_schedule_export['Day'] = df_schedule_rich['Day']
        df_schedule_export['Date'] = df_schedule_rich['Date']
        df_schedule_export['IsWeekend'] = df_schedule_rich['IsWeekend']
        
        train_cols = [c for c in df_schedule_rich.columns if c.endswith('_Task')]
        train_ids = sorted([c.split('_')[0] for c in train_cols])
        
        # 100% 还原原版：单元格只包含任务标识（如 EQ、WD_H600、REST 等）
        for tid in train_ids:
            df_schedule_export[tid] = df_schedule_rich[f"{tid}_Task"]
            
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_schedule_export.to_excel(writer, index=False)
    output.seek(0)
    return send_file(output, download_name="output_schedule_v18.xlsx", as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)