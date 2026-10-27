# -*- coding: utf-8 -*-
"""
Движок: Chrome (через DevTools Protocol), горячие клавиши OBS, Zoom и Mirapolis,
очередь Zoom-конференций и работа по расписанию, переименование записей OBS.
Все настройки — в config.json рядом с этим файлом (меняются через приложение app.pyw).
"""

import copy
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, quote

import websocket  # pip install websocket-client

# В собранном exe настройки и профиль Chrome лежат рядом с exe, а не во временной папке
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
PROFILE_DIR = os.path.join(BASE_DIR, "chrome-zoom-profile")
DEBUG_PORT = 9222
CHECK_INTERVAL_SEC = 2

DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DAY_CHOICES = DAYS + ["Будни", "Каждый день"]
WEEK_CHOICES = ["Каждая", "Верхняя", "Нижняя"]

DEFAULT_CONFIG = {
    "zoom_name": "Ошмарин Никита",
    "last_name": "Ошмарин",
    "first_name": "Никита",
    "middle_name": "",
    "mic_off": True,
    "camera_off": True,
    "hotkeys": {"start": "pause", "stop": "pause", "pause": "shift+pause", "unpause": "shift+pause"},
    "grace_minutes": 10,
    "end_confirm_sec": 20,
    "join_timeout_min": 30,
    "connect_before_min": 2,     # за сколько минут до начала пары подключаться
    "zoom_overtime_min": 15,     # пара в Zoom затянулась — ждать конца конференции до N минут
    "lesson_minutes": 90,
    "upper_week_monday": None,   # понедельник любой верхней недели, ГГГГ-ММ-ДД
    "obs_rename": True,
    "obs_folder": os.path.join(os.path.expanduser("~"), "Videos"),
    "obs_template": "{name} {date} {time}",
    "chrome_path": "",
    "links": [
        {"name": "Zoom 1", "url": "https://us05web.zoom.us/j/88979909606?pwd=vK8NVdYphoBg1L8ZJUbSwAxruQObIa.1"},
        {"name": "Zoom 2", "url": "https://us05web.zoom.us/j/85805268106?pwd=6wP1cbr8JcuyDfEhJA3Aqazzigafle.1"},
        {"name": "Zoom 3", "url": "https://us05web.zoom.us/j/86035651027?pwd=0VWvRDV8zFLSTc9aaQqbl9Dj2qsedx.1"},
        {"name": "Мира 1", "url": "http://b23259.vr.mirapolis.ru/mira/miravr/5224692382"},
    ],
    "queue": ["Zoom 1", "Zoom 2", "Zoom 3"],
    "pairs": [
        {"num": 1, "start": "08:30", "end": "10:00"},
        {"num": 2, "start": "10:10", "end": "11:40"},
        {"num": 3, "start": "12:10", "end": "13:40"},
        {"num": 4, "start": "13:50", "end": "15:20"},
        {"num": 5, "start": "15:30", "end": "17:00"},
        {"num": 6, "start": "17:10", "end": "18:40"},
    ],
    "schedule": [],
}


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ---------- время и расписание ----------

def parse_hm(s):
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", s or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"Неверное время '{s}', нужно ЧЧ:ММ")
    return int(m.group(1)), int(m.group(2))


def add_minutes(hm, minutes):
    h, m = parse_hm(hm)
    t = datetime(2000, 1, 1, h, m) + timedelta(minutes=minutes)
    return t.strftime("%H:%M")


def day_matches(day, date):
    day = (day or "").strip()
    if day == "Каждый день":
        return True
    if day == "Будни":
        return date.weekday() < 5
    if day in DAYS:
        return DAYS.index(day) == date.weekday()
    try:
        return datetime.strptime(day, "%d.%m.%Y").date() == date
    except ValueError:
        return False


def valid_day(day):
    if day in DAY_CHOICES:
        return True
    try:
        datetime.strptime(day, "%d.%m.%Y")
        return True
    except ValueError:
        return False


def monday_of(date):
    return date - timedelta(days=date.weekday())


