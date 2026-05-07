import pandas as pd
import numpy as np
import random
import json
from datetime import datetime, timedelta
from collections import deque

BASE_DATE = datetime(2026, 2, 1)
STATUS_OP = 0; STATUS_RACK = 8; STATUS_MAJOR = 9
STATUS_SPEC12 = 10; STATUS_SPEC6 = 11; STATUS_EQ = 12; STATUS_SP = 13
FULL_STOP_STATUSES = [STATUS_RACK, STATUS_MAJOR, STATUS_SPEC12, STATUS_SPEC6]
LIMITED_STATUSES = [STATUS_EQ, STATUS_SP]

MAJOR_START_DAY_ANCHOR = 325; MAJOR_INTERVAL = 25; MAJOR_DURATION = 50
RACK_MIN_INTERVAL = 15; RACK_DURATION = 30
CYCLE_EQ = 30; CYCLE_SP = 30; CYCLE_SPEC6 = 180; CYCLE_SPEC12 = 365
SP_OFFSET = 15; LIMITED_GEAR_KM = 300

def parse_date(s):
    if pd.isna(s) or s == "": return None
    try: return datetime.strptime(str(s).strip(), '%Y.%m.%d')
    except: return None

def get_next_repair_day(first_date, cycle_days, base_date):
    if first_date is None: return None
    current = first_date
    while current < base_date: current += timedelta(days=cycle_days)
    return (current - base_date).days + 1

def day_to_date(day): return (BASE_DATE + timedelta(days=day - 1)).strftime('%Y-%m-%d')

def generate_task_pool(config):
    pool = []
    for item in config:
        for _ in range(int(item.get('count', 0))): pool.append(item)
    pool.sort(key=lambda x: float(x.get('km', 0)), reverse=True)
    return pool

class Train:
    def __init__(self, uid, mileage, is_rack_group, initial_status_day1,
                 first_eq, first_sp, first_spec6, first_spec12, spec12_duration,
                 limit_target_major, limit_hard_major, limit_trigger_rack, limit_hard_rack):
        self.id = uid; self.mileage = mileage
        self.group = 'RACK' if is_rack_group else 'MAJOR'
        self.initial_status_day1 = initial_status_day1; self.spec12_duration = spec12_duration
        self.hard_limit = limit_hard_rack if is_rack_group else limit_hard_major
        self.target_limit = limit_trigger_rack if is_rack_group else limit_target_major
        self.assigned_repair_day = None; self.status = STATUS_OP
        self.repair_days_counter = 0; self.has_finished_repair = False
        self.is_forced_stop = False; self.is_day1_fault = False
        self.log_repair_start_day = None; self.log_repair_entry_mileage = None
        self.next_eq_day = get_next_repair_day(first_eq, CYCLE_EQ, BASE_DATE)
        self.next_sp_day = get_next_repair_day(first_sp, CYCLE_SP, BASE_DATE)
        self.next_spec6_day = get_next_repair_day(first_spec6, CYCLE_SPEC6, BASE_DATE)
        self.next_spec12_day = get_next_repair_day(first_spec12, CYCLE_SPEC12, BASE_DATE)

    def is_under_full_stop(self): return self.status in FULL_STOP_STATUSES

