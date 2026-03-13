import cv2
import numpy as np
import pyautogui
import serial
import time
import sys
import keyboard
import os
import requests
import math
import random
import io
import threading
import serial.tools.list_ports # 아두이노 COM 포트 자동 스캔
from collections import deque  # 블랙박스 로그용
from datetime import datetime, date # 06시 정기보고 및 블랙박스 시간 기록
from PIL import Image # 이미지 캐싱용 메모리 관리 모듈
import json
import mss # [초고속 캡처 엔진]

# mss 객체는 딱 한 번만 생성해두고 계속 재사용합니다 (메모리 최적화)
sct = mss.mss()

def fast_screenshot(region=None):
    """pyautogui.screenshot()을 완벽히 대체할 mss 기반 초고속 캡처 함수"""
    if region:
        # pyautogui의 region(x, y, w, h) 형식을 mss 형식으로 번역
        monitor = {"left": int(region[0]), "top": int(region[1]), "width": int(region[2]), "height": int(region[3])}
    else:
        # 전체 화면 (기본 1번 모니터)
        monitor = sct.monitors[1]
    
    # 0.002초 만에 빛의 속도로 화면 캡처
    sct_img = sct.grab(monitor)
    
    # 기존 코드들이 에러를 뿜지 않도록, 원래 pyautogui가 주던 똑같은 'PIL Image' 형태로 변환해서 던져줍니다.
    return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

def fast_cv_screenshot(region=None, gray=True):
    """PIL을 거치지 않고 mss에서 즉시 OpenCV Numpy 배열(흑백/컬러)로 직행하는 극초고속 캡처 함수"""
    if region:
        monitor = {"left": int(region[0]), "top": int(region[1]), "width": int(region[2]), "height": int(region[3])}
    else:
        monitor = sct.monitors[1]
    
    sct_img = sct.grab(monitor)
    screen_np = np.array(sct_img)
    
    if gray:
        return cv2.cvtColor(screen_np, cv2.COLOR_BGRA2GRAY)
    else:
        return cv2.cvtColor(screen_np, cv2.COLOR_BGRA2BGR)

# [마법의 뇌수술] 이제부터 코드 전체의 pyautogui.screenshot은 fast_screenshot으로 강제 교체됩니다!
pyautogui.screenshot = fast_screenshot

# =========================================================================
# [환경 설정] 다른 PC 이동 시 여기만 확인하세요!
# =========================================================================
SCREEN_W, SCREEN_H = 1920, 1080  # 본인 해상도로 수정
CENTER_X, CENTER_Y = SCREEN_W // 2, SCREEN_H // 2
P_GAIN = 0.6  # 비례 제어 게인

# [스마트 로더] 로컬에 있는 config.json 파일을 읽어서 변수를 자동 세팅합니다.
config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# [유니버설 하이브리드 패치] 기본값 세팅
TELEGRAM_TOKEN = ""
CHAT_ID = ""
BOT_NAME = "베릭"
CMD_PREFIX = "/2"
USE_TELEGRAM = False # 텔레그램 스위치

try:
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)
        TELEGRAM_TOKEN = config_data.get("TELEGRAM_TOKEN", "")
        CHAT_ID = config_data.get("CHAT_ID", "")
        BOT_NAME = config_data.get("BOT_NAME", "베릭") 
        CMD_PREFIX = config_data.get("CMD_PREFIX", "/2") 
        
        # 토큰과 아이디가 모두 들어있을 때만 스위치 ON
        if TELEGRAM_TOKEN.strip() and CHAT_ID.strip():
            USE_TELEGRAM = True
except FileNotFoundError:
    pass # 출력 순서를 미루기 위해 맨 위에서는 아무것도 띄우지 않고 조용히 넘어갑니다.
# =========================================================================

# --- 블랙박스 링 버퍼 시스템 ---
blackbox_buffer = deque(maxlen=20) # 항상 최신 20개의 로그만 기억

def bprint(msg):
    """일반 콘솔 출력과 동시에 블랙박스에 시간을 찍어 기록합니다."""
    print(msg)
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    blackbox_buffer.append(f"[{time_str}] {msg}")

def dump_blackbox_log(reason):
    """오류 발생 시 기억하고 있던 20개의 로그를 파일로 영구 보존합니다."""
    try:
        time_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = "fishing_blackbox.txt"
        with open(filename, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*50}\n")
            f.write(f"🛑 [BLACKBOX DUMP] 발생 원인: {reason} | 시간: {time_str}\n")
            f.write(f"{'='*50}\n")
            for log in blackbox_buffer:
                f.write(log + "\n")
        bprint(f"  > [Blackbox] 오류 전후 로그 20개가 '{filename}'에 보존되었습니다.")
    except Exception as e:
        print(f"블랙박스 저장 실패: {e}")

# 즉각 정지를 위한 커스텀 예외 클래스
class BotStopException(Exception): pass

# 모든 time.sleep 에 일괄적으로 +-0.05초 랜덤 딜레이 적용
original_sleep = time.sleep

def jitter_sleep(seconds):
    global bot_active
    # -0.05 ~ 0.05초 사이의 랜덤한 실수 생성
    jitter = random.uniform(-0.05, 0.05)
    # 딜레이가 0보다 작아지는 것을 방지
    final_time = max(0, seconds + jitter)
    
    # 함수가 실행될 때(대기 시작 시점)의 봇 가동 상태를 기억합니다.
    was_active_at_start = bot_active
    
    # [핵심] 대기 시간을 0.05초 단위로 쪼개어 감시
    start_t = time.time()
    while time.time() - start_t < final_time:
        # '가동 중'에 sleep에 들어왔는데, 도중에 '정지(False)'로 바뀌었을 때만 즉시 폭파!
        # (원래 정지 상태에서 대기하는 경우에는 예외를 던지지 않고 조용히 정상 대기함)
        if was_active_at_start and not bot_active:
            raise BotStopException()
        original_sleep(max(0, min(0.05, final_time - (time.time() - start_t))))

# 기존 time.sleep 함수를 커스텀 함수로 바꿔치기
time.sleep = jitter_sleep

# 파이썬 내장 keyboard 라이브러리 가로채기 (가짜 키 입력 인식용)
remote_keys = {'[': False, ']': False}
orig_is_pressed = keyboard.is_pressed

def _custom_is_pressed(key):
    # 원격으로 찔러준 키라면 무조건 True 반환, 아니면 원래 키보드 상태 확인
    if remote_keys.get(key, False): return True
    return orig_is_pressed(key)

# 기존 함수 덮어쓰기 (뇌수술)
keyboard.is_pressed = _custom_is_pressed

# 통계 결산용 전역 변수 모음
stats = {
    'daily_hook': 0,              # [일일] 입질
    'daily_catch': 0,             # [일일] 포획
    'daily_skip': 0,              # [일일] 잡어 스킵
    'total_hook': 0,              # [빅데이터] 누적 입질
    'total_catch': 0,             # [빅데이터] 누적 포획
    'total_skip': 0,              # [빅데이터] 누적 잡어 스킵
    'watchdog_recovery_count': 0, # 워치독 발동 횟수
    'inventory_clear_count': 0,   # 인벤토리 비움 발동 횟수
    'afk_bypass_count': 0,        # 잠수방지 해제 횟수
    'pure_run_time': 0.0,         # 순수 가동 시간 누적기 (초)
    'hourly_records': [],         # 1시간 단위 완료 기록 저장 리스트
    'last_snapshot_time': 0.0,    # 마지막 스냅샷 시점의 누적 가동 시간
    'last_snapshot_hook': 0,      # 스냅샷 기준 누적 입질
    'last_snapshot_catch': 0,     # 스냅샷 기준 누적 포획
    'last_snapshot_skip': 0       # 스냅샷 기준 누적 스킵
}

run_start_time = None 
last_update_id = 0
last_report_date = date.today() # 부팅 날짜 기록

STATS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats_cache.json")

def load_stats_cache():
    """부팅 시 하드디스크에 저장된 이전 통계 기록을 불러옵니다."""
    global stats, last_report_date
    try:
        if os.path.exists(STATS_CACHE_FILE):
            with open(STATS_CACHE_FILE, "r", encoding="utf-8") as f:
                saved_data = json.load(f)
                for k in stats.keys():
                    if k in saved_data:
                        stats[k] = saved_data[k]
                if "last_report_date" in saved_data:
                    last_report_date = datetime.strptime(saved_data["last_report_date"], "%Y-%m-%d").date()
    except Exception as e:
        print(f"  > [경고] 통계 캐시 불러오기 실패: {e}")

def save_stats_cache():
    """현재 메모리의 통계를 하드디스크에 영구 저장합니다."""
    try:
        save_data = stats.copy()
        save_data["last_report_date"] = last_report_date.strftime("%Y-%m-%d")
        with open(STATS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(save_data, f, indent=4, ensure_ascii=False)
    except:
        pass

# 파이썬 실행 시 즉시 통계 복구
load_stats_cache()

def get_formatted_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours}시간 {minutes}분 {secs}초"