def upper_ref(cfg):
    """Понедельник верхней недели; если не задан — текущая неделя считается верхней."""
    if cfg.get("upper_week_monday"):
        return datetime.strptime(cfg["upper_week_monday"], "%Y-%m-%d").date()
    return monday_of(datetime.now().date())


def week_type(date, ref):
    return "Верхняя" if ((monday_of(date) - ref).days // 7) % 2 == 0 else "Нижняя"


def occurrences(schedule, now, ref=None, days_ahead=15):
    """Все занятия, которые ещё не закончились, по времени начала."""
    ref = ref or monday_of(now.date())
    result = []
    for entry in schedule:
        for d in range(-1, days_ahead):
            date = (now + timedelta(days=d)).date()
            if not day_matches(entry["day"], date):
                continue
            week = entry.get("week") or "Каждая"
            if week != "Каждая" and week_type(date, ref) != week:
                continue
            (sh, sm), (eh, em) = parse_hm(entry["start"]), parse_hm(entry["end"])
            s = datetime(date.year, date.month, date.day, sh, sm)
            e = datetime(date.year, date.month, date.day, eh, em)
            if e <= s:
                e += timedelta(days=1)
            if e > now:
                result.append((s, e, entry))
    result.sort(key=lambda x: x[0])
    return result


def fmt_left(seconds):
    seconds = max(0, int(seconds))
    d, rest = divmod(seconds, 86400)
    h, rest = divmod(rest, 3600)
    m, s = divmod(rest, 60)
    return (f"{d} д " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"


# ---------- горячие клавиши ----------

user32 = ctypes.WinDLL("user32", use_last_error=True)

VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
      "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B,
      "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E,
      "pageup": 0x21, "pagedown": 0x22, "pause": 0x13}
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
VK.update({f"num{i}": 0x60 + i for i in range(10)})
VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
VK.update({str(i): 0x30 + i for i in range(10)})

KEYEVENTF_KEYUP = 0x0002


def parse_hotkey(combo):
    codes = []
    for k in (k.strip().lower() for k in combo.split("+")):
        if k not in VK:
            raise ValueError(f"Неизвестная клавиша '{k}' в '{combo}'. Допустимые: "
                             + ", ".join(sorted(VK)))
        codes.append(VK[k])
    return codes


def press_hotkey(combo):
    codes = parse_hotkey(combo)
    for c in codes:
        user32.keybd_event(c, 0, 0, 0)
        time.sleep(0.03)
    time.sleep(0.1)  # OBS опрашивает клавиатуру — держим чуть дольше
    for c in reversed(codes):
        user32.keybd_event(c, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)


# ---------- имя файла записи ----------

VIDEO_EXT = {".mkv", ".mp4", ".mov", ".flv", ".ts", ".m3u8", ".m4v", ".webm"}


def format_name(template, name, dt):
    s = (template.replace("{name}", name)
                 .replace("{date}", dt.strftime("%d.%m.%Y"))
                 .replace("{time}", dt.strftime("%H-%M"))
                 .replace("{day}", DAYS[dt.weekday()]))
    s = re.sub(r'[\\/:*?"<>|]', "-", s).strip(" .")
    return s or dt.strftime("%Y-%m-%d %H-%M")


def rename_recording(folder, started_at, new_name, log):
    """Ждёт, пока OBS допишет файл, и переименовывает самый новый файл записи."""
    time.sleep(5)
    deadline = time.time() + 300
    last_size = None
    while time.time() < deadline:
        try:
            files = [e for e in os.scandir(folder)
                     if e.is_file() and os.path.splitext(e.name)[1].lower() in VIDEO_EXT
                     and e.stat().st_ctime >= started_at - 60]
        except OSError as ex:
            log(f"Папка записей недоступна: {ex}")
            return
        if files:
            f = max(files, key=lambda e: e.stat().st_ctime)
            size = f.stat().st_size
            if size == last_size:
                ext = os.path.splitext(f.name)[1]
                target = os.path.join(folder, new_name + ext)
                n = 2
                while os.path.exists(target):
                    target = os.path.join(folder, f"{new_name} ({n}){ext}")
                    n += 1
                try:
                    os.rename(f.path, target)
                    log(f"Запись сохранена как: {os.path.basename(target)}")
                    return
                except OSError:
                    pass  # OBS ещё держит файл
            last_size = size
        time.sleep(3)
    log("Не удалось переименовать запись (файл не найден или занят). "
        "Проверьте папку записей OBS в настройках.")


# ---------- Chrome ----------

def find_chrome(path=""):
    if path:
        return path
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.exists(p):
                return p
    raise FileNotFoundError("Chrome не найден — укажите путь к chrome.exe в настройках")


class Chrome:
    def __init__(self, chrome_path=""):
        subprocess.Popen([
            find_chrome(chrome_path),
            f"--remote-debugging-port={DEBUG_PORT}",
            "--remote-allow-origins=*",
            f"--user-data-dir={PROFILE_DIR}",
            "--start-fullscreen",                 # режим F11
            "--autoplay-policy=no-user-gesture-required",
            "--no-first-run", "--no-default-browser-check",
            "about:blank",
        ])
        self.ws = None
        self.msg_id = 0
        self.lock = threading.Lock()
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json"))
                page = next(t for t in tabs if t.get("type") == "page")
                self.ws = websocket.create_connection(page["webSocketDebuggerUrl"],
                                                      suppress_origin=True, timeout=30)
                break
            except Exception:
                time.sleep(1)
        if not self.ws:
            raise RuntimeError("Не удалось подключиться к Chrome. Закройте окна Chrome, "
                               "открытые программой, и запустите снова.")
        self.call("Page.enable")
        # Веб-клиенту Zoom заранее разрешаем микрофон и камеру, чтобы не было всплывающего
        # запроса (сами они всё равно выключаются кнопками, если так задано в настройках)
        try:
            self.call("Browser.grantPermissions", origin="https://app.zoom.us",
                      permissions=["audioCapture", "videoCapture"])
        except Exception:
            pass

    def call(self, method, **params):
        with self.lock:
            self.msg_id += 1
            my_id = self.msg_id
            self.ws.send(json.dumps({"id": my_id, "method": method, "params": params}))
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("method") == "Page.javascriptDialogOpening":
                    # «Покинуть сайт?» и подобные окна — сразу подтверждаем
                    self.msg_id += 1
                    self.ws.send(json.dumps({"id": self.msg_id, "method": "Page.handleJavaScriptDialog",
                                             "params": {"accept": True}}))
                if msg.get("id") == my_id:
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    return msg.get("result", {})

    def navigate(self, url):
        self.call("Page.navigate", url=url)

    def evaluate(self, js):
        try:
            r = self.call("Runtime.evaluate", expression=js, returnByValue=True)
            return r.get("result", {}).get("value")
        except Exception:
            return None

    def ensure_fullscreen(self):
        try:
            win = self.call("Browser.getWindowForTarget")
            if win.get("bounds", {}).get("windowState") != "fullscreen":
                self.call("Browser.setWindowBounds", windowId=win["windowId"],
                          bounds={"windowState": "fullscreen"})
        except Exception:
            press_hotkey("f11")

    def block_devices(self, origin, mic, cam):
        for name, on in (("audioCapture", mic), ("videoCapture", cam)):
            if on:
                try:
                    self.call("Browser.setPermission", permission={"name": name},
                              setting="denied", origin=origin)
                except Exception:
                    pass

    def close(self):
        try:
            self.call("Browser.close")
        except Exception:
            pass


# ---------- сайты ----------

def site_of(url):
    host = urlparse(url).netloc.lower()
    if "zoom.us" in host:
        return "zoom"
    if "mirapolis" in host or "virtualroom" in host:
        return "mira"
    return "other"


def zoom_web_url(url):
    if re.fullmatch(r"\s*\d[\d -]*\d(\s+\S+)?\s*", url):     # «номер код»
        parts = url.split()
        conf, pwd = parts[0].replace("-", ""), (parts[1] if len(parts) > 1 else "")
    else:
        u = urlparse(url)
        m = re.search(r"/(?:j|w|wc/join|wc)/(\d+)", u.path)
        if not m:
            raise ValueError(f"Не удалось найти номер конференции в ссылке: {url}")
        conf = m.group(1)
        pwd = parse_qs(u.query).get("pwd", [""])[0]
    return f"https://app.zoom.us/wc/join/{conf}" + (f"?pwd={quote(pwd)}" if pwd else "")


def open_url(url):
    """Адрес, который открывается в Chrome (Zoom — через веб-клиент)."""
    if site_of(url) == "zoom" or re.fullmatch(r"\s*\d[\d -]*\d(\s+\S+)?\s*", url):
        return zoom_web_url(url)
    return url


def kind(url):
    return site_of(open_url(url))


def same_url(a, b):
    """Одна и та же ссылка, даже если отличается http/https, регистром или «/» в конце."""
    if not a or not b:
        return False
    def norm(u):
        u = re.sub(r"^https?://", "", u.strip()).rstrip("/")
        host, _, rest = u.partition("/")
        return host.lower() + "/" + rest      # код доступа в ссылке Zoom зависит от регистра
    return norm(open_url(a)) == norm(open_url(b))


def split_urls(text):
    """Одна запись может хранить цепочку Zoom-конференций — по ссылке на строку."""
    return [u.strip() for u in (text or "").splitlines() if u.strip()]


# Zoom: "in" — в конференции, "joining" — нажали «Войти», "out" — не в конференции.
# При каждой проверке выключает микрофон и камеру, если они включены.
ZOOM_JS = r"""
((autoJoin, name, micOff, camOff) => {
  const docs = [document];
  for (const f of document.querySelectorAll('iframe')) {
    try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {}
  }
  const labels = b => [(b.innerText || '').trim().toLowerCase(),
                       (b.getAttribute('aria-label') || '').trim().toLowerCase()];
  const is = (b, re) => labels(b).some(t => re.test(t));
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const buttons = d => [...d.querySelectorAll('button')].filter(visible);

  const MIC_ON = /^(mute|mute my microphone|mute microphone|выключить звук|отключить звук|выключить микрофон|отключить микрофон)$/;
  const CAM_ON = /^(stop video|stop my video|stop sending my video|turn off camera|остановить видео|выключить видео|выключить камеру|остановить мое видео|остановить моё видео)$/;
  for (const d of docs)
    for (const b of buttons(d)) {
      if (micOff && is(b, MIC_ON)) b.click();
      if (camOff && is(b, CAM_ON)) b.click();
    }

  let inMeeting = false;
  for (const d of docs)
    if (d.querySelector('.footer__leave-btn') ||
        buttons(d).some(b => is(b, /^(leave|leave meeting|end|выйти|выйти из конференции|завершить)$/)))
      inMeeting = true;
  if (inMeeting) {
    for (const d of docs)
      for (const b of buttons(d))
        if (is(b, /join audio by computer|звук компьютера/)) b.click();
    return 'in';
  }
  if (!autoJoin) return 'out';
  for (const d of docs) {
    const inp = d.querySelector('#input-for-name, input[placeholder*="name" i], input[placeholder*="имя" i]');
    if (inp && !inp.value && name) {
      const setter = Object.getOwnPropertyDescriptor(d.defaultView.HTMLInputElement.prototype, 'value').set;
      setter.call(inp, name);
      inp.dispatchEvent(new Event('input', {bubbles: true}));
    }
    const join = buttons(d).find(b => !b.disabled && is(b, /^(join|войти|присоединиться)$/));
    if (join) { join.click(); return 'joining'; }
  }
  return 'out';
})
"""

# Mirapolis Virtual Room: на странице входа заполняет ФИО и жмёт «Войти»,
# затем на заставке «Добро пожаловать в VirtualRoom!» жмёт «Начать работу».
MIRA_JS = r"""
((autoJoin, last, first, middle) => {
  if (document.readyState !== 'complete' || !/virtualroom|mirapolis/.test(location.host)) return 'out';
  const docs = [document];
  for (const f of document.querySelectorAll('iframe')) {
    try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {}
  }
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  for (const d of docs) {
    const start = [...d.querySelectorAll('button, [role="button"], a')].find(b =>
      visible(b) && !b.disabled && /^(начать работу|start working|get started|start)$/i.test((b.innerText || '').trim()));
    if (start) {
      if (autoJoin) start.click();
      return 'joining';
    }
  }
  const form = document.querySelector('input[name="pfirstname"]');
  if (form) {
    if (!autoJoin) return 'out';
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    const set = (n, v) => {
      const i = document.querySelector(`input[name="${n}"]`);
      if (i && v && i.value !== v) {
        setter.call(i, v);
        i.dispatchEvent(new Event('input', {bubbles: true}));
        i.dispatchEvent(new Event('change', {bubbles: true}));
      }
    };
    set('plastname', last); set('pfirstname', first); set('psurname', middle);
    const btn = [...document.querySelectorAll('button')].find(b =>
      !b.disabled && /^(войти|enter|log in|login)$/i.test((b.innerText || '').trim()));
    if (btn) btn.click();
    return 'joining';
  }
  return document.body && document.body.innerText.trim() ? 'in' : 'out';
})
"""


class Stopped(Exception):
    pass


class Runner:
    """Выполняет очередь Zoom или расписание в отдельном потоке."""

    def __init__(self, cfg, log, status):
        self.cfg = cfg
        self.log = log
        self.status = status
        self.stop_event = threading.Event()
        self.chrome = None
        self.current_url = None
        self.recording = False
        self.paused = False
        self.rec_started = 0
        self.rec_name = ""
        self.blocked_origins = set()

    # --- общее ---

    def stop(self):
        self.stop_event.set()

    def sleep(self, sec):
        if self.stop_event.wait(sec):
            raise Stopped

    def press(self, key, label):
        combo = self.cfg["hotkeys"][key]
        press_hotkey(combo)
        self.log(f"OBS: {label} ({combo})")

    def start_recording(self, name):
        self.press("start", "старт записи")
        self.recording, self.paused = True, False
        self.rec_started, self.rec_name = time.time(), name

    def pause_recording(self):
        if self.recording and not self.paused:
            self.press("pause", "пауза")
            self.paused = True

    def unpause_recording(self):
        if self.recording and self.paused:
            self.press("unpause", "снять с паузы")
            self.paused = False

    def stop_recording(self):
        if not self.recording:
            return
        self.press("stop", "остановка записи")
        self.recording = self.paused = False
        if self.cfg["obs_rename"] and self.cfg["obs_folder"]:
            new_name = format_name(self.cfg["obs_template"], self.rec_name,
                                   datetime.fromtimestamp(self.rec_started))
            threading.Thread(target=rename_recording, daemon=False,
                             args=(self.cfg["obs_folder"], self.rec_started, new_name, self.log)).start()

    def link_urls(self, name):
        for l in self.cfg["links"]:
            if l["name"] == name:
                urls = split_urls(l["url"])
                if urls:
                    return urls
        raise ValueError(f"Нет ссылки с названием «{name}»")

    def state(self, url, auto_join):
        site = kind(url)
        c = self.cfg
        if site == "zoom":
            args = (auto_join, c["zoom_name"], c["mic_off"], c["camera_off"])
            return self.chrome.evaluate(f"{ZOOM_JS}({', '.join(json.dumps(a) for a in args)})")
        if site == "mira":
            args = (auto_join, c["last_name"], c["first_name"], c["middle_name"])
            return self.chrome.evaluate(f"{MIRA_JS}({', '.join(json.dumps(a) for a in args)})")
        ready = self.chrome.evaluate("document.readyState === 'complete' && location.href !== 'about:blank'")
        return "in" if ready else "out"

    def open(self, url):
        target = open_url(url)
        self.log(f"Открываю {target}")
        self.chrome.navigate(target)
        self.current_url = url

    def leave(self):
        if self.current_url:
            self.log("Отключаюсь")
            self.chrome.navigate("about:blank")
            self.current_url = None

    def wait_joined(self, url, deadline):
        """Ждёт входа (с автозаполнением). True — вошли."""
        need = 3 if kind(url) != "zoom" else 1   # Mirapolis: убеждаемся, что форма не вернётся
        ok = 0
        while time.time() < deadline:
            self.sleep(CHECK_INTERVAL_SEC)
            if kind(url) == "mira" and (self.cfg["mic_off"] or self.cfg["camera_off"]):
                origin = self.chrome.evaluate("location.origin")
                if origin and origin.startswith("http") and origin not in self.blocked_origins:
                    self.chrome.block_devices(origin, self.cfg["mic_off"], self.cfg["camera_off"])
                    self.blocked_origins.add(origin)
            ok = ok + 1 if self.state(url, auto_join=True) == "in" else 0
            if ok >= need:
                self.chrome.ensure_fullscreen()
                return True
        return False

    def run(self, mode):
        try:
            for combo in self.cfg["hotkeys"].values():
                parse_hotkey(combo)
            self.log("Запускаю Chrome")
            self.chrome = Chrome(self.cfg.get("chrome_path", ""))
            if mode == "queue":
                self.run_queue()
            else:
                self.run_schedule()
        except Stopped:
            self.log("Остановлено")
        except Exception as e:
            self.log(f"Ошибка: {e}")
        finally:
            try:
                self.stop_recording()
            except Exception as e:
                self.log(f"Ошибка при остановке записи: {e}")
            if self.chrome:
                self.chrome.close()
            self.status("Остановлено")
            self.log("Готово")

    # --- Zoom по очереди ---

    def run_queue(self):
        names = self.cfg["queue"]
        if not names:
            self.log("Очередь пуста")
            return
        steps = [(n, u) for n in names for u in self.link_urls(n)]
        c = self.cfg
        for i, (name, url) in enumerate(steps, 1):
            self.log(f"=== {i} из {len(steps)}: {name} ===")
            self.status(f"Подключение: {name} ({i} из {len(steps)})")
            self.open(url)
            if not self.wait_joined(url, time.time() + c["join_timeout_min"] * 60):
                self.log(f"Не удалось подключиться за {c['join_timeout_min']} мин — пропускаю")
                continue
            self.log("Подключено")
            joined_at = time.time()
            self.sleep(3)
            if not self.recording:
                self.start_recording(" + ".join(names))
            else:
                self.unpause_recording()
            self.status(f"Запись: {name} ({i} из {len(steps)})")
            self.wait_zoom_end(url, joined_at, None)
            self.log("Конференция завершилась")
            if i < len(steps):
                self.pause_recording()
        self.stop_recording()

    def wait_zoom_end(self, url, joined_at, until):
        """Ждёт конца Zoom-конференции (или времени until). True — закончилась сама."""
        missing_since = None
        c = self.cfg
        while True:
            self.sleep(CHECK_INTERVAL_SEC)
            now = time.time()
            if until:
                self.status(f"Запись: {self.rec_name}, до конца {fmt_left(until - now)}")
                if now >= until:
                    return False
            if self.state(url, auto_join=False) == "in":
                missing_since = None
                continue
            if missing_since is None:
                missing_since = now
            if now - joined_at < c["grace_minutes"] * 60:
                continue
            if now - missing_since >= c["end_confirm_sec"]:
                return True

    # --- по расписанию ---

    def run_schedule(self):
        c = self.cfg
        if not c["schedule"]:
            self.log("Расписание пустое")
            return
        self.log("Работаю по расписанию")
        while True:
            now = datetime.now()
            upcoming = occurrences(c["schedule"], now, upper_ref(c))
            if not upcoming:
                self.log("В расписании нет занятий на ближайшие две недели")
                return
            s, e, entry = upcoming[0]
            connect_at = s - timedelta(minutes=c["connect_before_min"])
            title = (f"{entry['link']} ({DAYS[s.weekday()]} {entry['start']}–{entry['end']}, "
                     f"{week_type(s.date(), upper_ref(c)).lower()} неделя)")
            if now < connect_at:
                self.log(f"Следующее занятие: {title}")
                while datetime.now() < connect_at:
                    self.status(f"Ожидание: {title}, подключение через "
                                f"{fmt_left((connect_at - datetime.now()).total_seconds())}")
                    self.sleep(1)
            self.run_lesson(s, e, entry, title)

    def run_lesson(self, s, e, entry, title):
        name = entry["link"]
        urls = self.link_urls(name)
        url = urls[0]
        self.log(f"=== {title} ===")
        if same_url(self.current_url, url) and self.state(url, auto_join=False) == "in":
            self.log("Ссылка та же — остаюсь в комнате, без переподключения")
            self.current_url = url
        else:
            self.open(url)
        self.status(f"Подключение: {title}")
        if not self.wait_joined(url, e.timestamp()):
            self.log("Не удалось подключиться до конца занятия")
            self.leave()
            return
        self.log("Подключено")
        while datetime.now() < s:
            self.status(f"Подключено: {title}, запись начнётся через "
                        f"{fmt_left((s - datetime.now()).total_seconds())}")
            self.sleep(1)
        self.start_recording(name)

        if kind(url) == "zoom":
            self.record_zoom_lesson(urls, e, title)
            url = self.current_url
        else:
            while datetime.now() < e:
                self.status(f"Запись: {title}, до конца {fmt_left((e - datetime.now()).total_seconds())}")
                self.sleep(CHECK_INTERVAL_SEC)
                if self.state(url, auto_join=True) == "joining":   # выкинуло — входим снова
                    self.log("Вход заново")
        self.stop_recording()
        self.log(f"Занятие закончилось: {title}")

        # Если следующее занятие по той же ссылке скоро — остаёмся на месте
        self.leave_unless_next_same(url)

    def record_zoom_lesson(self, urls, e, title):
        """Zoom по расписанию. Конференция закончилась — запись на паузу и вход в следующую
        по цепочке (после последней — снова в последнюю, если пара ещё идёт).
        После конца пары ждём окончания конференции не дольше zoom_overtime_min."""
        c = self.cfg
        end_ts = e.timestamp()
        overtime_ts = end_ts + c["zoom_overtime_min"] * 60
        idx = 0
        url = urls[0]
        joined_at = time.time()
        missing_since = None
        last_open = 0
        while True:
            self.sleep(CHECK_INTERVAL_SEC)
            now = time.time()
            if self.state(url, auto_join=self.paused) == "in":
                if self.paused:
                    self.log(f"Подключено к конференции {idx + 1} из {len(urls)}")
                    self.chrome.ensure_fullscreen()
                    joined_at = now
                    self.unpause_recording()
                missing_since = None
                if now < end_ts:
                    self.status(f"Запись: {title}, до конца {fmt_left(end_ts - now)}")
                elif now < overtime_ts:
                    self.status(f"Пара затянулась — запись идёт, жду конца конференции "
                                f"(ещё не больше {fmt_left(overtime_ts - now)})")
                else:
                    self.log("Время ожидания затянувшейся пары вышло")
                    return
                continue
            if now >= end_ts:
                self.log("Конференция закончилась")
                return
            if not self.paused:
                if missing_since is None:
                    missing_since = now
                if now - joined_at < c["grace_minutes"] * 60 or now - missing_since < c["end_confirm_sec"]:
                    continue
                self.pause_recording()
                if idx + 1 < len(urls):
                    idx += 1
                    url = urls[idx]
                    self.log(f"Конференция закончилась — запись на паузе, "
                             f"подключаюсь к следующей ({idx + 1} из {len(urls)})")
                else:
                    self.log("Конференция закончилась — запись на паузе, пробую войти снова "
                             "(до конца пары)")
                last_open = 0
            self.status(f"Переподключение: {title}, конференция {idx + 1} из {len(urls)}, "
                        f"до конца пары {fmt_left(end_ts - now)}")
            if now - last_open >= 60:   # раз в минуту открываем ссылку заново
                self.open(url)
                last_open = now

    def leave_unless_next_same(self, url):
        """Следующее занятие сегодня и по той же ссылке (даже под другим названием) —
        остаёмся в комнате; иначе отключаемся."""
        nxt = occurrences(self.cfg["schedule"], datetime.now() + timedelta(seconds=1), upper_ref(self.cfg))
        if nxt and same_url(self.link_urls(nxt[0][2]["link"])[0], url) \
                and nxt[0][0].date() == datetime.now().date():
            self.log(f"Следующее занятие «{nxt[0][2]['link']}» по той же ссылке — остаюсь в комнате")
        else:
            self.leave()