class MasterScheduler:
    def __init__(self, fleet):
        self.fleet = fleet; self.last_rack_entry_day = -999

    def assign_major_timeline(self):
        majors = [t for t in self.fleet if t.group == 'MAJOR']
        majors.sort(key=lambda x: x.mileage, reverse=True)
        current_slot = MAJOR_START_DAY_ANCHOR
        for t in majors: t.assigned_repair_day = current_slot; current_slot += MAJOR_INTERVAL

    def _is_repairing_on_day(self, train, block_start, offset):
        target = block_start + offset
        if train.status in FULL_STOP_STATUSES:
            dur = MAJOR_DURATION if train.status == STATUS_MAJOR else (RACK_DURATION if train.status == STATUS_RACK else (train.spec12_duration if train.status == STATUS_SPEC12 else 1))
            if offset < (dur - train.repair_days_counter): return True
        if train.group == 'MAJOR' and train.assigned_repair_day and train.assigned_repair_day <= target < train.assigned_repair_day + MAJOR_DURATION: return True
        if train.next_spec12_day and train.next_spec12_day <= target < train.next_spec12_day + train.spec12_duration: return True
        if train.next_spec6_day and train.next_spec6_day == target: return True
        return False

    def generate_rest_matrix_block(self, block_start_day, req_wd, req_we):
        num_trains = len(self.fleet)
        matrix = np.ones((num_trains, 30), dtype=int)
        weights = [max(0.1, (max(0, t.target_limit - t.mileage) / 10000.0) * random.uniform(0.8, 1.2) + (200 if t.has_finished_repair else 0)) if t.status not in FULL_STOP_STATUSES else 0 for t in self.fleet]
        weights = np.array(weights)

        for d in range(30):
            current_date = BASE_DATE + timedelta(days=block_start_day + d - 1)
            is_weekend = current_date.weekday() >= 5
            req_ops = req_we if is_weekend else req_wd
            
            available = [i for i, t in enumerate(self.fleet) if not self._is_repairing_on_day(t, block_start_day, d)]
            quota = max(0, len(available) - req_ops)
            if quota > 0 and available:
                w = weights[available]; probs = w / np.sum(w) if np.sum(w) > 0 else None
                for idx in np.random.choice(available, size=min(len(available), quota), replace=False, p=probs): matrix[idx][d] = 0
        self.rest_matrix = {t.id: matrix[i] for i, t in enumerate(self.fleet)}
        return self.rest_matrix

