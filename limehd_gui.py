#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LimeHD Эфир — настольное GUI для просмотра каналов limehd.tv.

Похоже на rutube-efir:
  * список каналов с группами и поиском;
  * встроенный mpv (python-mpv/libmpv);
  * информация о канале и ближайшая программа;
  * настройки: RU API-прокси (включая выбор из списка hideip.me),
    отдельный прокси для потока mpv, максимальное качество;
  * кэш списка каналов для быстрого/офлайн-старта.

Запуск:
    python3 limehd_gui.py

Зависимости:
    pip install PyQt6 requests python-mpv
    sudo apt install mpv libmpv2        # Debian/Ubuntu

API-логика получения m3u8 — в limehd_core.py (без CLI).
"""

from __future__ import annotations

import ctypes
import io
import json
import locale
import os
import queue
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional

# libmpv требует C-локаль для чисел; иначе печатает предупреждение.
try:
    locale.setlocale(locale.LC_NUMERIC, "C")
except locale.Error:
    pass

# Чтобы GUI находил limehd_core.py/proxy_bridge.py рядом с собой.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Не найден requests. Установите: python3 -m pip install requests")

requests.packages.urllib3.disable_warnings()

try:
    from PyQt6.QtCore import (
        QObject, QSettings, QStandardPaths, Qt, QThread, QTimer, QUrl,
        pyqtSignal,
    )
    from PyQt6.QtGui import (
        QBrush, QColor, QKeySequence, QOpenGLContext, QPixmap, QShortcut,
    )
    from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    from PyQt6.QtWidgets import (
        QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
        QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
        QMainWindow, QMessageBox, QPushButton, QSizePolicy, QSplitter,
        QStyle, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
    )
except ImportError:  # pragma: no cover
    sys.exit("Не найден PyQt6. Установите: python3 -m pip install PyQt6")

import limehd_core as core
from proxy_bridge import LocalProxyBridge

# --------------------------------------------------------------------------
# Константы и настройки по умолчанию
# --------------------------------------------------------------------------

APP_TITLE = "LimeHD Эфир"
ORG_NAME = "limehd"
APP_NAME = "limehd-gui"

DEFAULT_API_PROXY = "https://92.242.41.77:443"
DEFAULT_CHANNEL = "rusmult"
RU_PROXY_LIST_URL = (
    "https://raw.githubusercontent.com/zloi-user/hideip.me/HEAD/https.txt"
)
RU_PROXY_SCHEME = "https"

QUALITY_OPTIONS = [
    ("Авто (адаптивно)", "auto"),
    ("1080p (Full HD)", "1080"),
    ("720p (HD)", "720"),
    ("480p", "480"),
    ("360p", "360"),
    ("240p", "240"),
]

HW_DEC_OPTIONS = [
    ("auto-safe (рекомендуется)", "auto-safe"),
    ("auto", "auto"),
    ("no (только CPU)", "no"),
    ("vaapi", "vaapi"),
    ("vdpau", "vdpau"),
    ("nvdec", "nvdec"),
    ("videotoolbox", "videotoolbox"),
]

NO_GROUP_LABEL = "Другое"


def _mpv_dbg(*args):
    if os.environ.get("LIMEHD_MPV_LOG"):
        print("[mpv]", *args, file=sys.stderr)


# --------------------------------------------------------------------------
# Прокси / файлы / время
# --------------------------------------------------------------------------

def normalize_proxy(text: str, default_scheme: str = "http") -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = f"{default_scheme}://{text}"
    return text


def build_proxies(enabled: bool, text: str) -> Optional[Dict[str, str]]:
    if not enabled:
        return None
    url = normalize_proxy(text)
    if not url:
        return None
    return {"http": url, "https": url}


def parse_ru_proxies(text: str) -> List[str]:
    urls: List[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.endswith(":Russia"):
            continue
        host_port = line[: -len(":Russia")].strip()
        if not host_port:
            continue
        url = host_port if "://" in host_port else f"{RU_PROXY_SCHEME}://{host_port}"
        if url not in urls:
            urls.append(url)
    return urls


def fetch_ru_proxy_list(proxies=None):
    """(текст_списка, ошибка). Пробуем текущим прокси, затем напрямую."""
    err = "неизвестная ошибка"
    for attempt in (proxies, None):
        try:
            r = requests.get(
                RU_PROXY_LIST_URL,
                headers={"User-Agent": core.USER_AGENT, "Accept": "*/*"},
                proxies=attempt if attempt is not None else {},
                timeout=20,
                verify=False,
            )
            if r.status_code == 200:
                return r.text, ""
            err = f"HTTP {r.status_code}"
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:120]}"
    return "", err


def cache_file() -> str:
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.CacheLocation
    )
    if not base:
        base = os.path.expanduser("~/.cache/limehd-gui")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "channels.json")


def fmt_time(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return "--:--"


def fmt_ts(ts) -> str:
    if not ts:
        return "неизвестно"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "неизвестно"


def event_time_range(ev: dict) -> str:
    return f"{fmt_time(ev.get('timestart'))}–{fmt_time(ev.get('timestop'))}"


def event_title(ev: dict) -> str:
    return str(ev.get("title") or ev.get("name") or "Без названия")


# --------------------------------------------------------------------------
# Фоновые потоки
# --------------------------------------------------------------------------

class ChannelsWorker(QThread):
    """Загрузка категорий и списка каналов (с запасным прямым запросом)."""

    done = pyqtSignal(object)

    def __init__(self, proxies, device_id: str, region: str, tz: str,
                 timeout: float = 30.0, parent=None):
        super().__init__(parent)
        self.proxies = proxies
        self.device_id = device_id
        self.region = region
        self.tz = tz
        self.timeout = timeout

    def _load(self, proxies):
        cats = core.fetch_categories(self.timeout, proxies, self.device_id)
        channels, total = core.fetch_channels(
            self.region, self.tz, self.timeout, proxies, self.device_id
        )
        cat_dicts = [
            {"id": c.id, "identifier": c.identifier, "name": c.name}
            for c in cats
        ]
        return cat_dicts, channels, total

    def run(self):
        warning = ""
        used_proxy = self.proxies is not None
        try:
            cats, channels, total = self._load(self.proxies)
        except Exception as e:
            traceback.print_exc()
            if self.proxies is not None:
                try:
                    cats, channels, total = self._load(None)
                    warning = (
                        f"Прокси не сработал ({e}); список загружен напрямую. "
                        "Он может быть короче."
                    )
                    used_proxy = False
                except Exception as e2:
                    self.done.emit({
                        "categories": [], "channels": [], "total": 0,
                        "error": f"{e}; напрямую: {e2}", "warning": "",
                        "used_proxy": False,
                    })
                    return
            else:
                self.done.emit({
                    "categories": [], "channels": [], "total": 0,
                    "error": str(e), "warning": "", "used_proxy": False,
                })
                return

        self.done.emit({
            "categories": cats,
            "channels": channels,
            "total": total,
            "error": "",
            "warning": warning,
            "used_proxy": used_proxy,
        })


class StreamWorker(QThread):
    """Получение m3u8 для канала и выбор качества."""

    done = pyqtSignal(int, object)

    def __init__(self, token: int, slug: str, quality: str, proxies,
                 device_id: str, region: str, tz: str,
                 timeout: float = 30.0, parent=None):
        super().__init__(parent)
        self.token = token
        self.slug = slug
        self.quality = quality
        self.proxies = proxies
        self.device_id = device_id
        self.region = region
        self.tz = tz
        self.timeout = timeout

    def _load(self, proxies):
        info = core.fetch_stream(
            self.slug, self.region, self.tz, self.device_id,
            self.timeout, proxies, resolve=True,
        )
        q = None if self.quality == "auto" else self.quality
        selected = core.choose_variant(info.variants, q)
        url = selected.url if selected else info.master_url
        return {
            "url": url,
            "master_url": info.master_url,
            "channel_id": info.channel_id,
            "selected": selected.label if selected else "Авто",
            "expires": core.exp_from_url(url) or core.exp_from_url(info.master_url),
            "variants": [
                {
                    "label": v.label,
                    "height": v.height,
                    "bandwidth": v.average_bandwidth or v.bandwidth,
                    "codecs": v.codecs,
                    "url": v.url,
                }
                for v in info.variants
            ],
            "error": "",
            "warning": "",
        }

    def run(self):
        warning = ""
        try:
            data = self._load(self.proxies)
        except Exception as e:
            traceback.print_exc()
            if self.proxies is not None:
                try:
                    data = self._load(None)
                    warning = "API-прокси не сработал; поток получен напрямую."
                except Exception as e2:
                    self.done.emit(self.token, {"error": f"{e}; напрямую: {e2}"})
                    return
            else:
                self.done.emit(self.token, {"error": str(e)})
                return

        data["warning"] = warning
        data["used_proxy"] = self.proxies is not None
        self.done.emit(self.token, data)


class ProxyListWorker(QThread):
    done = pyqtSignal(list, str)

    def __init__(self, proxies=None, parent=None):
        super().__init__(parent)
        self.proxies = proxies

    def run(self):
        urls, err = [], "сбой загрузки списка"
        try:
            text, err = fetch_ru_proxy_list(self.proxies)
            if not err:
                urls = parse_ru_proxies(text)
                if not urls:
                    err = "в списке нет адресов Russia"
                else:
                    err = ""
        except Exception:
            traceback.print_exc()
            urls, err = [], "сбой загрузки списка"
        self.done.emit(urls, err)


_PROXY_LIST_WORKERS = set()


def _forget_proxy_worker(worker):
    _PROXY_LIST_WORKERS.discard(worker)
    worker.deleteLater()


class ProxyCheckWorker(QThread):
    done = pyqtSignal(bool, str)

    def __init__(self, proxies, device_id: str, parent=None):
        super().__init__(parent)
        self.proxies = proxies
        self.device_id = device_id

    def run(self):
        try:
            cats = core.fetch_categories(20, self.proxies, self.device_id)
            self.done.emit(True, f"API доступен. Категорий: {len(cats)}")
        except Exception as e:
            self.done.emit(False, str(e))


class EpgWorker(QThread):
    """Полная программа передач канала на сегодня."""

    done = pyqtSignal(int, str, object)

    def __init__(self, token: int, slug: str, proxies, device_id: str,
                 region: str, tz: str, timeout: float = 20.0, parent=None):
        super().__init__(parent)
        self.token = token
        self.slug = slug
        self.proxies = proxies
        self.device_id = device_id
        self.region = region
        self.tz = tz
        self.timeout = timeout

    def _load(self, proxies):
        url = f"{core.API_BASE}/epg/{self.slug}"
        params = {
            "region": self.region,
            "tz": self.tz,
            "epg_from": "0",
            "epgcnt": "15",
        }
        data = core.http_json(
            url, core.request_headers(self.device_id),
            self.timeout, proxies, params=params,
        )
        if not isinstance(data, dict):
            return []
        blocks = data.get("epg") or []
        if not blocks:
            return []
        today = datetime.now().strftime("%d.%m.%Y")
        block = None
        for b in blocks:
            if isinstance(b, dict) and b.get("date") == today:
                block = b
                break
        if block is None:
            block = next(
                (b for b in blocks if isinstance(b, dict) and b.get("data")),
                blocks[0],
            )
        return list((block or {}).get("data") or [])

    def run(self):
        try:
            events = self._load(self.proxies)
        except Exception:
            if self.proxies is not None:
                try:
                    events = self._load(None)
                except Exception as e:
                    self.done.emit(self.token, self.slug, {"error": str(e)})
                    return
            else:
                self.done.emit(self.token, self.slug,
                               {"error": "не удалось загрузить программу"})
                return
        self.done.emit(self.token, self.slug, events)


# --------------------------------------------------------------------------
# mpv: render API в QOpenGLWidget + очередь команд
# --------------------------------------------------------------------------

class MpvVideo(QOpenGLWidget):
    """Видео через libmpv render API (без wid)."""

    update_wanted = pyqtSignal()

    _KEYS = {
        Qt.Key.Key_Space: "SPACE", Qt.Key.Key_Escape: "ESC",
        Qt.Key.Key_Up: "UP", Qt.Key.Key_Down: "DOWN",
        Qt.Key.Key_Left: "LEFT", Qt.Key.Key_Right: "RIGHT",
        Qt.Key.Key_Enter: "ENTER", Qt.Key.Key_Return: "ENTER",
        Qt.Key.Key_Home: "HOME", Qt.Key.Key_End: "END",
        Qt.Key.Key_PageUp: "PGUP", Qt.Key.Key_PageDown: "PGDN",
        Qt.Key.Key_Delete: "DEL", Qt.Key.Key_Backspace: "BS",
        Qt.Key.Key_Tab: "TAB", Qt.Key.Key_Insert: "INS",
        Qt.Key.Key_F1: "F1", Qt.Key.Key_F2: "F2", Qt.Key.Key_F3: "F3",
        Qt.Key.Key_F4: "F4", Qt.Key.Key_F5: "F5", Qt.Key.Key_F6: "F6",
        Qt.Key.Key_F7: "F7", Qt.Key.Key_F8: "F8", Qt.Key.Key_F9: "F9",
        Qt.Key.Key_F10: "F10", Qt.Key.Key_F11: "F11", Qt.Key.Key_F12: "F12",
    }
    _MBTN = {
        Qt.MouseButton.LeftButton: "MBTN_LEFT",
        Qt.MouseButton.MiddleButton: "MBTN_MID",
        Qt.MouseButton.RightButton: "MBTN_RIGHT",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mpv = None
        self._mpv_mod = None
        self._enq = None
        self._rctx = None
        self._gl = False
        self._gp = None
        self._mouse_t = 0.0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.update_wanted.connect(
            self.update, Qt.ConnectionType.QueuedConnection
        )

    # ---------- render ----------
    def bind_mpv(self, mpv_inst, mpv_mod, enqueue=None):
        self._mpv = mpv_inst
        self._mpv_mod = mpv_mod
        self._enq = enqueue
        if self._gl:
            try:
                self.makeCurrent()
                self._make_ctx()
                self.doneCurrent()
            except Exception as e:
                _mpv_dbg("bind_mpv:", e)

    def unbind_mpv(self):
        r, self._rctx = self._rctx, None
        if r is None:
            return
        try:
            self.makeCurrent()
            r.free()
            self.doneCurrent()
        except Exception as e:
            _mpv_dbg("render free:", e)

    def initializeGL(self):
        self._gl = True
        self._make_ctx()

    def _make_ctx(self):
        if self._rctx or not self._gl or self._mpv is None:
            return
        mod = self._mpv_mod
        try:
            gp = mod.MpvGlGetProcAddressFn(self._get_proc)
            ctx = mod.MpvRenderContext(
                self._mpv, "opengl",
                opengl_init_params={"get_proc_address": gp},
                advanced_control=True,
            )
            ctx.update_cb = self._on_update
            self._gp = gp
            self._rctx = ctx
            _mpv_dbg("render context created")
        except Exception as e:
            _mpv_dbg("render context:", e)

    def _get_proc(self, _ud, name):
        try:
            ctx = QOpenGLContext.currentContext()
            if ctx is None:
                return None
            addr = ctx.getProcAddress(name)
            if not addr:
                return None
            v = int(addr)
            return v or None
        except Exception:
            return None

    def _on_update(self):
        try:
            self.update_wanted.emit()
        except Exception:
            pass

    def _clear_black(self):
        try:
            ctx = QOpenGLContext.currentContext()
            if ctx is None:
                return

            def _fn(sig, name):
                addr = ctx.getProcAddress(name)
                v = int(addr) if addr else 0
                return sig(v) if v else None

            pc = _fn(
                ctypes.CFUNCTYPE(
                    None, ctypes.c_float, ctypes.c_float,
                    ctypes.c_float, ctypes.c_float,
                ),
                "glClearColor",
            )
            cl = _fn(ctypes.CFUNCTYPE(None, ctypes.c_uint), "glClear")
            if pc and cl:
                pc(0.0, 0.0, 0.0, 1.0)
                cl(0x4000 | 0x0100 | 0x0400)
        except Exception:
            pass

    def paintGL(self):
        r = self._rctx
        if r is None:
            self._clear_black()
            return
        dpr = self.devicePixelRatioF()
        w = int(self.width() * dpr)
        h = int(self.height() * dpr)
        if w <= 0 or h <= 0:
            return
        try:
            r.update()
            r.render(
                opengl_fbo={
                    "w": w,
                    "h": h,
                    "fbo": int(self.defaultFramebufferObject()),
                    "internal_format": 0x8058,
                },
                flip_y=True,
            )
            r.report_swap()
        except Exception as e:
            _mpv_dbg("mpv render:", e)

    # ---------- input ----------
    def _cmd(self, *args):
        m = self._mpv
        if m is None:
            return
        job = lambda: m.command(*args)
        if self._enq is not None:
            self._enq(job)
            return
        try:
            job()
        except Exception as e:
            _mpv_dbg("mpv input:", e)

    def _xy(self, e):
        dpr = self.devicePixelRatioF()
        return (
            str(int(e.position().x() * dpr)),
            str(int(e.position().y() * dpr)),
        )

    def keyPressEvent(self, event):
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            super().keyPressEvent(event)
            return
        name = self._KEYS.get(event.key())
        if name is None:
            text = event.text()
            if text and text.isprintable():
                name = text
        if name:
            self._cmd("keypress", name)
            event.accept()
        else:
            super().keyPressEvent(event)

    def mouseMoveEvent(self, event):
        now = time.monotonic()
        if now - self._mouse_t < 0.04:
            return
        self._mouse_t = now
        x, y = self._xy(event)
        self._cmd("mouse", x, y)
        event.accept()

    def mousePressEvent(self, event):
        name = self._MBTN.get(event.button())
        if name is None:
            super().mousePressEvent(event)
            return
        x, y = self._xy(event)
        self._cmd("mouse", x, y)
        self._cmd("keydown", name)
        event.accept()

    def mouseReleaseEvent(self, event):
        name = self._MBTN.get(event.button())
        if name is None:
            super().mouseReleaseEvent(event)
            return
        x, y = self._xy(event)
        self._cmd("mouse", x, y)
        self._cmd("keyup", name)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            x, y = self._xy(event)
            self._cmd("mouse", x, y, "0", "double")
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta:
            self._cmd(
                "keypress", "WHEEL_UP" if delta > 0 else "WHEEL_DOWN"
            )
            event.accept()
        else:
            super().wheelEvent(event)


class Player(QObject):
    """Обёртка над mpv: очередь команд, отложенный play, fallback опций."""

    failed = pyqtSignal(str)
    key = pyqtSignal(str)
    fs_changed = pyqtSignal(bool)

    MPV_KEYS = {
        "SPACE": "pause",
        "f": "fullscreen",
        "F11": "fullscreen",
        "ESC": "escape",
        "UP": "ch_up",
        "DOWN": "ch_down",
        "LEFT": "vol_down",
        "RIGHT": "vol_up",
        "WHEEL_UP": "vol_up",
        "WHEEL_DOWN": "vol_down",
        "m": "mute",
        "M": "mute",
        "+": "vol_up",
        "-": "vol_down",
        "MBTN_LEFT_DBL": "fullscreen",
    }

    def __init__(self, mpv_module, widget: MpvVideo, opts: dict,
                 parent=None):
        super().__init__(parent)
        self._mpv_module = mpv_module
        self._widget = widget
        self._opts = dict(opts)
        self.mpv = None
        self.error = ""
        self._play_seq = 0
        self._mpvq = queue.Queue()
        self._mpvq_thread = threading.Thread(
            target=self._mpvq_loop, name="mpv-cmd", daemon=True
        )
        self._mpvq_thread.start()
        self._create_mpv()

    # ---------- очередь ----------
    def _mpvq_loop(self):
        while True:
            job = self._mpvq.get()
            if job is None:
                return
            try:
                job()
            except Exception as e:
                _mpv_dbg("mpv job:", e)

    def enqueue(self, job):
        self._mpvq.put(job)

    # ---------- создание ----------
    def _create_mpv(self):
        opts = dict(self._opts)
        try:
            self.mpv = self._mpv_module.MPV(**opts)
            self.error = ""
        except Exception as e:
            _mpv_dbg("mpv create:", e)
            self.mpv = self._retry_mpv_opts(opts, e)
        if self.mpv is None:
            return

        @self.mpv.event_callback("end-file")
        def _on_end_file(event):
            data = getattr(event, "data", None)
            reason = data.get("reason") if isinstance(data, dict) else data
            if reason == "error":
                self.failed.emit("ошибка воспроизведения потока")

        self._bind_keys()
        self._observe_fullscreen()
        try:
            self._widget.bind_mpv(
                self.mpv, self._mpv_module, self.enqueue
            )
        except Exception as e:
            _mpv_dbg("bind_mpv:", e)

    def _retry_mpv_opts(self, opts: dict, first_error: Exception):
        last = first_error
        for key in ("gpu_context", "vo", "hwdec"):
            if key not in opts:
                continue
            trial = dict(opts)
            trial.pop(key)
            try:
                mpv_obj = self._mpv_module.MPV(**trial)
                _mpv_dbg("mpv: option dropped:", key)
                self._opts = dict(trial)
                self.error = f"{key} не поддерживается, опция отброшена"
                return mpv_obj
            except Exception as e:
                last = e
        self.error = str(last)
        return None

    def _bind_keys(self):
        def make(action):
            def handler(*args):
                state = args[0] if args else None
                s = str(state) if state is not None else ""
                if not s or s[0] in ("d", "p", "D", "P"):
                    self.key.emit(action)
            return handler

        for key_name, action in self.MPV_KEYS.items():
            try:
                self.mpv.register_key_binding(key_name, make(action))
            except Exception as e:
                _mpv_dbg("register_key_binding", key_name, e)

    def _observe_fullscreen(self):
        def on_fullscreen(_name, value):
            if value is None:
                return
            self.fs_changed.emit(bool(value))

        try:
            self.mpv.observe_property("fullscreen", on_fullscreen)
        except Exception as e:
            _mpv_dbg("observe fullscreen:", e)

    # ---------- воспроизведение ----------
    def play(self, url: str, headers=None, proxy=None, title=None):
        if self.mpv is None:
            self.failed.emit(self.error or "mpv недоступен")
            return
        self._play_seq += 1
        self._defer_play(
            url, self._play_seq, 0, headers, proxy, title
        )

    def _defer_play(self, url, seq, tries, headers, proxy, title):
        if seq != self._play_seq:
            return
        if getattr(self._widget, "_rctx", None) is None:
            if tries >= 200:
                self.failed.emit("render context mpv не создан")
                return
            QTimer.singleShot(
                50,
                lambda: self._defer_play(
                    url, seq, tries + 1, headers, proxy, title
                ),
            )
            return

        def job():
            if seq != self._play_seq:
                return
            try:
                if headers is not None:
                    self.mpv["http-header-fields"] = list(headers)
                try:
                    self.mpv["http-proxy"] = proxy or ""
                except Exception as e:
                    _mpv_dbg("http-proxy:", e)
                if title:
                    try:
                        self.mpv["title"] = title
                    except Exception:
                        pass
                self.mpv.play(url)
            except Exception as e:
                self.failed.emit(str(e))

        self.enqueue(job)

    def command(self, *args):
        if self.mpv is None:
            return
        self.enqueue(lambda: self.mpv.command(*args))

    def toggle_pause(self):
        self.command("cycle", "pause")

    def volume(self, delta):
        def _vol():
            try:
                cur = int(self.mpv.volume or 50)
                self.mpv.volume = max(0, min(150, cur + delta))
            except Exception as e:
                _mpv_dbg("volume:", e)

        self.enqueue(_vol)

    def set_hwdec(self, value: str):
        def _hw():
            try:
                self.mpv.hwdec = value
            except Exception as e:
                _mpv_dbg("hwdec:", e)

        self.enqueue(_hw)

    def set_fullscreen(self, value: bool):
        self.enqueue(lambda: self._sync_fullscreen(bool(value)))

    def _sync_fullscreen(self, value: bool):
        try:
            if bool(self.mpv.fullscreen) != value:
                self.mpv.fullscreen = value
        except Exception as e:
            _mpv_dbg("fullscreen sync:", e)

    def recreate(self, opts: dict):
        self._play_seq += 1
        try:
            self._widget.unbind_mpv()
        except Exception as e:
            _mpv_dbg("unbind:", e)
        try:
            self.mpv.terminate()
        except Exception as e:
            _mpv_dbg("mpv terminate:", e)
        self.mpv = None
        self._opts = dict(opts)
        self._create_mpv()

    def terminate(self):
        self._play_seq += 1
        try:
            self._widget.unbind_mpv()
        except Exception:
            pass
        try:
            if self.mpv is not None:
                self.enqueue(lambda: self.mpv.terminate())
            self.enqueue(None)
            self._mpvq_thread.join(timeout=1.0)
        except Exception:
            pass
        self.mpv = None


# --------------------------------------------------------------------------
# Диалог выбора прокси из списка hideip.me
# --------------------------------------------------------------------------

class ProxyPickDialog(QDialog):
    def __init__(self, proxies=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RU прокси — список")
        self.setModal(True)
        self.resize(460, 440)
        self._proxies = proxies
        self._worker = None
        self._urls: List[str] = []

        root = QVBoxLayout(self)
        self.status = QLabel("Загрузка списка…")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#777;")
        root.addWidget(self.status)

        self.list = QTreeWidget()
        self.list.setColumnCount(1)
        self.list.setHeaderLabels(["Прокси (Россия)"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setAlternatingRowColors(True)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.header().setStretchLastSection(True)
        self.list.itemDoubleClicked.connect(lambda *_: self.accept())
        root.addWidget(self.list, 1)

        bottom = QHBoxLayout()
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.reload)
        bottom.addWidget(self.refresh_btn)
        bottom.addStretch(1)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bottom.addWidget(bb)
        root.addLayout(bottom)

        self.reload()

    def reload(self):
        if self._worker is not None and self._worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self.status.setText("Загрузка списка…")
        # Без parent: если диалог закрыть во время загрузки, поток не
        # будет уничтожен вместе с ним.
        worker = ProxyListWorker(self._proxies)
        _PROXY_LIST_WORKERS.add(worker)
        worker.done.connect(self._loaded)
        worker.finished.connect(lambda w=worker: _forget_proxy_worker(w))
        self._worker = worker
        worker.start()

    def _detach_worker(self):
        if self._worker is not None:
            try:
                self._worker.done.disconnect(self._loaded)
            except Exception:
                pass
            self._worker = None

    def accept(self):
        self._detach_worker()
        super().accept()

    def reject(self):
        self._detach_worker()
        super().reject()

    def _loaded(self, urls: List[str], err: str):
        self.refresh_btn.setEnabled(True)
        self._worker = None
        if err and not urls:
            self.status.setText(f"Ошибка: {err}")
            return
        self._urls = urls
        self.list.clear()
        for url in urls:
            self.list.addTopLevelItem(QTreeWidgetItem([url]))
        self.status.setText(
            f"Адресов: {len(urls)}" + (f" · {err}" if err else "")
        )
        if urls:
            self.list.setCurrentItem(self.list.topLevelItem(0))

    def proxy_url(self) -> str:
        item = self.list.currentItem()
        return item.text(0) if item is not None else ""


# --------------------------------------------------------------------------
# Диалог настроек
# --------------------------------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.win = parent
        self._check_worker = None
        self.setWindowTitle("Настройки")
        self.setModal(True)
        self.resize(700, 330)

        root = QVBoxLayout(self)
        grid = QGridLayout()

        self.api_check = QCheckBox("RU API proxy:")
        self.api_edit = QLineEdit()
        self.api_edit.setPlaceholderText(DEFAULT_API_PROXY)
        self.api_edit.setToolTip(
            "Прокси для pl.iptv2021.com (список каналов и m3u8). "
            "Российский адрес даёт полный список каналов."
        )
        self.api_pick = QPushButton("Из списка…")
        self.api_pick.setToolTip("Выбрать российский прокси из списка hideip.me")
        self.api_pick.clicked.connect(self._pick_api_proxy)

        self.stream_check = QCheckBox("Stream proxy (mpv):")
        self.stream_edit = QLineEdit()
        self.stream_edit.setPlaceholderText("Пусто — как API-прокси")
        self.stream_edit.setToolTip(
            "Прокси только для воспроизведения в mpv. "
            "Если пусто и галочка стоит — используется API-прокси."
        )
        self.stream_copy = QPushButton("Как API")
        self.stream_copy.setToolTip("Скопировать адрес из API-прокси")
        self.stream_copy.clicked.connect(self._copy_api_to_stream)

        self.quality_label = QLabel("Макс. качество:")
        self.quality_combo = QComboBox()
        for text, value in QUALITY_OPTIONS:
            self.quality_combo.addItem(text, value)
        self.quality_combo.setToolTip(
            "При следующем запуске канала выбирается вариант не выше этого. "
            "«Авто» — master-playlist, плеер выберет сам."
        )

        self.hwdec_label = QLabel("hwdec:")
        self.hwdec_combo = QComboBox()
        for text, value in HW_DEC_OPTIONS:
            self.hwdec_combo.addItem(text, value)
        self.hwdec_combo.setToolTip(
            "Аппаратное декодирование mpv. Применяется сразу; "
            "если видео чёрное/тормозит — попробуйте no."
        )

        grid.addWidget(self.api_check, 0, 0)
        grid.addWidget(self.api_edit, 0, 1)
        grid.addWidget(self.api_pick, 0, 2)
        grid.addWidget(self.stream_check, 1, 0)
        grid.addWidget(self.stream_edit, 1, 1)
        grid.addWidget(self.stream_copy, 1, 2)
        grid.addWidget(self.quality_label, 2, 0)
        grid.addWidget(self.quality_combo, 2, 1)
        grid.addWidget(self.hwdec_label, 3, 0)
        grid.addWidget(self.hwdec_combo, 3, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        actions = QHBoxLayout()
        self.check_btn = QPushButton("Проверить API")
        self.check_btn.setToolTip("Сделать тестовый запрос к pl.iptv2021.com")
        self.check_btn.clicked.connect(self._check_api)
        self.cache_btn = QPushButton("Очистить кэш")
        self.cache_btn.setToolTip("Удалить сохранённый список каналов")
        self.cache_btn.clicked.connect(self._clear_cache)
        actions.addWidget(self.check_btn)
        actions.addWidget(self.cache_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        hint = QLabel(
            "Пустое поле — прокси не используется. Схема (http:// или https://) "
            "добавляется автоматически. «Из списка…» подставляет российский "
            "адрес из hideip.me. Для API по умолчанию используется "
            f"{DEFAULT_API_PROXY}. Прокси потока применяется только к mpv; "
            "если поле пустое — берётся API-прокси. Многие каналы LimeHD "
            "geo-blocked, поэтому stream proxy по умолчанию включён. "
            "hwdec применяется сразу; если видео чёрное/тормозит — "
            "попробуйте no."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#777;")
        root.addWidget(hint)
        root.addStretch(1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self.api_check.toggled.connect(self._sync_enabled)
        self.stream_check.toggled.connect(self._sync_enabled)
        self._load()

    # ---------- значения ----------
    def _load(self):
        s = self.settings
        self.api_check.setChecked(
            s.value("api_proxy_enabled", True, type=bool)
        )
        self.api_edit.setText(
            s.value("api_proxy", DEFAULT_API_PROXY, type=str)
        )
        self.stream_check.setChecked(
            s.value("stream_proxy_enabled", True, type=bool)
        )
        self.stream_edit.setText(
            s.value("stream_proxy", "", type=str)
        )
        idx = self.quality_combo.findData(
            s.value("quality", "auto", type=str)
        )
        self.quality_combo.setCurrentIndex(idx if idx >= 0 else 0)
        hw_idx = self.hwdec_combo.findData(
            s.value("hwdec", "auto-safe", type=str)
        )
        self.hwdec_combo.setCurrentIndex(hw_idx if hw_idx >= 0 else 0)
        self._sync_enabled()

    def _save(self):
        s = self.settings
        s.setValue("api_proxy_enabled", self.api_check.isChecked())
        s.setValue("api_proxy", self.api_edit.text())
        s.setValue("stream_proxy_enabled", self.stream_check.isChecked())
        s.setValue("stream_proxy", self.stream_edit.text())
        s.setValue("quality", self.quality_combo.currentData())
        s.setValue("hwdec", self.hwdec_combo.currentData())
        s.sync()

    def _sync_enabled(self):
        self.api_edit.setEnabled(self.api_check.isChecked())
        self.api_pick.setEnabled(self.api_check.isChecked())
        self.stream_edit.setEnabled(self.stream_check.isChecked())
        self.stream_copy.setEnabled(self.stream_check.isChecked())

    def exec(self):
        self._load()
        return super().exec()

    def accept(self):
        self._save()
        super().accept()

    def reject(self):
        self._load()
        super().reject()

    # ---------- прокси ----------
    def api_url(self) -> Optional[str]:
        if not self.api_check.isChecked():
            return None
        return normalize_proxy(self.api_edit.text())

    def api_proxies(self) -> Optional[Dict[str, str]]:
        url = self.api_url()
        if not url:
            return None
        return {"http": url, "https": url}

    def stream_url(self) -> Optional[str]:
        if not self.stream_check.isChecked():
            return None
        text = self.stream_edit.text().strip()
        if not text:
            return self.api_url()
        return normalize_proxy(text)

    def quality(self) -> str:
        return str(self.quality_combo.currentData() or "auto")

    def hwdec(self) -> str:
        return str(self.hwdec_combo.currentData() or "auto-safe")

    def _pick_api_proxy(self):
        dlg = ProxyPickDialog(self.api_proxies(), self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.proxy_url():
            self.api_edit.setText(dlg.proxy_url())
            self.api_check.setChecked(True)
            self._sync_enabled()

    def _copy_api_to_stream(self):
        self.stream_edit.setText(self.api_edit.text())
        self.stream_check.setChecked(True)
        self._sync_enabled()

    def _check_api(self):
        if self._check_worker is not None and self._check_worker.isRunning():
            return
        self.check_btn.setEnabled(False)
        self.check_btn.setText("Проверка…")
        proxies = self.api_proxies()
        device_id = getattr(self.win, "device_id", core.new_device_id())
        self._check_worker = ProxyCheckWorker(proxies, device_id, self)
        self._check_worker.done.connect(self._check_done)
        self._check_worker.finished.connect(self._check_worker.deleteLater)
        self._check_worker.start()

    def _check_done(self, ok: bool, text: str):
        self.check_btn.setEnabled(True)
        self.check_btn.setText("Проверить API")
        self._check_worker = None
        if ok:
            QMessageBox.information(self, "Проверка API", text)
        else:
            QMessageBox.warning(
                self, "Проверка API",
                "Не удалось подключиться:\n" + text +
                "\n\nПопробуйте другой прокси или снимите галочку API-прокси."
            )

    def _clear_cache(self):
        if self.win is not None:
            self.win.clear_cache()
        QMessageBox.information(
            self, "Кэш",
            "Кэш списка каналов удалён. Список будет загружен заново."
        )


# --------------------------------------------------------------------------
# Главное окно
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, mpv_module):
        super().__init__()
        self.mpv_module = mpv_module
        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 760)
        self.setWindowIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        )

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.device_id = self.settings.value("device_id", "", type=str)
        if not self.device_id:
            self.device_id = core.new_device_id()
            self.settings.setValue("device_id", self.device_id)

        self.categories: List[dict] = []
        self.channels: List[dict] = []
        self.channel_by_slug: Dict[str, dict] = {}
        self.total = 0

        self._workers = set()
        self._channels_worker = None
        self._stream_token = 0
        self._current_slug = ""
        self._current_channel = None
        self._suppress_play = False
        self._closing = False
        self._stream_bridge = None
        self._stream_bridge_upstream = ""
        self._fullscreen = False
        self._fs_t = 0.0
        self._was_maximized = False
        self._list_visible = True
        self._info_visible = True
        self._cache_cleared = False
        self._auto_start_done = False
        self._epg_cache: Dict[str, tuple] = {}
        self._epg_token = 0
        self._logo_cache: Dict[str, QPixmap] = {}
        self._logo_replies = {}
        self._logo_slug = ""

        self._build_ui()
        self.settings_dlg = SettingsDialog(self.settings, self)
        self._logo_mgr = QNetworkAccessManager(self)

        self.player = Player(
            mpv_module, self.video, self._mpv_options(), self
        )
        self.player.failed.connect(self._on_player_failed)
        self.player.key.connect(self._on_mpv_key)
        self.player.fs_changed.connect(self._on_mpv_fullscreen)
        if self.player.mpv is None:
            QTimer.singleShot(
                0,
                lambda: QMessageBox.critical(
                    self, "mpv",
                    "Плеер mpv недоступен:\n\n"
                    + (self.player.error or "неизвестная ошибка")
                    + "\n\nПроверьте mpv/libmpv и python-mpv."
                ),
            )
        elif self.player.error:
            QTimer.singleShot(
                0,
                lambda: self.statusBar().showMessage(
                    "mpv: " + self.player.error, 12000
                ),
            )

        # геометрия
        geo = self.settings.value("geometry")
        if geo is not None:
            try:
                self.restoreGeometry(geo)
            except Exception:
                pass
        state = self.settings.value("splitter_state")
        if state is not None:
            try:
                self.splitter.restoreState(state)
            except Exception:
                pass

        self._load_cache()
        if self.list.topLevelItemCount() == 0:
            QTimer.singleShot(0, self.refresh_channels)
        else:
            # фоном обновляем список, не мешая уже показанному кэшу
            QTimer.singleShot(300, self.refresh_channels)

    def _mpv_options(self) -> dict:
        hwdec = str(
            self.settings.value("hwdec", "auto-safe", type=str)
            or "auto-safe"
        )
        vo = os.environ.get("LIMEHD_MPV_VO") or "libmpv"
        opts = {
            "vo": vo,
            "hwdec": hwdec,
            "ytdl": False,
            "osc": True,
            "input_default_bindings": False,
            "input_vo_keyboard": True,
            "user_agent": core.USER_AGENT,
            "referrer": "https://limehd.tv/",
            "cache": "yes",
            "demuxer_max_bytes": "64MiB",
            "log_handler": self._mpv_log,
        }
        ao = os.environ.get("LIMEHD_MPV_AO")
        if ao:
            opts["ao"] = ao
        return opts

    # ---------- UI ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(9, 9, 9, 9)
        root.setSpacing(6)

        # верхняя панель
        self.controls = QWidget()
        row = QHBoxLayout(self.controls)
        row.setContentsMargins(0, 0, 0, 0)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск канала…")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Поиск по названию, slug и категории (Ctrl+F)")

        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.setToolTip("Обновить список каналов и программу (Ctrl+R)")
        self.refresh_btn.clicked.connect(self.refresh_channels)

        self.list_btn = QPushButton("Скрыть список")
        self.list_btn.setToolTip("Показать/скрыть список каналов (Ctrl+L)")
        self.list_btn.clicked.connect(self.toggle_list)

        self.info_btn = QPushButton("Скрыть инфо")
        self.info_btn.setToolTip("Показать/скрыть панель информации (Ctrl+I)")
        self.info_btn.clicked.connect(self.toggle_info)

        self.fs_btn = QPushButton("На весь экран")
        self.fs_btn.setToolTip(
            "Полный экран (F11; в окне видео — F, Esc — выйти)"
        )
        self.fs_btn.clicked.connect(self.toggle_fullscreen)

        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedWidth(36)
        self.settings_btn.setToolTip("Настройки: прокси, качество (Ctrl+,)")
        self.settings_btn.clicked.connect(self.open_settings)

        row.addWidget(self.search, 1)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.list_btn)
        row.addWidget(self.info_btn)
        row.addWidget(self.fs_btn)
        row.addWidget(self.settings_btn)
        root.addWidget(self.controls)

        # список / видео / инфо
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        self.list = QTreeWidget()
        self.list.setHeaderHidden(True)
        self.list.setUniformRowHeights(True)
        self.list.setAlternatingRowColors(True)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.list.setMinimumWidth(240)
        self.list.setStyleSheet(
            "QTreeWidget::item { padding: 4px; }"
        )
        self.list.itemSelectionChanged.connect(self._on_selection_changed)
        self.splitter.addWidget(self.list)

        self.video = MpvVideo()
        self.splitter.addWidget(self.video)

        self.info_panel = QWidget()
        self.info_panel.setMinimumWidth(300)
        info = QVBoxLayout(self.info_panel)
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(4)

        self.channel_title = QLabel("Выберите канал")
        self.channel_title.setWordWrap(True)
        self.channel_title.setStyleSheet(
            "font-size: 17px; font-weight: 600;"
        )

        self.channel_meta = QLabel("")
        self.channel_meta.setWordWrap(True)
        self.channel_meta.setStyleSheet("color:#777;")

        self.logo_label = QLabel()
        self.logo_label.setFixedSize(64, 64)
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_logo_placeholder("LimeHD")

        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(
            self.logo_label, 0, Qt.AlignmentFlag.AlignTop
        )
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.addWidget(self.channel_title)
        text_col.addWidget(self.channel_meta)
        header.addLayout(text_col, 1)
        info.addLayout(header)

        self.now_label = QLabel("Сейчас")
        self.now_label.setStyleSheet("color:#777; font-size:11px;")
        info.addWidget(self.now_label)

        self.now_title = QLabel("—")
        self.now_title.setWordWrap(True)
        self.now_title.setStyleSheet("font-weight: 600;")
        info.addWidget(self.now_title)

        self.now_desc = QLabel("")
        self.now_desc.setWordWrap(True)
        self.now_desc.setStyleSheet("color:#666;")
        info.addWidget(self.now_desc)

        self.next_label = QLabel("Далее")
        self.next_label.setStyleSheet("color:#777; font-size:11px;")
        info.addWidget(self.next_label)

        self.next_title = QLabel("—")
        self.next_title.setWordWrap(True)
        info.addWidget(self.next_title)

        self.program = QTreeWidget()
        self.program.setColumnCount(2)
        self.program.setHeaderLabels(["Время", "Передача"])
        self.program.setRootIsDecorated(False)
        self.program.setUniformRowHeights(True)
        self.program.setAlternatingRowColors(True)
        self.program.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.program.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.program.setColumnWidth(0, 92)
        self.program.header().setStretchLastSection(True)
        info.addWidget(self.program, 1)

        self.stream_status = QLabel("Поток не выбран")
        self.stream_status.setWordWrap(True)
        self.stream_status.setStyleSheet("color:#777; font-size:11px;")
        info.addWidget(self.stream_status)

        self.splitter.addWidget(self.info_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([320, 690, 340])
        root.addWidget(self.splitter, 1)

        self.statusBar().showMessage("Готово")

        # сигналы
        self.search.textChanged.connect(self.apply_filter)

        # горячие клавиши (F не делаем глобальной — она должна попадать
        # в поле поиска; в окне видео F обрабатывает сам mpv)
        sc = QShortcut(QKeySequence("F11"), self)
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.activated.connect(self.toggle_fullscreen)
        esc = QShortcut(QKeySequence("Esc"), self)
        esc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        esc.activated.connect(self.leave_fullscreen)
        find = QShortcut(QKeySequence("Ctrl+F"), self)
        find.setContext(Qt.ShortcutContext.ApplicationShortcut)
        find.activated.connect(self._focus_search)
        refresh = QShortcut(QKeySequence("Ctrl+R"), self)
        refresh.setContext(Qt.ShortcutContext.ApplicationShortcut)
        refresh.activated.connect(self.refresh_channels)
        lst = QShortcut(QKeySequence("Ctrl+L"), self)
        lst.setContext(Qt.ShortcutContext.ApplicationShortcut)
        lst.activated.connect(self.toggle_list)
        inf = QShortcut(QKeySequence("Ctrl+I"), self)
        inf.setContext(Qt.ShortcutContext.ApplicationShortcut)
        inf.activated.connect(self.toggle_info)
        st = QShortcut(QKeySequence("Ctrl+,"), self)
        st.setContext(Qt.ShortcutContext.ApplicationShortcut)
        st.activated.connect(self.open_settings)

    # ---------- прокси/качество из диалога ----------
    def api_proxies(self):
        return self.settings_dlg.api_proxies()

    def api_proxy_url(self):
        return self.settings_dlg.api_url()

    def stream_proxy(self):
        return self.settings_dlg.stream_url()

    def quality(self) -> str:
        return self.settings_dlg.quality()

    def _stop_stream_bridge(self):
        if self._stream_bridge is not None:
            try:
                self._stream_bridge.stop()
            except Exception:
                pass
            self._stream_bridge = None
            self._stream_bridge_upstream = ""

    def _mpv_proxy_url(self):
        """
        Что отдать mpv в http-proxy.

        mpv/ffmpeg понимают только http:// и socks://. Если пользователь
        указал https:// прокси, поднимаем локальный HTTP→HTTPS мост и
        отдаём mpv его local_url.
        """
        proxy = self.stream_proxy()
        if not proxy:
            self._stop_stream_bridge()
            return None
        scheme = urllib.parse.urlparse(proxy).scheme.lower()
        if scheme != "https":
            self._stop_stream_bridge()
            return proxy
        if (
            self._stream_bridge is None
            or self._stream_bridge_upstream != proxy
        ):
            self._stop_stream_bridge()
            try:
                self._stream_bridge = LocalProxyBridge(proxy)
                self._stream_bridge_upstream = proxy
            except Exception as e:
                traceback.print_exc()
                self.stream_status.setText(
                    "Не удалось запустить локальный прокси-мост: " + str(e)
                )
                return None
        return self._stream_bridge.local_url

    # ---------- кэш ----------
    def _load_cache(self):
        path = cache_file()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cats = data.get("categories") or []
            channels = data.get("channels") or []
            total = int(data.get("total") or len(channels))
            ts = int(data.get("ts") or 0)
            if not channels:
                return
            self.categories = cats
            self.channels = channels
            self.total = total
            self.channel_by_slug = {
                c.get("address"): c for c in channels if c.get("address")
            }
            # Не запускаем mpv до showEvent: нативное окно ещё не готово.
            self._populate_list(autoplay=False)
            when = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "?"
            self.statusBar().showMessage(
                f"Кэш: {len(channels)} каналов от {when}. Идёт обновление…"
            )
        except Exception:
            traceback.print_exc()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._auto_start_done:
            self._auto_start_done = True
            # Player.play сам ждёт готовности render context (_rctx).
            QTimer.singleShot(300, self._auto_start)

    def _auto_start(self):
        if self._closing or self._stream_token > 0:
            return
        slug = (
            self._current_slug
            or self.settings.value("last_channel", "", type=str)
            or DEFAULT_CHANNEL
        )
        item = self._find_channel_item(slug) or self._first_channel_item()
        if item is not None:
            self._select_item(item, play=True)

    def _save_cache(self):
        path = cache_file()
        data = {
            "version": 1,
            "ts": int(time.time()),
            "categories": self.categories,
            "channels": self.channels,
            "total": self.total,
        }
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            traceback.print_exc()

    def clear_cache(self):
        path = cache_file()
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            traceback.print_exc()
        self._cache_cleared = True
        self.refresh_channels()

    # ---------- список каналов ----------
    def refresh_channels(self):
        if self._channels_worker is not None and self._channels_worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self.statusBar().showMessage("Загрузка списка каналов…")
        worker = ChannelsWorker(
            self.api_proxies(), self.device_id, "77", "3", 30.0, self
        )
        self._channels_worker = worker
        self._track_worker(worker)
        worker.done.connect(self._on_channels_done)
        worker.start()

    def _on_channels_done(self, result: dict):
        self.refresh_btn.setEnabled(True)
        self._channels_worker = None
        err = (result or {}).get("error") or ""
        warning = (result or {}).get("warning") or ""
        channels = (result or {}).get("channels") or []
        cats = (result or {}).get("categories") or []

        if err or not channels:
            msg = err or "пустой список каналов"
            self.statusBar().showMessage("Не удалось обновить список: " + msg)
            if self.list.topLevelItemCount() == 0:
                QMessageBox.critical(
                    self, "Список каналов",
                    "Не удалось загрузить список каналов:\n\n" + msg +
                    "\n\nПроверьте настройки прокси (⚙) или отключите его."
                )
            return

        self.categories = cats
        self.channels = channels
        self.total = int((result or {}).get("total") or len(channels))
        self.channel_by_slug = {
            c.get("address"): c for c in channels if c.get("address")
        }
        self._save_cache()
        self._populate_list(
            autoplay=(
                self._auto_start_done
                and self._stream_token == 0
            )
        )
        msg = f"Список обновлён: {len(channels)} каналов"
        if warning:
            msg += " · " + warning
            self.statusBar().showMessage(msg, 12000)
        else:
            self.statusBar().showMessage(msg, 6000)

    def _primary_category(self, ch: dict) -> Optional[dict]:
        cat_map = {c["id"]: c for c in self.categories}
        ids = ch.get("categories") or []
        skip = {1, 337, 148}
        for cid in ids:
            if cid not in skip and cid in cat_map:
                return cat_map[cid]
        for cid in (148, 1):
            if cid in ids and cid in cat_map:
                return cat_map[cid]
        return None

    def _populate_list(self, autoplay: bool):
        current = (
            self._current_slug
            or self.settings.value("last_channel", "", type=str)
            or DEFAULT_CHANNEL
        )
        self._suppress_play = True
        self.list.clear()

        # Показываем только те каналы, которые реально доступны без подписки.
        # В API их 489, но часть (public=false / available=false) не играет.
        visible_channels = [
            ch for ch in self.channels
            if ch.get("public") and ch.get("available")
            and not ch.get("is_web_player_disabled")
        ]
        if not visible_channels:
            visible_channels = self.channels

        cat_map = {c["id"]: c for c in self.categories}
        groups: Dict[int, List[dict]] = {}
        for ch in visible_channels:
            cat = self._primary_category(ch)
            cid = cat["id"] if cat else 0
            groups.setdefault(cid, []).append(ch)

        order = [c["id"] for c in self.categories] + [0]
        seen = set()
        for cid in order:
            if cid in seen:
                continue
            seen.add(cid)
            items = groups.get(cid)
            if not items:
                continue
            cat = cat_map.get(cid)
            cat_name = cat["name"] if cat else NO_GROUP_LABEL
            cat_item = QTreeWidgetItem([cat_name])
            cat_item.setData(
                0, Qt.ItemDataRole.UserRole,
                {"kind": "category", "id": cid, "name": cat_name}
            )
            font = cat_item.font(0)
            font.setBold(True)
            cat_item.setFont(0, font)
            cat_item.setFlags(
                cat_item.flags() & ~Qt.ItemFlag.ItemIsSelectable
            )
            cat_item.setToolTip(0, f"{cat_name}: {len(items)} каналов")
            for ch in sorted(
                items,
                key=lambda c: (
                    c.get("number") if isinstance(c.get("number"), int) else 10**9,
                    str(c.get("name_ru") or c.get("title") or ""),
                ),
            ):
                name = str(
                    ch.get("name_ru") or ch.get("title") or
                    ch.get("address") or "Без названия"
                )
                item = QTreeWidgetItem([name])
                item.setData(0, Qt.ItemDataRole.UserRole, ch)
                item.setToolTip(0, self._channel_tooltip(ch))
                cat_item.addChild(item)
            self.list.addTopLevelItem(cat_item)

        self._suppress_play = False

        target = self._find_channel_item(current)
        if target is None:
            target = self._first_channel_item()
            current = (
                target.data(0, Qt.ItemDataRole.UserRole).get("address")
                if target is not None else ""
            )

        if target is not None:
            self._select_item(target, play=autoplay)

        # если канал уже играет, не перезапускаем, но обновляем инфо
        if not autoplay and self._current_slug:
            item = self._find_channel_item(self._current_slug)
            if item is not None:
                self._update_info(item.data(0, Qt.ItemDataRole.UserRole))

        # сохранить активный поиск после обновления списка
        if self.search.text().strip():
            self.apply_filter(self.search.text())

    def _find_channel_item(self, slug: str):
        if not slug:
            return None
        for i in range(self.list.topLevelItemCount()):
            cat = self.list.topLevelItem(i)
            for j in range(cat.childCount()):
                item = cat.child(j)
                ch = item.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(ch, dict) and ch.get("address") == slug:
                    return item
        return None

    def _first_channel_item(self):
        for i in range(self.list.topLevelItemCount()):
            cat = self.list.topLevelItem(i)
            if cat.childCount():
                return cat.child(0)
        return None

    def _select_item(self, item: QTreeWidgetItem, play: bool):
        self._suppress_play = True
        parent = item.parent()
        if parent is not None:
            parent.setExpanded(True)
        self.list.setCurrentItem(item)
        self.list.scrollToItem(item)
        self._suppress_play = False
        ch = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(ch, dict):
            return
        self._current_channel = ch
        self._current_slug = str(ch.get("address") or "")
        self._update_info(ch)
        if play:
            self._play_channel(ch)

    def _channel_tooltip(self, ch: dict) -> str:
        name = str(ch.get("name_ru") or ch.get("title") or "")
        cats = self._channel_category_names(ch)
        now = ""
        events = ch.get("current") or []
        if isinstance(events, list):
            now_ts = time.time()
            for ev in events:
                try:
                    t0, t1 = int(ev.get("timestart", 0)), int(ev.get("timestop", 0))
                except (TypeError, ValueError):
                    continue
                if t0 <= now_ts < t1:
                    now = event_title(ev)
                    break
        parts = [name, str(ch.get("address") or "")]
        if ch.get("number") is not None:
            parts.append(f"№ {ch.get('number')}")
        if cats:
            parts.append(cats)
        if now:
            parts.append("Сейчас: " + now)
        return "\n".join(p for p in parts if p)

    def _channel_category_names(self, ch: dict) -> str:
        cat_map = {c["id"]: c for c in self.categories}
        names = [
            cat_map[i]["name"] for i in (ch.get("categories") or [])
            if i in cat_map
        ]
        return ", ".join(names)

    def _on_selection_changed(self):
        if self._suppress_play:
            return
        items = self.list.selectedItems()
        if not items:
            return
        ch = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(ch, dict) or not ch.get("address"):
            return
        if ch.get("address") == self._current_slug:
            # тот же канал — просто обновляем информацию (перезапуск
            # делается кнопкой «Перезапустить»)
            self._current_channel = ch
            self._update_info(ch)
            return
        self._current_channel = ch
        self._current_slug = str(ch.get("address"))
        self._update_info(ch)
        self._play_channel(ch)

    def apply_filter(self, text: str):
        q = (text or "").strip().lower()
        for i in range(self.list.topLevelItemCount()):
            cat = self.list.topLevelItem(i)
            cat_name = cat.text(0).lower()
            cat_match = bool(q) and q in cat_name
            visible = 0
            for j in range(cat.childCount()):
                item = cat.child(j)
                ch = item.data(0, Qt.ItemDataRole.UserRole)
                hay = []
                if isinstance(ch, dict):
                    hay = [
                        str(ch.get("name_ru") or ""),
                        str(ch.get("title") or ""),
                        str(ch.get("address") or ""),
                        self._channel_category_names(ch),
                    ]
                match = (not q) or cat_match or any(
                    q in h.lower() for h in hay
                )
                item.setHidden(not match)
                if match:
                    visible += 1
            cat.setHidden(visible == 0)
            if q and visible:
                cat.setExpanded(True)
            elif not q:
                # не меняем ручное раскрытие
                pass

    def _focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    # ---------- логотип канала ----------
    @staticmethod
    def _initials(name: str) -> str:
        text = (name or "").replace("«", "").replace("»", "")
        text = text.replace('"', "").strip()
        words = [w for w in text.split() if w]
        if len(words) >= 2:
            return (words[0][0] + words[1][0]).upper()
        if text:
            return text[:2].upper()
        return "?"

    def _set_logo_placeholder(self, name: str):
        if not hasattr(self, "logo_label"):
            return
        self.logo_label.setPixmap(QPixmap())
        self.logo_label.setText(self._initials(name))
        self.logo_label.setStyleSheet(
            "background:#e7edf0; border-radius:8px; color:#5b6b73; "
            "font-weight:600; font-size:20px;"
        )

    def _apply_logo(self, pix: QPixmap):
        if pix is None or pix.isNull():
            return
        self.logo_label.setText("")
        self.logo_label.setStyleSheet(
            "background:transparent; border-radius:8px;"
        )
        self.logo_label.setPixmap(
            pix.scaled(
                64, 64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _load_logo(self, ch: dict):
        slug = str(ch.get("address") or "")
        if not slug or slug == self._logo_slug:
            return
        self._logo_slug = slug
        name = str(ch.get("name_ru") or ch.get("title") or slug)
        urls = []
        for key in ("cached_logo_url", "image"):
            url = ch.get(key)
            if isinstance(url, str) and url and url not in urls:
                urls.append(url)
        if not urls:
            self._set_logo_placeholder(name)
            return
        for url in urls:
            pix = self._logo_cache.get(url)
            if pix is not None and not pix.isNull():
                self._apply_logo(pix)
                return
        self._set_logo_placeholder(name)
        self._request_logo(slug, urls, 0)

    def _request_logo(self, slug: str, urls: List[str], idx: int):
        if slug != self._logo_slug or idx >= len(urls):
            return
        url = urls[idx]
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"User-Agent", core.USER_AGENT.encode())
        try:
            req.setTransferTimeout(15000)
        except AttributeError:
            pass
        reply = self._logo_mgr.get(req)
        self._logo_replies[reply] = (slug, urls, idx)
        reply.finished.connect(
            lambda r=reply: self._on_logo_reply(r)
        )

    def _on_logo_reply(self, reply):
        info = self._logo_replies.pop(reply, None)
        try:
            data = bytes(reply.readAll())
        except Exception:
            data = b""
        reply.deleteLater()
        if not info:
            return
        slug, urls, idx = info
        pix = QPixmap()
        ok = bool(data) and pix.loadFromData(data) and not pix.isNull()
        if not ok and data:
            # Qt без плагина WebP не читает .webp — конвертируем через Pillow.
            try:
                from PIL import Image

                im = Image.open(io.BytesIO(data)).convert("RGBA")
                buf = io.BytesIO()
                im.save(buf, "PNG")
                pix = QPixmap()
                ok = pix.loadFromData(buf.getvalue()) and not pix.isNull()
            except Exception:
                ok = False
        if ok:
            self._logo_cache[urls[idx]] = pix
            if slug == self._logo_slug:
                self._apply_logo(pix)
            return
        if slug == self._logo_slug:
            if idx + 1 < len(urls):
                self._request_logo(slug, urls, idx + 1)
            else:
                name = ""
                if self._current_channel:
                    name = str(
                        self._current_channel.get("name_ru")
                        or self._current_channel.get("title")
                        or slug
                    )
                self._set_logo_placeholder(name or slug)

    # ---------- информация о канале ----------
    def _update_info(self, ch: dict, events=None):
        name = str(ch.get("name_ru") or ch.get("title") or "Без названия")
        self.channel_title.setText(name)
        cats = self._channel_category_names(ch)
        meta = []
        if ch.get("number") is not None:
            meta.append(f"№ {ch.get('number')}")
        if cats:
            meta.append(cats)
        if ch.get("with_archive"):
            days = ch.get("day_archive") or 0
            meta.append(f"архив {days} дн." if days else "есть архив")
        self.channel_meta.setText(" · ".join(meta))
        self._load_logo(ch)

        if events is None:
            slug = str(ch.get("address") or "")
            cached = self._epg_cache.get(slug)
            if cached and time.time() - cached[0] < 300:
                events = cached[1]
            else:
                events = ch.get("current") or []
        if not isinstance(events, list):
            events = []
        norm = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            try:
                t0 = int(ev.get("timestart", 0))
                t1 = int(ev.get("timestop", 0))
            except (TypeError, ValueError):
                t0 = t1 = 0
            norm.append((t0, t1, ev))
        norm.sort(key=lambda x: x[0])

        now_ts = time.time()
        current_idx = -1
        for idx, (t0, t1, _ev) in enumerate(norm):
            if t0 <= now_ts < t1:
                current_idx = idx
                break
        if current_idx < 0 and norm:
            current_idx = 0

        self.program.clear()
        for idx, (t0, t1, ev) in enumerate(norm):
            item = QTreeWidgetItem([
                event_time_range(ev), event_title(ev)
            ])
            if idx == current_idx:
                item.setBackground(0, QBrush(QColor("#d7f2ef")))
                item.setBackground(1, QBrush(QColor("#d7f2ef")))
                f = item.font(0)
                f.setBold(True)
                item.setFont(0, f)
                f = item.font(1)
                f.setBold(True)
                item.setFont(1, f)
            self.program.addTopLevelItem(item)

        if current_idx >= 0:
            ev = norm[current_idx][2]
            self.now_title.setText(
                f"{event_time_range(ev)}  {event_title(ev)}"
            )
            desc = str(ev.get("desc") or "").strip()
            if len(desc) > 400:
                desc = desc[:399] + "…"
            self.now_desc.setText(desc)
            if current_idx + 1 < len(norm):
                nxt = norm[current_idx + 1][2]
                self.next_title.setText(
                    f"{event_time_range(nxt)}  {event_title(nxt)}"
                )
            else:
                self.next_title.setText("—")
        else:
            self.now_title.setText("Нет данных о программе")
            self.now_desc.setText("")
            self.next_title.setText("—")



    # ---------- поток ----------
    def _play_channel(self, ch: dict):
        if self._closing or not ch:
            return
        slug = str(ch.get("address") or "")
        if not slug:
            return
        self._current_slug = slug
        self._current_channel = ch
        self.settings.setValue("last_channel", slug)

        self._stream_token += 1
        token = self._stream_token
        name = str(ch.get("name_ru") or ch.get("title") or slug)
        self.statusBar().showMessage(f"Загрузка потока: {name}…")
        self.stream_status.setText("Загрузка потока…")
        self._load_epg(ch)

        worker = StreamWorker(
            token, slug, self.quality(), self.api_proxies(),
            self.device_id, "77", "3", 30.0, self,
        )
        self._track_worker(worker)
        worker.done.connect(self._on_stream_done)
        worker.start()

    def _load_epg(self, ch: dict):
        slug = str(ch.get("address") or "")
        if not slug:
            return
        cached = self._epg_cache.get(slug)
        if cached and time.time() - cached[0] < 300:
            if self._current_slug == slug:
                self._update_info(ch, cached[1])
            return
        self._epg_token += 1
        token = self._epg_token
        worker = EpgWorker(
            token, slug, self.api_proxies(), self.device_id,
            "77", "3", 20.0, self,
        )
        self._track_worker(worker)
        worker.done.connect(self._on_epg_done)
        worker.start()

    def _on_epg_done(self, token: int, slug: str, events):
        if token != self._epg_token:
            return
        if isinstance(events, dict) and events.get("error"):
            return
        if not isinstance(events, list):
            return
        self._epg_cache[slug] = (time.time(), events)
        if self._current_slug == slug and self._current_channel is not None:
            self._update_info(self._current_channel, events)

    def _on_stream_done(self, token: int, data: dict):
        if self._closing or token != self._stream_token:
            return
        err = (data or {}).get("error") or ""
        if err:
            self.stream_status.setText("Ошибка: " + err)
            self.statusBar().showMessage("Не удалось получить поток: " + err)
            return

        self._play_url(str(data.get("url") or ""))

        exp = data.get("expires")
        warning = data.get("warning") or ""
        text = f"Поток: {data.get('selected')} · до {fmt_ts(exp)}"
        sp = self.stream_proxy()
        if sp:
            scheme = urllib.parse.urlparse(sp).scheme.lower()
            if self._stream_bridge is not None:
                text += " · mpv: локальный мост → прокси"
            elif scheme == "https":
                text += " · mpv: прокси не применён"
            else:
                text += " · mpv через прокси"
        if self.player.mpv is None:
            text += " · mpv не запущен"
        if warning:
            text += " · " + warning
        self.stream_status.setText(text)

    def _play_url(self, url: str):
        if not url:
            return
        agent = core.make_agent_header(self.device_id)
        fields = [
            f"x-lhd-agent: {agent}",
            f"User-Agent: {core.USER_AGENT}",
            "Referer: https://limehd.tv/",
        ]
        title = ""
        if self._current_channel:
            title = str(
                self._current_channel.get("name_ru") or
                self._current_channel.get("title") or ""
            )
        proxy = self._mpv_proxy_url()
        self.player.play(
            url, headers=fields, proxy=proxy, title=title or APP_TITLE
        )
        self.statusBar().showMessage(f"Запуск: {title}")
        try:
            self.video.setFocus()
        except Exception:
            pass

    def _on_player_failed(self, text: str):
        self.stream_status.setText("Ошибка mpv: " + text)
        self.statusBar().showMessage("Ошибка mpv: " + text)

    def _mpv_log(self, level, prefix, text):
        if os.environ.get("LIMEHD_MPV_LOG"):
            print(f"[mpv] {level} {prefix}: {text}", file=sys.stderr)

    def _on_mpv_key(self, action: str):
        if action == "pause":
            self.player.toggle_pause()
        elif action == "fullscreen":
            self.toggle_fullscreen()
        elif action == "escape":
            self.leave_fullscreen()
        elif action == "ch_up":
            self._switch_channel(-1)
        elif action == "ch_down":
            self._switch_channel(1)
        elif action == "vol_up":
            self.player.volume(5)
        elif action == "vol_down":
            self.player.volume(-5)
        elif action == "mute":
            self.player.command("cycle", "mute")

    def _switch_channel(self, delta: int):
        items = []
        for i in range(self.list.topLevelItemCount()):
            cat = self.list.topLevelItem(i)
            for j in range(cat.childCount()):
                item = cat.child(j)
                if item.isHidden():
                    continue
                ch = item.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(ch, dict) and ch.get("address"):
                    items.append(item)
        if not items:
            return
        cur = None
        for idx, item in enumerate(items):
            ch = item.data(0, Qt.ItemDataRole.UserRole)
            if ch.get("address") == self._current_slug:
                cur = idx
                break
        if cur is None:
            target = items[0]
        else:
            target = items[(cur + delta) % len(items)]
        self._select_item(target, play=True)

    def _on_mpv_fullscreen(self, value: bool):
        if value and not self.isFullScreen():
            self.enter_fullscreen()
        elif not value and self.isFullScreen():
            self.leave_fullscreen()

    # ---------- панели/полный экран ----------
    def toggle_list(self):
        self._list_visible = not self._list_visible
        if not self.isFullScreen():
            self.list.setVisible(self._list_visible)
        self.list_btn.setText(
            "Скрыть список" if self._list_visible else "Показать список"
        )

    def toggle_info(self):
        self._info_visible = not self._info_visible
        if not self.isFullScreen():
            self.info_panel.setVisible(self._info_visible)
        self.info_btn.setText(
            "Скрыть инфо" if self._info_visible else "Показать инфо"
        )

    def toggle_fullscreen(self):
        now = time.monotonic()
        if now - getattr(self, "_fs_t", 0.0) < 0.4:
            return
        self._fs_t = now
        if self.isFullScreen():
            self.leave_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self):
        if self.isFullScreen():
            return
        self._was_maximized = self.isMaximized()
        self.controls.hide()
        self.list.hide()
        self.info_panel.hide()
        self.statusBar().hide()
        self.centralWidget().layout().setContentsMargins(0, 0, 0, 0)
        self.showFullScreen()
        self.player.set_fullscreen(True)

    def leave_fullscreen(self):
        if not self.isFullScreen():
            return
        self.controls.show()
        self.list.setVisible(self._list_visible)
        self.info_panel.setVisible(self._info_visible)
        self.statusBar().show()
        self.centralWidget().layout().setContentsMargins(9, 9, 9, 9)
        if self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.player.set_fullscreen(False)

    # ---------- настройки/сервис ----------
    def open_settings(self):
        old_api = self.api_proxies()
        old_stream = self.stream_proxy()
        old_quality = self.quality()
        old_hwdec = self.settings_dlg.hwdec()
        self._cache_cleared = False
        if self.settings_dlg.exec() == QDialog.DialogCode.Accepted:
            new_api = self.api_proxies()
            new_stream = self.stream_proxy()
            new_quality = self.quality()
            new_hwdec = self.settings_dlg.hwdec()
            if old_api != new_api or old_stream != new_stream:
                self._stop_stream_bridge()
            if old_hwdec != new_hwdec:
                self.player.set_hwdec(new_hwdec)
            if old_api != new_api:
                self.refresh_channels()
            if self._cache_cleared:
                self.refresh_channels()
            if self._current_channel and (
                old_api != new_api or
                old_stream != new_stream or
                old_quality != new_quality
            ):
                self._play_channel(self._current_channel)

    # ---------- потоки ----------
    def _track_worker(self, worker: QThread):
        self._workers.add(worker)
        worker.finished.connect(
            lambda w=worker: self._workers.discard(w)
        )
        worker.finished.connect(worker.deleteLater)

    def closeEvent(self, event):
        if self._closing:
            event.accept()
            return
        self._closing = True
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter_state", self.splitter.saveState())
        self.settings.setValue("last_channel", self._current_slug)
        self.settings.sync()

        try:
            self.player.terminate()
        except Exception:
            pass

        self._stop_stream_bridge()

        deadline = time.monotonic() + 2.0
        for w in list(self._workers):
            if w.isRunning():
                left = int((deadline - time.monotonic()) * 1000)
                if left <= 0:
                    break
                try:
                    w.wait(left)
                except Exception:
                    pass
        event.accept()


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------

def main() -> int:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName(ORG_NAME)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationDisplayName(APP_TITLE)
    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:
        pass

    try:
        import mpv as mpv_module
    except (OSError, ImportError) as e:
        what = (
            "не установлен python-mpv"
            if isinstance(e, ImportError)
            else "не найдена libmpv.so.2"
        )
        QMessageBox.critical(
            None, "LimeHD Эфир",
            f"Плеер недоступен: {what}\n\n"
            "sudo apt install mpv libmpv2\n"
            "python3 -m pip install python-mpv\n\n" + str(e),
        )
        return 1

    win = MainWindow(mpv_module)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
