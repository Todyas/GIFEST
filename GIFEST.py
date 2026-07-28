import os
import glob
import shutil
import tempfile
import argparse
import subprocess
import concurrent.futures
from typing import List

try:
    from tqdm import tqdm
    HAVE_TQDM = True
except ImportError:
    HAVE_TQDM = False

def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None

def create_fast_prepass(input_path: str, temp_path: str, ultra: bool = False, speed_factor: float = 1.0, mode: str = "default") -> bool:
    if mode == "smooth":
        target_fps = 30
        target_width = 720
    elif ultra:
        target_fps = 5
        target_width = 480
    else:
        target_fps = 15
        target_width = 720
    
    pts_factor = 1.0 / speed_factor
    vf_filter = f"setpts={pts_factor}*PTS,fps={target_fps},scale='min({target_width},iw)':-2"
    
    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-i", input_path,
        "-vf", vf_filter,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-crf", "23",
        "-an",
        temp_path
    ]
    
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError:
        return False

def compress_single_file(input_path: str, output_dir: str, target_size_mb: float = 10.0, ultra: bool = False, speed: float = 1.0, mode: str = "default") -> str:
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    speed_suffix = f"_{speed}x" if speed != 1.0 else ""
    output_path = os.path.join(output_dir, f"{base_name}{speed_suffix}.gif")
    initial_size = os.path.getsize(input_path) / (1024 * 1024)

    logs: List[str] = []

    if input_path.lower().endswith(".gif") and initial_size <= target_size_mb and speed == 1.0:
        return f" ⏩ Пропуск: {os.path.basename(input_path)} ({initial_size:.2f} МБ <= {target_size_mb} МБ)"

    logs.append(f"🎬 Обработка: {os.path.basename(input_path)} ({initial_size:.2f} МБ) | Лимит: {target_size_mb:.1f} МБ")
    if speed != 1.0:
        logs.append(f" ⏱️  Модификатор скорости: {speed}x")

    is_video = not input_path.lower().endswith(".gif")
    
    # ВАЖНО: H.264 убивает прозрачность (альфа-канал). 
    # Если делаем смайлик - пре-пасс вырубаем жестко, иначе фон станет черным.
    if mode == "emote":
        use_prepass = False
    else:
        use_prepass = ultra or initial_size > 50.0 or is_video or speed != 1.0

    actual_input = input_path
    temp_prepass = None

    try:
        if use_prepass:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
                temp_prepass = tf.name
            
            logs.append(" 🚀 [Пре-пасс] Быстрый рендер H.264...")
            if create_fast_prepass(input_path, temp_prepass, ultra=ultra, speed_factor=speed, mode=mode):
                actual_input = temp_prepass
            else:
                logs.append(" ⚠️ Пре-пасс не удался, пробуем кодировать из исходника...")

        # Настраиваем шаги в зависимости от режима
        if mode == "smooth":
            steps = [
                {"fps": 30, "width": 480, "colors": 128, "dither": "none"},
                {"fps": 24, "width": 360, "colors": 64,  "dither": "none"},
                {"fps": 20, "width": 320, "colors": 64,  "dither": "none"},
                {"fps": 15, "width": 240, "colors": 32,  "dither": "none"},
                {"fps": 12, "width": 200, "colors": 16,  "dither": "none"},
            ]
        elif mode == "emote":
            # Режим для Discord/Twitch эмодзи: жесткие пиксели, малый размер, прозрачность
            steps = [
                {"fps": 20, "width": 128, "colors": 128, "dither": "none", "scale_algo": "neighbor", "alpha": 128},
                {"fps": 15, "width": 96,  "colors": 128, "dither": "none", "scale_algo": "neighbor", "alpha": 128},
                {"fps": 15, "width": 64,  "colors": 64,  "dither": "none", "scale_algo": "neighbor", "alpha": 128},
                {"fps": 12, "width": 48,  "colors": 64,  "dither": "none", "scale_algo": "neighbor", "alpha": 128},
                {"fps": 10, "width": 32,  "colors": 32,  "dither": "none", "scale_algo": "neighbor", "alpha": 128},
            ]
        elif ultra:
            steps = [
                {"fps": 3,   "width": 240, "colors": 128, "dither": "bayer:bayer_scale=5"},
                {"fps": 2,   "width": 160, "colors": 96,  "dither": "bayer:bayer_scale=5"},
                {"fps": 1,   "width": 128, "colors": 64,  "dither": "none"},
                {"fps": "1/2", "width": 128, "colors": 48,  "dither": "none"},
            ]
        else:
            steps = [
                {"fps": 15, "width": 640, "colors": 256, "dither": "bayer:bayer_scale=5"},
                {"fps": 12, "width": 500, "colors": 256, "dither": "bayer:bayer_scale=5"},
                {"fps": 10, "width": 450, "colors": 128, "dither": "bayer:bayer_scale=4"},
                {"fps": 8,  "width": 360, "colors": 128, "dither": "none"},
                {"fps": 6,  "width": 320, "colors": 64,  "dither": "none"},
            ]

        success = False

        for idx, step in enumerate(steps, 1):
            fps, width, colors = step["fps"], step["width"], step["colors"]
            dither = step.get("dither", "none")
            scale_algo = step.get("scale_algo", "lanczos")
            alpha = step.get("alpha", 128)
            
            speed_filter = f"setpts={1.0/speed}*PTS," if not use_prepass and speed != 1.0 else ""
            
            # Добавлена поддержка жестких пикселей (scale_algo) и обтравки альфа-канала (alpha_threshold)
            vf_filter = (
                f"{speed_filter}fps={fps},scale='min({width},iw)':-2:flags={scale_algo},"
                f"split[s0][s1];[s0]palettegen=max_colors={colors}:reserve_transparent=1[p];"
                f"[s1][p]paletteuse=dither={dither}:alpha_threshold={alpha}"
            )

            cmd = [
                "ffmpeg", "-y",
                "-loglevel", "error",
                "-i", actual_input,
                "-vf", vf_filter,
                output_path
            ]

            try:
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError:
                logs.append(f" ❌ Ошибка FFmpeg на попытке {idx}")
                continue

            if os.path.exists(output_path):
                new_size = os.path.getsize(output_path) / (1024 * 1024)
                if new_size <= target_size_mb:
                    logs.append(f" ✅ УСПЕХ (Шаг {idx}, FPS: {fps}, Алгоритм: {scale_algo}): Ужато до {new_size:.2f} МБ -> {os.path.basename(output_path)}")
                    success = True
                    break
        
        if not success:
            final_size = os.path.getsize(output_path) / (1024 * 1024) if os.path.exists(output_path) else 0.0
            logs.append(f" ⚠️ Достигнуты минимальные настройки. Итоговый размер: {final_size:.2f} МБ")

    finally:
        if temp_prepass and os.path.exists(temp_prepass):
            os.remove(temp_prepass)

    return str('\n'.join(logs))

