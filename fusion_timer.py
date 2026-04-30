import cv2
import numpy as np
import pyautogui
import serial
import time
import sys
import keyboard
import os
import requests
import random
import io
import threading
import serial.tools.list_ports
from collections import deque
from datetime import datetime, date
from PIL import Image
import json
import mss
import winsound
import math
import screen_brightness_control as sbc
import hashlib

# [네트워크 고속화] 매번 새로운 연결을 맺는 requests.post 대신 열려있는 통로(Session)를 사용합니다.
# 이를 통해 DNS 조회 및 SSL 핸드쉐이크 시간을 0.1초 미만으로 단축합니다.
HTTP_SESSION = requests.Session()
# 재시도 로직 및 타임아웃을 위한 기본 어댑터 설정 (필요시)
HTTP_SESSION.mount('https://', requests.adapters.HTTPAdapter(max_retries=3))

# [밝기 제어 최적화] 낚시 매크로와 동일한 전역 브로드캐스트 방식으로 롤백
def set_all_monitors_brightness(target_val):
    try:
        sbc.set_brightness(target_val) # 연결된 모든 모니터 일괄 적용
    except: pass

# === [초고속 캡처 엔진] ===
sct = mss.mss()

def fast_cv_screenshot(region=None, gray=True):
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

# === [환경 설정] ===
SCREEN_W, SCREEN_H = 1920, 1080
CENTER_X, CENTER_Y = SCREEN_W // 2, SCREEN_H // 2

# [초고속화 1] PyAutoGUI의 숨겨진 기본 딜레이(0.1초) 강제 해제
pyautogui.PAUSE = 0
# [초고속화 2] OpenCV 내부 C++ 하드웨어 가속 최적화 강제 활성화
cv2.setUseOptimized(True)

config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
TELEGRAM_TOKEN = ""
CHAT_ID = ""
BOT_NAME = "베릭(융합)"
CMD_PREFIX = "/2"
USE_TELEGRAM = False

try:
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)
        TELEGRAM_TOKEN = config_data.get("TELEGRAM_TOKEN", "")
        CHAT_ID = config_data.get("CHAT_ID", "")
        BOT_NAME = config_data.get("BOT_NAME", "베릭(융합)") 
        CMD_PREFIX = config_data.get("CMD_PREFIX", "/2") 
        if TELEGRAM_TOKEN.strip() and CHAT_ID.strip():
            USE_TELEGRAM = True
except:
    pass

blackbox_buffer = deque(maxlen=20)

def bprint(msg):
    current_time = datetime.now().strftime("%H:%M:%S")
    print(f"[{current_time}] {msg}")
    full_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    blackbox_buffer.append(f"[{full_time}] {msg}")

# [V23.11 텔레그램 고속 세션 엔진] 
    # 발송 속도 때문에 메인 루프(이미지 인식)가 지연되는 것을 막기 위해 비동기 스레드 방식을 채택합니다.
    if USE_TELEGRAM:
        def _bg_send(m):
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                payload = {"chat_id": CHAT_ID, "text": f"[{BOT_NAME}] {m}"}
                # timeout을 3초로 제한하여 네트워크 장애 시에도 좀비 스레드 방지
                HTTP_SESSION.post(url, data=payload, timeout=3)
            except:
                pass
        # 0.001초 만에 스레드를 생성하고 본체는 즉시 다음 로직으로 복귀
        threading.Thread(target=_bg_send, args=(msg,), daemon=True).start()

def dump_blackbox_log(reason):
    try:
        time_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = "fusion_blackbox.txt"
        with open(filename, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*50}\n")
            f.write(f"🛑 [BLACKBOX DUMP] 발생 원인: {reason} | 시간: {time_str}\n")
            f.write(f"{'='*50}\n")
            for log in blackbox_buffer:
                f.write(log + "\n")
    except: pass

class BotStopException(Exception): pass

original_sleep = time.sleep
bot_active = False
bot_mode = 1 # 1: 단일 타이머, 2: 멀티 루프, 3: 자동 융합, 4: 5/5 복사, 5: 분별 모드
original_brightness = [100] # 모니터 원래 밝기 복구용 저장소 (다중 모니터 대응 리스트)
enable_dimming = False # [수정] 기본값을 '꺼짐(False)'으로 변경했습니다. 단축키(-)를 눌러야 활성화됩니다.
is_dimmed = False # 현재 밝기가 0%로 낮춰진 상태인지 추적
char_thread_active = False # 수동 캐릭터 변경 스레드 제어 플래그
char_inventory_memory = {} # 캐릭터별 인벤토리 탐색 위치 기억용 딕셔너리
ENABLE_POPUP_MAIN_CHECK = True # 자정 팝업 감지 기능 활성화 상태 변수

# [밝기 제어 최적화] 낚시 매크로와 동일한 전역 브로드캐스트 방식으로 롤백
def restore_monitors_brightness(bright_data):
    try:
        if isinstance(bright_data, list) and len(bright_data) > 0:
            sbc.set_brightness(bright_data[0]) # 리스트의 첫 번째 값으로 모든 모니터 일괄 복구
        else:
            sbc.set_brightness(bright_data)
    except: pass

# =====================================================================
# 👑 [캐릭터 마스터 컨트롤러] 👑
# 이곳에 캐릭터를 추가/수정/삭제하면 봇 전체의 모든 로직(단축키, 순서 등)이 100% 자동 적용됩니다!
#  - img: 캡처해둔 파일명 (1.png, 5.png 등)
#  - name: 로그에 출력될 예쁜 이름
#  - hotkey: 수동 접속 단축키 (F5 ~ F11 등 자유 지정)
#  - is_anchor: 타이머 보상을 수령할 앵커 캐릭터인지 (True는 파티에 딱 1명만!)
#  - use_fusion: 모드 3, 4 (자동 융합) 사이클에 포함시킬지 여부
# =====================================================================
MY_CHARACTERS = [
    {"img": "13.png", "name": "베릭핑크",  "hotkey": "F5", "is_anchor": False, "use_fusion": False},
    {"img": "5.png",  "name": "베릭산성1", "hotkey": "F6",  "is_anchor": True,  "use_fusion": True},
    {"img": "8.png",  "name": "베릭산성2", "hotkey": "F7",  "is_anchor": False, "use_fusion": True},
    {"img": "9.png",  "name": "베릭산성3", "hotkey": "F8",  "is_anchor": False, "use_fusion": True},
    {"img": "10.png", "name": "베릭유전1", "hotkey": "F9",  "is_anchor": False, "use_fusion": True},
    {"img": "11.png", "name": "베릭유전2", "hotkey": "F10", "is_anchor": False, "use_fusion": True},
    {"img": "12.png", "name": "베릭유전3", "hotkey": "F11", "is_anchor": False, "use_fusion": True}
]

# [1/5 자동화] 마스터 배열을 바탕으로 CHAR_NAMES 자동 생성
CHAR_NAMES = {c["img"]: c["name"] for c in MY_CHARACTERS}

def toggle_dimming_setting():
    global enable_dimming, is_dimmed, original_brightness
    enable_dimming = not enable_dimming
    
    print()
    if enable_dimming:
        bprint("💡 [설정 변경] '모니터 자동 절전' 기능이 [활성화] 되었습니다.")
        if bot_active and not is_dimmed:
            try:
                curr_b = sbc.get_brightness()
                if curr_b and any(b > 10 for b in curr_b): original_brightness = curr_b
                set_all_monitors_brightness(0)
                is_dimmed = True
                bprint("  > 🌙 [절전 모드] 즉시 모니터 밝기를 0%로 낮춥니다.")
            except: pass
    else:
        bprint("💡 [설정 변경] '모니터 자동 절전' 기능이 [비활성화] 되었습니다.")
        if bot_active and is_dimmed:
            try:
                restore_monitors_brightness(original_brightness)
                is_dimmed = False
                bprint(f"  > ☀️ [화면 복구] 즉시 모니터 밝기를 원래대로({original_brightness}%) 되돌립니다.")
            except: pass

def jitter_sleep(seconds):
    global bot_active, char_thread_active
    # [핵심] 수동 캐릭터 변경 스레드(char_thread_active)가 돌아가고 있을 때는 에러를 발생시키지 않고 무사통과시킵니다!
    if not bot_active and not char_thread_active: raise BotStopException() 
    jitter = random.uniform(-0.05, 0.05)
    final_time = max(0, seconds + jitter)
    was_active_at_start = bot_active
    start_t = time.time()
    
    while time.time() - start_t < final_time:
        if not bot_active and not char_thread_active:
            raise BotStopException()
        original_sleep(max(0, min(0.05, final_time - (time.time() - start_t))))

time.sleep = jitter_sleep

remote_keys = {'[': False, ']': False, '>': False}
orig_is_pressed = keyboard.is_pressed

def _custom_is_pressed(key):
    if remote_keys.get(key, False): return True
    return orig_is_pressed(key)
keyboard.is_pressed = _custom_is_pressed

# === [아두이노 통신] ===
def auto_connect_arduino():
    ports = serial.tools.list_ports.comports()
    target_port = None
    for p in ports:
        desc = p.description.upper()
        if "CH340" in desc or "ARDUINO" in desc or "USB SERIAL" in desc or "CP210" in desc or "FT232" in desc or "직렬" in desc:
            target_port = p.device
            break
    if target_port:
        bprint(f">>> [시스템] 아두이노({target_port}) 발견! 포트 개방 시도 중... <<<")
        # 무한 대기(행) 방지용 타임아웃 안전장치 추가
        return serial.Serial(target_port, 115200, timeout=2, write_timeout=2)
    else:
        bprint("!!! [오류] 아두이노를 찾을 수 없습니다 !!!")
        sys.exit()

try:
    arduino = auto_connect_arduino()
    bprint(">>> [시스템] 아두이노 포트 통신 개방 완벽 성공! <<<")
    original_sleep(2)
except Exception as e:
    bprint(f"\n!!! [치명적 오류] 아두이노 연결 실패: {e}")
    bprint("원인: 백그라운드에 꼬여있는 봇 프로세스가 포트를 점유 중이거나 장치 인식이 불안정합니다.")
    input("엔터키를 누르면 종료됩니다...")
    sys.exit(1)

def send_cmd(cmd, dx=None, dy=None):
    global bot_active, char_thread_active
    # 융합 매크로(bot_active)가 정지 상태여도, 수동 캐릭터 변경 스레드(char_thread_active)가 켜져 있다면 예외적으로 아두이노 명령을 허용합니다!
    if not bot_active and not char_thread_active and cmd not in ['U', 'R']:
        raise BotStopException()
    
    if dx is not None and dy is not None:
        data = f"{cmd}{dx},{dy}\n"
        arduino.write(data.encode())
    else:
        arduino.write(cmd.encode())
    arduino.flush()

def play_melody():
    """융합 완료 시 맑은 멜로디 출력"""
    for _ in range(2): 
        winsound.Beep(523, 150) 
        winsound.Beep(659, 150) 
        winsound.Beep(784, 400) 
        original_sleep(0.3)

def is_truly_tier_1(roi, x, y, h):
    # [수정] 중앙(center_y)만 검사하면 3번 숫자의 패인 공간(빈틈)을 만나 1로 오탐할 수 있습니다.
    # 전체 높이(y 부터 y+h 까지)를 모두 스캔하여 왼쪽에 픽셀이 하나라도 있으면 즉시 차단합니다!
    probe_x_start = max(0, x - 18)
    probe_x_end = max(0, x - 3)
    probe_y_start = max(0, y)
    probe_y_end = min(roi.shape[0], y + h)

    if probe_x_start >= roi.shape[1]: return True

    sample_area = roi[probe_y_start:probe_y_end, probe_x_start:probe_x_end]
    if sample_area.size == 0: return True

    # 숫자 몸통(밝은 픽셀)이 왼쪽에 감지되면 1이 아닙니다. 허용치를 40에서 50으로 살짝 조절하여 노이즈 대비.
    if np.max(sample_area) > 50: 
        return False 
    return True 

# === [AI 비전 엔진 및 융합 환경 설정] ===
FUSION_CONF = {
    'stop_btn.png': 0.85,
    '1.png': 0.75, '2.png': 0.75, '3.png': 0.75,
    '6.png': 0.75, '7.png': 0.75, '14.png': 0.70,
    'check_mark.png': 0.85,
    'get_reward.png': 0.85,
    'select_2_2.png': 0.85,
    'fusion_start.png': 0.85,
    'chance.png': 0.85,
    'level_5.png': 0.75,
    'fusion_material.png': 0.85,
    'select_0_2.png': 0.85,
    'popup_main.png': 0.85,
    'popup_char.png': 0.85, 
    'inv_title.png': 0.85,
    'trait.png': 0.70,
    
    'item_A1.png': 0.95, 'item_B1.png': 0.95,
    'item_A2.png': 0.95, 'item_B2.png': 0.95,
    
    'ability_label.png': 0.92,
    'tier_1.png': 0.72, 'tier_2.png': 0.72, 'tier_3.png': 0.72, 'tier_4.png': 0.72,
    'exit_notice.png': 0.85,
    'bug_time.png': 0.85
}

# [2/5 자동화] 마스터 배열 캐릭터들의 인식률(0.92)을 FUSION_CONF에 자동 등록
for c in MY_CHARACTERS:
    FUSION_CONF[c["img"]] = 0.92

FUSION_CACHE = {}
GRAY_IMAGES = [
    'stop_btn.png', '1.png', '2.png', '3.png', 
    '6.png', '7.png', '14.png',
    'get_reward.png', 'select_2_2.png', 'chance.png', 'fusion_material.png', 'select_0_2.png',
    'popup_main.png', 'popup_char.png', 'inv_title.png', 'ability_label.png', 'trait.png',
    'exit_notice.png', 'bug_time.png'
]

# [3/5 자동화] 마스터 배열 캐릭터들을 이미지 스캔 풀(GRAY_IMAGES)에 자동 등록
GRAY_IMAGES.extend([c["img"] for c in MY_CHARACTERS])
COLOR_IMAGES = [
    'check_mark.png', 'item_A1.png', 'item_B1.png', 'item_A2.png', 'item_B2.png', 
    'level_5.png', 'fusion_start.png',
    'tier_1.png', 'tier_2.png', 'tier_3.png', 'tier_4.png'
]
target_images = GRAY_IMAGES + COLOR_IMAGES

base_dir = os.path.dirname(os.path.abspath(__file__))

# [독립 폴더 생성] 융합 봇 전용 이미지 폴더
base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fusion_imgs")
os.makedirs(base_dir, exist_ok=True)

# [깃허브 해시 생성기] 파이썬 메모리에서 Git SHA-1 알고리즘을 100% 동일하게 모방하여 해시값을 계산합니다.
def get_git_sha(filepath):
    if not os.path.exists(filepath): return None
    with open(filepath, 'rb') as f: data = f.read()
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

bprint(">>> [시스템] 클라우드 해시 스캔 및 메모리(RAM) 적재 시작...")
API_URL = "https://api.github.com/repos/awwr502/my-bot-source/contents/fusion_imgs"