def run_simulation(df, config=None, progress_callback=None):
    if config is None: config = {}
    
    REQ_WD = int(config.get('req_count_wd', 29))
    REQ_WE = int(config.get('req_count_we', 28))
    LIMIT_TARGET_MAJOR = int(config.get('limit_target_major', 1580000))
    LIMIT_TRIGGER_RACK = int(config.get('limit_trigger_rack', 790000))

    try: TASK_WD = json.loads(config.get('task_wd', '[]'))
    except: TASK_WD = []
    try: TASK_WE = json.loads(config.get('task_we', '[]'))
    except: TASK_WE = []

    pool_wd = generate_task_pool(TASK_WD)
    pool_we = generate_task_pool(TASK_WE)

    fleet = []
    for _, row in df.iterrows():
        car_no = str(row.get('车号', '0000')).split('.')[0].zfill(4)
        mileage = float(row.get('累积里程', 0))
        fleet.append(Train(
            car_no, mileage, int(str(row.get('列车序号', 'T00')).replace('T', '')) >= 24,
            int(row.get('运营状态', 1)) if str(row.get('运营状态', 1)).strip() else 1,
            parse_date(row.get('第一次均衡修时间')), parse_date(row.get('第一次特别修时间')),
            parse_date(row.get('第一次专项修6时间')), parse_date(row.get('第一次专项修12时间')),
            int(row.get('年检维修耗时/天', 3)) if str(row.get('年检维修耗时/天', '')).strip() else 3,
            LIMIT_TARGET_MAJOR, LIMIT_TARGET_MAJOR + 20000, LIMIT_TRIGGER_RACK, LIMIT_TRIGGER_RACK + 10000
        ))

    if len(fleet) == 0: return pd.DataFrame(), pd.DataFrame()
    fleet.sort(key=lambda x: x.mileage, reverse=True)
    
    scheduler = MasterScheduler(fleet)
    scheduler.assign_major_timeline()

    day = 0; logs_schedule = []
    
    while True:
        day += 1
        current_date = BASE_DATE + timedelta(days=day - 1)
        is_weekend = current_date.weekday() >= 5
        
        if progress_callback and day % 10 == 0: progress_callback(day, 4000)
        if (day - 1) % 30 == 0: rest_matrix = scheduler.generate_rest_matrix_block(day, REQ_WD, REQ_WE)
        
        finished_cnt = sum(1 for t in fleet if t.has_finished_repair)
        if finished_cnt == len(fleet): break
        if day > 4000: break
            
        day_idx = (day - 1) % 30
        if day == 1:
            for t in fleet: t.is_day1_fault = (t.initial_status_day1 == 0)
        else:
            for t in fleet: t.is_day1_fault = False

        active_majors = 0
        for t in fleet:
            if t.status == STATUS_MAJOR:
                t.repair_days_counter += 1
                if t.repair_days_counter >= MAJOR_DURATION:
                    t.status = STATUS_OP; t.mileage = 0; t.repair_days_counter = 0; t.has_finished_repair = True
                    t.next_eq_day = day + CYCLE_EQ; t.next_sp_day = day + CYCLE_EQ + SP_OFFSET; t.next_spec6_day = day + CYCLE_SPEC6; t.next_spec12_day = day + CYCLE_SPEC12
                else: active_majors += 1
            elif t.status == STATUS_RACK:
                t.repair_days_counter += 1
                if t.repair_days_counter >= RACK_DURATION:
                    t.status = STATUS_OP; t.mileage = 0; t.repair_days_counter = 0; t.has_finished_repair = True
                    t.next_eq_day = day + CYCLE_EQ; t.next_sp_day = day + CYCLE_EQ + SP_OFFSET; t.next_spec6_day = day + CYCLE_SPEC6; t.next_spec12_day = day + CYCLE_SPEC12
            elif t.status == STATUS_SPEC12:
                t.repair_days_counter += 1
                if t.repair_days_counter >= t.spec12_duration: t.status = STATUS_OP; t.repair_days_counter = 0; t.next_spec12_day += CYCLE_SPEC12
            elif t.status == STATUS_SPEC6:
                t.repair_days_counter += 1
                if t.repair_days_counter >= 1: t.status = STATUS_OP; t.repair_days_counter = 0; t.next_spec6_day += CYCLE_SPEC6
            elif t.status == STATUS_EQ:
                t.repair_days_counter += 1
                if t.repair_days_counter >= 1: t.status = STATUS_OP; t.repair_days_counter = 0; t.next_eq_day += CYCLE_EQ
            elif t.status == STATUS_SP:
                t.repair_days_counter += 1
                if t.repair_days_counter >= 1: t.status = STATUS_OP; t.repair_days_counter = 0; t.next_sp_day += CYCLE_SP

        for t in fleet:
            if t.group == 'MAJOR' and not t.has_finished_repair and t.status == STATUS_OP and day >= t.assigned_repair_day:
                t.status = STATUS_MAJOR; t.log_repair_start_day = day; t.log_repair_entry_mileage = t.mileage; active_majors += 1

        if active_majors < 2 and (day - scheduler.last_rack_entry_day) >= RACK_MIN_INTERVAL:
            urgent = [t for t in fleet if t.group == 'RACK' and not t.has_finished_repair and t.status == STATUS_OP and (t.is_forced_stop or t.mileage > t.target_limit)]
            if urgent: urgent.sort(key=lambda x: (not x.is_forced_stop, -x.mileage)); top = urgent[0]; top.status = STATUS_RACK; top.log_repair_start_day = day; top.log_repair_entry_mileage = top.mileage; scheduler.last_rack_entry_day = day

        for t in fleet:
            if t.status == STATUS_OP and t.next_spec12_day and day >= t.next_spec12_day: t.status = STATUS_SPEC12; t.repair_days_counter = 0
            if t.status == STATUS_OP and t.next_spec6_day and day >= t.next_spec6_day: t.status = STATUS_SPEC6; t.repair_days_counter = 0
            if t.status == STATUS_OP and t.next_eq_day and day >= t.next_eq_day: t.status = STATUS_EQ; t.repair_days_counter = 0
            if t.status == STATUS_OP and t.next_sp_day and day >= t.next_sp_day: t.status = STATUS_SP; t.repair_days_counter = 0

        # --- 核心修复：绝对数量调平算法 ---
        repairing = []; eq_sp = []; forced = []; candidates = []
        for t in fleet:
            if t.is_under_full_stop(): repairing.append(t)
            elif t.status in [STATUS_EQ, STATUS_SP]: eq_sp.append(t)
            elif t.is_forced_stop or t.is_day1_fault: forced.append(t)
            else: candidates.append(t)

        req_ops = REQ_WE if is_weekend else REQ_WD
        target_normal_ops = req_ops - len(eq_sp)

        # 动态干预：如果常规车过多，强迫按矩阵休息；如果不够，从休息的人中拉出来上班
        if target_normal_ops <= 0:
            forced.extend(candidates)
            available = []
        else:
            if len(candidates) <= target_normal_ops:
                available = candidates
            else:
                planned_work = [t for t in candidates if rest_matrix[t.id][day_idx] == 1]
                planned_rest = [t for t in candidates if rest_matrix[t.id][day_idx] == 0]
                
                if len(planned_work) > target_normal_ops:
                    planned_work.sort(key=lambda x: x.mileage, reverse=True)
                    excess = len(planned_work) - target_normal_ops
                    forced.extend(planned_work[:excess])
                    available = planned_work[excess:]
                    forced.extend(planned_rest)
                elif len(planned_work) < target_normal_ops:
                    shortage = target_normal_ops - len(planned_work)
                    planned_rest.sort(key=lambda x: x.mileage)
                    available = planned_work + planned_rest[:shortage]
                    forced.extend(planned_rest[shortage:])
                else:
                    available = planned_work
                    forced.extend(planned_rest)

        available.sort(key=lambda x: x.mileage, reverse=True)
        pool = pool_we if is_weekend else pool_wd
        low_tasks = [tk for tk in pool if float(tk['km']) <= LIMITED_GEAR_KM]
        normal_tasks = [tk for tk in pool if float(tk['km']) > LIMITED_GEAR_KM]

        log = {'Day': day, 'Date': current_date.strftime('%Y-%m-%d'), 'IsWeekend': is_weekend}

        for i, t in enumerate(eq_sp):
            lbl = "EQ" if t.status == STATUS_EQ else "SP"
            if i < len(low_tasks) and t.mileage + float(low_tasks[i]['km']) < t.hard_limit:
                t.mileage += float(low_tasks[i]['km'])
                log[f"{t.id}_Task"] = lbl
                log[f"{t.id}_Tag"] = low_tasks[i]['tag']
                log[f"{t.id}_Code"] = low_tasks[i]['code'] # 透传给前端
            else: 
                log[f"{t.id}_Task"] = "STOP" if i < len(low_tasks) else lbl
                log[f"{t.id}_Tag"] = "-"
                log[f"{t.id}_Code"] = "-"
            log[f"{t.id}_KM"] = round(t.mileage); log[f"{t.id}_Group"] = t.group

        rem_tasks = normal_tasks + low_tasks[len(eq_sp):]
        for i, t in enumerate(available):
            if i < len(rem_tasks) and t.mileage + float(rem_tasks[i]['km']) < t.hard_limit:
                t.mileage += float(rem_tasks[i]['km'])
                log[f"{t.id}_Task"] = rem_tasks[i]['code']
                log[f"{t.id}_Tag"] = rem_tasks[i]['tag']
                log[f"{t.id}_Code"] = rem_tasks[i]['code']
            else: 
                log[f"{t.id}_Task"] = "STOP" if i < len(rem_tasks) else "REST"
                log[f"{t.id}_Tag"] = "-"
                log[f"{t.id}_Code"] = "-"
            log[f"{t.id}_KM"] = round(t.mileage); log[f"{t.id}_Group"] = t.group

        for t in forced:
            log[f"{t.id}_Task"] = "STOP" if t.is_forced_stop else "REST"
            log[f"{t.id}_Tag"] = "-"
            log[f"{t.id}_Code"] = "-"
            log[f"{t.id}_KM"] = round(t.mileage); log[f"{t.id}_Group"] = t.group
            
        for t in repairing:
            status_map = {STATUS_MAJOR:"MAJOR", STATUS_RACK:"RACK", STATUS_SPEC12:"SPEC12", STATUS_SPEC6:"SPEC6"}
            log[f"{t.id}_Task"] = status_map.get(t.status, "REP")
            log[f"{t.id}_Tag"] = "-"
            log[f"{t.id}_Code"] = "-"
            log[f"{t.id}_KM"] = round(t.mileage); log[f"{t.id}_Group"] = t.group

        logs_schedule.append(log)

    if progress_callback: progress_callback(day, 4000)
    
    rep_data = []
    for t in fleet:
        if t.log_repair_start_day:
            rep_data.append({
                "TrainID": t.id,
                "Group": t.group,
                "StartDay": t.log_repair_start_day,
                "StartDate": day_to_date(t.log_repair_start_day),
                "EntryMileage": t.log_repair_entry_mileage
            })
    df_rep = pd.DataFrame(rep_data)
    if not df_rep.empty:
        df_rep.sort_values("StartDay", inplace=True)
        
    return df_rep, pd.DataFrame(logs_schedule)