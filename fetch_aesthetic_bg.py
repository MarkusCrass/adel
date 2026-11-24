#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_aesthetic_bg.py — автоматический поиск, скачивание и подготовка
вертикальных эстетичных видеофонов для Reels (астрология / женский ресурс).

Что делает:
  1. Ищет короткие вертикальные ролики (#shorts) по английским запросам.
  2. Отбирает кандидатов: длительность 10–60 с источника, кадр 9:16,
     по умолчанию только лицензия Creative Commons Attribution (reuse allowed).
  3. Скачивает их через `python -m yt_dlp`.
  4. Обрабатывает через ffmpeg (из пакета imageio-ffmpeg):
       • масштаб + кроп до точных 1080x1920;
       • 30 или 60 fps;
       • 10–15 с (длиннее обрезается, из источника берётся начало);
       • звуковая дорожка удаляется (-an).
  5. Сохраняет результат как bg_01.mp4 … bg_15.mp4 и ведёт манифест
     sources.json (ссылка, автор, лицензия — нужны для атрибуции CC BY).

Установка зависимостей:
    pip install yt-dlp imageio-ffmpeg

Запуск:
    python fetch_aesthetic_bg.py
    python fetch_aesthetic_bg.py --count 15 --fps 60 --out aesthetic_backgrounds
    python fetch_aesthetic_bg.py --any-license      # снять фильтр лицензий (см. ниже)

Повторный запуск безопасен: уже готовые bg_NN.mp4 и уже использованные
видео из sources.json пропускаются.

ВАЖНО о правах:
  • Фильтр по CC BY — значение по умолчанию. Ролики под обычной
    лицензией YouTube скачивать и публиковать как свой фон нельзя.
  • CC BY требует указать автора. Используйте sources.json для титров.
  • Скрипт не проверяет наличие текста и водяных знаков на кадре
    автоматически — просмотрите готовые файлы перед публикацией.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Поисковые запросы (английские) + суффикс #shorts добавляется при поиске.
QUERIES = [
    "aesthetic full moon night sky vertical no text",
    "aesthetic candle cozy evening rain vertical",
    "aesthetic morning coffee sunlight calm vertical",
    "aesthetic ocean waves night moonlight vertical",
]

CC_LICENSE = "Creative Commons Attribution license (reuse allowed)"
SRC_MIN_SEC = 10.0   # минимальная длительность источника
SRC_MAX_SEC = 60.0   # Shorts не длиннее 60 с
CLIP_MAX_SEC = 15.0  # итоговый фон: не длиннее 15 с (нижняя граница 10 с)
OUT_W, OUT_H = 1080, 1920
MANIFEST_NAME = "sources.json"
SEARCH_ERRORS: list[str] = []  # ошибки поиска (сеть, yt-dlp)


def log(msg: str) -> None:
    print(msg, flush=True)


def require_deps() -> str:
    """Проверяет зависимости и возвращает путь к бинарнику ffmpeg."""
    try:
        import imageio_ffmpeg
    except ImportError:
        sys.exit("Не найден imageio-ffmpeg. Установите: pip install imageio-ffmpeg yt-dlp")

    probe = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--version"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        sys.exit("Не найден yt-dlp для этого интерпретатора. Установите: pip install yt-dlp")

    return imageio_ffmpeg.get_ffmpeg_exe()


def run_yt(args: list[str], timeout: int) -> subprocess.CompletedProcess | None:
    """Запуск yt-dlp с таймаутом, чтобы скрипт не зависал на сетевых запросах."""
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--ignore-errors", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        log("   ! yt-dlp не ответил за отведённое время, пропускаю")
        return None


def parse_json_lines(text: str) -> list[dict]:
    items: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def search_flat(query: str, n: int) -> list[dict]:
    """Быстрый поиск: только id, название и длительность (без полной загрузки каждого ролика)."""
    proc = run_yt(["--flat-playlist", "--dump-json", f"ytsearch{n}:{query} #shorts"], timeout=180)
    if proc is None:
        return []
    items = parse_json_lines(proc.stdout)
    if proc.returncode != 0 and not items:
        tail = (proc.stderr or "").strip()[-300:]
        log(f"   ! поиск завершился с ошибкой: {tail}")
        SEARCH_ERRORS.append(tail)
    return items


def fetch_info(video_id: str) -> dict | None:
    """Полные метаданные одного ролика: размеры, лицензия."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    proc = run_yt(["--skip-download", "--dump-json", "--no-playlist", url], timeout=90)
    if proc is None:
        return None
    items = parse_json_lines(proc.stdout)
    return items[0] if items else None


def is_short_enough(entry: dict) -> bool:
    """Предварительный фильтр по длительности из быстрого поиска (если известна)."""
    dur = entry.get("duration")
    return dur is None or SRC_MIN_SEC <= float(dur) <= SRC_MAX_SEC


def is_good_candidate(info: dict, any_license: bool) -> bool:
    """Фильтр: длительность, вертикальный кадр, лицензия."""
    if info.get("is_live") or info.get("was_live"):
        return False

    dur = float(info.get("duration") or 0)
    if not SRC_MIN_SEC <= dur <= SRC_MAX_SEC:
        return False

    w = int(info.get("width") or 0)
    h = int(info.get("height") or 0)
    if not w or not h:
        return False
    if not 1.5 <= h / w <= 2.1:  # 9:16 ≈ 1.78
        return False

    if not any_license and info.get("license") != CC_LICENSE:
        return False

    return True


def quality_key(info: dict) -> tuple:
    """Сортировка «лучших»: сначала Full HD+, затем по просмотрам."""
    return (int(info.get("height") or 0) >= 1080, int(info.get("view_count") or 0))


def interleave(pools: list[tuple[str, list[dict]]]) -> list[tuple[str, dict]]:
    """Чередует запросы (по одному ролику из каждого по кругу), без дублей."""
    seen: set[str] = set()
    result: list[tuple[str, dict]] = []
    depth = max((len(g) for _, g in pools), default=0)
    for i in range(depth):
        for query, good in pools:
            if i < len(good):
                vid = good[i].get("id")
                if vid and vid not in seen:
                    seen.add(vid)
                    result.append((query, good[i]))
    return result


def download(video_id: str, tmp: Path) -> Path | None:
    """Скачивает видеодорожку (звук нам не нужен) во временную папку."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    proc = run_yt([
        "--no-playlist",
        "-f", "bv*[ext=mp4][height>=1080]/bv*[ext=mp4]/bv*",
        "-o", str(tmp / "%(id)s.%(ext)s"),
        url,
    ], timeout=900)
    if proc is None:
        return None
    files = [p for p in tmp.glob(f"{video_id}.*") if p.suffix not in (".part", ".ytdl")]
    if proc.returncode != 0 or not files:
        tail = (proc.stderr or "").strip()[-200:]
        log(f"   ! загрузка не удалась: {tail}")
        return None
    return files[0]


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
    ap = argparse.ArgumentParser(description="Скачать и подготовить 9:16 видеофоны для Reels.")
    ap.add_argument("--count", type=int, default=15, help="сколько фонов нужно (по умолчанию 15)")
    ap.add_argument("--out", default="aesthetic_backgrounds", help="папка для результата")
    ap.add_argument("--fps", type=int, choices=[30, 60], default=30, help="частота кадров")
    ap.add_argument("--per-query", type=int, default=30,
                    help="сколько результатов поиска смотреть на один запрос")
    ap.add_argument("--check-per-query", type=int, default=15,
                    help="сколько подходящих по длительности роликов на запрос проверять подробно")
    ap.add_argument("--any-license", action="store_true",
                    help="не фильтровать по CC BY (скачивать и обычные ролики YouTube — "
                         "проверьте права сами)")
    args = ap.parse_args()

    ffmpeg = require_deps()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / MANIFEST_NAME
    manifest = load_manifest(manifest_path)
    used_ids = {m["video_id"] for m in manifest}

    slots = [n for n in range(1, args.count + 1)
             if not (out / f"bg_{n:02d}.mp4").exists()]
    if not slots:
        log(f"Все {args.count} фонов уже есть в {out}/")
        return 0

    log(f"Нужно создать фонов: {len(slots)} → {out}/")
    log(f"Фильтр лицензии: {'отключён (--any-license)' if args.any_license else 'Creative Commons BY'}")

    # 1. Поиск и фильтрация
    pools: list[tuple[str, list[dict]]] = []
    for q in QUERIES:
        log(f"\n🔎 {q}")
        entries = search_flat(q, args.per_query)
        pre = [e for e in entries
               if e.get("id") and e["id"] not in used_ids and is_short_enough(e)]
        to_check = pre[:args.check_per_query]
        log(f"   найдено: {len(entries)}, по длительности подходят: {len(pre)}, "
            f"проверяю полные данные: {len(to_check)}")
        good: list[dict] = []
        for k, entry in enumerate(to_check, 1):
            info = fetch_info(entry["id"])
            if info and is_good_candidate(info, args.any_license):
                good.append(info)
                log(f"   [{k}/{len(to_check)}] ✓ {(info.get('title') or '')[:60]}")
            else:
                log(f"   [{k}/{len(to_check)}] — не подходит")
        good.sort(key=quality_key, reverse=True)
        log(f"   подходящих: {len(good)}")
        pools.append((q, good))

    candidates = interleave(pools)
    log(f"\nКандидатов всего: {len(candidates)}")

    # 2. Скачивание и обработка
    tmp = Path(tempfile.mkdtemp(prefix=".tmp_bg_", dir=out))
    try:
        for query, info in candidates:
            if not slots:
                break
            vid = info["id"]
            title = (info.get("title") or "").strip()
            duration = float(info.get("duration") or 0)
            log(f"\n⬇ [{vid}] {title[:70]} ({duration:.0f} с)")

            src = download(vid, tmp)
            if src is None:
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
                "video_id": vid,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "title": title,
                "channel": info.get("channel") or info.get("uploader") or "",
                "license": info.get("license") or "",
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
            log("Поиск на YouTube не работает (ошибка сети или устаревший yt-dlp). "
                "Проверьте соединение и выполните: python -m pip install -U yt-dlp")
        else:
            log("Недостаточно подходящих роликов. Попробуйте --per-query 60, "
                "--any-license (с проверкой прав) или расширьте QUERIES.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