try:
    res = HTTP_SESSION.get(API_URL, timeout=5)
    if res.status_code == 200:
        github_data = {item['name']: item for item in res.json() if item['type'] == 'file'}
        
        # 1. 깃허브에 있는 파일 목록을 돌며 내 컴퓨터와 해시(SHA) 대조
        for img_name, item_data in github_data.items():
            if not img_name.endswith('.png'): continue
            full_path = os.path.join(base_dir, img_name)
            
            remote_sha = item_data['sha']
            local_sha = get_git_sha(full_path)
            
            # 해시값이 서로 다르거나 내 컴퓨터에 아예 없으면 깃허브에서 다운로드하여 '덮어쓰기'
            if local_sha != remote_sha:
                bprint(f"  > ☁️ [클라우드 패치] '{img_name}' 최신화 다운로드 중...")
                dl_res = requests.get(item_data['download_url'], timeout=5)
                dl_res.raise_for_status()
                with open(full_path, 'wb') as f:
                    f.write(dl_res.content)
except Exception as e:
    bprint(f"  > ⚠️ 클라우드 연결 실패. 기존 로컬 환경으로 부팅합니다: {e}")

CHAR_IMG_NAMES = [c["img"] for c in MY_CHARACTERS]

# 2. 패치가 완료된 로컬 폴더에서 RAM으로 일괄 적재 (캐릭터 사진 포함)
for img_name in target_images:
    full_path = os.path.join(base_dir, img_name)
    img_array = None
    
    if os.path.exists(full_path):
        img_array = np.fromfile(full_path, np.uint8)
    else:
        # 공용 UI는 위에서 다운로드 되므로, 여기서 없다는 건 캡처 안 한 캐릭터 사진뿐임
        if img_name in CHAR_IMG_NAMES:
            bprint(f"  > ❌ [경고] 캐릭터 사진({img_name})이 폴더에 없습니다! 직접 캡처해주세요.")
        continue
        
    if img_array is not None:
        if img_name in GRAY_IMAGES:
            FUSION_CACHE[img_name] = cv2.imdecode(img_array, cv2.IMREAD_GRAYSCALE)
        else:
            FUSION_CACHE[img_name] = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

# [전역 킬스위치] 팝업 인식 논리적 꼬임 방지 및 속도 극대화를 위해 RAM 캐시에서 강제 삭제
FUSION_CACHE['popup_char.png'] = None

FUSION_ROI = {}
FUSION_ROI_FILE = os.path.join(base_dir, "fusion_roi_cache.json")

