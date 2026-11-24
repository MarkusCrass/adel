#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_aesthetic_bg.py — автоматический поиск, скачивание и подготовка
вертикальных эстетичных видеофонов для Reels (астрология / женский ресурс).

Источник: Pexels (официальный API, лицензия Pexels License — бесплатно,
в том числе для коммерческого использования; атрибуция не обязательна,
но автора записываем в sources.json).

Что делает:
  1. Ищет вертикальные (portrait) видео по англоязычным запросам.
  2. Выбирает файлы MP4 с кадром 9:16 и шириной не меньше 1080 px.
  3. Скачивает их во временную папку.
  4. Обрабатывает через ffmpeg (из пакета imageio-ffmpeg):
       • масштаб + кроп до точных 1080x1920;
       • 30 или 60 fps;
       • 10–15 с (длиннее обрезается, берётся начало);
       • звуковая дорожка удаляется (-an).
  5. Сохраняет результат как bg_01.mp4 … bg_15.mp4 и ведёт манифест
     sources.json (ссылка на видео, автор, лицензия).

Подготовка:
    pip install imageio-ffmpeg
    Бесплатный API-ключ: https://www.pexels.com/api/
    Ключ передаётся через переменную окружения PEXELS_API_KEY.
    Не вставляйте ключ в файлы и в Git.

Windows PowerShell (на одну сессию):
    $env:PEXELS_API_KEY = "ВАШ_КЛЮЧ"
    python fetch_aesthetic_bg.py

Запуск:
    python fetch_aesthetic_bg.py
    python fetch_aesthetic_bg.py --count 15 --fps 60 --out aesthetic_backgrounds

Повторный запуск безопасен: уже готовые bg_NN.mp4 и уже использованные
видео из sources.json пропускаются.

Проверьте готовые файлы глазами перед публикацией: скрипт не ищет текст
и водяные знаки на кадре автоматически.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://api.pexels.com/videos/search"
API_KEY_ENV = "PEXELS_API_KEY"

# Запросы по теме: «aesthetic» и «no text» не используем — поиск Pexels ключевой,
# вертикальность задаётся параметром orientation=portrait.
QUERIES = [
    "full moon night sky",
    "candle cozy evening rain",
    "morning coffee sunlight",
    "ocean waves night moonlight",
]

SRC_MIN_SEC = 10.0   # минимальная длительность источника
CLIP_MAX_SEC = 15.0  # итоговый фон: не длиннее 15 с (нижняя граница 10 с)
OUT_W, OUT_H = 1080, 1920
MANIFEST_NAME = "sources.json"
USER_AGENT = "fetch_aesthetic_bg/2.0"
SEARCH_ERRORS: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def require_deps() -> str:
    """Проверяет зависимости и ключ API; возвращает путь к бинарнику ffmpeg."""
    try:
        import imageio_ffmpeg
    except ImportError:
        sys.exit("Не найден imageio-ffmpeg. Установите: pip install imageio-ffmpeg")
    if not os.environ.get(API_KEY_ENV):
        sys.exit(
            f"Не задан API-ключ Pexels. Получите бесплатный ключ на https://www.pexels.com/api/\n"
            f"и задайте переменную {API_KEY_ENV} (PowerShell: $env:{API_KEY_ENV} = \"ключ\")."
        )
    return imageio_ffmpeg.get_ffmpeg_exe()


def pexels_search(query: str, per_page: int, api_key: str) -> list[dict]:
    """Поиск вертикальных видео по запросу. Возвращает список объектов видео из API."""
    params = {"query": query, "orientation": "portrait", "per_page": per_page}
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": api_key, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("videos", [])
    except urllib.error.HTTPError as e:
        hint = {401: "неверный ключ API", 403: "доступ запрещён", 429: "лимит запросов, попробуйте позже"}
        msg = f"HTTP {e.code}: {hint.get(e.code, e.reason)}"
        log(f"   ! поиск не удался ({msg})")
        SEARCH_ERRORS.append(msg)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        msg = f"сеть: {e}"
        log(f"   ! поиск не удался ({msg})")
        SEARCH_ERRORS.append(msg)
    return []


def pick_file(video: dict) -> dict | None:
    """Выбирает MP4-файл 9:16 с шириной от 1080 px (ближайший к 1080, иначе самый широкий)."""
    best = None
    best_key = None
    for f in video.get("video_files", []):
        if f.get("file_type") != "video/mp4":
            continue
        w, h = int(f.get("width") or 0), int(f.get("height") or 0)
        if not w or not h or h <= w:
            continue
        if not 1.5 <= h / w <= 2.1:  # 9:16 ≈ 1.78
            continue
        # ближайший ширина >= 1080 — лучший вариант; если нет — самый широкий
        key = (0, w - OUT_W) if w >= OUT_W else (1, -w)
        if best_key is None or key < best_key:
            best, best_key = f, key
    return best


def is_good_video(video: dict) -> bool:
    return float(video.get("duration") or 0) >= SRC_MIN_SEC and pick_file(video) is not None


def interleave(pools: list[tuple[str, list[dict]]]) -> list[tuple[str, dict]]:
    """Чередует запросы по кругу, без дублей видео."""
    seen: set[int] = set()
    result: list[tuple[str, dict]] = []
    depth = max((len(g) for _, g in pools), default=0)
    for i in range(depth):
        for query, good in pools:
            if i < len(good):
                vid = good[i].get("id")
                if vid is not None and vid not in seen:
                    seen.add(vid)
                    result.append((query, good[i]))
    return result