def send_telegram_report():
    if not USE_TELEGRAM: return # 스위치가 꺼져있으면 즉시 차단 (통신 시도 안 함)
    current_run_time = stats['pure_run_time']
    if bot_active and run_start_time is not None:
        current_run_time += (time.time() - run_start_time)

    # 1. 일일 통계 (06시 초기화 기준)
    d_hook, d_catch, d_skip = stats['daily_hook'], stats['daily_catch'], stats['daily_skip']
    d_rate = round((d_catch / d_hook) * 100, 1) if d_hook > 0 else 0

    # 2. 누적 빅데이터 통계 (수동 초기화 전까지 영구 보존)
    t_hook, t_catch, t_skip = stats['total_hook'], stats['total_catch'], stats['total_skip']
    t_rate = round((t_catch / t_hook) * 100, 1) if t_hook > 0 else 0

    # 3. 1시간 평균 지표 (실제 누적 데이터 기반)
    records = stats['hourly_records']
    completed_hours = len(records)
    
    if completed_hours > 0:
        avg_hook = round(sum(r['hook'] for r in records) / completed_hours, 1)
        avg_catch = round(sum(r['catch'] for r in records) / completed_hours, 1)
        avg_skip = round(sum(r['skip'] for r in records) / completed_hours, 1)
        avg_rate = round((avg_catch / avg_hook) * 100, 1) if avg_hook > 0 else 0
        
        hourly_status_text = (
            f"⏳ <b>[1시간 평균 지표]</b> (총 {completed_hours}시간 누적)\n"
            f" ├ 평균 입질: {avg_hook}회/h\n"
            f" ├ 평균 포획: {avg_catch}마리/h\n"
            f" ├ 잡어 스킵: {avg_skip}회/h\n"
            f" └ 평균 성공률: {avg_rate}%\n"
        )
    else:
        hourly_status_text = (
            f"⏳ <b>[1시간 평균 지표]</b>\n"
            f" └ ⚠️ 가동 1시간 미만 (데이터 수집 중...)\n"
        )

    report_text = (
        f"📊 <b>[{BOT_NAME} 낚시봇 결산 보고서]</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"⏱️ <b>가동 시간:</b> {get_formatted_time(current_run_time)}\n"
        f"━━━━━━━━━━━━━━\n"
        f"📈 <b>[하루 누적 통계]</b> (06시 리셋)\n"
        f" ├ 총 입질: {d_hook}회\n"
        f" ├ 총 포획: {d_catch}마리\n"
        f" ├ 잡어 스킵: {d_skip}회\n"
        f" └ 성공률: {d_rate}%\n"
        f"━━━━━━━━━━━━━━\n"
        f"📚 <b>[전체 빅데이터 통계]</b>\n"
        f" ├ 총 입질: {t_hook}회\n"
        f" ├ 총 포획: {t_catch}마리\n"
        f" ├ 잡어 스킵: {t_skip}회\n"
        f" └ 성공률: {t_rate}%\n"
        f"━━━━━━━━━━━━━━\n"
        f"{hourly_status_text}"
        f"━━━━━━━━━━━━━━\n"
        f"⚠️ 워치독 복구: {stats['watchdog_recovery_count']}회\n"
        f"🎒 인벤 비움: {stats['inventory_clear_count']}회\n"
        f"🛡️ 잠수 해제: {stats['afk_bypass_count']}회"
    )
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "parse_mode": "HTML", "text": report_text}
    try:
        requests.post(url, json=payload, timeout=5)
        bprint("  > [Telegram] 결산 보고서 발송 완료")
    except Exception as e:
        bprint(f"  > [Telegram] 보고서 발송 에러: {e}")

def reset_stats():
    """통계를 초기화하고 가동 스톱워치를 재정렬합니다."""
    global stats, run_start_time
    # [핵심] 일일 통계만 날리고, 빅데이터(total_~)와 1시간 평균(hourly_records)은 냅둡니다.
    stats['daily_hook'] = 0
    stats['daily_catch'] = 0
    stats['daily_skip'] = 0
    stats['watchdog_recovery_count'] = 0
    stats['inventory_clear_count'] = 0
    stats['afk_bypass_count'] = 0
    stats['pure_run_time'] = 0.0
    
    stats['last_snapshot_time'] = 0.0
    # last_snapshot 시리즈는 '빅데이터'를 기준으로 델타를 계산하므로 06시에 0으로 만들면 안 됩니다!
    # 대신 06시 시점의 누적값을 스냅샷으로 갱신해 주어 델타 계산이 튀지 않도록 안전하게 동기화합니다.
    stats['last_snapshot_hook'] = stats['total_hook']
    stats['last_snapshot_catch'] = stats['total_catch']
    stats['last_snapshot_skip'] = stats['total_skip']
    
    save_stats_cache() # 06시 정기 초기화 후 깨끗해진 상태를 세이브 파일에 덮어쓰기
    if bot_active:
        run_start_time = time.time()
    else:
        run_start_time = None

def _telegram_listener():
    if not USE_TELEGRAM: return # 스위치가 꺼져있으면 리스너 스레드 가동 자체를 차단
    global last_update_id, last_report_date
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    
    while True:
        try:
            # 1. 06시 정기 리포트 검사
            now = datetime.now()
            if now.hour == 6 and now.minute == 0:
                if last_report_date != now.date():
                    bprint("\n>>> [시스템] 오전 6시 정기 결산 및 데이터 초기화 작동 <<<")
                    send_telegram_report()
                    reset_stats()
                    last_report_date = now.date()
                    dump_blackbox_log("06시_정기_초기화_완료")

            # 2. 명령어 수신 검사
            params = {"offset": last_update_id + 1, "timeout": 5}
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            
            if data.get("ok") and data.get("result"):
                for item in data["result"]:
                    last_update_id = item["update_id"]
                    msg_text = item.get("message", {}).get("text", "").strip()
                    chat_id = str(item.get("message", {}).get("chat", {}).get("id", ""))
                    
                    if chat_id == CHAT_ID:
                        if msg_text == f"{CMD_PREFIX}시작":
                            bprint(f">>> [텔레그램 원격 제어] {BOT_NAME} 가동 트리거 <<<")
                            toggle_start()
                            remote_keys[']'] = True; original_sleep(0.3); remote_keys[']'] = False
                        
                        elif msg_text == f"{CMD_PREFIX}정지":
                            bprint(f">>> [텔레그램 원격 제어] {BOT_NAME} 정지 트리거 <<<")
                            toggle_stop()
                            remote_keys['['] = True; original_sleep(0.3); remote_keys['['] = False
                        
                        elif msg_text == f"{CMD_PREFIX}종료":
                            bprint(f">>> [텔레그램 원격 제어] {BOT_NAME} 긴급 종료 <<<")
                            force_exit()

                        elif msg_text.startswith(f"{CMD_PREFIX}입력"):
                            try:
                                parts = msg_text.split()
                                if len(parts) >= 2:
                                    input_str = parts[1].upper() # 무조건 대문자 변환
                                    
                                    key_map = {
                                        'E': ('E', 'ESC'),
                                        'ESC': ('E', 'ESC'),
                                        'L': ('L', '좌클릭'),
                                        'CLICK': ('L', '좌클릭'),
                                        'C': ('C', '챔질(C)'),
                                        'H': ('H', '보관(H)'),
                                        'F': ('F', '수거(F)')
                                    }
                                    
                                    arduino_cmd, display_name = key_map.get(input_str, (input_str, input_str))
                                    
                                    duration = 0.5
                                    if len(parts) >= 3:
                                        duration = float(parts[2])
                                    
                                    # 명령을 즉각 수행하지 못하는 병목을 유발하므로, 리스너 스레드에서 아두이노로 즉시 직결합니다.
                                    bprint(f"\n>>> [원격 터미널] 어떠한 간섭도 없이 순수하게 {display_name}({arduino_cmd}) 키를 {duration}초간 발사합니다. <<<")
                                    try:
                                        arduino.write(arduino_cmd.encode())
                                        arduino.flush()
                                        original_sleep(0.1) 
                                        arduino.write('R'.encode())
                                        arduino.flush()
                                        
                                        url_msg = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                                        requests.post(url_msg, json={"chat_id": CHAT_ID, "text": f"⌨️ [터미널] {display_name}키 전송 완료"})
                                    except Exception as e:
                                        bprint(f"  > [터미널 전송 오류] {e}")
                                else:
                                    bprint("  > [원격 터미널] 키 인자가 누락되었습니다. (예: /2입력 w 3)")
                            except Exception as e:
                                bprint(f"  > [원격 터미널] 파싱 오류: {e}")
                            
                        elif msg_text == f"{CMD_PREFIX}보고서":
                            bprint(f">>> [텔레그램 원격 제어] {BOT_NAME} 보고서 요청 수신 <<<")
                            send_telegram_report()

                        elif msg_text == f"{CMD_PREFIX}상태":
                            bprint(f">>> [텔레그램 원격 제어] {BOT_NAME} CCTV 요청 수신 <<<")
                            try:
                                current_time = time.strftime("%H:%M:%S")
                                # [버그 픽스] 스레드 간 mss 인스턴스 충돌(_thread._local 에러) 방지를 위해 텔레그램 전용 일회용 캡처기 생성
                                with mss.mss() as temp_sct:
                                    sct_img = temp_sct.grab(temp_sct.monitors[1])
                                    screenshot = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                                
                                status_str = "🟢 정상 가동 중" if bot_active else "🔴 대기 중 (정지 상태)"
                                msg = f"👀 실시간 CCTV 모니터링\n\n상태: {status_str}\n금일 포획량: {stats['daily_catch']}마리 (총 누적: {stats['total_catch']}마리)"
                                
                                threading.Thread(target=_telegram_worker, args=(msg, screenshot, current_time), daemon=True).start()
                            except Exception as e:
                                bprint(f"  > [Telegram] CCTV 캡처 에러: {e}")

                        elif msg_text == f"{CMD_PREFIX}메뉴":
                                    bprint(f">>> [텔레그램 원격 제어] {BOT_NAME} 터치형 리모컨 UI 출력 <<<")
                                    
                                    # [스마트 UI] 텔레그램 하단에 앱처럼 고정되는 커스텀 키보드 배치
                                    reply_markup = {
                                        "keyboard": [
                                            [{"text": f"{CMD_PREFIX}시작"}, {"text": f"{CMD_PREFIX}정지"}, {"text": f"{CMD_PREFIX}상태"}],
                                            [{"text": f"{CMD_PREFIX}보고서"}, {"text": f"{CMD_PREFIX}종료"}],
                                            [{"text": f"{CMD_PREFIX}입력 F"}, {"text": f"{CMD_PREFIX}입력 E"}, {"text": f"{CMD_PREFIX}입력 H"}]
                                        ],
                                        "resize_keyboard": True, # 스마트폰 화면에 맞게 버튼 크기 자동 조절
                                        "one_time_keyboard": False # 한 번 누르고 사라지지 않게 영구 고정
                                    }
                                    
                                    url_msg = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                                    payload = {
                                        "chat_id": CHAT_ID,
                                        "text": "📱 **리모컨 UI가 활성화되었습니다.**\n채팅창 하단의 버튼을 터치하여 봇을 제어하세요.",
                                        "parse_mode": "Markdown",
                                        "reply_markup": reply_markup
                                    }
                                    try:
                                        requests.post(url_msg, json=payload, timeout=5)
                                    except Exception as e:
                                        bprint(f"  > [Telegram] 메뉴 UI 출력 에러: {e}")

                        elif msg_text == f"{CMD_PREFIX}초기화":
                            bprint(f">>> [텔레그램 원격 제어] {BOT_NAME} 빅데이터 수동 초기화 요청 수신 <<<")
                            stats['total_hook'] = 0
                            stats['total_catch'] = 0
                            stats['total_skip'] = 0
                            stats['hourly_records'] = []
                            stats['last_snapshot_hook'] = stats['daily_hook']
                            stats['last_snapshot_catch'] = stats['daily_catch']
                            stats['last_snapshot_skip'] = stats['daily_skip']
                            save_stats_cache()
                            
                            url_msg = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                            requests.post(url_msg, json={"chat_id": CHAT_ID, "text": "♻️ **[빅데이터 초기화 완료]**\n전체 누적 통계 및 1시간 평균 지표가 0으로 깨끗하게 리셋되었습니다."}, timeout=5)

        except:
            pass 
        original_sleep(1) 