def load_fusion_roi():
    try:
        if os.path.exists(FUSION_ROI_FILE):
            with open(FUSION_ROI_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.items():
                    FUSION_ROI[k] = {
                        'samples': deque(v.get('samples', []), maxlen=10),
                        'master_box': v.get('master_box'),
                        'last_fallback': 0,
                        'last_pos': v.get('last_pos')
                    }
    except: pass

def save_fusion_roi():
    try:
        save_data = {}
        for k, v in FUSION_ROI.items():
            save_data[k] = {
                'samples': list(v['samples']),
                'master_box': v['master_box'],
                'last_pos': v['last_pos']
            }
        with open(FUSION_ROI_FILE, "w", encoding="utf-8") as f:
            json.dump(save_data, f, indent=4, ensure_ascii=False)
    except: pass

load_fusion_roi()

def check_img(img_name, thread_sct, force_full=False):
    try:
        template = FUSION_CACHE.get(img_name)
        if template is None: return False
        
        if img_name not in FUSION_ROI:
            FUSION_ROI[img_name] = {'samples': deque(maxlen=10), 'master_box': None, 'last_fallback': 0, 'last_pos': None}
            
        roi_data = FUSION_ROI[img_name]
        # [확장식 ROI 로직]
        monitor = thread_sct.monitors[1]
        is_roi_mode = False
        
        # 1단계: ROI 구역이 있다면 먼저 시도
        if not force_full and roi_data['master_box']:
            monitor = roi_data['master_box']
            is_roi_mode = True
        
        sct_img = thread_sct.grab(monitor)
        screen_processed = cv2.cvtColor(np.asarray(sct_img), cv2.COLOR_BGRA2GRAY) if len(template.shape) == 2 else cv2.cvtColor(np.asarray(sct_img), cv2.COLOR_BGRA2BGR)
        
        res = cv2.matchTemplate(screen_processed, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        target_conf = FUSION_CONF.get(img_name, 0.75)
        
        # 2단계: ROI에서 실패했다면 전체 화면에서 재시도 (유저님 제안 Fallback)
        if max_val < target_conf and is_roi_mode:
            monitor = thread_sct.monitors[1]
            sct_img = thread_sct.grab(monitor)
            screen_processed = cv2.cvtColor(np.asarray(sct_img), cv2.COLOR_BGRA2GRAY) if len(template.shape) == 2 else cv2.cvtColor(np.asarray(sct_img), cv2.COLOR_BGRA2BGR)
            res = cv2.matchTemplate(screen_processed, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)

        if max_val >= target_conf:
            # 시작 버튼 컬러 필터링은 유지
            if img_name == 'fusion_start.png':
                h, w = template.shape[:2]
                roi_color = screen_processed[max_loc[1]:max_loc[1]+h, max_loc[0]:max_loc[0]+w]
                if len(roi_color.shape) == 3:
                    b, g, r = cv2.split(roi_color)
                    if int(np.mean(b)) - int(np.mean(r)) < 30: return False

            h, w = template.shape[:2]
            real_x = max_loc[0] + monitor["left"]
            real_y = max_loc[1] + monitor["top"]
            
            roi_data['last_pos'] = (int(real_x + w/2), int(real_y + h/2))
            roi_data['samples'].append((real_x, real_y, w, h)) # 최근 10개 샘플 (maxlen=10 자동 동작)
            
            # [ROI 업데이트] 기존 범위 + 새 범위를 포함하도록 확장
            pad = 50
            min_x = min(s[0] for s in roi_data['samples']) - pad
            min_y = min(s[1] for s in roi_data['samples']) - pad
            max_x = max(s[0] + s[2] for s in roi_data['samples']) + pad
            max_y = max(s[1] + s[3] for s in roi_data['samples']) + pad
            
            roi_data['master_box'] = {
                "left": int(max(0, min_x)), "top": int(max(0, min_y)),
                "width": int(max_x - min_x), "height": int(max_y - min_y)
            }
            save_fusion_roi()
            return True
        return False
    except: return False

def wait_vanish(img_name, thread_sct):
    bprint(f"  > [대기] {img_name} 소멸 검증 중 (0.01초 간격 최대 10회)...")
    vanish_count = 0
    while bot_active:
        if not bot_active: raise BotStopException() 
        if check_img(img_name, thread_sct):
            vanish_count = 0
        else:
            vanish_count += 1
            
        if vanish_count >= 10:
            break 
        time.sleep(0.01)
    if bot_active: 
        bprint(f"  > [완료] {img_name} 완벽 소멸 확인!")

def check_popup_main(thread_sct):

    # [ROI 적용 완료] force_full=True를 제거했습니다.
    if check_img('popup_main.png', thread_sct):
        bprint("  > 💡 [자정 팝업] 최근 뉴스 팝업 감지! 체크박스 수학적 정밀 타겟팅 시도...")
        cx, cy = FUSION_ROI['popup_main.png']['last_pos']
        
        # [핵심 1] 체크박스 거리 재조정: 확인 버튼 중앙에서 좌측으로 약 350픽셀 이동 (1920x1080 기준)
        target_x = cx - 350
        target_y = cy
        pyautogui.moveTo(target_x, target_y); time.sleep(0.1); send_cmd('C'); time.sleep(0.3)
        bprint(f"  > ✅ [체크 완료] 기준점({cx},{cy}) -> 체크박스({target_x},{target_y}) 명중")
        
        # [핵심 2] 이중 클릭 방지: 확인 버튼 클릭 후 마우스를 즉시 대피소로 치우고 1.5초간 확정 대기합니다.
        pyautogui.moveTo(cx, cy); time.sleep(0.1); send_cmd('C'); time.sleep(0.1)
        pyautogui.moveTo(200, 500); time.sleep(1.5)
        
        wait_vanish('popup_main.png', thread_sct)
        return True
    return False

def check_popup_char(thread_sct):
    # [ROI 적용 완료] force_full=True를 제거했습니다.
    if check_img('popup_char.png', thread_sct):
        bprint("  > 💡 [자정 팝업] 인게임 팝업(신청하기) 감지! (ESC로 닫기)")
        send_cmd('E'); time.sleep(0.1); send_cmd('R')
        wait_vanish('popup_char.png', thread_sct)
        return True
    return False

def check_fusion_afk(thread_sct):
    """융합기 대기 전용 잠수 방지 해제 로직"""
    if check_img('exit_notice.png', thread_sct, force_full=True):
        bprint("  > [긴급] 융합 중 잠수 방지 알림 감지! 전용 해제 시퀀스 진입")
        
        # 1. F 입력하여 알림창 닫기 (완전히 사라질 때까지 검증)
        while check_img('exit_notice.png', thread_sct, force_full=True):
            send_cmd('F'); time.sleep(0.1); send_cmd('R')
            for _ in range(20):
                time.sleep(0.1)
                if not check_img('exit_notice.png', thread_sct, force_full=True):
                    break
        
        bprint("  > [1단계] 알림창 소멸 확인. ESC 1회 입력")
        send_cmd('E'); time.sleep(0.1); send_cmd('R')
        
        # 2. 7.png 최대 3초 능동 대기
        bprint("  > [2단계] 7.png 탐색 중 (최대 3초)...")
        wait_7_start = time.time()
        found_7 = False
        while time.time() - wait_7_start < 3.0 and bot_active:
            if check_img('7.png', thread_sct, force_full=True):
                found_7 = True
                bprint("  > [성공] 7.png 포착 완료")
                break
            time.sleep(0.05)
            
        if not found_7:
            bprint("  > [실패] 3초 내 7.png 미발견. ESC 추가 1회 입력")
            send_cmd('E'); time.sleep(0.1); send_cmd('R')
            
        # 3. 위치 보정 (S 0.8초 -> W 0.8초)
        bprint("  > [3단계] 위치 보정: S(후진) 0.8초 -> W(전진) 0.8초")
        time.sleep(0.5)
        send_cmd('S'); time.sleep(0.8); send_cmd('R')
        time.sleep(0.2)
        send_cmd('W'); time.sleep(0.8); send_cmd('R')
        time.sleep(0.5)
        
        # 4. 14.png 탐색 (마우스 회전) 및 F 입력
        bprint("  > [4단계] 14.png 융합기 탐색 시작 (마우스 회전)")
        while bot_active:
            if check_img('14.png', thread_sct, force_full=True):
                bprint("  > [완료] 14.png 발견! F 입력 후 융합 대기로 복귀합니다.")
                send_cmd('F'); time.sleep(0.1); send_cmd('R')
                break
            else:
                send_cmd('M', 60, 0); time.sleep(0.15)
        
        time.sleep(1.0)
        return True
    return False

def toggle_stop():
    global bot_active, original_brightness, is_dimmed, char_thread_active
    char_thread_active = False # 수동 캐릭터 변경 스레드도 함께 정지
    if bot_active:
        print() # [복구] 강제 줄바꿈을 추가하여 \r 동적 타이머 라인과 텍스트가 겹치는 현상을 원천 차단합니다.
        bot_active = False
        bprint("\n=============================================")
        bprint("🔴 [정지 명령 접수] 단축키([) 입력 감지 및 봇 정지")
        bprint("=============================================")
        if is_dimmed:
            try:
                restore_monitors_brightness(original_brightness)
                is_dimmed = False
                bprint(f"  > ☀️ [화면 복구] 모니터 밝기를 원래대로({original_brightness}%) 되돌렸습니다.")
            except: pass

def toggle_start(mode=1):
    global bot_active, bot_mode, original_brightness, enable_dimming, is_dimmed
    # [꼬임 방지] 이미 봇이 실행 중이면 다른 시작 단축키 입력을 완벽히 차단합니다.
    if bot_active:
        bprint(f"⚠️ [입력 무시] 현재 매크로가 이미 실행 중입니다. 정지 단축키([)를 누른 후 다시 실행해주세요.")
        return
        
    print()
    bot_mode = mode
    bprint("\n=============================================")
    
    # [모드 5 강제 예외] 감염물 분별 모드는 사용자가 화면을 봐야 하므로, 절전 토글 상태와 무관하게 항상 밝기를 유지합니다.
    if enable_dimming and mode != 5:
        try:
            curr_b = sbc.get_brightness()
            # 모니터별 밝기를 리스트 형태로 모두 기억
            if curr_b and any(b > 10 for b in curr_b): 
                original_brightness = curr_b
            set_all_monitors_brightness(0)
            is_dimmed = True
            bprint("  > 🌙 [절전 모드] 모니터 밝기를 0%로 낮춥니다. (이미지 인식 정상 작동)")
        except: pass
    else:
        bprint("  > ☀️ [절전 모드 비활성] 모니터 밝기를 그대로 유지합니다.")
        
    if mode == 1:
        bprint("🟢 [융합 타이머(기본) 시작] 단축키(]) 입력 감지")
    elif mode == 2:
        bprint("🟡 [캐릭터 자동 전환 모드] 단축키(>) 입력 감지")
    elif mode == 3:
        bprint("✨ [모드 3: 깡 복사 모드 시작] 단축키(?) 입력 감지")
    elif mode == 4:
        bprint("🔥 [모드 4: 5/5 복사 모드 시작] 단축키(<) 입력 감지")
    elif mode == 5:
        bprint("🔍 [모드 5: 감염물 분별 모드 시작] 단축키(;) 입력 감지")
    bprint("=============================================")
    
    # 매크로 새로 시작 시 캐릭터 인벤토리 탐색 기억 초기화
    char_inventory_memory.clear()
    
    # 모든 세팅과 콘솔 메시지 출력이 완전히 끝난 후 봇 스레드 가동
    bot_active = True

# === [메인 융합 봇 루프] ===
def fusion_bot_loop():
    global bot_active, bot_mode
    state = 0
    fusion_end_time = 0.0
    char_index = 0
    skipped_chars = set() 
    go_to_state_6_next = False 

    with mss.mss() as thread_sct:
        # [초가속 능동 대기 함수] 마우스를 치우고 툴팁(ability_label)이 사라지는 즉시 0.01초 만에 탈출합니다!
        def fast_clear_tooltip():
            # [핵심 수정] 인벤토리 위치에 따라 마우스 대피 방향을 분리! (모드 5는 오른쪽, 나머지는 왼쪽)
            if bot_mode == 5:
                pyautogui.moveTo(1700, 500)
            else:
                pyautogui.moveTo(200, 500)
                
            wt = time.time()
            while time.time() - wt < 1.0 and bot_active:
                if not check_img('ability_label.png', thread_sct): break
                time.sleep(0.01)
        while True:
            try:
                if not bot_active:
                    original_sleep(0.1)
                    state = 0
                    char_index = 0
                    skipped_chars.clear() 
                    go_to_state_6_next = False 
                    continue
                
                # [4/5 자동화] 중앙 관리 배열(MY_CHARACTERS)에서 동적으로 교체 순서와 개수를 뽑아냅니다.
                if bot_mode in [3, 4]:
                    # 융합 모드: 앵커가 아닌 서브 캐릭들을 먼저 배치하고, 앵커를 항상 배열의 [마지막]에 자동 배치!
                    sub_chars = [c["img"] for c in MY_CHARACTERS if c["use_fusion"] and not c["is_anchor"]]
                    anchor_chars = [c["img"] for c in MY_CHARACTERS if c["use_fusion"] and c["is_anchor"]]
                    char_images = sub_chars + anchor_chars
                    loop_count = len(char_images)
                    anchor_idx = loop_count - 1 # 앵커는 무조건 맨 마지막 번호로 자동 계산됨
                else:
                    # 일반 멀티 모드: 등록된 순서대로 전부 순회합니다.
                    char_images = [c["img"] for c in MY_CHARACTERS]
                    loop_count = len(char_images)
                    # 리스트를 뒤져 앵커(is_anchor=True)의 번호를 자동으로 찾아냅니다.
                    anchor_idx = next((i for i, c in enumerate(MY_CHARACTERS) if c["is_anchor"]), 0)
                    
                # --- [신설: Mode 5 감염물 자동 분별 및 분해 모드] ---
                if bot_mode == 5:
                    if state == 0:
                        bprint("  > 🔍 [모드 5] 좌측 영역(0~960) 감염물 일괄 탐색 시작...")
                        
                        inv_roi = {"left": 0, "top": 0, "width": 960, "height": 1080}
                        screen_bgr = cv2.cvtColor(np.asarray(thread_sct.grab(inv_roi)), cv2.COLOR_BGRA2BGR)
                        X_OFFSET = 0 

                        all_candidates = []
                        search_items_mode5 = ['item_A1.png', 'item_B1.png', 'item_A2.png', 'item_B2.png']
                        
                        for item_name in search_items_mode5:
                            template = FUSION_CACHE.get(item_name)
                            if template is None: continue
                            res = cv2.matchTemplate(screen_bgr, template, cv2.TM_CCOEFF_NORMED)
                            loc = np.where(res >= 0.85)
                            h, w = template.shape[:2]
                            for pt in zip(*loc[::-1]):
                                real_x, real_y = pt[0] + X_OFFSET, pt[1]
                                if any(math.hypot(real_x-cp[0], real_y-cp[1]) < 80 for cp in all_candidates): continue
                                all_candidates.append((real_x, real_y, w, h, item_name))
                        
                        all_candidates.sort(key=lambda c: (-int(c[0] / 80), c[1]))
                        bprint(f"  > [완료] 총 {len(all_candidates)}개의 감염물을 발견했습니다. 자동 판독을 시작합니다!")
                        
                        for pt_data in all_candidates:
                            if not bot_active or bot_mode != 5: break
                            real_x, real_y, w, h, item_name = pt_data
                            cx, cy = real_x + w//2, real_y + h//2
                            pyautogui.moveTo(cx, cy)
                            
                            template_label = FUSION_CACHE.get('ability_label.png')
                            mon = thread_sct.monitors[1]
                            r_left, r_top = max(mon["left"], cx - 20), mon["top"]
                            r_width, r_height = min(500, mon["left"] + mon["width"] - r_left), mon["height"]
                            tooltip_roi = {"left": int(r_left), "top": int(r_top), "width": int(r_width), "height": int(r_height)}

                            # 1단계: 라벨 기반 동기화 (이벤트 트리거)
                            label_found = False
                            lx, ly = 0, 0
                            wait_l = time.time()
                            while time.time() - wait_l < 0.8 and bot_active:
                                hover_gray = cv2.cvtColor(np.asarray(thread_sct.grab(tooltip_roi)), cv2.COLOR_BGRA2GRAY)
                                res_l = cv2.matchTemplate(hover_gray, template_label, cv2.TM_CCOEFF_NORMED)
                                _, mv_l, _, ml_l = cv2.minMaxLoc(res_l)
                                if mv_l >= 0.90: 
                                    label_found = True; lx, ly = ml_l[0], ml_l[1]; break
                                time.sleep(0.01)

                            if not label_found: fast_clear_tooltip(); continue
                                
                            # [단계 2]: 민트색 존재 증명(5레벨) + 2-Stage 특성 판독 엔진
                            time.sleep(0.15)
                            
                            is_level_5 = False
                            has_trait = False
                            
                            # 특성 템플릿 로드 (컬러 유지)
                            t_trait_color = FUSION_CACHE['trait.png']
                            if len(t_trait_color.shape) == 2:
                                t_trait_color = cv2.cvtColor(t_trait_color, cv2.COLOR_GRAY2BGR)
                            
                            conf_trait = FUSION_CONF.get('trait.png', 0.70)

                            # 판독 영역 설정 (5레벨은 앵커 바로 뒤 100px 구간만 집중 감시)
                            col_x1, col_x2 = lx + template_label.shape[1], lx + template_label.shape[1] + 100
                            col_y1, col_y2 = max(0, ly - 25), ly + 80
                            
                            trait_x1, trait_x2 = max(0, lx - 10), lx + 200
                            trait_y1, trait_y2 = ly + 30, ly + 300
                            
                            # 1차 캡처
                            sct_frame = np.asarray(thread_sct.grab(tooltip_roi))
                            hover_color = cv2.cvtColor(sct_frame, cv2.COLOR_BGRA2BGR)
                            
                            roi_col_color = hover_color[col_y1:col_y2, col_x1:col_x2]
                            roi_trait_color = hover_color[trait_y1:trait_y2, trait_x1:trait_x2]

                            # [5레벨 판독]: 형태를 보지 않고 민트색 존재 여부만 확인
                            if roi_col_color.size > 0:
                                hsv_roi = cv2.cvtColor(roi_col_color, cv2.COLOR_BGR2HSV)
                                # 확장형 민트색 범위 (빛 번짐 포함)
                                lower_mint = np.array([35, 30, 80]) 
                                upper_mint = np.array([105, 255, 255])
                                mask = cv2.inRange(hsv_roi, lower_mint, upper_mint)
                                
                                # 민트색 픽셀이 아주 조금(5개 이상)이라도 있으면 5레벨로 즉시 확정
                                if cv2.countNonZero(mask) > 5:
                                    is_level_5 = True

                            # [특성 판독]: 1차 매칭 및 25% 조기 기각 판단
                            trait_val = 0
                            if not is_level_5 and roi_trait_color.size > 0:
                                for scale in [0.95, 1.0, 1.05]:
                                    width, height = int(t_trait_color.shape[1] * scale), int(t_trait_color.shape[0] * scale)
                                    if width <= roi_trait_color.shape[1] and height <= roi_trait_color.shape[0]:
                                        res_t = cv2.matchTemplate(roi_trait_color, cv2.resize(t_trait_color, (width, height)), cv2.TM_CCOEFF_NORMED)
                                        trait_val = max(trait_val, np.max(res_t))

                                # 1차 확정 또는 2차 대기 결정
                                if trait_val >= conf_trait:
                                    has_trait = True
                                elif trait_val >= 0.25: # 25% 이상이면 렌더링 대기
                                    time.sleep(0.15)
                                    sct_frame_2 = np.asarray(thread_sct.grab(tooltip_roi))
                                    hover_color_2 = cv2.cvtColor(sct_frame_2, cv2.COLOR_BGRA2BGR)
                                    roi_trait_color_2 = hover_color_2[trait_y1:trait_y2, trait_x1:trait_x2]
                                    
                                    if roi_trait_color_2.size > 0:
                                        for scale in [0.95, 1.0, 1.05]:
                                            width, height = int(t_trait_color.shape[1] * scale), int(t_trait_color.shape[0] * scale)
                                            if width <= roi_trait_color_2.shape[1] and height <= roi_trait_color_2.shape[0]:
                                                res_t2 = cv2.matchTemplate(roi_trait_color_2, cv2.resize(t_trait_color, (width, height)), cv2.TM_CCOEFF_NORMED)
                                                if np.max(res_t2) >= conf_trait:
                                                    has_trait = True
                                                    break

                            # [최종 의사결정]
                            if is_level_5:
                                bprint("  > 🛑 [보호] 5레벨 감염물.")
                            elif has_trait:
                                bprint("  > ♻️ [분해] 특성 포착."); pyautogui.moveTo(cx, cy); time.sleep(0.02); send_cmd('C'); time.sleep(0.05)
                            else:
                                bprint("  > 💎 [보관] 확정적 순정.")
                            
                            fast_clear_tooltip()
                        
                        bprint("  > 🛑 [종료] 감염물 분별 처리 완료."); toggle_stop(); continue
                
                # --- [State 0] 타이머 대기 및 카운트다운 ---
                if state == 0:
                    if bot_mode in [3, 4]:
                        mode_name = "모드 3(깡 복사)" if bot_mode == 3 else "모드 4(5/5 복사)"
                        anchor_img = char_images[anchor_idx] if loop_count > 0 else '5.png'
                        anchor_name = CHAR_NAMES.get(anchor_img, '앵커 캐릭')
                        bprint(f"✨ [{mode_name} 시작] 앵커({anchor_name}) 융합 보상 수령 및 세팅(State 7) 진입.")
                        char_index = anchor_idx # 과거의 하드코딩 숫자 5를 능동형 변수로 교체! 
                        fusion_end_time = 0.0 
                        state = 7
                        continue
                        
                    bprint("  > [State 0] 융합 중단(stop_btn.png) 대기 중...")
                    while bot_active:
                        if not bot_active: raise BotStopException()
                        if check_img('stop_btn.png', thread_sct):
                            bprint("🎯 '융합 중단' 버튼 감지 완료!")
                            
                            if bot_mode == 1:
                                bprint("⏳ [모드 1] 스마트 5분(300초) 카운트다운 시작...")
                                fusion_end_time = time.time() + 300.0
                                
                                # 1단계: 순수 카운트다운 루프
                                while True:
                                    if not bot_active: raise BotStopException()
                                    
                                    # 수동 캐릭터 변경 중일 때는 팝업창 간섭 방지를 위해 AFK 체크를 임시 중단합니다.
                                    if not char_thread_active:
                                        check_fusion_afk(thread_sct)
                                        
                                    remaining_sec = int(fusion_end_time - time.time())
                                    
                                    if remaining_sec <= 0: 
                                        # 2단계: 타이머 종료 직후 '즉시' 비프음 출력
                                        print(f"\r  > 융합 완료까지 남은 시간: 00분 00초\033[K", flush=True)
                                        print()
                                        bprint("✅ 5분 경과! 멜로디를 재생하며 시각적 검증을 시작합니다.")
                                        play_melody()
                                        break
                                    
                                    mins = remaining_sec // 60
                                    secs = remaining_sec % 60
                                    
                                    # 수동 캐릭터 변경 스레드가 작동 중일 때는 화면이 깨지지 않게 \r 동적 출력을 잠시 멈춥니다.
                                    if not char_thread_active:
                                        print(f"\r  > 융합 완료까지 남은 시간: {mins:02d}분 {secs:02d}초\033[K", end="", flush=True)
                                    
                                    original_sleep(1)

                                # 3단계: 시각적 사후 검증 (서버 렉 및 화면 복구 대기)
                                while True:
                                    if not bot_active: raise BotStopException()
                                    
                                    # 기계가 시각적으로 여전히 돌아가고 있다면 서버 렉 보정 대기
                                    if check_img('stop_btn.png', thread_sct):
                                        if not char_thread_active:
                                            print(f"\r  > ⏳ 융합 대기 중... (서버 렉 보정: 기계 가동 중)\033[K", end="", flush=True)
                                        time.sleep(1)
                                        continue
                                        
                                    # 게임 화면(chance.png)이 닫혀 있다면 복구될 때까지 대기
                                    if not check_img('chance.png', thread_sct):
                                        if not char_thread_active:
                                            print(f"\r  > ✅ 게임 화면(chance.png) 복구 대기 중...\033[K", end="", flush=True)
                                        time.sleep(0.5)
                                        continue
                                        
                                    # 레이스 컨디션 방어 및 최종 확정
                                    time.sleep(0.2)
                                    if check_img('stop_btn.png', thread_sct): continue
                                    
                                    print()
                                    bprint("✅ 시각적 검증 완료! 융합이 완벽히 종료되었습니다.")
                                    original_sleep(0.5)
                                    bprint("  > [모드 1] 기본 타이머 종료. 버튼 재감지 대기...")
                                    time.sleep(1)
                                    break
                                
                            elif bot_mode == 2:
                                bprint("⏳ [모드 2] 백그라운드 5분 타이머 가동! 즉시 멀티 캐릭터 교체 진입.")
                                fusion_end_time = time.time() + 300.0 
                                state = 1
                            
                            elif bot_mode in [3, 4]:
                                mode_name = "모드 3" if bot_mode == 3 else "모드 4"
                                bprint(f"⏳ [{mode_name}] 백그라운드 5분 타이머 가동! 즉시 지능형 자동 교체 진입.")
                                fusion_end_time = time.time() + 300.0 
                                state = 1
                                
                            break 
                        time.sleep(0.1)

                # --- [State 1] 1.png 확인 ---
                elif state == 1:
                    bprint("  > [State 1] 정상 세팅 완료. 초고속 탈출을 진행합니다.")
                    while bot_active:
                        if not bot_active: raise BotStopException() 
                        
                        bprint("  > [탈출 1/2] ESC 입력 후 7.png 초고속 능동 대기 (0.5초 간격 타격)...")
                        time.sleep(0.15)
                        
                        found_7 = False
                        while bot_active and not found_7:
                            send_cmd('E'); time.sleep(0.15); send_cmd('R')
                            
                            wait_7 = time.time()
                            while time.time() - wait_7 < 0.5 and bot_active:
                                if check_popup_char(thread_sct): 
                                    wait_7 = time.time() 
                                    continue 
                                # [핵심] 7.png ROI 적용 (force_full 제거)
                                if check_img('7.png', thread_sct):
                                    found_7 = True
                                    break
                                time.sleep(0.03)
                                
                            if not found_7:
                                bprint("  > [초고속 재시도] 7.png 화면 전환 미감지. ESC 즉시 재입력!")
                                time.sleep(0.05) 
                                
                        if not bot_active: continue
                        bprint("  > [성공] 7.png 인식 완료!")
                        
                        bprint("  > [탈출 2/2] ESC 1회 추가 입력 후 1.png 초고속 능동 대기...")
                        while bot_active:
                            send_cmd('E'); time.sleep(0.15); send_cmd('R')
                            
                            found_1 = False
                            wait_1 = time.time()
                            while time.time() - wait_1 < 0.5 and bot_active:
                                if check_popup_main(thread_sct): 
                                    wait_1 = time.time()
                                    continue
                                # [핵심] 1.png ROI 적용 (force_full 제거)
                                if check_img('1.png', thread_sct):
                                    found_1 = True
                                    break
                                time.sleep(0.03)
                                
                            if found_1:
                                bprint("  > [성공] 1.png 확인 완료! 마우스 클릭 후 2.png 동적 대기(최대 5초)...")
                                cx, cy = FUSION_ROI['1.png']['last_pos']
                                pyautogui.moveTo(cx, cy); time.sleep(0.05); send_cmd('C')
                                
                                wait_2 = time.time()
                                found_2 = False
                                while time.time() - wait_2 < 5.0 and bot_active:
                                    if check_img('2.png', thread_sct):
                                        found_2 = True
                                        break
                                    time.sleep(0.05)
                                    
                                if found_2:
                                    bprint("  > [동적 대기 성공] 2.png 화면 전환 확인! 2단계로 이동.")
                                    state = 2
                                    break
                                else:
                                    bprint("  > [동적 대기 실패] 5초 경과. 화면 전환 누락으로 간주하여 재시도합니다.")
                            else:
                                bprint("  > [초고속 재시도] 1.png 화면 전환 미감지. ESC 즉시 재입력!")
                                if check_img('6.png', thread_sct, force_full=True):
                                    time.sleep(0.5)
                                    
                        break

                # --- [State 2] 2.png 대기 및 3.png 전이 ---
                elif state == 2:
                    bprint("  > [State 2] 2.png 탐색 및 3.png 대기 중...")
                    while bot_active:
                        if not bot_active: raise BotStopException() 
                        check_popup_main(thread_sct)
                        
                        if check_img('2.png', thread_sct):
                            bprint("  > 2.png 확인! N 입력 후 3.png 동적 대기(최대 10초)...")
                            send_cmd('N'); time.sleep(0.1); send_cmd('R')
                            
                            wait_3 = time.time()
                            found_3 = False
                            # [요청 반영] 30초 동안 3.png가 확인될 때까지 0.1초 간격으로 마우스 좌클릭(C)을 반복합니다.
                            while time.time() - wait_3 < 30.0 and bot_active:
                                check_popup_main(thread_sct)
                                if check_img('3.png', thread_sct, force_full=True):
                                    found_3 = True
                                    break
                                
                                send_cmd('C')
                                time.sleep(0.1)
                                
                            if found_3:
                                bprint("  > [동적 대기 성공] 3.png 확인 완료. 3단계 이동.")
                                state = 3
                                break
                            else:
                                bprint("  > [동적 대기 실패] 10초간 3.png 미발견. N키를 다시 누르기 위해 루프를 재시작합니다.")
                                continue
                                
                        else:
                            bprint("  > [꼬임 방지] 2.png 미발견. 1.png 다시 클릭 후 동적 대기(최대 5초)...")
                            # [핵심] 꼬임 방지용 1.png에도 ROI 적용 (force_full 제거)
                            if check_img('1.png', thread_sct):
                                cx, cy = FUSION_ROI['1.png']['last_pos']
                                pyautogui.moveTo(cx, cy); time.sleep(0.05); send_cmd('C')
                                
                                wait_2_fb = time.time()
                                while time.time() - wait_2_fb < 5.0 and bot_active:
                                    if check_img('2.png', thread_sct):
                                        bprint("  > [동적 대기 성공] 2.png 화면 복구 완료!")
                                        break
                                    time.sleep(0.05)
                            time.sleep(0.1)

                # --- [State 3] 캐릭터 지정 및 F 입력 ---
                elif state == 3:
                    target_char = char_images[char_index]
                    c_name = CHAR_NAMES.get(target_char, target_char)
                    bprint(f"  > [State 3] G 1회 입력 후 '{c_name}' 탐색 (진행: {char_index+1}/{loop_count})")
                    
                    send_cmd('G'); time.sleep(0.1); send_cmd('R')
                    
                    while bot_active:
                        if not bot_active: raise BotStopException() 
                        
                        found_char = False
                        wait_g = time.time()
                        while time.time() - wait_g < 1.5 and bot_active:
                            if check_img(target_char, thread_sct, force_full=True):
                                found_char = True
                                break
                            time.sleep(0.1)

                        if found_char:
                            bprint(f"  > [성공] '{c_name}' 발견! 클릭 실행")
                            cx, cy = FUSION_ROI[target_char]['last_pos']
                            pyautogui.moveTo(cx, cy); time.sleep(0.05); send_cmd('C'); time.sleep(0.1)
                            
                            send_cmd('F'); time.sleep(0.1); send_cmd('R')
                            state = 4
                            break
                        else:
                            bprint("  > [재시도] 캐릭터 미발견. 목록이 닫혔을 가능성에 대비하여 G 재입력...")
                            send_cmd('G'); time.sleep(0.1); send_cmd('R')

                # --- [State 4] 6.png 등장 5초 동적 대기 및 소멸 검증 ---
                elif state == 4:
                    target_char = char_images[char_index]
                    bprint("  > [State 4] F키 입력 후 화면 전환(6.png) 동적 대기(최대 5초)...")
                    while bot_active:
                        if not bot_active: raise BotStopException() 
                        
                        found_6 = False
                        wait_6 = time.time()
                        while time.time() - wait_6 < 5.0 and bot_active:
                            # [핵심] 6.png ROI 적용 (force_full 제거)
                            if check_img('6.png', thread_sct):
                                found_6 = True
                                break
                            time.sleep(0.05)
                            
                        if found_6:
                            wait_vanish('6.png', thread_sct)
                            bprint("  > [성공] 6.png 소멸 완료. 5단계 이동.")
                            state = 5
                            break
                        else:
                            bprint("  > [재시도] 화면 전환(6.png) 미감지. F키를 다시 입력합니다...")
                            send_cmd('F'); time.sleep(0.1); send_cmd('R')

                # --- [State 5] 7.png 확인 및 루프 갱신 ---
                elif state == 5:
                    bprint("  > [State 5] 7.png 탐색 중...")
                    while bot_active:
                        if not bot_active: raise BotStopException()
                        if check_popup_char(thread_sct): continue 
                        
                        if check_img('7.png', thread_sct):
                            bprint("  > [성공] 7.png 확인! '즉각 상호작용(F)'을 시도합니다.")
                            
                            # [초가속 돌파] 이미 캐릭터가 융합기를 바라보고 있을 확률이 높으므로, 마우스를 돌리기 전 F부터 입력해 봅니다.
                            send_cmd('F'); time.sleep(0.1); send_cmd('R')
                            
                            blind_f_success = False
                            wait_chance = time.time()
                            # [핵심 방어 1] 1.5초 대기 중 자정 팝업이 chance.png를 가리는 현상을 실시간으로 방어합니다.
                            while time.time() - wait_chance < 1.5 and bot_active:
                                if check_popup_char(thread_sct): 
                                    wait_chance = time.time() # 팝업을 치우느라 소모된 시간을 보상하기 위해 타이머를 리셋합니다.
                                    continue
                                    
                                if check_img('chance.png', thread_sct):
                                    blind_f_success = True
                                    break
                                time.sleep(0.05)
                            
                            if blind_f_success:
                                bprint("  > 🎯 [진입 성공!] 융합기 창(chance.png) 다이렉트 팝업 확인! State 7로 직행합니다.")
                            else:
                                bprint("  > 🔄 [정상 패턴 전환] 즉각 진입 실패. 14.png 탐색(마우스 회전) 루프로 진입합니다.")
                                while bot_active:
                                    if not bot_active: raise BotStopException()
                                    if check_popup_char(thread_sct): continue 
                                    
                                    # [핵심 방어 2] 눈감고 F가 실패한 줄 알았는데, 팝업을 치우고 보니 이미 융합기 안(chance.png)일 수 있습니다! (지연 진입 확인)
                                    if check_img('chance.png', thread_sct):
                                        bprint("  > 🎯 [지연 진입 성공] 팝업에 가려졌던 융합기 창(chance.png)이 확인되었습니다! State 7로 직행합니다.")
                                        break
                                        
                                    # [주의] 14.png는 지도 텍스트이므로 오탐 방지를 위해 force_full=True를 유지합니다.
                                    if check_img('14.png', thread_sct, force_full=True):
                                        bprint("  > [성공] 14.png 확인! F를 입력하여 융합기에 진입합니다.")
                                        while bot_active:
                                            send_cmd('F'); time.sleep(0.1); send_cmd('R')
                                            
                                            vanish_start = time.time()
                                            is_vanished = False
                                            
                                            # 2초간 능동 대기하며 소멸 검증
                                            while time.time() - vanish_start < 2.0 and bot_active:
                                                # [핵심 방어 3] 14.png 소멸 대기 중에도 팝업이 뜨면 타이머를 리셋합니다.
                                                if check_popup_char(thread_sct):
                                                    vanish_start = time.time()
                                                    continue
                                                    
                                                # 확실한 소멸을 위해 연달아 2번 안 보일 때만 소멸로 판정
                                                if not check_img('14.png', thread_sct, force_full=True):
                                                    time.sleep(0.05)
                                                    if not check_img('14.png', thread_sct, force_full=True):
                                                        is_vanished = True
                                                        break
                                                time.sleep(0.05)
                                            
                                            if is_vanished:
                                                bprint("  > [완료] 14.png 소멸 확인! 다음 단계로 이동합니다.")
                                                break
                                            else:
                                                bprint("  > ⚠️ [재시도] 2초 대기 초과. F키를 다시 입력합니다.")
                                        break
                                    else:
                                        if bot_mode in [3, 4]:
                                            print("\r  > 14.png 탐색 중... (마우스 이동)\033[K", end="", flush=True)
                                            pyautogui.moveTo(CENTER_X, CENTER_Y)
                                            time.sleep(0.01)
                                            send_cmd('M', 400, 0)
                                            time.sleep(0.01) 
                                        time.sleep(0.02)
                                print() # 줄바꿈 복구

                            if bot_mode in [3, 4]:
                                if go_to_state_6_next:
                                    go_to_state_6_next = False 
                                    c_name = CHAR_NAMES.get(char_images[char_index], char_images[char_index])
                                    bprint(f"\n✨ [사이클 완료] 앵커 캐릭 '{c_name}' 접속 성공!")
                                    bprint("⏳ 기계 가동 상태 확인 및 보상 수령(State 7)을 위해 이동합니다.\n")
                                    state = 7 
                                else:
                                    c_name = CHAR_NAMES.get(char_images[char_index], char_images[char_index])
                                    bprint(f"\n🔄 ['{c_name}' 접속 완료] 융합 세팅(State 7)으로 진입합니다.\n")
                                    state = 7
                            else:
                                char_index += 1
                                if char_index >= loop_count:
                                    bprint(f"\n✨ [사이클 완료] {loop_count}개 캐릭터 융합 시작을 모두 마쳤습니다!")
                                    bprint("⏳ 남은 융합 시간 대기 모드(State 6)로 이동합니다.\n")
                                    state = 6
                                else:
                                    bprint(f"\n🔄 [캐릭터 변경] 다음 루프(진행: {char_index+1}/{loop_count})를 위해 State 1로 복귀합니다.\n")
                                    state = 1
                            break
                        time.sleep(0.1)
                        
                # --- [State 6] 멀티 캐릭터 남은 시간 대기 ---
                elif state == 6:
                    bprint("  > [State 6] 백그라운드 융합 완료 시간 대기 중...")
                    while bot_active:
                        if not bot_active: raise BotStopException() 
                        check_fusion_afk(thread_sct)
                            
                        remaining_sec = int(fusion_end_time - time.time())
                        
                        if remaining_sec <= 0:
                            print(f"\r  > 융합 완료까지 남은 시간: 00분 00초\033[K", end="", flush=True)
                            print() 
                            bprint("✅ 타이머 종료! 모든 융합이 완료되었습니다!")
                            play_melody()
                            original_sleep(0.5)
                            
                            if bot_mode in [3, 4]:
                                mode_name = "모드 3" if bot_mode == 3 else "모드 4"
                                c_name = CHAR_NAMES.get(char_images[char_index], char_images[char_index])
                                bprint(f"  > [{mode_name}] 앵커 캐릭 '{c_name}' 보상 수령으로 이동합니다.")
                                state = 7
                            else:
                                char_index = 0
                                while char_index in skipped_chars and char_index < loop_count:
                                    char_index += 1
                                state = 0
                            break
                            
                        mins = remaining_sec // 60
                        secs = remaining_sec % 60
                        print(f"\r  > '{c_name}' 융합 완료까지 남은 시간: {mins:02d}분 {secs:02d}초\033[K", end="", flush=True)
                        original_sleep(1)

                # --- [State 7] 지능형 감염물 세팅 시퀀스 ---
                elif state == 7:
                    bprint("  > [State 7] 지능형 융합 세팅 진행 중...")
                    search_items = ['item_A1.png', 'item_B1.png']
                    
                    bprint("  > 1. 융합기 현재 상태 확인 중...")
                    template_check = FUSION_CACHE.get('check_mark.png') # 변수 재선언
                    skip_setup = False 
                    is_machine_empty = False 

                    while bot_active:
                        if not bot_active: raise BotStopException()
                        
                        remaining_sec = int(fusion_end_time - time.time())
                        
                        # [절대 타이머 방어] 현재 생존한 앵커 캐릭터를 동적으로 판별하여 타이머 대기!
                        current_anchor = anchor_idx
                        if anchor_idx in skipped_chars:
                            current_anchor = 0
                            while current_anchor in skipped_chars and current_anchor < anchor_idx:
                                current_anchor += 1
                                
                        if char_index == current_anchor and remaining_sec > 0:
                            disp_sec = max(0, remaining_sec)
                            mins = disp_sec // 60
                            secs = disp_sec % 60
                            
                            # [핵심] stop_btn.png ROI 적용 (force_full 제거)
                            if check_img('stop_btn.png', thread_sct):
                                print(f"\r  > ⏳ [스마트 대기] 기계 가동 확인. '{c_name}' 융합 완료까지 남은 시간: {mins:02d}분 {secs:02d}초\033[K", end="", flush=True)
                            # [핵심] chance.png ROI 적용 (force_full 제거)
                            elif not check_img('chance.png', thread_sct):
                                print(f"\r  > 🙈 [화면 최소화] 백그라운드 융합 남은 시간: {mins:02d}분 {secs:02d}초\033[K", end="", flush=True)
                            else:
                                print(f"\r  > ⏳ [서버 렉 방어] 기계 UI 렌더링 동기화 대기 중... 남은 시간: {mins:02d}분 {secs:02d}초\033[K", end="", flush=True)
                            
                            time.sleep(1)
                            continue # 타이머가 끝날 때까지 1.5단계(세팅)로 절대 돌입하지 않음!
                            
                        # === 타이머가 0이 되었거나, 캐릭터(진입 즉시 세팅)일 때의 확인 로직 ===
                        
                       # 1) 타이머가 끝났음에도 기계가 시각적으로 여전히 돌아가고 있다면 서버 렉 보정 대기
                       # [핵심] stop_btn.png ROI 적용 (force_full 제거)
                        if check_img('stop_btn.png', thread_sct):
                            c_name = CHAR_NAMES.get(char_images[char_index], "캐릭")
                            print(f"\r  > ⏳ '{c_name}' 융합 대기 중... (기계 가동 중)\033[K", end="", flush=True)
                            check_fusion_afk(thread_sct)
                            time.sleep(1)
                            continue
                            
                        # 2) 게임 화면(chance.png)이 닫혀 있다면 복구될 때까지 대기
                        # [핵심] chance.png ROI 적용 (force_full 제거)
                        if not check_img('chance.png', thread_sct):
                            if char_index == current_anchor:
                                disp_sec = max(0, remaining_sec)
                                mins = disp_sec // 60
                                secs = disp_sec % 60
                                print(f"\r  > ✅ 게임 화면(chance.png) 복구 대기 중... 남은 시간: {mins:02d}분 {secs:02d}초\033[K", end="", flush=True)
                            time.sleep(0.5)
                            continue
                            
                        # 3) [레이스 컨디션 방어] 화면은 떴으나 버튼이 렌더링되지 않은 찰나 방지
                        time.sleep(0.2)
                        # [핵심] stop_btn.png ROI 적용 (force_full 제거)
                        if check_img('stop_btn.png', thread_sct):
                            continue 
                            
                        # 4) 기계 가동이 끝났으므로 보상(get_reward.png) 유무 확인 및 완료 애니메이션(2~3초) 동적 대기
                        if check_popup_char(thread_sct): continue 
                        
                        # [핵심] 융합 완료 직후 2~3초의 애니메이션 시간 동안에는 아무 버튼도 없으므로 능동 대기합니다.
                        reward_found = False
                        wait_anim = time.time()
                        while time.time() - wait_anim < 2.5 and bot_active:
                            if check_img('get_reward.png', thread_sct, force_full=True):
                                reward_found = True
                                break
                            # 만약 시작 버튼이나 빈 기계 텍스트가 보인다면, 확실히 기계가 멈춘 상태(세팅 대기)이므로 즉시 탈출하여 속도를 높입니다!
                            if check_img('fusion_start.png', thread_sct) or check_img('fusion_material.png', thread_sct, force_full=True):
                                break
                            time.sleep(0.05)
                        
                        if reward_found:
                            print() # 줄바꿈 복구
                            bprint("  > [보상 있음] '획득' 창 확인 완료! F를 입력합니다.")
                            
                            while bot_active:
                                send_cmd('F'); time.sleep(0.05); send_cmd('R')
                                bprint("  > [대기] get_reward.png 초고속 소멸 검증 중...")
                                
                                vanish_count = 0
                                wait_start = time.time()
                                while time.time() - wait_start < 5.0 and bot_active:
                                    # [초가속 핵심 1] force_full=True 제거! 메모리에 저장된 좁은 구역(ROI)만 0.001초 만에 스캔합니다.
                                    if check_img('get_reward.png', thread_sct):
                                        vanish_count = 0
                                    else:
                                        vanish_count += 1
                                        
                                    # [초가속 핵심 2] 20회 -> 5회 연속 안 보이면 즉시 소멸 확정 (약 0.05초 소요)
                                    if vanish_count >= 5:
                                        break
                                    time.sleep(0.01)
                                    
                                if vanish_count >= 5:
                                    bprint("  > [완료] get_reward.png 완벽 소멸 확인!")
                                    break
                                else:
                                    bprint("  > [재시도] 5초 대기 초과! 획득 창이 닫히지 않아 F를 다시 입력합니다.")
                            
                            if check_popup_char(thread_sct):
                                bprint("  > [꼬임 방지] F 입력 직후 팝업 간섭 감지! 루프를 재시작하여 보상을 다시 획득합니다.")
                                continue 
                            
                            bprint("  > [수령 완료] 보상을 획득했습니다. 기계에 남은 이전 감염물을 빼기 위해 2단계로 이동합니다.")
                            is_machine_empty = False
                            break 
                            
                        # [디싱크 버그 감지 및 강제 치유 로직]
                        if not check_img('stop_btn.png', thread_sct) and check_img('bug_time.png', thread_sct):
                            print() # 줄바꿈 복구
                            bprint("  > 🚨 [버그 감지] UI 디싱크 버그 확인! 기계를 강제로 재부팅합니다.")
                            send_cmd('E'); time.sleep(0.15); send_cmd('R')
                            
                            bprint("  > [버그 치유 1/2] 메인 화면(7.png) 복귀 대기 중...")
                            wait_7 = time.time()
                            is_out = False
                            while time.time() - wait_7 < 3.0 and bot_active:
                                if check_img('7.png', thread_sct, force_full=True):
                                    is_out = True
                                    break
                                time.sleep(0.1)
                                
                            if not is_out:
                                bprint("  > [재시도] 화면 닫기 지연. ESC 추가 입력.")
                                send_cmd('E'); time.sleep(0.15); send_cmd('R')
                                time.sleep(1.0)
                                
                            bprint("  > [버그 치유 2/2] 14.png 탐색(마우스 회전) 및 상호작용(F) 재진입...")
                            while bot_active:
                                if check_img('14.png', thread_sct, force_full=True):
                                    bprint("  > [확인] 14.png 발견! 상호작용(F) 시도 및 융합기 진입 대기...")
                                    entered_machine = False
                                    
                                    while bot_active:
                                        send_cmd('F'); time.sleep(0.1); send_cmd('R')
                                        
                                        wait_chance = time.time()
                                        found_chance = False
                                        # 최대 3초간 0.1초 간격으로 chance.png 능동 대기
                                        while time.time() - wait_chance < 3.0 and bot_active:
                                            if check_img('chance.png', thread_sct):
                                                found_chance = True
                                                break
                                            time.sleep(0.1)
                                            
                                        if found_chance:
                                            bprint("  > ✅ [버그 치유 완료] 융합기(chance.png) 재진입 성공! 상태를 재확인합니다.")
                                            entered_machine = True
                                            break
                                        else:
                                            bprint("  > ⚠️ [재시도] 3초간 chance.png 미발견(서버 렉). F키를 다시 입력합니다.")
                                            
                                    if entered_machine:
                                        break # 바깥쪽 14.png 탐색(마우스 회전) 루프까지 완전히 탈출
                                else:
                                    send_cmd('M', 60, 0); time.sleep(0.15)
                                    
                            continue # State 7 처음으로 돌아가서 정상적으로 상태 확인 수행

                        # 5) 보상이 없다면 앵커/기타 캐릭터 세팅 분기
                        current_anchor = anchor_idx
                        if anchor_idx in skipped_chars:
                            current_anchor = 0
                            while current_anchor in skipped_chars and current_anchor < anchor_idx:
                                current_anchor += 1
                                
                        if char_index == current_anchor:
                            print() # 줄바꿈 복구
                            c_name = CHAR_NAMES.get(char_images[char_index], "앵커")
                            bprint(f"  > [앵커 '{c_name}'] 기계 상태 판별 중 (fusion_material.png 유무 확인)...")
                            # [핵심] fusion_material.png ROI 적용 (force_full 제거)
                            if not check_img('fusion_material.png', thread_sct):
                                bprint("  > [빈 기계] '융합 재료' 텍스트 미인식. 체크 해제를 생략하고 새 감염물 세팅(1.5단계)으로 이동합니다.")
                                is_machine_empty = True
                                break
                            else:
                                # [핵심] fusion_start.png ROI 적용 (force_full 제거)
                                if check_img('fusion_start.png', thread_sct):
                                    bprint("  > [채워짐] 활성화된 컬러 시작 버튼 즉시 확인! 융합 시작(4.5단계)으로 직행합니다.")
                                    skip_setup = True
                                    break
                                else:
                                    bprint("  > [꼬임 방지] 시작 버튼 비활성화 감지! 딜레이 없이 즉시 체크 해제 및 재세팅(1.5단계)으로 돌입합니다.")
                                    is_machine_empty = False 
                                    break
                        else:
                            # [핵심 수정] 부캐릭터의 경우 절대 타이머 보호가 없으므로, 1.5단계 진입 전 'stop_btn.png'를 최종 교차 검증하여 오판을 방어합니다.
                            if check_img('stop_btn.png', thread_sct):
                                print() # 동적 타이머 줄바꿈 복구
                                c_name_err = CHAR_NAMES.get(char_images[char_index], "부캐")
                                bprint(f"  > ⚠️ [서버 렉 방어] '{c_name_err}' 융합 완료 오판 감지! 기계가 여전히 가동 중이므로 대기로 복귀합니다.")
                                continue

                            c_name = CHAR_NAMES.get(char_images[char_index], "부캐")
                            bprint(f"  > ['{c_name}' 세팅] 무조건 체크 해제가 필요하므로 즉시 1.5단계로 진입합니다.")
                            is_machine_empty = False
                            break
                        
                    if not bot_active: continue
                    
                    if not skip_setup:
                        # [단계 1.5] 융합 재료 슬롯 강제 클릭 (인벤토리 열기)
                        bprint("  > 1.5. 융합 재료 슬롯(좌측)을 클릭하여 감염물 창을 엽니다.")
                        pyautogui.moveTo(1150, 300); time.sleep(0.05); send_cmd('C')
                        
                        bprint("  > [능동 대기] 인벤토리 UI 팝업 초고속 확인 중...")
                        wait_inv_start = time.time()
                        while bot_active and time.time() - wait_inv_start < 3.0:
                            # [핵심] 능동 대기 시에도 아이템 및 체크마크 ROI 적용 (force_full 제거)
                            if check_img('item_A1.png', thread_sct) or \
                               check_img('item_B1.png', thread_sct) or \
                               check_img('check_mark.png', thread_sct):
                                break
                            time.sleep(0.05)

                        if not bot_active: continue

                        # [단계 2] 기존 체크마크 해제 (모드 3/4 내부에서 후처리를 위해 단계 건너뜀)
                        bprint("  > 2. 신규 감염물 탐색 후 체크 해제를 진행합니다.")

                        if not bot_active: continue

                        # [단계 3] 신규 감염물 탐색 및 2개 클릭
                        bprint("  > 3. 신규 감염물 탐색 및 페어링 중 (체크된 감염물 배제)...")
                        search_attempts = 0 
                        skip_current_char = False
                        last_cx, last_cy = CENTER_X, CENTER_Y
                        
                        # [메모리 초기화]
                        char_key = char_images[char_index]
                        if char_key not in char_inventory_memory:
                            char_inventory_memory[char_key] = (0, 0)
                        is_memory_rescan = False
                        
                        while bot_active:
                            if not bot_active: raise BotStopException()
                            pyautogui.moveTo(200, 500); time.sleep(0.25)

                            inv_roi = {"left": 960, "top": 0, "width": 960, "height": 1080}
                            screen_bgr = cv2.cvtColor(np.asarray(thread_sct.grab(inv_roi)), cv2.COLOR_BGRA2BGR)
                            pair_found = False
                            X_OFFSET = 960

                            if template_check is not None:
                                res_c2 = cv2.matchTemplate(screen_bgr, template_check, cv2.TM_CCOEFF_NORMED)
                                loc_c2 = np.where(res_c2 >= 0.85)
                                check_pts_global = [(pt[0] + X_OFFSET, pt[1]) for pt in zip(*loc_c2[::-1])]
                                if len(check_pts_global) > 4: check_pts_global = []
                                
                            if bot_mode == 4:
                                if search_attempts == 0:
                                    bprint("  > 🧠 [모드 4] 5/5 교차 페어링을 위한 정밀 판독 시작...")
                                
                                all_candidates = []
                                search_items_mode4 = ['item_A1.png', 'item_B1.png', 'item_A2.png', 'item_B2.png']
                                
                                for item_name in search_items_mode4:
                                    template = FUSION_CACHE.get(item_name)
                                    if template is None: continue
                                    
                                    conf = FUSION_CONF.get(item_name, 0.92)
                                    if item_name in ['item_A2.png', 'item_B2.png']: conf = min(conf, 0.88)

                                    res = cv2.matchTemplate(screen_bgr, template, cv2.TM_CCOEFF_NORMED)
                                    loc = np.where(res >= conf)
                                    template_brightness = np.mean(template)
                                    h, w = template.shape[:2]
                                    
                                    for pt in zip(*loc[::-1]):
                                        real_x = pt[0] + X_OFFSET
                                        real_y = pt[1]
                                        
                                        is_checked = any(math.hypot(real_x-c[0], real_y-c[1]) < 40 for c in check_pts_global)
                                        if is_checked: continue
                                        
                                        roi = screen_bgr[pt[1]:pt[1]+h, pt[0]:pt[0]+w]
                                        if np.max(roi) < 120: continue
                                        
                                        if any(math.hypot(real_x-cp[0], real_y-cp[1]) < 80 for cp in all_candidates): continue
                                        all_candidates.append((real_x, real_y, w, h, item_name))
                                
                                all_candidates.sort(key=lambda c: (c[0] // 100, c[1]))
                                
                                memory_pool = {} 
                                safe_pts = [] 
                                
                                for pt_data in all_candidates:
                                    if pair_found: break
                                    real_x, real_y, w, h, item_name = pt_data
                                    # [스마트 메모리 패스 로직]
                                    curr_sort_key = (real_x // 100, real_y)
                                    mem_x, mem_y = char_inventory_memory[char_key]
                                    if not is_memory_rescan and curr_sort_key < (mem_x // 100, mem_y):
                                        continue # 이미 과거에 확인했던 위치이므로 즉시 패스
                                        
                                    cx, cy = real_x + w//2, real_y + h//2
                                    
                                    pyautogui.moveTo(cx, cy)
                                    
                                    # [완벽 복원 1] 모드 4: 마우스 좌측 깊숙한 곳 캡처. X폭 늘리고 Y축 전체
                                    template_label = FUSION_CACHE.get('ability_label.png')
                                    mon = thread_sct.monitors[1]
                                    r_left = max(mon["left"], cx - 1100)
                                    r_top = mon["top"]
                                    r_width = 1100
                                    r_height = mon["height"]
                                    tooltip_roi = {"left": int(r_left), "top": int(r_top), "width": int(r_width), "height": int(r_height)}
                                    
                                    # [모드 4 전용: 라벨 트리거 0.05초 확정 판독 엔진]
                                    label_found = False
                                    lx, ly = 0, 0
                                    wait_start = time.time()
                                    while time.time() - wait_start < 1.0 and bot_active:
                                        hover_gray = cv2.cvtColor(np.asarray(thread_sct.grab(tooltip_roi)), cv2.COLOR_BGRA2GRAY)
                                        res_l = cv2.matchTemplate(hover_gray, template_label, cv2.TM_CCOEFF_NORMED)
                                        _, mv_l, _, ml_l = cv2.minMaxLoc(res_l)
                                        if mv_l >= 0.90:
                                            label_found = True; lx, ly = ml_l[0], ml_l[1]; break
                                        time.sleep(0.01)

                                    if not label_found:
                                        fast_clear_tooltip(); continue
                                        
                                    time.sleep(0.05) # 데이터 페치 딜레이 헷지
                                    
                                    hover_gray = cv2.cvtColor(np.asarray(thread_sct.grab(tooltip_roi)), cv2.COLOR_BGRA2GRAY)
                                    label_w = template_label.shape[1]
                                    col_x_start = lx + label_w
                                    col_x_end = min(hover_gray.shape[1], lx + label_w + 360)
                                    col_y_start = max(0, ly - 20)
                                    col_y_end = min(hover_gray.shape[0], ly + 150)
                                    roi_col = hover_gray[col_y_start:col_y_end, col_x_start:col_x_end]

                                    parse_success = False
                                    points_5 = []
                                    template_5 = FUSION_CACHE.get('level_5.png')
                                    if template_5 is not None:
                                        t5_gray = cv2.cvtColor(template_5, cv2.COLOR_BGR2GRAY)
                                        res_5 = cv2.matchTemplate(roi_col, t5_gray, cv2.TM_CCOEFF_NORMED)
                                        loc_5 = np.where(res_5 >= FUSION_CONF.get('level_5.png', 0.75))
                                        for pt in zip(*loc_5[::-1]):
                                            if not any(math.hypot(pt[0]-p[0], pt[1]-p[1]) < 10 for p in points_5):
                                                points_5.append(pt)
                                        
                                        if len(points_5) == 0:
                                            bprint("  > ⏭️ [스킵] 5레벨이 없는 감염물입니다.")
                                            fast_clear_tooltip(); continue
                                        elif len(points_5) >= 2:
                                            bprint(f"  > 🛑 [경고] 5/5 감염물입니다! 보호를 위해 스킵합니다.")
                                            fast_clear_tooltip(); continue
                                            
                                        max_loc_5 = points_5[0]
                                        is_ability_5 = max_loc_5[1] < 50
                                        ability = 5 if is_ability_5 else 0
                                        activity = 5 if not is_ability_5 else 0
                                        
                                        search_y_start = 50 if is_ability_5 else 0
                                        search_y_end = 105 if is_ability_5 else 50
                                        roi_other = roi_col[search_y_start:search_y_end, :]
                                        
                                        found_other = 0
                                        t1_img = FUSION_CACHE.get('tier_1.png')
                                        t1_h = t1_img.shape[0] if t1_img is not None else 24
                                        
                                        for t_idx in [4, 3, 2]: 
                                            t_img = FUSION_CACHE.get(f'tier_{t_idx}.png')
                                            if t_img is None: continue
                                            t_img_g = cv2.cvtColor(t_img, cv2.COLOR_BGR2GRAY)
                                            if roi_other.shape[0] < t_img_g.shape[0] or roi_other.shape[1] < t_img_g.shape[1]: continue
                                            res_o = cv2.matchTemplate(roi_other, t_img_g, cv2.TM_CCOEFF_NORMED)
                                            _, max_val_o, _, _ = cv2.minMaxLoc(res_o)
                                            if max_val_o >= FUSION_CONF.get(f'tier_{t_idx}.png', 0.72):
                                                found_other = t_idx
                                                break
                                                
                                        if found_other == 0 and t1_img is not None:
                                            t_img_g_1 = cv2.cvtColor(t1_img, cv2.COLOR_BGR2GRAY)
                                            if roi_other.shape[0] >= t_img_g_1.shape[0] and roi_other.shape[1] >= t_img_g_1.shape[1]:
                                                res_1 = cv2.matchTemplate(roi_other, t_img_g_1, cv2.TM_CCOEFF_NORMED)
                                                _, max_val_1, _, max_loc_1 = cv2.minMaxLoc(res_1)
                                                if max_val_1 >= FUSION_CONF.get('tier_1.png', 0.72):
                                                    if is_truly_tier_1(roi_other, max_loc_1[0], max_loc_1[1], t1_h):
                                                        found_other = 1
                                                        
                                        if found_other == 0:
                                            bprint("  > ⚠️ [판독 실패] 나머지 1개의 등급 숫자를 명확히 읽지 못했습니다. 스킵합니다.")
                                            fast_clear_tooltip(); continue
                                            
                                        if is_ability_5: activity = found_other
                                        else: ability = found_other
                                        
                                        bprint(f"  > 🔍 [판독 완료] 어빌리티 {ability} / 활성 {activity}")
                                        target_pair = (activity, ability)
                                    
                                    if target_pair in memory_pool:
                                        match_cx, match_cy = memory_pool[target_pair]
                                        bprint(f"  > 🎯 [페어링 성공] {ability}/{activity} 와 {activity}/{ability} 짝을 찾았습니다!")
                                        safe_pts.append((match_cx, match_cy, match_cx, match_cy))
                                        safe_pts.append((real_x, real_y, cx, cy)) 
                                        pair_found = True
                                        char_inventory_memory[char_key] = (real_x, real_y) # [메모리 저장] 찾은 위치 기록
                                    else:
                                        memory_pool[(ability, activity)] = (cx, cy)
                                            
                                    fast_clear_tooltip()
                                            
                                    if len(safe_pts) >= 2:
                                        # [1단계: 기존 체크 해제 반복] 0/2(select_0_2.png)가 보일 때까지 무한 반복
                                        bprint("  > 🔄 [단계 1] 기존 체크 해제 및 0/2 상태 검증 시작...")
                                        while bot_active:
                                            dc_sct = cv2.cvtColor(np.asarray(thread_sct.grab({"left": 960, "top": 0, "width": 960, "height": 1080})), cv2.COLOR_BGRA2BGR)
                                            res_dc = cv2.matchTemplate(dc_sct, FUSION_CACHE['check_mark.png'], cv2.TM_CCOEFF_NORMED)
                                            loc_dc = np.where(res_dc >= 0.85)
                                            pts_dc = list(zip(*loc_dc[::-1]))
                                            
                                            # 체크마크가 보이면 일단 모두 해제
                                            if len(pts_dc) > 0:
                                                dc_unique = []
                                                for ptd in pts_dc:
                                                    if not any(math.hypot(ptd[0]-u[0], ptd[1]-u[1]) < 40 for u in dc_unique):
                                                        dc_unique.append(ptd)
                                                for ptd in dc_unique:
                                                    hx, hy = ptd[0] + 960 + 15, ptd[1] + 15
                                                    pyautogui.moveTo(hx, hy); time.sleep(0.02); send_cmd('C'); time.sleep(0.1)
                                                pyautogui.moveTo(200, 500); time.sleep(0.2)
                                            
                                            # 0/2 인지 최종 확인 (성공 시 루프 탈출)
                                            if check_img('select_0_2.png', thread_sct):
                                                bprint("  > ✅ [확인] 0/2 상태 진입 완료.")
                                                break
                                            time.sleep(0.1)

                                        # [2단계: 신규 재료 클릭 반복] 2/2(select_2_2.png)가 보일 때까지 무한 반복
                                        bprint("  > 🔄 [단계 2] 신규 재료 선택 및 2/2 상태 검증 시작...")
                                        while bot_active:
                                            # 확보된 safe_pts 2개 클릭
                                            for idx, (px, py, pcx, pcy) in enumerate(safe_pts):
                                                pyautogui.moveTo(pcx, pcy); time.sleep(0.05); send_cmd('C'); time.sleep(0.1)
                                                pyautogui.moveTo(200, 500); time.sleep(0.1)
                                            
                                            # 2/2 인지 최종 확인 (성공 시 루프 탈출)
                                            if check_img('select_2_2.png', thread_sct):
                                                bprint("  > ✅ [확인] 2/2 세팅 완료.")
                                                break
                                            bprint("  > ⚠️ [재시도] 2/2 미달성. 다시 클릭합니다.")
                                            time.sleep(0.2)
                                            
                                            # [추가된 방어 로직] 다시 클릭하기 전, 꼬여있는 체크마크가 있다면 모두 해제하여 0/2 상태로 초기화합니다.
                                            dc_sct = cv2.cvtColor(np.asarray(thread_sct.grab({"left": 960, "top": 0, "width": 960, "height": 1080})), cv2.COLOR_BGRA2BGR)
                                            res_dc = cv2.matchTemplate(dc_sct, FUSION_CACHE['check_mark.png'], cv2.TM_CCOEFF_NORMED)
                                            loc_dc = np.where(res_dc >= 0.85)
                                            pts_dc = list(zip(*loc_dc[::-1]))
                                            
                                            if len(pts_dc) > 0:
                                                bprint("  > 🛡️ [방어 로직] 체크 꼬임 감지! 체크마크를 모두 해제하고 초기화합니다.")
                                                dc_unique = []
                                                for ptd in pts_dc:
                                                    if not any(math.hypot(ptd[0]-u[0], ptd[1]-u[1]) < 40 for u in dc_unique):
                                                        dc_unique.append(ptd)
                                                for ptd in dc_unique:
                                                    hx, hy = ptd[0] + 960 + 15, ptd[1] + 15
                                                    pyautogui.moveTo(hx, hy); time.sleep(0.02); send_cmd('C'); time.sleep(0.1)
                                                pyautogui.moveTo(200, 500); time.sleep(0.2)
                                            
                                        last_cx, last_cy = safe_pts[-1][2], safe_pts[-1][3]
                                        pair_found = True
                                        break
                                        
                                if pair_found: break
                                
                                # [메모리 리스캔(안전망) 로직]
                                mem_x, mem_y = char_inventory_memory[char_key]
                                if not is_memory_rescan and (mem_x > 0 or mem_y > 0):
                                    bprint("  > 🧠 [메모리 리스캔] 기억된 위치 이후로 짝을 찾지 못했습니다. 안전을 위해 처음부터 1회 전체 스캔을 재진행합니다.")
                                    is_memory_rescan = True
                                    char_inventory_memory[char_key] = (0, 0) # 메모리 초기화 후 루프 재시작
                                    continue

                                search_attempts += 1
                                if search_attempts >= 1:
                                    bprint("  > 🛑 [재료 고갈] 5/5 페어링 가능한 감염물이 없습니다. 해당 캐릭터를 스킵합니다.")
                                    skip_current_char = True
                                    break
                                time.sleep(0.1)
                                
                            else:
                                # [모드 3: 깡 복사 가속화 및 동선 최적화] A1, B1 상관없이 모든 후보를 수집하여 모드 4와 동일한 (상->하, 좌->우) 스캔을 지원합니다.
                                all_candidates = []
                                for item_name in search_items:
                                    template = FUSION_CACHE.get(item_name)
                                    if template is None: continue
                                    
                                    conf = FUSION_CONF.get(item_name, 0.92)
                                    res = cv2.matchTemplate(screen_bgr, template, cv2.TM_CCOEFF_NORMED)
                                    loc = np.where(res >= conf)
                                    template_brightness = np.mean(template)
                                    h, w = template.shape[:2]
                                    
                                    for pt in zip(*loc[::-1]):
                                        real_x = pt[0] + X_OFFSET
                                        real_y = pt[1]
                                        
                                        if real_x < 960: continue
                                        is_checked = any(math.hypot(real_x-c[0], real_y-c[1]) < 40 for c in check_pts_global)
                                        if is_checked: continue
                                        
                                        roi = screen_bgr[pt[1]:pt[1]+h, pt[0]:pt[0]+w]
                                        if np.mean(roi) < (template_brightness * 0.75): continue
                                        if np.max(roi) < 90: continue 
                                        
                                        if any(math.hypot(real_x-cp[0], real_y-cp[1]) < 80 for cp in all_candidates): continue
                                        all_candidates.append((real_x, real_y, w, h, item_name))
                                
                                # [핵심] 화면 좌상단부터 우하단까지 깔끔하게 훑도록 정렬합니다. (모드 4와 동일한 인간적인 스캔 동선)
                                all_candidates.sort(key=lambda c: (c[0] // 100, c[1]))
                                
                                safe_pts = [] 
                                for pt_data in all_candidates:
                                    if len(safe_pts) >= 2: break
                                    real_x, real_y, w, h, item_name = pt_data
                                    # [스마트 메모리 패스 로직]
                                    curr_sort_key = (real_x // 100, real_y)
                                    mem_x, mem_y = char_inventory_memory[char_key]
                                    if not is_memory_rescan and curr_sort_key < (mem_x // 100, mem_y):
                                        continue # 과거 확인 위치 패스
                                        
                                    cx, cy = real_x + w//2, real_y + h//2
                                    pyautogui.moveTo(cx, cy)
                                    
                                    template_label = FUSION_CACHE.get('ability_label.png')
                                    mon = thread_sct.monitors[1]
                                    r_left = max(mon["left"], cx - 1100)
                                    r_top = mon["top"]
                                    r_width = 1100
                                    r_height = mon["height"]
                                    tooltip_roi = {"left": int(r_left), "top": int(r_top), "width": int(r_width), "height": int(r_height)}
                                    
                                    # [모드 3 전용: 라벨 트리거 0.05초 확정 판독 엔진]
                                    label_found = False
                                    lx, ly = 0, 0
                                    wait_start = time.time()
                                    while time.time() - wait_start < 1.0 and bot_active:
                                        hover_gray = cv2.cvtColor(np.asarray(thread_sct.grab(tooltip_roi)), cv2.COLOR_BGRA2GRAY)
                                        res_l = cv2.matchTemplate(hover_gray, template_label, cv2.TM_CCOEFF_NORMED)
                                        _, mv_l, _, ml_l = cv2.minMaxLoc(res_l)
                                        if mv_l >= 0.90:
                                            label_found = True; lx, ly = ml_l[0], ml_l[1]; break
                                        time.sleep(0.01)

                                    if not label_found:
                                        fast_clear_tooltip(); continue
                                        
                                    time.sleep(0.05) # 데이터 페치 딜레이 헷지
                                    
                                    hover_gray = cv2.cvtColor(np.asarray(thread_sct.grab(tooltip_roi)), cv2.COLOR_BGRA2GRAY)
                                    label_w = template_label.shape[1]
                                    col_x_start = lx + label_w
                                    col_x_end = min(hover_gray.shape[1], lx + label_w + 360)
                                    col_y_start = max(0, ly - 20)
                                    col_y_end = min(hover_gray.shape[0], ly + 150)
                                    roi_col = hover_gray[col_y_start:col_y_end, col_x_start:col_x_end]

                                    is_level_5 = False
                                    template_5 = FUSION_CACHE.get('level_5.png')
                                    if template_5 is not None:
                                        t5_gray = cv2.cvtColor(template_5, cv2.COLOR_BGR2GRAY)
                                        res_5 = cv2.matchTemplate(roi_col, t5_gray, cv2.TM_CCOEFF_NORMED)
                                        if np.max(res_5) >= FUSION_CONF.get('level_5.png', 0.75):
                                            is_level_5 = True
                                    
                                    if is_level_5:
                                        bprint(f"  > 🛑 [경고] 5레벨이 포함된 감염물입니다! 보호를 위해 스킵합니다.")
                                        fast_clear_tooltip()
                                        continue
                                        
                                    safe_pts.append((real_x, real_y, cx, cy))
                                    bprint(f"  > 🔍 [탐색] 안전한 감염물({item_name}) {len(safe_pts)}/2 확보")
                                    fast_clear_tooltip()
                                        
                                if len(safe_pts) >= 2:
                                    # [1단계: 기존 체크 해제 반복] 0/2(select_0_2.png)가 보일 때까지 무한 반복
                                    bprint("  > 🔄 [단계 1] 기존 체크 해제 및 0/2 상태 검증 시작...")
                                    while bot_active:
                                        dc_sct = cv2.cvtColor(np.asarray(thread_sct.grab({"left": 960, "top": 0, "width": 960, "height": 1080})), cv2.COLOR_BGRA2BGR)
                                        res_dc = cv2.matchTemplate(dc_sct, FUSION_CACHE['check_mark.png'], cv2.TM_CCOEFF_NORMED)
                                        loc_dc = np.where(res_dc >= 0.85)
                                        pts_dc = list(zip(*loc_dc[::-1]))
                                        
                                        if len(pts_dc) > 0:
                                            dc_unique = []
                                            for ptd in pts_dc:
                                                if not any(math.hypot(ptd[0]-u[0], ptd[1]-u[1]) < 40 for u in dc_unique):
                                                    dc_unique.append(ptd)
                                            for ptd in dc_unique:
                                                hx, hy = ptd[0] + 960 + 15, ptd[1] + 15
                                                pyautogui.moveTo(hx, hy); time.sleep(0.02); send_cmd('C'); time.sleep(0.1)
                                            fast_clear_tooltip()
                                        
                                        if check_img('select_0_2.png', thread_sct):
                                            bprint("  > ✅ [확인] 0/2 상태 진입 완료.")
                                            break
                                        time.sleep(0.1)

                                    # [2단계: 신규 재료 클릭 반복] 2/2(select_2_2.png)가 보일 때까지 무한 반복
                                    bprint("  > 🔄 [단계 2] 신규 재료 선택 및 2/2 상태 검증 시작...")
                                    while bot_active:
                                        for idx, (px, py, pcx, pcy) in enumerate(safe_pts):
                                            pyautogui.moveTo(pcx, pcy); time.sleep(0.05); send_cmd('C'); time.sleep(0.1)
                                            fast_clear_tooltip()
                                        
                                        if check_img('select_2_2.png', thread_sct):
                                            bprint("  > ✅ [확인] 2/2 세팅 완료.")
                                            break
                                        bprint("  > ⚠️ [재시도] 2/2 미달성. 다시 클릭합니다.")
                                        time.sleep(0.2)
                                        
                                        # [추가된 방어 로직] 다시 클릭하기 전, 꼬여있는 체크마크가 있다면 모두 해제하여 0/2 상태로 초기화합니다.
                                        dc_sct = cv2.cvtColor(np.asarray(thread_sct.grab({"left": 960, "top": 0, "width": 960, "height": 1080})), cv2.COLOR_BGRA2BGR)
                                        res_dc = cv2.matchTemplate(dc_sct, FUSION_CACHE['check_mark.png'], cv2.TM_CCOEFF_NORMED)
                                        loc_dc = np.where(res_dc >= 0.85)
                                        pts_dc = list(zip(*loc_dc[::-1]))
                                        
                                        if len(pts_dc) > 0:
                                            bprint("  > 🛡️ [방어 로직] 체크 꼬임 감지! 체크마크를 모두 해제하고 초기화합니다.")
                                            dc_unique = []
                                            for ptd in pts_dc:
                                                if not any(math.hypot(ptd[0]-u[0], ptd[1]-u[1]) < 40 for u in dc_unique):
                                                    dc_unique.append(ptd)
                                            for ptd in dc_unique:
                                                hx, hy = ptd[0] + 960 + 15, ptd[1] + 15
                                                pyautogui.moveTo(hx, hy); time.sleep(0.02); send_cmd('C'); time.sleep(0.1)
                                            fast_clear_tooltip()
                                        
                                    last_cx, last_cy = safe_pts[-1][2], safe_pts[-1][3]
                                    pair_found = True
                                    char_inventory_memory[char_key] = (safe_pts[-1][0], safe_pts[-1][1]) # [메모리 저장] 마지막 채택 감염물 위치 기록
                                    break
                                    
                                if pair_found: break
                                
                                # [메모리 리스캔(안전망) 로직]
                                mem_x, mem_y = char_inventory_memory[char_key]
                                if not is_memory_rescan and (mem_x > 0 or mem_y > 0):
                                    bprint("  > 🧠 [메모리 리스캔] 기억된 위치 이후로 짝을 찾지 못했습니다. 안전을 위해 처음부터 1회 전체 스캔을 재진행합니다.")
                                    is_memory_rescan = True
                                    char_inventory_memory[char_key] = (0, 0)
                                    continue

                                search_attempts += 1
                                if search_attempts >= 1:
                                    bprint("  > 🛑 [재료 고갈] 안전한 페어링 감염물이 없습니다. 해당 캐릭터를 스킵합니다.")
                                    skip_current_char = True
                                    break
                                time.sleep(0.1)

                        if not bot_active: continue
                        
                        # [스킵 로직] 재료 소진 시 융합기를 안전하게 닫고 즉시 다음 캐릭터로 넘깁니다.
                        if skip_current_char:
                            skipped_chars.add(char_index)
                            active_chars = loop_count - len(skipped_chars)
                            
                            if active_chars <= 0:
                                bprint("  > 🛑 [알림] 모든 캐릭터의 재료가 소진되었습니다. 매크로를 자동 정지합니다.")
                                toggle_stop()
                                continue
                                
                            bprint("  > [탈출 준비] 인벤토리 창 닫기 (inv_title.png 소멸 능동 대기)...")
                            inv_closed = False
                            while bot_active and not inv_closed:
                                send_cmd('E'); time.sleep(0.1); send_cmd('R')
                                wait_inv = time.time()
                                while time.time() - wait_inv < 1.5 and bot_active:
                                    if check_popup_char(thread_sct):
                                        wait_inv = time.time() 
                                        continue
                                    if not check_img('inv_title.png', thread_sct): 
                                        inv_closed = True
                                        break
                                    time.sleep(0.03)
                                if not inv_closed:
                                    bprint("  > [꼬임 방지] 인벤토리 창이 닫히지 않았습니다. ESC 재입력...")
                                    time.sleep(0.1)

                            if bot_mode in [3, 4]:
                                # [핵심 수정] 스킵 시에도 방금 건너뛴 녀석이 마지막 서브 캐릭터였는지 검사
                                last_active_index = -1
                                for i in range(anchor_idx - 1, -1, -1):
                                    if i not in skipped_chars:
                                        last_active_index = i
                                        break
                                        
                                if char_index == anchor_idx or char_index > last_active_index:
                                    bprint(f"\n✨ [사이클 완료] 모든 캐릭터 세팅이 완료되었습니다. 대기 앵커로 이동합니다.")
                                    go_to_state_6_next = True 
                                    
                                    wait_anchor = anchor_idx
                                    if anchor_idx in skipped_chars:
                                        wait_anchor = 0
                                        while wait_anchor in skipped_chars and wait_anchor < anchor_idx:
                                            wait_anchor += 1
                                    char_index = wait_anchor
                                else:
                                    while True:
                                        char_index += 1
                                        if char_index >= loop_count: char_index = 0
                                        if char_index not in skipped_chars: break
                            else:
                                char_index += 1
                                while char_index in skipped_chars and char_index < loop_count:
                                    char_index += 1
                                if char_index >= loop_count:
                                    char_index = 0
                                    while char_index in skipped_chars:
                                        char_index += 1
                                        
                            state = 1
                            bprint("  > 🚀 [스킵 로직 완료] 메인 스레드로 복귀하여 State 1을 가동합니다.")
                            continue 
                        else:
                            skipped_chars.discard(char_index)

                        # [단계 4] 2/2 선택 완료 대기 및 F 입력
                        bprint("  > 4. 선택 완료(select_2_2.png) 창 대기 중...")
                        wait_sel = time.time()
                        while bot_active:
                            if not bot_active: raise BotStopException()
                            # [핵심] 2/2 확인용 select_2_2.png ROI 적용 (force_full 제거)
                            if check_img('select_2_2.png', thread_sct):
                                # [결함 방어] 2/2 상태가 되었더라도, 내가 클릭한 감염물(safe_pts)이 아닌 엉뚱한 곳에 버그성 체크가 발생했는지 무결성 교차 검증!
                                dc_sct = cv2.cvtColor(np.asarray(thread_sct.grab({"left": 960, "top": 0, "width": 960, "height": 1080})), cv2.COLOR_BGRA2BGR)
                                res_dc = cv2.matchTemplate(dc_sct, FUSION_CACHE['check_mark.png'], cv2.TM_CCOEFF_NORMED)
                                loc_dc = np.where(res_dc >= 0.85)
                                pts_dc = list(zip(*loc_dc[::-1]))

                                rogue_check_found = False
                                rogue_pts = []

                                if len(pts_dc) > 0:
                                    dc_unique = []
                                    for ptd in pts_dc:
                                        if not any(math.hypot(ptd[0]-u[0], ptd[1]-u[1]) < 40 for u in dc_unique):
                                            dc_unique.append(ptd)
                                            
                                    for ptd in dc_unique:
                                        abs_check_x = ptd[0] + 960
                                        abs_check_y = ptd[1]
                                        
                                        is_safe = False
                                        for sp in safe_pts:
                                            # sp[0], sp[1]은 목표 감염물 이미지 인식 좌상단 좌표입니다.
                                            # 체크마크가 타겟 감염물 반경 100픽셀 이내에 있는지 확인하여 본인이 맞는지 대조합니다.
                                            if math.hypot(abs_check_x - sp[0], abs_check_y - sp[1]) < 100:
                                                is_safe = True
                                                break
                                                
                                        if not is_safe:
                                            rogue_check_found = True
                                            rogue_pts.append(ptd)
                                            
                                if rogue_check_found:
                                    bprint("  > 🚨 [치명적 버그 방어] 의도하지 않은 과거 감염물에 버그성 체크마크가 발생했습니다!")
                                    # 버그 체크마크 클릭하여 강제 해제
                                    for ptd in rogue_pts:
                                        hx, hy = ptd[0] + 960 + 15, ptd[1] + 15
                                        pyautogui.moveTo(hx, hy); time.sleep(0.02); send_cmd('C'); time.sleep(0.1)
                                        
                                    bprint("  > 🔄 버그 체크마크 해제 완료. 올바른 감염물을 다시 클릭합니다.")
                                    for idx, (px, py, pcx, pcy) in enumerate(safe_pts):
                                        pyautogui.moveTo(pcx, pcy); time.sleep(0.05); send_cmd('C'); time.sleep(0.1)
                                    pyautogui.moveTo(200, 500); time.sleep(0.2)
                                    
                                    wait_sel = time.time() # 2/2 대기 타이머 리셋
                                    continue # F를 누르지 않고 while 루프로 돌아가 select_2_2.png 상태를 처음부터 재검증합니다.

                                bprint("  > [성공] 2/2 선택 완료 및 무결성 검증 통과! F를 입력하여 선택창을 닫습니다.")
                                send_cmd('F'); time.sleep(0.05); send_cmd('R')
                                wait_vanish('select_2_2.png', thread_sct)
                                break
                                
                            if time.time() - wait_sel > 1.0:
                                bprint("  > [재시도] 2/2 선택창 미탐(서버 렉). 마지막 감염물 재클릭 시도...")
                                pyautogui.moveTo(last_cx, last_cy); time.sleep(0.05); send_cmd('C')
                                pyautogui.moveTo(200, 500); time.sleep(0.25) 
                                wait_sel = time.time()
                            time.sleep(0.1)

                    if not bot_active: continue
                    
                    # [단계 4.5] 융합 시작 버튼 좌클릭 및 F 입력
                    bprint("  > 4.5. '융합 시작(fusion_start.png)' 버튼 탐색 중...")
                    bug_detected_in_start = False
                    while bot_active:
                        if not bot_active: raise BotStopException()
                        
                        found_in_2s = False
                        wait_start_btn = time.time()
                        
                        # 2초 동안 0.1초 간격으로 능동 대기
                        while time.time() - wait_start_btn < 2.0 and bot_active:
                            if check_img('fusion_start.png', thread_sct):
                                cx, cy = FUSION_ROI['fusion_start.png']['last_pos']
                                pyautogui.moveTo(cx, cy); time.sleep(0.02)
                                
                                # [찰나의 버그 방어] 마우스 이동 직후, 클릭 방아쇠를 당기기 직전에 bug_time.png가 튀어나왔는지 최종 교차 검증!
                                if not check_img('stop_btn.png', thread_sct) and check_img('bug_time.png', thread_sct):
                                    bprint("  > 🚨 [긴급 방어] 클릭 직전 디싱크 버그(bug_time.png) 난입 감지! 클릭을 즉시 취소합니다.")
                                    bug_detected_in_start = True
                                    found_in_2s = True
                                    break

                                bprint("  > [성공] 융합 시작 버튼 최종 확인! 좌클릭 후 F를 입력합니다.")
                                send_cmd('C'); time.sleep(0.05)
                                
                                send_cmd('F'); time.sleep(0.05); send_cmd('R')
                                found_in_2s = True
                                break
                                
                            # [방어 로직] 4.5 탐색 중 디싱크 버그 감지 (주 목표가 없을 때만 후순위 검사)
                            if not check_img('stop_btn.png', thread_sct) and check_img('bug_time.png', thread_sct):
                                bug_detected_in_start = True
                                found_in_2s = True
                                break
                                
                            time.sleep(0.1)
                            
                        # 2초 이내에 성공적으로 찾았으면 루프 탈출
                        if found_in_2s:
                            break
                            
                        # 2초를 초과하여 못 찾았을 경우 select_2_2.png 소멸 여부 재확인
                        bprint("  > ⚠️ [시간 초과] 2초간 융합 시작 버튼 미인식. select_2_2.png 소멸 여부를 재확인합니다.")
                        if check_img('select_2_2.png', thread_sct):
                            bprint("  > [꼬임 방지] select_2_2.png가 남아있습니다! F를 재입력하여 닫기를 시도합니다.")
                            send_cmd('F'); time.sleep(0.05); send_cmd('R')
                            wait_vanish('select_2_2.png', thread_sct)
                        else:
                            bprint("  > [확인] select_2_2.png는 정상 소멸 상태입니다. 융합 시작 버튼을 다시 탐색합니다.")
                        
                    if not bot_active: continue
                    
                    if not bug_detected_in_start:
                        # [단계 4.8] 융합 진행 상태(stop_btn.png) 전환 능동 대기 중...
                        bprint("  > 4.8. 융합 진행 상태(stop_btn.png) 전환 능동 대기 중...")
                        wait_stop = time.time()
                        while bot_active:
                            if not bot_active: raise BotStopException()
                            if check_popup_char(thread_sct):
                                wait_stop = time.time()
                                continue
                                
                            # [추가된 방어 로직] 4.8 대기 중 무한 루프 빠짐 방지 (버그 자가치유)
                            if not check_img('stop_btn.png', thread_sct) and check_img('bug_time.png', thread_sct):
                                bug_detected_in_start = True
                                break
                            
                            # [핵심] stop_btn.png ROI 적용 (force_full 제거)
                            if check_img('stop_btn.png', thread_sct):
                                bprint("  > [성공] stop_btn.png 전환 완료! ESC 탈출을 위해 State 1로 배턴을 넘깁니다.")
                                
                                current_anchor = 5
                                if 5 in skipped_chars:
                                    current_anchor = 0
                                    while current_anchor in skipped_chars and current_anchor < 5:
                                        current_anchor += 1
                                        
                                if bot_mode in [3, 4] and char_index == current_anchor:
                                    fusion_end_time = time.time() + 300.0
                                    bprint("  > ⏱️ [타이머 갱신] 앵커 캐릭터 융합 가동 시작! 5분(300초) 타이머가 설정되었습니다.")
                                break
                                
                            if time.time() - wait_stop > 1.0:
                                bprint("  > [재시도] 기계 가동 미확인(서버 렉). 시작(F) 재입력 시도...")
                                send_cmd('F'); time.sleep(0.05); send_cmd('R')
                                wait_stop = time.time()
                                
                            time.sleep(0.05)

                    # [통합 버그 치유 로직] 단계 4.5 ~ 4.8 구간에서 버그가 발생했을 때 공통 처리
                    if bug_detected_in_start:
                        print() # 줄바꿈 복구
                        bprint("  > 🚨 [버그 감지] 융합 시작 시퀀스 중 UI 디싱크 버그 확인! 기계를 강제로 재부팅합니다.")
                        send_cmd('E'); time.sleep(0.15); send_cmd('R')
                        
                        bprint("  > [버그 치유 1/2] 메인 화면(7.png) 복귀 대기 중...")
                        wait_7 = time.time()
                        is_out = False
                        while time.time() - wait_7 < 3.0 and bot_active:
                            if check_img('7.png', thread_sct, force_full=True):
                                is_out = True
                                break
                            time.sleep(0.1)
                            
                        if not is_out:
                            bprint("  > [재시도] 화면 닫기 지연. ESC 추가 입력.")
                            send_cmd('E'); time.sleep(0.15); send_cmd('R')
                            time.sleep(1.0)
                            
                        bprint("  > [버그 치유 2/2] 14.png 탐색(마우스 회전) 및 상호작용(F) 재진입...")
                        entered_machine = False
                        while bot_active:
                            if check_img('14.png', thread_sct, force_full=True):
                                bprint("  > [확인] 14.png 발견! 상호작용(F) 시도 및 융합기 진입 대기...")
                                while bot_active:
                                    send_cmd('F'); time.sleep(0.1); send_cmd('R')
                                    wait_chance = time.time()
                                    found_chance = False
                                    while time.time() - wait_chance < 3.0 and bot_active:
                                        if check_img('chance.png', thread_sct):
                                            found_chance = True
                                            break
                                        time.sleep(0.1)
                                    if found_chance:
                                        bprint("  > ✅ [버그 치유 완료] 융합기(chance.png) 재진입 성공! 세팅을 처음부터 다시 시작합니다.")
                                        entered_machine = True
                                        break
                                    else:
                                        bprint("  > ⚠️ [재시도] 3초간 chance.png 미발견(서버 렉). F키를 다시 입력합니다.")
                                if entered_machine:
                                    break
                            else:
                                send_cmd('M', 60, 0); time.sleep(0.15)
                        continue # State 7 처음으로 돌아가서 정상적으로 상태 확인 수행

                    if not bot_active: continue

                    # [단계 5] 사이클 마무리 및 분기
                    active_chars = loop_count - len(skipped_chars)
                    
                    if bot_mode in [3, 4]:
                        if active_chars == 1:
                            c_name = CHAR_NAMES.get(char_images[char_index], "캐릭")
                            bprint(f"  > 👑 [단일 생존] 마지막 남은 '{c_name}' 입니다! 접속 해제 없이 즉시 대기 모드로 돌입합니다.")
                            state = 7
                        else:
                            # 과거의 숫자 4(앵커 5의 앞번호)를 anchor_idx 기준으로 자동 대응되게 수정
                            last_active_index = -1
                            for i in range(anchor_idx - 1, -1, -1):
                                if i not in skipped_chars:
                                    last_active_index = i
                                    break
                                    
                            if char_index == anchor_idx or char_index < last_active_index: 
                                bprint(f"\n🔄 [세팅 완료] 다음 생존 캐릭터로 교체하기 위해 탐색을 시작합니다.\n")
                                while True:
                                    char_index += 1
                                    if char_index >= loop_count: char_index = 0
                                    if char_index not in skipped_chars: break
                                state = 1
                            else:
                                bprint(f"\n✨ [사이클 완료] 모든 캐릭터 세팅이 완료되었습니다. 대기 앵커로 이동합니다.")
                                go_to_state_6_next = True 
                                
                                wait_anchor = anchor_idx
                                if anchor_idx in skipped_chars:
                                    wait_anchor = 0
                                    while wait_anchor in skipped_chars and wait_anchor < anchor_idx:
                                        wait_anchor += 1
                                char_index = wait_anchor
                                state = 1
                    else:
                        char_index += 1
                        while char_index in skipped_chars and char_index < loop_count:
                            char_index += 1
                        if char_index >= loop_count:
                            char_index = 0
                            while char_index in skipped_chars:
                                char_index += 1
                        state = 1

            except BotStopException:
                bprint("  > [작동 정지] 안전하게 대기 모드로 전환되었습니다.")
                try:
                    arduino.write('U'.encode()); arduino.flush()
                    arduino.write('R'.encode()); arduino.flush()
                except: pass
                continue

def force_change_character(char_key):
    """F6~F11 단축키를 통해 즉시 실행되는 수동 캐릭터 변경 전용 로직"""
    global bot_active, char_thread_active, bot_mode
    
    # 이미 수동 캐릭터 변경이 진행 중이면 중복 실행을 완벽히 차단합니다.
    if char_thread_active:
        bprint("\n⚠️ [경고] 이미 캐릭터 수동 변경이 진행 중입니다. 무시됩니다.")
        return
    
    was_active = bot_active
    
    # [핵심 수정] 모드 1(단일 타이머)이 가동 중일 때는 봇을 끄지 않고 백그라운드 타이머를 유지합니다!
    # 그 외의 모드(모드 2~4 멀티 교체 등)가 동작 중일 때만 마우스 충돌 방지를 위해 봇을 자동 정지시킵니다.
    if was_active and bot_mode != 1:
        toggle_stop()
        
    char_thread_active = True

    if was_active:
        if bot_mode != 1:
            time.sleep(0.5)
        else:
            print() # 동적 타이머( \r )와 로그 텍스트가 겹쳐서 깨지는 현상을 방지하기 위해 강제 줄바꿈

    c_name = CHAR_NAMES.get(char_key, char_key)
    bprint(f"\n🚀 [수동 캐릭터 변경] '{c_name}' 접속 시퀀스 시작!")

    with mss.mss() as thread_sct:
        state = 1
        
        try: 
            while char_thread_active:
                # --- [State 1] 1.png 확인 및 초고속 탈출 ---
                if state == 1:
                    bprint("  > ESC 1회 입력 후 1.png 초고속 능동 대기...")
                    send_cmd('E'); time.sleep(0.15); send_cmd('R')
                    
                    found_1 = False
                    wait_1 = time.time()
                    # 7.png 대기 없이 곧바로 1.png 탐색 (force_full=True 및 2.0초 대기)
                    while time.time() - wait_1 < 2.0 and char_thread_active:
                        if check_popup_main(thread_sct): 
                            wait_1 = time.time()
                            continue
                        if check_img('1.png', thread_sct, force_full=True):
                            found_1 = True
                            break
                        time.sleep(0.05)
                        
                    if not char_thread_active: break
                        
                    if found_1:
                        bprint("  > [성공] 1.png 확인 완료! 마우스 클릭 후 2.png 동적 대기(최대 5초)...")
                        cx, cy = FUSION_ROI['1.png']['last_pos']
                        pyautogui.moveTo(cx, cy); time.sleep(0.05); send_cmd('C')
                        
                        wait_2 = time.time()
                        found_2 = False
                        while time.time() - wait_2 < 5.0 and char_thread_active:
                            if check_img('2.png', thread_sct):
                                found_2 = True
                                break
                            time.sleep(0.05)
                            
                        if not char_thread_active: break
                            
                        if found_2:
                            bprint("  > [동적 대기 성공] 2.png 화면 전환 확인! 2단계로 이동.")
                            state = 2
                        else:
                            bprint("  > [동적 대기 실패] 5초 경과. 화면 전환 누락으로 간주하여 재시도합니다.")
                    else:
                        bprint("  > [초고속 재시도] 1.png 화면 전환 미감지. ESC 즉시 재입력!")
                        if check_img('6.png', thread_sct, force_full=True):
                            time.sleep(0.5)

                # --- [State 2] 2.png 대기 및 3.png 전이 ---
                elif state == 2:
                    bprint("  > [State 2] 2.png 탐색 및 3.png 대기 중...")
                    check_popup_main(thread_sct)
                    
                    if check_img('2.png', thread_sct):
                        bprint("  > 2.png 확인! N 입력 후 3.png 동적 대기(최대 10초)...")
                        send_cmd('N'); time.sleep(0.1); send_cmd('R')
                        
                        wait_3 = time.time()
                        found_3 = False
                        # [요청 반영] 30초 동안 3.png가 확인될 때까지 0.1초 간격으로 마우스 좌클릭(C)을 반복합니다.
                        while time.time() - wait_3 < 30.0 and char_thread_active:
                            check_popup_main(thread_sct)
                            if check_img('3.png', thread_sct, force_full=True):
                                found_3 = True
                                break
                            
                            send_cmd('C')
                            time.sleep(0.1)
                            
                        if not char_thread_active: break
                        
                        if found_3:
                            bprint("  > [동적 대기 성공] 3.png 확인 완료. 3단계 이동.")
                            state = 3
                        else:
                            bprint("  > [동적 대기 실패] 10초간 3.png 미발견. N키를 다시 누르기 위해 루프를 재시작합니다.")
                            continue
                            
                    else:
                        bprint("  > [꼬임 방지] 2.png 미발견. 1.png 다시 클릭 후 동적 대기(최대 5초)...")
                        if check_img('1.png', thread_sct, force_full=True):
                            cx, cy = FUSION_ROI['1.png']['last_pos']
                            pyautogui.moveTo(cx, cy); time.sleep(0.05); send_cmd('C')
                            
                            wait_2_fb = time.time()
                            while time.time() - wait_2_fb < 5.0 and char_thread_active:
                                if check_img('2.png', thread_sct):
                                    bprint("  > [동적 대기 성공] 2.png 화면 복구 완료!")
                                    break
                                time.sleep(0.05)
                    time.sleep(0.1)

                # --- [State 3] 캐릭터 지정 및 F 입력 ---
                elif state == 3:
                    bprint(f"  > [State 3] G 1회 입력 후 '{c_name}' 탐색")
                    send_cmd('G'); time.sleep(0.1); send_cmd('R')
                    
                    found_char = False
                    wait_g = time.time()
                    while time.time() - wait_g < 1.5 and char_thread_active:
                        if check_img(char_key, thread_sct, force_full=True):
                            found_char = True
                            break
                        time.sleep(0.1)
                        
                    if not char_thread_active: break

                    if found_char:
                        bprint(f"  > [성공] '{c_name}' 발견! 클릭 실행")
                        cx, cy = FUSION_ROI[char_key]['last_pos']
                        pyautogui.moveTo(cx, cy); time.sleep(0.05); send_cmd('C'); time.sleep(0.1)
                        
                        send_cmd('F'); time.sleep(0.1); send_cmd('R')
                        state = 4
                    else:
                        bprint("  > [재시도] 캐릭터 미발견. 목록이 닫혔을 가능성에 대비하여 G 재입력...")

                # --- [State 4] 6.png 등장 동적 대기 및 소멸 검증 ---
                elif state == 4:
                    bprint("  > [State 4] 화면 전환(6.png) 동적 대기(최대 5초)...")
                    found_6 = False
                    wait_6 = time.time()
                    while time.time() - wait_6 < 5.0 and char_thread_active:
                        if check_img('6.png', thread_sct):
                            found_6 = True
                            break
                        time.sleep(0.05)
                        
                    if not char_thread_active: break
                        
                    if found_6:
                        wait_vanish('6.png', thread_sct)
                        if not char_thread_active: break
                        bprint(f"  > ✅ [성공] 6.png 소멸 완료. '{c_name}' 캐릭터 변경 완료!\n")
                        break # 완전히 종료
                    else:
                            bprint("  > [재시도] 화면 전환(6.png) 미감지. F키를 다시 입력합니다...")
                            send_cmd('F'); time.sleep(0.1); send_cmd('R')

        except BotStopException:
            pass 
            
        finally:
            # [핵심 수정] 스레드가 정상 종료되든 오류가 나든 무조건 플래그를 False로 복구하여 타이머 화면 출력을 재개합니다.
            char_thread_active = False

        if bot_active and bot_mode == 1:
            print()
            bprint(f"✅ '{c_name}' 수동 변경 완료. 백그라운드 타이머 화면 출력을 재개합니다.")
        else:
            bprint(f"🛑 '{c_name}' 수동 변경 시퀀스가 정지 명령에 의해 중단되었거나 완료되었습니다.\n")

# === [시작점 및 단축키 설정] ===
def main_bot():
    threading.Thread(target=fusion_bot_loop, daemon=True).start()
    
    keyboard.add_hotkey('[', toggle_stop)
    keyboard.add_hotkey(']', lambda: toggle_start(1)) # 모드 1: 타이머만
    keyboard.add_hotkey('>', lambda: toggle_start(2)) # 모드 2: 멀티 교체
    keyboard.add_hotkey('?', lambda: toggle_start(3)) # 모드 3: 지능형 융합
    keyboard.add_hotkey('<', lambda: toggle_start(4)) # 모드 4: 5/5 지능형 복사
    keyboard.add_hotkey(';', lambda: toggle_start(5)) # 모드 5: 감염물 분별
    keyboard.add_hotkey('-', toggle_dimming_setting) # 모니터 절전 토글
    
    # [5/5 자동화] 중앙 관리 배열(MY_CHARACTERS)을 스캔하여 수동 캐릭터 단축키를 자동으로 생성합니다!
    for c in MY_CHARACTERS:
        if c.get("hotkey"):
            # 람다 클로저 충돌을 피하기 위해 k=c["img"]로 변수 바인딩
            keyboard.add_hotkey(c["hotkey"], lambda k=c["img"]: threading.Thread(target=force_change_character, args=(k,), daemon=True).start())

    bprint("\n=========================================")
    bprint(" 🚀 원스휴먼 스마트 융합 봇 가동 준비 🚀")
    bprint("=========================================")
    bprint(" [ : 정지 (대기 상태)")
    bprint(" ] : 융합 타이머(기본) 시작")
    bprint(" > : 캐릭터 자동 전환 모드 시작")
    bprint(" ? : 깡 복사 모드 시작")
    bprint(" < : 5/5 복사 모드 시작")
    bprint(" ; : 감염물 분별 모드 시작")
    bprint(" - : 모니터 절전(밝기 0%) 자동 켜기/끄기")
    bprint(" ---------------------------------------")
    bprint(" [수동 캐릭터 변경 단축키]")
    
    # 생성된 단축키를 메뉴판에 보기 좋게 자동 출력
    for c in MY_CHARACTERS:
        if c.get("hotkey"):
            bprint(f" {c['hotkey']:<4} : {c['name']}")
    bprint("=========================================\n")

    while True:
        original_sleep(1)

if __name__ == "__main__":
    main_bot()