def main():
    parser = argparse.ArgumentParser(
        description="🛠️ GIF FACTORY v4.2 — Универсальный многопоточный станок для GIF/видео.",
        formatter_class=argparse.RawTextHelpFormatter
    )

    parser.add_argument("path", nargs="?", default=".", help="Путь к файлу или папке")
    parser.add_argument("-compress", "-s", "--target-size", type=float, default=10.0, help="Лимит в МБ")
    parser.add_argument("-ultra", "-u", action="store_true", help="Ultra-сжатие (очень длинные фильмы)")
    parser.add_argument("-mode", "-m", choices=["default", "smooth", "emote"], default="default", 
                        help="Метод: default (баланс), smooth (высокий FPS), emote (смайлики Discord, пиксель-арт)")
    parser.add_argument("-o", "--output", default="GIFs", help="Папка выгрузки")
    parser.add_argument("-speed", type=float, default=1.0, help="Фактор скорости")
    parser.add_argument("-spdup", action="store_true", help="Алиас для ускорения 2x")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2, help="Количество потоков")

    args = parser.parse_args()
    
    if not check_ffmpeg():
        print(" ❌ Ошибка: FFmpeg не найден в системе. Добавь его в PATH.")
        return

    final_speed = 2.0 if args.spdup else args.speed
    target_path = args.path
    
    if os.path.isfile(target_path):
        source_dir = os.path.dirname(os.path.abspath(target_path)) or "."
        files = [os.path.abspath(target_path)]
    else:
        source_dir = os.path.abspath(target_path)
        files = glob.glob(os.path.join(source_dir, "*.*"))

    out_dir = os.path.join(source_dir, args.output)
    valid_exts = (".gif", ".mp4", ".mkv", ".avi", ".mov", ".webm", ".webp")
    
    media_files = [
        f for f in files 
        if f.lower().endswith(valid_exts) and os.path.dirname(os.path.abspath(f)) != os.path.abspath(out_dir)
    ]

    if not media_files:
        print(" ❌ Подходящие медиафайлы не найдены.")
        return

    os.makedirs(out_dir, exist_ok=True)

    print("=" * 65)
    print(" 🛠️  GIF FACTORY v4.2 — АВТОМАТИЧЕСКИЙ СТАНОК (МНОГОПОТОК)")
    print("=" * 65)
    print(f" 📂 Сканирование: {source_dir}")
    print(f" 🎯 Целевой лимит: {args.target_size} МБ")
    print(f" 🕹️  Метод сжатия: {args.mode.upper()}")
    print(f" ⏱️  Модификатор:   {final_speed}x скорости")
    print(f" 🚀 Потоков:       {args.jobs}")
    print(f" 📑 Найдено файлов: {len(media_files)}")
    print("=" * 65)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(compress_single_file, media, out_dir, args.target_size, args.ultra, final_speed, args.mode): media 
            for media in media_files
        }
        
        iterator = concurrent.futures.as_completed(futures)
        if HAVE_TQDM:
            iterator = tqdm(iterator, total=len(media_files), desc="Обработка", unit="файл")
            
        for future in iterator:
            result_log = future.result()
            if HAVE_TQDM:
                tqdm.write(result_log + '\n')
            else:
                print(result_log + '\n')

    print("=" * 65)
    print(" 🎉 Вся работа успешно завершена!")

if __name__ == '__main__':
    main()