threading.Thread(target=_telegram_listener, daemon=True).start()

def _telegram_worker(msg, screenshot, current_time):
    """실제 사진 압축과 업로드를 담당하는 백그라운드 일꾼 (메인 봇 속도에 영향을 주지 않음)"""
    if not USE_TELEGRAM: return # 스위치가 꺼져있으면 스크린샷 전송 즉시 차단
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    caption_text = (
        f"🔔 <b>[{BOT_NAME} 낚시봇 알림]</b>\n"
        f"⏱️ 발생 시간: {current_time}\n"
        f"━━━━━━━━━━━━━━\n"
        f"💬 {msg}\n"
        f"━━━━━━━━━━━━━━"
    )
    data = {"chat_id": CHAT_ID, "caption": caption_text, "parse_mode": "HTML"}
    
    try:
        # [최적화 핵심] 메인 봇이 안 기다리게, 백그라운드 스레드 안에서 무거운 PNG 압축을 수행합니다.
        img_byte_arr = io.BytesIO()
        screenshot.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)
        
        files = {"photo": ("screenshot.png", img_byte_arr, "image/png")}
        requests.post(url, data=data, files=files, timeout=10)
        print(f"  > [Telegram] 스크린샷 알림 전송 완료 (백그라운드): {msg}")
    except Exception as e:
        print(f"  > [Telegram] 발송 에러 발생: {e}")

def send_blynk_notification(msg):
    """특이사항(인벤풀, 잠수방지 등) 발생 시 스크린샷을 찍고 딜레이 없이 스레드로 전송합니다."""
    current_time = time.strftime("%H:%M:%S")
    
    try:
        # 1. 화면 캡처만 0.02초 만에 찰칵! 하고 찍습니다 (초고속)
        screenshot = pyautogui.screenshot()
        
        # 2. 무거운 PNG 변환과 전송 작업은 스레드에 던져버리고 메인 봇은 즉시 다음 줄(F키 누르기 등)로 넘어갑니다!
        threading.Thread(target=_telegram_worker, args=(msg, screenshot, current_time), daemon=True).start()
        
    except Exception as e:
        print(f"  > [Telegram] 스크린샷 캡처 에러: {e}")

# [Integrity Guard] 전역 설정
pyautogui.raisePyAutoGUIImageNotFoundException = False

is_manual_stop = True
bot_active = False
remote_task = None # [핵심] 텔레그램 스레드와 메인 봇을 연결하는 지시용 우체통 변수

def toggle_stop():
    global bot_active, run_start_time
    
    # 사용자가 '정지됨'을 명확히 알 수 있도록 즉각적인 피드백 출력
    if bot_active:
        bprint("\n=============================================")
        bprint("🔴 [정지 명령 접수] 단축키([) 입력 감지")
        bprint("=============================================")
    else:
        # 멈춘 줄 모르고 여러 번 눌렀을 때의 답답함 해소용 안내
        print("  > [안내] 봇이 이미 정지(대기) 상태입니다.")
        return 

    if bot_active and run_start_time is not None:
        stats['pure_run_time'] += (time.time() - run_start_time)
        run_start_time = None
        
    bot_active = False
    save_stats_cache() # 봇이 대기 모드로 들어갈 때까지의 통계와 가동 시간을 안전하게 세이브
    try:
        # 강제 정지 시 마우스 좌클릭(당기기) 상태를 무조건 해제(U)
        arduino.write('U'.encode()); arduino.flush()
        arduino.write('R'.encode()); arduino.flush()
    except: 
        pass

def toggle_start():
    global bot_active, run_start_time
    bot_active = True
    run_start_time = time.time()
    bprint("\n=============================================")
    bprint("🟢 [시작 명령 접수] 단축키(]) 입력 감지")
    bprint("=============================================")

# --- 아두이노 자동 연결 (플러그 앤 플레이) ---
def auto_connect_arduino():
    ports = serial.tools.list_ports.comports()
    target_port = None
    
    # 1순위: 아두이노 칩셋 키워드 및 한국어 윈도우 드라이버 이름("직렬") 엄격 스캔
    for p in ports:
        desc = p.description.upper()
        # "직렬" 키워드를 추가하여 'USB 직렬 장치(COM3)'를 인식하도록 수정
        if "CH340" in desc or "ARDUINO" in desc or "USB SERIAL" in desc or "CP210" in desc or "FT232" in desc or "직렬" in desc:
            target_port = p.device
            break
            
    if target_port:
        bprint(f">>> [시스템] 아두이노 자동 감지 성공: {target_port} 포트 연결 중... <<<")
        return serial.Serial(target_port, 115200)
    else:
        # 아두이노가 정말로 안 꽂혀 있을 때만 이 문구가 뜹니다.
        bprint("!!! [오류] 아두이노 기기를 찾을 수 없습니다. USB를 확인하세요 !!!")
        sys.exit()

# 시스템 부팅 시 아두이노 자동 연결
try:
    arduino = auto_connect_arduino()
    time.sleep(2)
except Exception as e:
    print(f"시스템 중단: {e}")
    sys.exit()

def send_cmd(cmd):
    global bot_active
    # [핵심] 봇 정지 상태일 때 입력되는 모든 움직임/클릭 명령을 원천 차단하고 예외 발생
    if not bot_active and cmd not in ['U', 'R']:
        raise BotStopException()
    arduino.write(cmd.encode())
    arduino.flush()

# [스마트 비전 엔진] 이미지 램(RAM) 캐싱 저장소
IMAGE_CACHE = {} 

# 이미지별로 성공한 화면 비율(Scale)을 기억하여 렉을 방지하는 메모리
IMAGE_SCALE_CACHE = {}

class Box:
    """pyautogui의 리턴값과 100% 동일하게 동작하도록 만든 가짜 Box 객체 (하위 호환성 유지)"""
    def __init__(self, left, top, width, height):
        self.left = left
        self.top = top
        self.width = width
        self.height = height