def download(url: str, dst: Path) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(dst, "wb") as out:
            shutil.copyfileobj(resp, out)
        return dst.stat().st_size > 0
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log(f"   ! загрузка не удалась: {e}")
        dst.unlink(missing_ok=True)
        return False


def make_background(ffmpeg: str, src: Path, dst: Path, duration: float, fps: int) -> bool:
    """Приводит клип к 1080x1920, нужному fps, 10–15 с, без звука."""
    clip = min(CLIP_MAX_SEC, duration)
    vf = (
        f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={OUT_W}:{OUT_H},setsar=1,fps={fps}"
    )
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-t", f"{clip:.2f}",
        "-map", "0:v:0", "-an",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0 or not dst.exists():
        log(f"   ! ffmpeg не справился: {(proc.stderr or '').strip()[-200:]}")
        return False
    return verify(ffmpeg, dst, fps)


def verify(ffmpeg: str, path: Path, fps: int) -> bool:
    """Проверяет размер, наличие видео и отсутствие звука в готовом файле."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8",
    )
    info = proc.stderr or ""
    size = re.search(r"Video:[^\n]*?\b(\d{3,5})x(\d{3,5})\b", info)
    fps_m = re.search(r"(\d+(?:\.\d+)?)\s*fps", info)
    dur_m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", info)

    ok = bool(size) and (int(size[1]), int(size[2])) == (OUT_W, OUT_H)
    ok = ok and "Audio:" not in info
    if dur_m:
        secs = int(dur_m[1]) * 3600 + int(dur_m[2]) * 60 + float(dur_m[3])
        ok = ok and 9.5 <= secs <= 15.5
    if not ok:
        log(f"   ! проверка не пройдена для {path.name}")
    elif fps_m and abs(float(fps_m[1]) - fps) > 0.5:
        log(f"   ~ предупреждение: fps файла {fps_m[1]}, ожидалось {fps}")
    return ok


def load_manifest(path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return []


def save_manifest(path: Path, items: list[dict]) -> None:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Скачать и подготовить 9:16 видеофоны для Reels (Pexels).")
    ap.add_argument("--count", type=int, default=15, help="сколько фонов нужно (по умолчанию 15)")
    ap.add_argument("--out", default="aesthetic_backgrounds", help="папка для результата")
    ap.add_argument("--fps", type=int, choices=[30, 60], default=30, help="частота кадров")
    ap.add_argument("--per-query", type=int, default=40,
                    help="сколько результатов поиска смотреть на один запрос (макс. 80)")
    args = ap.parse_args()

    ffmpeg = require_deps()
    api_key = os.environ[API_KEY_ENV]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / MANIFEST_NAME
    manifest = load_manifest(manifest_path)
    used_ids = {m["pexels_id"] for m in manifest}

    slots = [n for n in range(1, args.count + 1)
             if not (out / f"bg_{n:02d}.mp4").exists()]
    if not slots:
        log(f"Все {args.count} фонов уже есть в {out}/")
        return 0

    log(f"Нужно создать фонов: {len(slots)} → {out}/")
    log("Источник: Pexels (лицензия Pexels License)")

    # 1. Поиск и фильтрация
    pools: list[tuple[str, list[dict]]] = []
    for q in QUERIES:
        log(f"\n🔎 {q}")
        videos = pexels_search(q, min(max(args.per_query, 1), 80), api_key)
        good = [v for v in videos
                if v.get("id") is not None and v["id"] not in used_ids and is_good_video(v)]
        good.sort(key=lambda v: float(v.get("duration") or 0), reverse=True)
        log(f"   найдено: {len(videos)}, подходящих: {len(good)}")
        pools.append((q, good))

    candidates = interleave(pools)
    log(f"\nКандидатов всего: {len(candidates)}")

    # 2. Скачивание и обработка
    tmp = Path(tempfile.mkdtemp(prefix=".tmp_bg_", dir=out))
    try:
        for query, video in candidates:
            if not slots:
                break
            vid = video["id"]
            duration = float(video.get("duration") or 0)
            file_info = pick_file(video)
            if file_info is None:
                continue
            user = video.get("user") or {}
            log(f"\n⬇ [pexels {vid}] {user.get('name', '')} ({duration:.0f} с, "
                f"{file_info.get('width')}x{file_info.get('height')})")

            src = tmp / f"pexels_{vid}.mp4"
            if not download(file_info["link"], src):
                continue

            slot = slots[0]
            dst = out / f"bg_{slot:02d}.mp4"
            ok = make_background(ffmpeg, src, dst, duration, args.fps)
            src.unlink(missing_ok=True)
            if not ok:
                dst.unlink(missing_ok=True)
                continue

            slots.pop(0)
            manifest.append({
                "file": dst.name,
                "pexels_id": vid,
                "url": video.get("url", ""),
                "author": user.get("name", ""),
                "author_url": user.get("url", ""),
                "license": "Pexels License",
                "source_duration_sec": duration,
                "query": query,
            })
            save_manifest(manifest_path, manifest)
            log(f"✅ {dst.name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    log(f"\nГотово. Всего фонов в {out}/: {args.count - len(slots)} из {args.count}.")
    if slots:
        if SEARCH_ERRORS and not candidates:
            log("Поиск в Pexels не работает. Проверьте ключ PEXELS_API_KEY и соединение.")
        else:
            log("Недостаточно подходящих видео. Попробуйте --per-query 80 или расширьте QUERIES.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
