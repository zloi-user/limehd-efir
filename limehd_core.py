#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
limehd_core.py — API-логика LimeHD для GUI.

Здесь нет CLI: только запросы к pl.iptv2021.com, разбор master-playlist
и выбор качества. GUI (limehd_gui.py) использует этот модуль.

Логика API
----------
Плеер LimeHD вызывает:

    GET https://pl.iptv2021.com/api/v4/stream/<slug>
        ?epg=0&podcasts=0&installts=<ms>&region=<region>&tz=<tz>

Обязательный заголовок:

    x-lhd-agent: {"platform":"web","app":"limehd.tv","device_id":"<id>"}

Ответ содержит временный URL master-playlist. Сама m3u8 и сегменты тоже
требуют x-lhd-agent и браузерный User-Agent (с Lavf/... сервер отдаёт 423).

Список каналов:

    GET https://pl.iptv2021.com/api/v4/playlist?region=77&tz=3&limit=1000
    GET https://pl.iptv2021.com/api/v4/categories
    GET https://pl.iptv2021.com/api/v4/epg/<slug>?region=77&tz=3
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit(
        "Не найден модуль requests. Установите: python3 -m pip install requests"
    )

# У прокси из настроек сертификат выписан на другой домен — предупреждения
# requests/urllib3 не нужны.
requests.packages.urllib3.disable_warnings()

API_BASE = "https://pl.iptv2021.com/api/v4"
REFERER = "https://limehd.tv/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

HTTP_RETRIES = 3


# --------------------------------------------------------------------------
# Заголовки/устройство
# --------------------------------------------------------------------------

def new_device_id() -> str:
    """Похожий на настоящий идентификатор устройства веб-плеера."""
    return "%d.%d-%d" % (
        random.randint(100_000, 999_999),
        random.randint(10**10, 10**11 - 1),
        int(time.time() * 1000),
    )


def make_agent_header(device_id: str) -> str:
    return json.dumps(
        {"platform": "web", "app": "limehd.tv", "device_id": device_id},
        separators=(",", ":"),
    )