def preload_all_images():
    """현재 폴더의 모든 PNG 이미지를 봇 구동 시점에 '흑백(Grayscale) cv2 포맷'으로 사전 적재합니다."""
    global IMAGE_CACHE
    count = 0
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    for filename in os.listdir(base_dir):
        if filename.lower().endswith('.png'):
            try:
                full_path = os.path.join(base_dir, filename)
                # [안전 업그레이드] PIL 대신 cv2 흑백으로 즉시 캐싱 + 한글 경로 에러 방지
                img_array = np.fromfile(full_path, np.uint8)
                img = cv2.imdecode(img_array, cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    IMAGE_CACHE[filename] = img
                    count += 1
            except Exception as e:
                print(f"  > [경고] {filename} 메모리 적재 실패: {e}")
    bprint(f">>> [시스템] 총 {count}개의 이미지 캐싱 완료! (탐색 준비 끝) <<<\n")

def safe_find_image(img_path, conf=0.6, region=None):
    """[배포용 범용 엔진] 해상도/창 크기를 무시하고 화면 전체에서 다중 스케일링으로 이미지를 찾아냅니다."""
    global IMAGE_CACHE
    global IMAGE_SCALE_CACHE
    template = IMAGE_CACHE.get(img_path)
    
    try:
        if template is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            full_path = os.path.join(base_dir, img_path)
            img_array = np.fromfile(full_path, np.uint8)
            template = cv2.imdecode(img_array, cv2.IMREAD_GRAYSCALE)
            if template is not None:
                IMAGE_CACHE[img_path] = template
            else:
                return None

        # 배포용은 Region 파라미터가 들어와도 무시하고 항상 전체 화면을 캡처하여 오류를 막습니다.
        target_monitor = sct.monitors[1]
        sct_img = sct.grab(target_monitor)
        screen_np = np.array(sct_img)
        screen_gray = cv2.cvtColor(screen_np, cv2.COLOR_BGRA2GRAY)

        # [핵심 최적화] 가장 최근에 성공했던 크기(Scale)를 1순위로 꺼내옵니다.
        last_scale = IMAGE_SCALE_CACHE.get(img_path, 1.0)
        
        # 1순위(기억) -> 원본 -> 90% -> 110% -> 80% -> 120% 순으로 유연하게 스캔합니다.
        scales = [last_scale, 1.0, 0.9, 1.1, 0.8, 1.2]
        unique_scales = []
        [unique_scales.append(x) for x in scales if x not in unique_scales]

        for scale in unique_scales:
            w, h = int(template.shape[1] * scale), int(template.shape[0] * scale)
            if w < 10 or h < 10: continue

            # 스케일이 1.0이면 리사이즈를 생략하여 CPU 낭비를 없앱니다.
            if scale == 1.0:
                resized = template
            else:
                resized = cv2.resize(template, (w, h), interpolation=cv2.INTER_AREA)

            res = cv2.matchTemplate(screen_gray, resized, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            
            # [오탐(False Positive) 완벽 차단 로직]
            # 크기를 억지로 줄여서 비교할 때는 흐릿해진 픽셀 때문에 아무 배경이나 정답으로 착각하는 '오탐'이 발생합니다.
            # 따라서 '이미 검증된 원래 비율(last_scale)'일 때는 요청한 conf를 그대로 적용하되,
            # '새로운 비율'을 찔러볼 때는 엉뚱한 배경을 잡지 못하게 무조건 0.85 이상의 깐깐한 잣대를 들이댑니다!
            required_conf = conf if scale == last_scale else max(conf, 0.85)
            
            if max_val >= required_conf:
                IMAGE_SCALE_CACHE[img_path] = scale # 다음 턴을 위해 성공한 창 크기 비율을 두뇌에 기억!
                real_x = max_loc[0] + target_monitor["left"]
                real_y = max_loc[1] + target_monitor["top"]
                return Box(real_x, real_y, w, h)

        return None
    except Exception as e:
        return None

# [v6 통합본] 이 블록 하나로 기존 엔진과 정렬 함수를 교체하세요.
last_success_scale = 1.0

def find_anchor_final(target_img_path):
    global last_success_scale
    try:
        screen_gray = fast_cv_screenshot(gray=True)
        img_array = np.fromfile(target_img_path, np.uint8)
        template = cv2.imdecode(img_array, cv2.IMREAD_GRAYSCALE)
        if template is None: return None

        ADJUST_Y = 0.35 # 하단 쏠림 방지 고정값
        scales = [last_success_scale, 0.4, 0.6, 0.8, 1.0, 1.2]
        unique_scales = []
        [unique_scales.append(x) for x in scales if x not in unique_scales]

        best_score = -1
        best_pos = None

        for scale in unique_scales:
            w, h = int(template.shape[1] * scale), int(template.shape[0] * scale)
            if w < 10 or h < 10: continue
            
            if scale == 1.0:
                resized = template
            else:
                resized = cv2.resize(template, (w, h), interpolation=cv2.INTER_AREA)
                
            res = cv2.matchTemplate(screen_gray, resized, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)

            # 앵커(닻) 역시 다른 스케일을 찔러볼 때는 허공을 앵커로 잡지 않도록 0.85의 엄격한 잣대를 적용합니다.
            req_conf = 0.65 if scale == last_success_scale else 0.85

            if max_val > req_conf and max_val > best_score:
                best_score = max_val
                last_success_scale = scale
                
                # [v6 핵심] 화면 중앙(1920/2) 기준 좌우 보정치 결정
                current_tx = max_loc[0] + (w / 2)
                if current_tx < 960: # 화면 왼쪽 영역
                    ADJUST_X = 0.35 # 왼쪽일 때 우측 쏠림을 방지하기 위해 타겟을 왼쪽으로 당김
                else: # 화면 오른쪽 영역
                    ADJUST_X = 0.5 # 오른쪽일 때 사용자님이 만족하신 중앙값 유지
                
                cx = max_loc[0] + (w * ADJUST_X)
                cy = max_loc[1] + (h * ADJUST_Y)
                best_pos = (int(cx), int(cy))
            if max_val > 0.9: break 
        return best_pos
    except: return None

def align_view_by_anchor(anchor_img):
    target = anchor_img[0] if isinstance(anchor_img, list) else anchor_img
    start_time = time.time()
    DYNAMIC_P = P_GAIN * 0.85 # 오버슈트 방지 게인

    while time.time() - start_time < 2.0 and bot_active:
        pos = find_anchor_final(target)
        if pos:
            tx, ty = pos
            # [수정] v5의 offset_x 로직을 제거하여 이중 보정을 막았습니다.
            err_x = tx - CENTER_X
            err_y = ty - CENTER_Y
            dist = math.hypot(err_x, err_y)
            
            if dist < 10: return True

            dx, dy = int(err_x * DYNAMIC_P), int(err_y * DYNAMIC_P)

            # 불응기 최소화 (정지 성능 유지)
            if abs(dx) < 2: dx = 0
            if abs(dy) < 2: dy = 0

            if dx != 0 or dy != 0:
                send_cmd(f'M{dx},{dy}')
            
            start_time = time.time() - 0.5 
            time.sleep(0.01)
        else:
            time.sleep(0.01)
            continue
    return False

def get_tension_status(exact_roi):
    """
    [v8 하이퍼 옵티마이즈] 무거운 이미지 검색 로직을 전부 폐기하고, 
    State 4 진입 시 딱 1번 찾아둔 110x110 좌표(exact_roi)만 빛의 속도로 캡처합니다.
    """
    if not exact_roi: return 0
    try:
        # 110x110 초소형 캡처 전용으로 속도 극대화 (약 0.001~0.005초 소요)
        target_img = fast_cv_screenshot(region=exact_roi, gray=False)
        
        # 명도와 채도를 엄격하게 깎아 핫핑크/레드만 반응하도록 유지 (fast_cv_screenshot은 BGR 반환이므로 BGR2HSV 사용)
        img_hsv = cv2.cvtColor(target_img, cv2.COLOR_BGR2HSV)
        lower_red1 = np.array([0, 145, 145])
        upper_red1 = np.array([10, 255, 255])
        lower_red2 = np.array([165, 160, 160])
        upper_red2 = np.array([180, 255, 255])
        
        mask1 = cv2.inRange(img_hsv, lower_red1, upper_red1)
        mask2 = cv2.inRange(img_hsv, lower_red2, upper_red2)
        mask = cv2.bitwise_or(mask1, mask2)
        
        red_pixels = cv2.countNonZero(mask)
        return red_pixels
        
    except Exception as e:
        return 0

def force_exit():
    global run_start_time
    bprint("\n\n!!! 긴급 종료 요청 감지 !!!")
    dump_blackbox_log("사용자_강제종료_요청") # [블랙박스 트리거 추가]
    
    # 봇이 종료되기 직전에 마지막 가동 시간을 쥐어짜서 기록하고 영구 세이브
    if bot_active and run_start_time is not None:
        stats['pure_run_time'] += (time.time() - run_start_time)
    save_stats_cache()
    try:
        send_cmd('U'); send_cmd('R')
    except: pass
    os._exit(1)

# --- [공통 로직] 잠수 방지 알림 제거 함수 ---
def check_exit_notification(skip_esc=False):
    """잠수 방지 알림이 뜨면 F를 눌러 즉시 제거합니다."""
    # 알림창이 화면을 가리고 있으므로 신뢰도를 높여 정확히 탐지합니다.
    if safe_find_image('exit_notice.png', 0.7):
        stats['afk_bypass_count'] += 1
        bprint("  > [긴급] 잠수 방지 알림 감지! 해제 시퀀스 진입")
        send_blynk_notification("⚠️ 잠수 방지 감지")
        dump_blackbox_log("잠수방지_알림_감지") # [블랙박스 트리거 추가]
        
        # 알림창 이미지가 화면에서 완전히 사라질 때까지 검증하며 F키 반복
        while safe_find_image('exit_notice.png', 0.7):
            send_cmd('F'); time.sleep(0.1); send_cmd('R')
            # [능동 추적] 고정 0.8초 대기 삭제, 알림창이 사라지는 즉시 딜레이 종료
            for _ in range(20):
                time.sleep(0.1)
                if not safe_find_image('exit_notice.png', 0.7):
                    break
            
            if safe_find_image('exit_notice.png', 0.7):
                bprint("  > [재시도] 알림창 잔존 확인. F키 재입력...")
        
        # 낚시 UI, 보관함 UI, 수거 UI 잔존 확인 (0.5초 간격 탐색)
        found_fishing = safe_find_image('fishing.png', 0.7)
        time.sleep(0.5) # 0.5초 딜레이
        found_specific_b = safe_find_image('specific_B.png', 0.7)
        time.sleep(0.5)
        found_catch = safe_find_image('catch_F.png', 0.7) # 수거 창 잔존 여부 확인
        
        # 셋 중 하나라도 발견되면 완전히 사라질 때까지 알맞은 키 반복
        if found_fishing or found_specific_b or found_catch:
            bprint("  > 잔존 UI(낚시/보관/수거) 확인 -> 알맞은 키(F 또는 ESC)로 강제 회수 루프 진입")
            while True:
                # 1. 수거 창이 발견되면 F 입력
                if safe_find_image('catch_F.png', 0.7):
                    send_cmd('F'); time.sleep(0.2); send_cmd('R')
                # 2. 낚시나 보관함 창이 발견되면 ESC(E) 입력
                elif safe_find_image('fishing.png', 0.7) or safe_find_image('specific_B.png', 0.6):
                    send_cmd('E'); time.sleep(0.2); send_cmd('R')
                # 3. 모두 사라졌으면 루프 탈출
                else:
                    break
                time.sleep(0.5) # UI 갱신을 위한 대기
            bprint("  > [성공] UI 회수 완료.")
        
        # UI 회수 여부와 무관하게 공통적으로 위치 보정 수행
        bprint("  > [보정] 잠수 방지 후 위치 보정: S(후진) 1초 -> W(전진) 1초")
        time.sleep(1)
        send_cmd('S'); time.sleep(1.0); send_cmd('R')
        time.sleep(0.2) # 키 입력 간 짧은 안정화
        send_cmd('W'); time.sleep(1.0); send_cmd('R')
        
        # skip_esc가 False일 때만 초기화 명령(R)을 보냄
        if not skip_esc:
            send_cmd('R')
            
        bprint("  > [완료] 잠수 방지 시퀀스 종료. 0.5초 대기 후 복귀합니다.")
        send_blynk_notification("[완료]잠수 방지 해제")
        time.sleep(0.5) # 최종 0.5초 딜레이 후 복귀
        return True
    return False

def fishing_bot(max_allowed_seconds):
    # 함수 내부에서도 전역 변수를 쓰겠다고 선언해야 오류가 나지 않습니다.
    global bot_active, run_start_time
    
    # 봇이 켜지자마자 가장 먼저 사진들부터 메모리에 싹 다 올립니다.
    preload_all_images()
    
    send_cmd('R')
    time.sleep(0.5)

    keyboard.add_hotkey('[', toggle_stop)
    keyboard.add_hotkey(']', toggle_start)

    # 텔레그램 모드가 켜져 있을 때만 원격 제어 안내 간판을 띄웁니다.
    if USE_TELEGRAM:
        bprint(f"\n>>> 원격 제어 수신 대기 시작 (명령어: {CMD_PREFIX}시작, {CMD_PREFIX}정지, {CMD_PREFIX}종료, {CMD_PREFIX}보고서, {CMD_PREFIX}상태) <<<\n")
    else:
        # [출력 순서 변경] 캐싱이 완전히 끝난 직후 오프라인 홍보 문구를 띄웁니다.
        # 첫 줄 앞과 마지막 줄 뒤에 \n을 넣어서 위아래로 깔끔하게 한 줄씩 간격을 벌립니다.
        print("\n🚫텔레그램 원격 봇 이용이 불가능한 오프라인 모드입니다.🚫")
        print("⬇️⬇️⬇️텔레그램 봇 이용문의는 판매자에게 연락 부탁드립니다.⬇️⬇️⬇️")
        print("📢카카오톡 오픈채팅: https://open.kakao.com/o/sB6Ca9ki\n")
    
    state = -1
    # 위에서 이미 \n으로 예쁘게 띄워두었으므로, 여기서는 \n을 제거해 줍니다.
    bprint("🚀낚시 매크로 V2.4 가동 시작🚀")
    bprint("(작동: ] , 정지: [ )")

    cast_fail_count = 0 # 연속 캐스팅 실패 감지용 카운터
    
    # [워치독] 상태 추적용 변수 초기화
    last_state = state
    state_start_time = time.time()

    while True:
        try:
            # [시간 누적 클러치] State 0(잠수방지 대기) 및 State -1(초기화/정지)에서는 스톱워치를 일시 정지합니다.
            if bot_active and state > 0:
                # 정상 낚시 모드 진입 시 스톱워치 가동
                if run_start_time is None:
                    run_start_time = time.time()
            else:
                # 낚시 중이 아닐 때 스톱워치가 돌고 있다면 정지하고 지금까지의 시간만 누적
                if run_start_time is not None:
                    stats['pure_run_time'] += (time.time() - run_start_time)
                    run_start_time = None

            # --- [1시간 단위 실제 통계 스냅샷 저장] ---
            # 클러치에 의해 run_start_time이 None이 되므로 대기 중에는 스냅샷 연산도 자동으로 멈춥니다.
            if bot_active and run_start_time is not None:
                current_total_time = stats['pure_run_time'] + (time.time() - run_start_time)
                
                # [라이선스 실시간 락] 누적 가동 시간이 할당된 시간을 초과하면 봇 강제 폭파
                if max_allowed_seconds > 0 and current_total_time >= max_allowed_seconds:
                    bprint(f"\n==================================================")
                    bprint(f"⏳ [이용 시간 만료] 할당된 이용 시간({max_allowed_seconds/3600}시간)을 모두 소진했습니다.")
                    bprint(f"관리자에게 문의하여 시간을 충전해주세요.")
                    bprint(f"==================================================")
                    send_blynk_notification("⏳ 이용 시간 만료 (봇 가동 중지)")
                    dump_blackbox_log("라이선스_누적시간_만료")
                    force_exit()

                if current_total_time - stats['last_snapshot_time'] >= 3600.0:
                    # 델타 연산은 영구히 깎이지 않는 빅데이터(total_*)를 기준으로 삼아야 정확합니다.
                    hook_delta = stats['total_hook'] - stats['last_snapshot_hook']
                    catch_delta = stats['total_catch'] - stats['last_snapshot_catch']
                    skip_delta = stats['total_skip'] - stats['last_snapshot_skip']
                    
                    stats['hourly_records'].append({'hook': hook_delta, 'catch': catch_delta, 'skip': skip_delta})
                    stats['last_snapshot_time'] += 3600.0
                    stats['last_snapshot_hook'] = stats['total_hook']
                    stats['last_snapshot_catch'] = stats['total_catch']
                    stats['last_snapshot_skip'] = stats['total_skip']
                    bprint(f"  > [통계 시스템] 1시간 단위 스냅샷 저장! (입질 {hook_delta} / 포획 {catch_delta} / 스킵 {skip_delta})")
                    save_stats_cache() # 1시간 스냅샷 달성 시 CMD가 꺼져도 안 날아가게 영구 세이브
            # ----------------------------------------
            
            # 1. 상태가 넘어가면(정상 진행) 타이머를 0초로 리셋
            if state != last_state:
                state_start_time = time.time()
                last_state = state
                
            # 2. 동일한 상태(State)에서 180초(3분) 이상 정체 시 "스마트 복구" 진행
            # [수정] 대기 상태(-1)와 잠수방지 전용 대기 상태(0)는 고장난 것이 아니므로 감시에서 아예 제외합니다.
            if bot_active and state not in [0, -1] and (time.time() - state_start_time > 180.0):
                stats['watchdog_recovery_count'] += 1
                bprint(f"\n!!! [긴급] 로직 꼬임 감지! (State {state}에서 180초 정체) !!!")
                bprint("  > [스마트 복구] 화면 스캔 및 자가 복구 시도 중...")
                send_blynk_notification(f"⚠️ 정체 감지. 스마트 복구 작동")
                dump_blackbox_log(f"워치독_180초정체_State{state}") # [블랙박스 트리거 추가]
                
                recovered = False
                
                # (0) [우선 실행] 블라인드 상호작용 (F) 시도
                # 어떤 화면이 떠 있든 가장 먼저 F 키를 눌러 상호작용/수거를 시도합니다.
                bprint("  > [분석 0단계] 블라인드 F 입력 선제 시도")
                send_cmd('F'); time.sleep(0.2); send_cmd('R'); time.sleep(1.0)
                
                # F 입력 후 UI 상태를 다시 확인하여 복구 여부를 판별합니다.
                if not safe_find_image('catch_F.png', 0.7) and not safe_find_image('specific_B.png', 0.6) and not safe_find_image('fishing.png', 0.7) and not safe_find_image('fishing_mode.png', 0.6):
                    recovered = True
                    bprint("  > [성공] 선제 F 입력으로 화면 복구 완료")

                # F 입력만으로 복구되지 않았을 경우 기존 분석 로직 진입
                if not recovered:
                    # (1) 수거 창(F) 잔존 꼬임
                    if safe_find_image('catch_F.png', 0.7):
                        bprint("  > [분석 1단계] 수거 창(F) 미처리. 강제 획득 시도")
                        for _ in range(10):
                            send_cmd('F'); time.sleep(0.2); send_cmd('R'); time.sleep(1.0)
                            if not safe_find_image('catch_F.png', 0.7):
                                recovered = True; break
                    
                    # (2) 보관함 창(B) 잔존 꼬임
                    elif safe_find_image('specific_B.png', 0.6):
                        bprint("  > [분석 2단계] 보관함 UI(B) 잔존. 강제 종료(ESC) 시도")
                        for _ in range(10):
                            send_cmd('E'); time.sleep(0.2); send_cmd('R'); time.sleep(1.0)
                            if not safe_find_image('specific_B.png', 0.6):
                                recovered = True; break
                                
                    # (3) 낚시 UI 잔존 꼬임
                    elif safe_find_image('fishing.png', 0.7) or safe_find_image('fishing_mode.png', 0.6):
                        bprint("  > [분석 3단계] 낚시 모드 잔존. 강제 취소(ESC) 시도")
                        for _ in range(10):
                            send_cmd('E'); time.sleep(0.2); send_cmd('R'); time.sleep(1.0)
                            if not safe_find_image('fishing.png', 0.7) and not safe_find_image('fishing_mode.png', 0.6):
                                recovered = True; break
                                
                    # (4) 기타 원인불명 (시점 꼬임 등) -> 위치 보정
                    else:
                        bprint("  > [분석 4단계] 원인불명(시점 꼬임 등). 위치 보정(S->W) 수행")
                        # F 입력은 0단계에서 선행되었으므로 S->W 시퀀스만 실행합니다.
                        send_cmd('S'); time.sleep(1.0); send_cmd('R'); time.sleep(0.2)
                        send_cmd('W'); time.sleep(1.0); send_cmd('R'); time.sleep(0.5)
                        recovered = True # 블라인드 처리 후 일단 재시도해봄
                    
                # 결과 판정 및 후속 조치
                if recovered:
                    bprint("  > [성공] 자가 복구 완료! 봇 초기화 상태로 복귀합니다.")
                    send_blynk_notification("✅ 복구 완료. 낚시 재개")
                    state = -1 # 사진 검증 상태로 강제 전환하여 처음부터 깔끔하게 재시작
                    last_state = -1
                    state_start_time = time.time()
                else:
                    bprint("  > [실패] 10회 시도에도 화면이 지워지지 않음. 봇을 강제 정지합니다.")
                    send_blynk_notification("❌ 복구 실패. 봇 강제 정지됨")
                    dump_blackbox_log("워치독_자가복구_최종실패") # [블랙박스 트리거 추가]
                    toggle_stop()
                    state_start_time = time.time()
                    
                continue

            # 봇 활성화 체크 및 정지/재시작(]) 즉시 반응 로직
            if not bot_active:
                time.sleep(0.1)
                state = -1 # 정지 상태일 때 강제로 초기화 상태(-1) 부여
                state_start_time = time.time() # [수정] 봇이 쉬는 동안에는 타이머가 흘러가지 않도록 매 순간 현재 시간으로 초기화합니다.
                continue

            if state == -1 or keyboard.is_pressed(']'):
                bprint("  > [시작 처리 완료] 시스템을 초기화하고 '작동 모드'로 전환합니다.")
                send_cmd('R'); time.sleep(1.0)
                
                # 봇 시작 시 특정 사진(예: start_condition.png) 확인
                if safe_find_image('start_condition.png', 0.75):
                    bprint("  > [확인] 정상 낚시 모드 진입 (State 1)")
                    state = 1
                else:
                    bprint("  > [대기] 대기 모드 작동 (State 0)")
                    state = 0

            # === [중요] 모든 State 시작 전 공통 체크 ===
            if check_exit_notification():
                send_cmd('U') # 혹시 모를 클릭 해제
                send_cmd('R')
                
                # 알림 처리 후에도 사진 유무를 다시 평가하여 복귀 상태 결정
                if safe_find_image('start_condition.png', 0.70):
                    state = 1
                else:
                    state = 0
                continue # 루프 처음으로 돌아가서 판단

            # --- [State 0] 잠수 방지 전용 모드 (대기) ---
            if state == 0:
                time.sleep(0.5) # CPU 부하를 방지하며 다음 루프의 공통 체크(알림 감시)만 반복
                continue

            # --- [State 1] 캐스팅 (좌클릭 홀딩 보강) ---
            if state == 1:
                if not bot_active: raise BotStopException() # [문제 2 해결] 즉각 폭파
                bprint(f"\n[State 1] 캐스팅 시퀀스 시작")

                # 캐스팅 전 시점 복구 (시각적 닻 정렬)
                align_view_by_anchor('anchor.png')
                if not bot_active: raise BotStopException() # 즉각 폭파
                
                # 1. 초기화 후 좌클릭 '꾹' 누르기 (Mouse.press 상태 진입)
                send_cmd('R'); time.sleep(0.1) # 충분한 초기화 대기
                send_cmd('L')                  # 'L' 명령 전송 (아두이노는 Mouse.press 수행)
                
                # [중요] 게임이 '차징 시작'을 인지할 수 있도록 물리적인 최소 대기 시간 부여
                time.sleep(0.3) 
                bprint("  > 좌클릭 유지 중 (Holding)... 게이지 탐색 시작")
                
                # 2. green_range.png 찾을 때까지 'L' 상태 유지하며 루프
                cast_start = time.time()
                found_gauge = False
                while time.time() - cast_start < 3.0 and bot_active:
                    if keyboard.is_pressed(']'): break
                    
                    # 게이지 포착 시에만 'U'(Release) 명령 전송 (region 제약을 풀어서 자유롭게 찾도록 개방)
                    if safe_find_image('green_range.png', 0.6):
                        send_cmd('U') # 즉시 떼기 (Mouse.releaseAll)
                        bprint("  > [성공] 게이지 포착 -> 즉시 투척")
                        found_gauge = True
                        break
                    time.sleep(0.01) # 스캔 주기 최적화
                
                # 루프를 빠져나왔는데 못 찾은 경우 안전을 위해 떼기
                if not found_gauge: 
                    send_cmd('U')
                    bprint("  > [타임아웃] 게이지 미포착 -> 강제 해제")
                
                if not bot_active: raise BotStopException() # 즉각 폭파

                # 3. fishing.png 확인 시 즉시 2단계로 (동적 대기)
                bprint("  > 찌 낙하 확인 대기...")
                wait_ui_start = time.time()
                found_ui = False
                
                while time.time() - wait_ui_start < 4.0 and bot_active:
                    if safe_find_image('fishing.png', 0.72):
                        bprint("  > [성공] fishing.png 포착 -> 즉시 2단계 전이")
                        found_ui = True
                        state = 2
                        break
                    time.sleep(0.1)

                if not bot_active: raise BotStopException() # [문제 1 해결] 전이 대기 중 [ 눌렀을 때 즉시 폭파

                # 알림 감지로 리셋된 경우 루프 재시작
                if state == 1 and not found_ui:
                    if bot_active: # 실패해서 리셋된 경우만
                         # 알림 때문이 아니라 UI 미발견인 경우 회수 로직 수행
                        if not check_exit_notification():
                             cast_fail_count += 1
                             bprint(f"  > [실패] UI 미발견 -> 낚싯대 회수 후 재투척 (연속 실패: {cast_fail_count}회)")
                             
                             # 연속 5회 실패 시 미끼 부족/위치 꼬임으로 판단하고 스마트 복구 강제 실행
                             if cast_fail_count >= 5:
                                 bprint("\n!!! [긴급] 캐스팅 연속 5회 실패! 스마트 복구 대기 !!!")
                                 # [문제 3 해결] 텔레그램 중복 발송을 막기 위해 알림 발송 코드는 제거하고 워치독 타이머만 조작
                                 state_start_time = time.time() - 200.0 # 스마트 복구 조건(180초 정체) 강제 달성
                                 cast_fail_count = 0
                                 continue
                                 
                             send_cmd('C'); time.sleep(0.1); send_cmd('R')
                             time.sleep(2.5)
                        state = 1
                    continue
                else:
                    cast_fail_count = 0 # 정상적으로 찌 낙하(UI 발견) 시 카운터 초기화

            # --- [State 2] 입질 대기 ---
            elif state == 2:
                if not bot_active: raise BotStopException()
                if safe_find_image('green_float.png', 0.65):
                    bprint("  > [!] 입질 확인! 챔질(C)!"); send_cmd('C'); time.sleep(0.1); send_cmd('R')
                    time.sleep(0.2); state = 3

            # --- [State 3] 어종 판별 및 줄 끊기 (정밀 검증 강화) ---
            elif state == 3:
                if not bot_active: raise BotStopException()
                stats['daily_hook'] += 1
                stats['total_hook'] += 1
                bprint(f"[State 3] 어종 고속 분석 중... (금일 입질: {stats['daily_hook']}회 / 누적: {stats['total_hook']}회)")
                found_target = False
                target_name = ""

                # [배포용 수정] 낡은 하드코딩 방식을 버리고, 해상도 자동 조절이 완벽히 적용된 safe_find_image 엔진에 판독을 맡깁니다.
                for i in range(60):
                    if keyboard.is_pressed(']'): break

                    # 1. 해파리 고속 판별
                    if safe_find_image('jellyfish.png', 0.6):
                        found_target = True; target_name = "해파리"; break

                    # 2. 뱀장어 고속 판별
                    if safe_find_image('eel.png', 0.6):
                        found_target = True; target_name = "전기뱀장어"; break
                    
                    # 3. 등록된 잡어 고속 판별 (조기 차단)
                    found_trash = False
                    for trash_img in ['none1.png', 'none2.png', 'none3.png']:
                        if safe_find_image(trash_img, 0.6):
                            found_trash = True; break
                    
                    if found_trash:
                        bprint("  > [잡어] 등록된 잡어 UI 확인 -> 즉시 줄 끊기 진입")
                        break

                    time.sleep(0.05)

                if found_target:
                    bprint(f"  > [성공] {target_name} 발견! -> 파이팅 진입"); state = 4
                else:
                    bprint("  > [잡어] 목표 아님 -> 줄 끊기 시퀀스 진입")

                    stats['daily_skip'] += 1
                    stats['total_skip'] += 1

                    while bot_active:
                        if keyboard.is_pressed(']'): state = 1; break
                        
                        # 1. ESC를 꾹 눌러서 줄 끊기 실행
                        bprint("  > [시도] ESC 누르기 (0.5초)...")
                        send_cmd('E'); time.sleep(0.5); send_cmd('R')
                        
                        # 2. 서버 응답 대기 (1초) 도중 알림이 뜨면?
                        # -> F만 눌러서 끄고(skip_esc=True), 바로 캐스팅(state=1)으로 복귀
                        wait_end = time.time() + 1.0
                        while time.time() < wait_end:
                            # 여기가 핵심입니다! ESC를 또 누르지 않도록 설정
                            if check_exit_notification(skip_esc=True): 
                                state = 1
                                break
                            time.sleep(0.1)
                        
                        # 알림이 감지되어 리셋됐다면, 아래 로직 수행하지 말고 루프 탈출
                        if state == 1: break
                        
                        # 3. UI가 여전히 남아있는지 재검사 (정밀 확인)
                        # fishing.png나 fishing_mode.png 중 하나라도 발견되면 아직 낚시 중인 것
                        is_still_fishing = safe_find_image('fishing.png', 0.6) or safe_find_image('fishing_mode.png', 0.6)
                        
                        if not is_still_fishing:
                            # 이미지가 둘 다 검색되지 않아야만 진정한 성공
                            bprint("  > [완료] UI 소멸 확인. 줄 끊기 성공.")
                            state = 1
                            time.sleep(1.5) # 회수 애니메이션 최종 대기
                            break
                        else:
                            # 이미지가 아직 남아있다면 끊기 실패로 간주하고 루프 재시작
                            bprint("  > [실패] UI 잔존 감지. 다시 줄 끊기 시도...")
                            # 여기서 break 없이 다시 위로 올라가서 ESC를 누릅니다.

            # --- [State 4] 파이팅 (QTE 로직 및 함수 구조 완전 복구) ---
            elif state == 4:
                bprint("[State 4] 파이팅 모드 진입")
                
                # 1. 진입 초기 UI 안착 대기
                wait_ui_start = time.time()
                while time.time() - wait_ui_start < 2.0 and bot_active:
                    if safe_find_image('fishing_mode.png', 0.6): break
                    time.sleep(0.1)

                if not bot_active: raise BotStopException()

                time.sleep(0.2)
                missing_ui_count = 0 
                
                # [핵심 1] 렌즈 고정 및 동적 스케일링 연산
                gauge_roi = None
                dynamic_threshold = 15 # 기본 임계값
                ui_pos = safe_find_image('fishing_mode.png', 0.6)
                
                if ui_pos:
                    # 현재 모니터 해상도에 맞게 스케일링된 비율(0.8배, 1.2배 등)을 가져옵니다.
                    current_scale = IMAGE_SCALE_CACHE.get('fishing_mode.png', 1.0)
                    
                    cx = ui_pos.left + ui_pos.width // 2
                    cy = ui_pos.top + ui_pos.height // 2
                    
                    # 110x110 박스도 현재 해상도 비율에 맞춰서 줄이거나 늘립니다. (예: 4K면 200x200으로 커짐)
                    half_size = int(55 * current_scale)
                    full_size = half_size * 2
                    x1 = max(0, cx - half_size)
                    y1 = max(0, cy - half_size)
                    gauge_roi = (int(x1), int(y1), full_size, full_size)
                    
                    # 빨간색 픽셀 임계값(15)도 해상도 면적(제곱)에 비례하여 동적으로 뻥튀기합니다!
                    dynamic_threshold = max(5, int(15 * (current_scale ** 2)))
                else:
                    gauge_roi = (CENTER_X - 55, CENTER_Y - 55, 110, 110)
                
                def check_status():
                    nonlocal missing_ui_count
                    if check_exit_notification(): return "RESET"

                    # QTE 탐지
                    for img, key in [('press_A.png', 'A'), ('press_D.png', 'D')]:
                        if safe_find_image(img, 0.6):
                            missing_ui_count = 0 
                            send_cmd('U') # QTE 시 당기기 중지
                            bprint(f"  ! [QTE] {key} 대응 시작")
                            while safe_find_image(img, 0.6) and bot_active:
                                send_cmd(key); time.sleep(0.05)
                            send_cmd('R')
                            return "QTE"

                    if safe_find_image('catch_F.png', 0.7): return "FINISH"

                    if not safe_find_image('fishing_mode.png', 0.6): 
                        missing_ui_count += 1

                        if missing_ui_count >= 50: 
                            bprint("  > [유실] 낚시 UI가 장시간 보이지 않음 -> 초기화")
                            dump_blackbox_log("파이팅중_UI장시간유실")
                            return "RESET" 
                    else:
                        missing_ui_count = 0 
                    
                    return "KEEP"

                bprint("  > [AI 파이팅] 110x110 고정 렌즈 추적 시작 (프레임 널뛰기 방지 패치)")
                is_pulling = False 
                last_ui_check = time.time()
                
                fight_start_time = time.time() # 파이팅 진입 시간 기록
                
                while True: # bot_active로 스르륵 탈출 방지
                    if not bot_active: raise BotStopException() # 즉시 폭파

                    # 1. [연산 최적화] 매번 찾지 않고, 110x110 초소형 영역만 0.001초 만에 스캔
                    red_count = get_tension_status(gauge_roi)
                    
                    # 파이팅 진입 직후 1.5초 동안은 애니메이션 붉은 잔상을 무시하고 무조건 당김!
                    if time.time() - fight_start_time < 1:
                        if not is_pulling:
                            send_cmd('L')
                            is_pulling = True
                        time.sleep(0.01)
                    else:
                        # 2. 동적 반응 임계값 적용 (해상도에 맞춰 조절된 픽셀 수 사용)
                        if red_count >= dynamic_threshold:
                            if is_pulling:
                                send_cmd('U') 
                                is_pulling = False
                            time.sleep(0.01) # 식힐 때 확실히 대기 (잔상 방지)
                        else:
                            if not is_pulling:
                                send_cmd('L') 
                                is_pulling = True
                            time.sleep(0.01) # 연산 딜레이 최소화 (초고속 반응 유지)
                    
                    # [핵심 2] 랜덤 렉의 진짜 원인 해결 (병목 제거)
                    # 전체 화면을 뒤지는 무거운 check_status()를 매 턴마다 실행하지 않고, 0.1초에 1번만 확인합니다.
                    if time.time() - last_ui_check > 0.1:
                        res = check_status()
                        last_ui_check = time.time()
                        
                        if res == "FINISH": 
                            send_cmd('U'); state = 5; break
                        if res == "RESET": 
                            send_cmd('U'); state = 1; break
                        if res == "QTE": 
                            bprint("  > [QTE] 대응 완료. 0.01초 대기 후 텐션 조절 재개")
                            is_pulling = False
                            time.sleep(0.01)
                            continue

            # --- [State 5] 수거 (완결성 검증 로직) ---
            elif state == 5:
                bprint("[State 5] 수거 단계 진입")
                time.sleep(0.5)
                wait_f_start = time.time()
                while True: # bot_active 조건으로 스르륵 빠져나가는 것 방지
                    # [v10 통합] 어설프게 break로 빠져나가지 않고, 멈춤 감지 시 즉시 폭탄(예외)을 터뜨려 2번 로그로 직행합니다.
                    if not bot_active: raise BotStopException() 
                    
                    if check_exit_notification(): state = 1; break
                    if safe_find_image('catch_F.png', 0.7): break
                    if time.time() - wait_f_start > 10.0: state = 1; break
                    time.sleep(0.1)
                
                if state == 1: continue

                bprint("  > [수거] F 연타 및 소멸 검증 시작")
                f_spam_start = time.time()

                # 고속 스캔 템플릿 로드 (루프 밖 1회)
                template_f = cv2.imread('catch_F.png', cv2.IMREAD_GRAYSCALE)
                template_ar = cv2.imread('auto_release.png', cv2.IMREAD_GRAYSCALE)

                # --- 1단계: F 연타 및 0.15초 초고속 소멸 검증 ---
                while True: # bot_active로 스르륵 탈출 방지
                    if not bot_active: raise BotStopException() # 즉시 폭파
                    if check_exit_notification(): state = 1; break 
                    
                    if time.time() - f_spam_start > 10.0:
                        bprint("  > [경고] 10초 타임아웃 경과! 강제 상태 검증 진입")
                        break

                    screen_gray = fast_cv_screenshot(gray=True)
                    has_f_now = (template_f is not None and cv2.minMaxLoc(cv2.matchTemplate(screen_gray, template_f, cv2.TM_CCOEFF_NORMED))[1] >= 0.7)

                    if has_f_now:
                        send_cmd('F'); time.sleep(0.1); send_cmd('R')
                        time.sleep(0.1) # 애니메이션 안정화 대기
                    else:
                        # F가 안 보이면 0.05초 간격으로 3회(총 0.15초) 연속 확인하여 깜빡임 완벽 차단
                        really_gone = True
                        for _ in range(5):
                            time.sleep(0.05)
                            screen_check = fast_cv_screenshot(gray=True)
                            if template_f is not None and cv2.minMaxLoc(cv2.matchTemplate(screen_check, template_f, cv2.TM_CCOEFF_NORMED))[1] >= 0.7:
                                really_gone = False
                                break
                        
                        if really_gone:
                            stats['daily_catch'] += 1
                            stats['total_catch'] += 1
                            break # 완전히 사라졌으므로 1단계 수거 루프 즉시 탈출

                if not bot_active or state == 1: continue

                # --- 2단계: 상태 검증 및 방생 알림(auto_release) 0.5초 정밀 감시 ---
                bprint("  > [완료] 수거 동작 종료. 상태 검증 진입...")
                if check_exit_notification(): state = 1; continue

                is_inventory_full = False
                for _ in range(5):
                    time.sleep(0.1) # 0.1초씩 5회 = 0.5초간 감시
                    screen_ar = fast_cv_screenshot(gray=True)
                    if template_ar is not None and cv2.minMaxLoc(cv2.matchTemplate(screen_ar, template_ar, cv2.TM_CCOEFF_NORMED))[1] >= 0.65:
                        is_inventory_full = True
                        break # 발견 시 즉시 감시 종료

                # --- 3단계: 인벤토리 감지 결과에 따른 보관 또는 낚시 재개 ---
                # 위 0.5초 감시에서 포착했거나, 탈출 후 혹시 막 떴을 경우를 모두 커버
                if is_inventory_full or safe_find_image('auto_release.png', 0.7):
                    stats['inventory_clear_count'] += 1
                    bprint("\n\n!!! [알림] 자동 방생(인벤토리 풀) 감지 - 자동 보관 시퀀스 진입 !!!")
                    send_blynk_notification("⚠️ 인벤토리 풀 감지")
                    dump_blackbox_log("인벤토리_가득참_감지") # [블랙박스 트리거 추가]
                    
                    # S 입력 전 딜레이 1초 (정지 가능 모드)
                    if bot_active:
                        bprint("  > [보관] S 입력 전 1초 대기 중...")
                    delay_start = time.time()
                    while time.time() - delay_start < 1.0:
                        if not bot_active: raise BotStopException() # 즉시 폭파 (2번 로그 직행)
                        time.sleep(0.1)

                    # 앵커 정렬 전 위치 보정 시퀀스 (S 1초 -> W 1초)
                    bprint("  > [보관] 위치 보정 시작: S(후진) 1초")
                    send_cmd('S')
                    move_start = time.time()
                    while time.time() - move_start < 1.0:
                        if not bot_active: raise BotStopException() # 즉시 폭파
                        time.sleep(0.1)
                    send_cmd('R')

                    bprint("  > [보관] 위치 보정 계속: W(전진) 1초")
                    send_cmd('W')
                    move_start = time.time()
                    while time.time() - move_start < 1.0:
                        if not bot_active: raise BotStopException() # 즉시 폭파
                        time.sleep(0.1)
                    send_cmd('R')

                    # 1. 앵커 정렬
                    align_view_by_anchor('anchor.png')
                    if not bot_active: continue
                    time.sleep(0.1)

                    # 2. Y축 최하단 이동 및 보관함(A) 탐색
                    bprint("  > [보관] 2. 바닥 보기 및 보관함(A) 탐색")
                    while True: # bot_active로 스르륵 탈출 방지
                        if not bot_active: raise BotStopException() # 즉시 폭파
                        if check_exit_notification(): state = 1; break
                        send_cmd('M0,150'); time.sleep(0.5)
                        if safe_find_image('specific_A.png', 0.6):
                            bprint("  > [보관] 보관함(A) 발견")
                            break
                        time.sleep(0.2)
                    if state == 1: continue

                    # 3. F 입력 및 보관함(B) 확인
                    bprint("  > [보관] 3. 보관함 열기(F) 및 아이템 넣기(H)")
                    while True: # bot_active로 스르륵 탈출 방지
                        if not bot_active: raise BotStopException() # 즉시 폭파
                        if check_exit_notification(): state = 1; break
                        send_cmd('F'); time.sleep(0.1); send_cmd('R'); time.sleep(0.7)
                        is_b_opened = False
                        for _ in range(20):
                            time.sleep(0.1)
                            if safe_find_image('specific_B.png', 0.6):
                                is_b_opened = True
                                break
                        if is_b_opened:
                            bprint("  > [보관] 보관함 UI(B) 확인됨. 아이템 전송(H)")
                            send_cmd('H'); time.sleep(0.1); send_cmd('R')
                            time.sleep(0.2)
                            send_cmd('H'); time.sleep(0.1); send_cmd('R')
                            break
                        bprint("  > [보관] UI(B) 미발견, F 재입력...")
                    if state == 1: continue

                    # 4-1. 전송 시퀀스 (C 뜰 때까지 H 반복)
                    bprint("  > [보관] 4-1. 완료창(C) 대기 중 (H 입력 후 능동 확인)...")
                    c_scan_start = time.time()
                    while True: # bot_active로 스르륵 탈출 방지
                        if not bot_active: raise BotStopException() # 즉시 폭파
                        
                        if check_exit_notification(): state = 1; break
                        if time.time() - c_scan_start > 10.0:
                            bprint("  > [보관] 타임아웃(10초): 완료창(C) 미발견. 강제 UI 종료 시도.")
                            send_cmd('E'); time.sleep(1.0); send_cmd('R')
                            break

                        send_cmd('H'); time.sleep(0.1); send_cmd('R')
                        found_c = False
                        for _ in range(10):
                            time.sleep(0.1)
                            if safe_find_image('specific_C.png', 0.65):
                                found_c = True
                                break
                        if found_c:
                            bprint("  > [보관] 완료창(C) 포착! 전송 성공. 1차 ESC")
                            send_cmd('E'); time.sleep(1.0); send_cmd('R')
                            break
                        else:
                            bprint("  > [보관] 완료창(C) 미발견. H 재입력...")
                    if state == 1: continue

                    # 4-2. UI 정리 시퀀스 (B 소멸까지 ESC 반복)
                    bprint("  > [보관] 4-2. 보관함 UI(B) 소멸 확인 중 (ESC 반복)...")
                    while True: # bot_active로 스르륵 탈출 방지
                        if not bot_active: raise BotStopException() # 즉시 폭파
                        if check_exit_notification(): state = 1; break
                        if not safe_find_image('specific_B.png', 0.6):
                            bprint("  > [보관] UI(B) 소멸 확인 완료. 필드 복귀")
                            break
                        else:
                            bprint("  > [보관] UI(B) 잔존 확인. ESC 재시도...")
                            send_cmd('E'); time.sleep(0.8); send_cmd('R')
                    if state == 1: continue

                    # 5. 시점 복구 및 앵커 재탐색
                    bprint("  > [보관] 5. 시점 복구 및 낚시 재개 준비")
                    send_cmd('M0,-200'); time.sleep(1)
                    
                    if bot_active and not align_view_by_anchor('anchor.png'):
                        bprint("  > [보관] 앵커 재탐색 실패, 정밀 스캔 시도")
                        pos = find_anchor_final('anchor.png')
                        if not pos and bot_active:
                            pyautogui.locateOnScreen('anchor.png', confidence=0.4)
                    
                    if bot_active:
                        send_blynk_notification("[완료]인벤토리 비움. 낚시 재개")
                        bprint("  > [완료]인벤토리 비움. 낚시 재개")
                    state = 1
                else:
                    bprint("  > [정상] 특이사항 없음. 다음 낚시를 시작합니다.")
                    state = 1

            # 무조건 예외 처리기(BotStopException)로 넘김
            if not bot_active: 
                raise BotStopException()
                
        except BotStopException:
            # 예외가 발생하면 진행 중이던 모든 껍데기를 즉시 내던지고 여기로 빠져나옵니다.
            bprint("  > [정지 처리 완료] 모든 동작을 즉시 파기하고 '대기 모드'로 전환합니다.")
            try:
                # 안전을 위해 물리적 키보드나 마우스가 눌려있을 가능성을 대비해 초기화
                arduino.write('U'.encode()); arduino.flush()
                arduino.write('R'.encode()); arduino.flush()
            except: pass
            continue # 루프의 가장 처음으로 돌아가서 bot_active 상태를 대기함

# ==========================================
# [보안 로직] 런처 우회 실행 방지 및 시간 파라미터 수신
# ==========================================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("❌ 비정상적인 접근입니다. 보안 로더(loader.exe)를 통해 실행해주세요.")
        time.sleep(3)
        sys.exit()
        
    try:
        allocated_hours = float(sys.argv[1])
        max_seconds = allocated_hours * 3600
    except Exception:
        print("❌ 라이선스 데이터 수신 오류.")
        sys.exit()
        
    fishing_bot(max_seconds)