def request_headers(device_id: str) -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer": REFERER,
        "x-lhd-agent": make_agent_header(device_id),
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_get(
    url: str,
    headers: Dict[str, str],
    timeout: float,
    proxies: Optional[Dict[str, str]],
    params: Optional[Dict[str, object]] = None,
) -> requests.Response:
    """
    GET с переходами по редиректам и повторами.

    Для прокси используется verify=False (сертификат прокси может быть
    выписан на другой домен). Для прямых запросов TLS-проверка включена.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=(min(15.0, timeout), timeout),
                proxies=proxies if proxies is not None else {},
                verify=proxies is None,
                allow_redirects=True,
            )
            if (
                resp.status_code in (429, 500, 502, 503, 504)
                and attempt < HTTP_RETRIES
            ):
                time.sleep(float(attempt))
                continue
            return resp
        except requests.RequestException as e:
            last_error = e
            if attempt < HTTP_RETRIES:
                time.sleep(float(attempt))
    raise RuntimeError(
        f"сетевая ошибка после {HTTP_RETRIES} попыток: {last_error}"
    )


def http_json(
    url: str,
    headers: Dict[str, str],
    timeout: float,
    proxies: Optional[Dict[str, str]],
    params: Optional[Dict[str, object]] = None,
) -> object:
    r = http_get(url, headers, timeout, proxies, params=params)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except ValueError as e:
        raise RuntimeError(f"сервер вернул не JSON: {e}") from e


# --------------------------------------------------------------------------
# Модели
# --------------------------------------------------------------------------

@dataclass
class Variant:
    url: str
    bandwidth: int = 0
    average_bandwidth: int = 0
    width: int = 0
    height: int = 0
    codecs: str = ""

    @property
    def label(self) -> str:
        if self.height:
            return f"{self.height}p"
        return f"{self.bandwidth // 1000} kbps"

    @property
    def score(self) -> Tuple[int, int]:
        return (self.height, self.average_bandwidth or self.bandwidth)


@dataclass
class StreamInfo:
    slug: str
    channel_id: Optional[int]
    api_url: str
    master_url: str
    headers: Dict[str, str]
    variants: List[Variant] = field(default_factory=list)
    is_master: bool = False


@dataclass
class Category:
    id: int
    identifier: str
    name: str


# --------------------------------------------------------------------------
# HLS
# --------------------------------------------------------------------------

def parse_attributes(text: str) -> Dict[str, str]:
    """Разбирает атрибуты HLS, не ломаясь на запятых внутри кавычек."""
    attrs: Dict[str, str] = {}
    for m in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', text):
        key = m.group(1)
        val = m.group(2).strip()
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        attrs[key] = val
    return attrs


def exp_from_url(url: str) -> Optional[int]:
    """Unix-время истечения из ссылки (токен после запятой или ?e=)."""
    m = re.search(r",(\d{10})/", url)
    if m:
        return int(m.group(1))
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "e" in qs:
        try:
            return int(qs["e"][0])
        except ValueError:
            pass
    return None


def parse_playlist(text: str, base_url: str) -> List[Variant]:
    lines = text.splitlines()
    variants: List[Variant] = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = parse_attributes(line.split(":", 1)[1])
        j = i + 1
        while j < len(lines) and (
            not lines[j].strip() or lines[j].lstrip().startswith("#")
        ):
            j += 1
        if j >= len(lines):
            continue
        uri = urllib.parse.urljoin(base_url, lines[j].strip())
        width = height = 0
        res = attrs.get("RESOLUTION", "")
        if "x" in res:
            try:
                width, height = (int(x) for x in res.lower().split("x", 1))
            except ValueError:
                pass
        variants.append(
            Variant(
                url=uri,
                bandwidth=int(attrs.get("BANDWIDTH", "0") or 0),
                average_bandwidth=int(
                    attrs.get("AVERAGE-BANDWIDTH", "0") or 0
                ),
                width=width,
                height=height,
                codecs=attrs.get("CODECS", ""),
            )
        )
    return variants


def choose_variant(
    variants: List[Variant], quality: Optional[str]
) -> Optional[Variant]:
    if not variants or not quality:
        return None
    quality = quality.lower()
    if quality in ("best", "max"):
        return max(variants, key=lambda v: v.score)
    if quality in ("worst", "min"):
        return min(variants, key=lambda v: v.score)
    try:
        target = int(quality.rstrip("p"))
    except ValueError:
        raise ValueError(
            "качество: best, worst или число (240/360/480/720/1080)"
        )
    below = [v for v in variants if v.height and v.height <= target]
    if below:
        return max(below, key=lambda v: v.score)
    return min(variants, key=lambda v: v.score)


# --------------------------------------------------------------------------
# API каналов/потока
# --------------------------------------------------------------------------

def fetch_stream(
    slug: str,
    region: str,
    tz: str,
    device_id: str,
    timeout: float,
    proxies: Optional[Dict[str, str]],
    resolve: bool = True,
) -> StreamInfo:
    headers = request_headers(device_id)
    api_url = "%s/stream/%s" % (API_BASE, urllib.parse.quote(slug))
    params = {
        "epg": "0",
        "podcasts": "0",
        "installts": str(int(time.time() * 1000)),
        "region": region,
        "tz": tz,
    }

    try:
        payload = http_json(
            api_url, headers, timeout, proxies, params=params
        )
    except RuntimeError as e:
        msg = str(e)
        if "404" in msg:
            raise RuntimeError(f"канал «{slug}» не найден (HTTP 404)") from e
        if "403" in msg:
            raise RuntimeError(
                "API вернул 403. Проверьте прокси и заголовок x-lhd-agent."
            ) from e
        raise

    if not isinstance(payload, dict):
        raise RuntimeError("неожиданный ответ API")

    if payload.get("status") == 404 or payload.get("name") == "Not Found":
        raise RuntimeError(f"канал «{slug}» не найден")

    if not payload.get("available", False):
        raise RuntimeError(
            "канал недоступен без авторизации/подписки "
            f"(available=false, our_id={payload.get('our_id')})"
        )

    url = (payload.get("url") or "").strip()
    if not url:
        raise RuntimeError("API не вернул ссылку на поток")

    info = StreamInfo(
        slug=slug,
        channel_id=payload.get("our_id"),
        api_url=api_url,
        master_url=url,
        headers=headers,
    )

    if resolve:
        try:
            r = http_get(url, headers, timeout, proxies)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
            info.master_url = r.url
            text = r.text
            if "#EXT-X-STREAM-INF:" in text:
                info.is_master = True
                info.variants = parse_playlist(text, r.url)
        except Exception as e:  # noqa: BLE001
            print(
                f"warning: не удалось проверить master-playlist: {e}",
                file=sys.stderr,
            )

    return info


def fetch_categories(
    timeout: float,
    proxies: Optional[Dict[str, str]],
    device_id: str,
) -> List[Category]:
    headers = request_headers(device_id)
    data = http_json(API_BASE + "/categories", headers, timeout, proxies)
    result: List[Category] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or "id" not in item:
                continue
            result.append(
                Category(
                    id=int(item["id"]),
                    identifier=str(
                        item.get("identifier") or item.get("address") or ""
                    ),
                    name=str(
                        item.get("name_ru")
                        or item.get("name_en")
                        or item.get("identifier")
                        or ""
                    ),
                )
            )
    return result


def fetch_channels(
    region: str,
    tz: str,
    timeout: float,
    proxies: Optional[Dict[str, str]],
    device_id: str,
) -> Tuple[List[dict], int]:
    """
    Возвращает (channels, total).

    limit=1000 обычно отдаёт всё (489 каналов), но на всякий случай
    поддерживаем пагинацию и убираем дубликаты по address.
    """
    headers = request_headers(device_id)
    params = {"region": region, "tz": tz, "limit": 1000, "page": 1}
    data = http_json(
        API_BASE + "/playlist", headers, timeout, proxies, params=params
    )
    if not isinstance(data, dict):
        raise RuntimeError("неожиданный ответ API списка каналов")

    total = int(data.get("total") or len(data.get("channels") or []))
    seen = set()
    unique: List[dict] = []

    def add_batch(batch: List[dict]) -> int:
        added = 0
        for ch in batch:
            addr = ch.get("address")
            if not addr or addr in seen:
                continue
            seen.add(addr)
            unique.append(ch)
            added += 1
        return added

    add_batch(list(data.get("channels") or []))

    page = 2
    while page <= 20:
        params["page"] = page
        more = http_json(
            API_BASE + "/playlist", headers, timeout, proxies, params=params
        )
        batch = list(more.get("channels") or []) if isinstance(more, dict) else []
        if not batch:
            break
        if add_batch(batch) == 0:
            break
        page += 1

    return unique, total
