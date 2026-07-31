import os
import glob
import shutil
import tempfile
import argparse
import subprocess
import concurrent.futures
import threading
import signal
import sys
from typing import List, Optional


# --- ГЛОБАЛЬНЫЙ РУБИЛЬНИК (ДЛЯ CTRL+C) ---
KILL_SWITCH = False
active_processes = []
process_lock = threading.Lock()
print_lock = threading.Lock()


def signal_handler(sig, frame):
    global KILL_SWITCH
    if not KILL_SWITCH:
        with print_lock:
            print("\n 🛑 [СИСТЕМА] Получен сигнал прерывания (Ctrl+C)")
        KILL_SWITCH = True
        with process_lock:
            for p in active_processes:
                try:
                    p.terminate()
                except:
                    pass
        sys.exit(1)

signal.signal(signal.SIGINT, signal_handler)


def tprint(msg: str):
    if not KILL_SWITCH:
        with print_lock:
            print(msg)


def run_ffmpeg(cmd: List[str]) -> bool:
    if KILL_SWITCH: return False
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with process_lock:
            active_processes.append(p)
        p.wait()
        with process_lock:
            if p in active_processes:
                active_processes.remove(p)
        return p.returncode == 0
    except Exception:
        return False


def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def create_fast_prepass(input_path: str, temp_path: str, ultra: bool = False, speed_factor: float = 1.0, 
                        mode: str = "default", use_gpu: bool = False, reverse: bool = False, nuke: bool = False,
                        ss: Optional[str] = None, to: Optional[str] = None) -> bool:
    if mode == "smooth":
        target_fps = 30; target_width = 720
    elif ultra:
        target_fps = 5; target_width = 480
    else:
        target_fps = 15; target_width = 720
    
    pts_factor = 1.0 / speed_factor
    vf_filter = f"setpts={pts_factor}*PTS,fps={target_fps},scale='min({target_width},iw)':-2"
    
    if nuke: vf_filter += ",eq=contrast=1.5:saturation=3,unsharp=7:7:2.5"
    if reverse: vf_filter += ",reverse"
    
    cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error"]
    
    # Флаги обрезки ставим ДО инпута для молниеносного поиска (Fast Seek)
    if ss: cmd.extend(["-ss", str(ss)])
    if to: cmd.extend(["-to", str(to)])
    
    cmd.extend(["-i", input_path, "-vf", vf_filter, "-pix_fmt", "yuv420p", "-an"])

    if use_gpu:
        cmd.extend(["-c:v", "h264_nvenc", "-preset", "p2", "-cq", "28"])
    else:
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"])

    cmd.append(temp_path)
    return run_ffmpeg(cmd)

def compress_single_file(input_path: str, output_dir: str, target_size_mb: float = 0.0, ultra: bool = False, 
                         speed: float = 1.0, mode: str = "smooth", use_gpu: bool = False, reverse: bool = False, nuke: bool = False,
                         ss: Optional[str] = None, to: Optional[str] = None):
    if KILL_SWITCH: return

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    
    name_parts = []
    if ss or to: name_parts.append("cut")
    if ultra: name_parts.append("ultra")
    if mode != "default": name_parts.append(mode)
    if nuke: name_parts.append("nuke")
    if reverse: name_parts.append("rev")
    if speed != 1.0: name_parts.append(f"{speed}x")
        
    suffix = "_" + "_".join(name_parts) if name_parts else ""
    output_path = os.path.join(output_dir, f"{base_name}{suffix}.gif")
    
    initial_size = os.path.getsize(input_path) / (1024 * 1024)

    has_effects = speed != 1.0 or mode != "default" or ultra or reverse or nuke or ss or to
        
        # Пропускаем, если это уже GIF, нет эффектов, и размер либо в лимите, либо лимит отключен (0.0)
    if input_path.lower().endswith(".gif") and not has_effects:
        if target_size_mb <= 0.0 or initial_size <= target_size_mb:
            tprint(f" ⏩ [{base_name}] Пропуск: Файл уже GIF и не требует обработки ({initial_size:.2f} МБ)")
            return

    limit_str = f"{target_size_mb:.1f} МБ" if target_size_mb > 0 else "Без лимита (Макс. качество)"
    tprint(f"\n🎬 [{base_name}] Обработка -> Лимит: {limit_str}")
    
    is_video = not input_path.lower().endswith(".gif")
    
    if mode == "emote":
        use_prepass = False
    else:
        use_prepass = ultra or initial_size > 50.0 or is_video or has_effects

    actual_input = input_path
    temp_prepass = None

    try:
        if use_prepass:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
                temp_prepass = tf.name
            
            tprint(f" 🚀 [{base_name}] Быстрый рендер прокси-файла H.264...")
            if create_fast_prepass(input_path, temp_prepass, ultra=ultra, speed_factor=speed, mode=mode, use_gpu=use_gpu, reverse=reverse, nuke=nuke, ss=ss, to=to):
                actual_input = temp_prepass
            else:
                if not KILL_SWITCH:
                    tprint(f" ⚠️ [{base_name}] Пре-пасс не удался, пробуем напрямую из исходника...")

        if mode == "smooth":
            steps = [
                {"fps": 30, "width": 480, "colors": 128, "dither": "none"},
                {"fps": 24, "width": 360, "colors": 64,  "dither": "none"},
                {"fps": 20, "width": 320, "colors": 64,  "dither": "none"},
                {"fps": 15, "width": 240, "colors": 32,  "dither": "none"},
                {"fps": 12, "width": 200, "colors": 16,  "dither": "none"},
            ]
        elif mode == "emote":
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
            if KILL_SWITCH: break
            
            fps, width, colors = step["fps"], step["width"], step["colors"]
            dither = step.get("dither", "none")
            scale_algo = step.get("scale_algo", "lanczos")
            alpha = step.get("alpha", 128)
            
            tprint(f" ⚙️ [{base_name}] Шаг {idx}: {fps} FPS, {width}px, {colors} цветов...")
            
            fun_str, speed_str = "", ""
            cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error"]
            
            if not use_prepass:
                if ss: cmd.extend(["-ss", str(ss)])
                if to: cmd.extend(["-to", str(to)])
                if speed != 1.0: speed_str = f"setpts={1.0/speed}*PTS,"
                if nuke: fun_str += "eq=contrast=1.5:saturation=3,unsharp=7:7:2.5,"
                if reverse: fun_str += "reverse,"
            
            cmd.extend(["-i", actual_input])
            
            vf_filter = (
                f"{speed_str}{fun_str}fps={fps},scale='min({width},iw)':-2:flags={scale_algo},"
                f"split[s0][s1];[s0]palettegen=max_colors={colors}:reserve_transparent=1[p];"
                f"[s1][p]paletteuse=dither={dither}:alpha_threshold={alpha}"
            )

            cmd.extend(["-vf", vf_filter, output_path])

            if not run_ffmpeg(cmd) and not KILL_SWITCH:
                tprint(f" ❌ [{base_name}] Ошибка кодирования на шаге {idx}")
                continue

            if not KILL_SWITCH and os.path.exists(output_path):
                new_size = os.path.getsize(output_path) / (1024 * 1024)
                
                if target_size_mb <= 0.0 or new_size <= target_size_mb:
                    tprint(f" ✅ [{base_name}] УСПЕХ: Сохранено ({new_size:.2f} МБ) -> {os.path.basename(output_path)}")
                    success = True
                    break
        
        if not success and not KILL_SWITCH:
            final_size = os.path.getsize(output_path) / (1024 * 1024) if os.path.exists(output_path) else 0.0
            tprint(f" ⚠️ [{base_name}] Достигнуто дно настроек. Итоговый размер: {final_size:.2f} МБ")

    finally:
        if temp_prepass and os.path.exists(temp_prepass):
            try: os.remove(temp_prepass)
            except: pass


def main():
    parser = argparse.ArgumentParser(
        description="GIF FACTORY v4.7",
        formatter_class=argparse.RawTextHelpFormatter
    )

    parser.add_argument("path", nargs="?", default=".", help="Путь к файлу или папке")
    parser.add_argument("-compress", "-s", "--target-size", type=float, default=0.0, help="Лимит в МБ")
    parser.add_argument("-ultra", "-u", action="store_true", help="Ultra-сжатие (очень длинные фильмы)")
    parser.add_argument("-mode", "-m", choices=["default", "smooth", "emote"], default="smooth", 
                        help="Метод: default (баланс), smooth (GD/геймплей), emote (микро-пиксели)")
    parser.add_argument("-speed", type=float, default=1.0, help="Фактор скорости (10.0 = в 10 раз быстрее)")
    
    # Тримминг
    parser.add_argument("-ss", type=str, default=None, help="✂️ Старт обрезки (напр. '00:01:23' или '83')")
    parser.add_argument("-to", type=str, default=None, help="✂️ Конец обрезки (напр. '00:01:30' или '90')")

    # Мемные параметры
    parser.add_argument("-reverse", "-rv", action="store_true", help="⏪ Воспроизведение задом наперед")
    parser.add_argument("-nuke", action="store_true", help="☢️ Эффект 'Deepfry' (выжженные цвета, перешакал)")
    
    parser.add_argument("-o", "--output", default="GIFs", help="Папка выгрузки")
    parser.add_argument("-gpu", action="store_true", help="Включить NVENC (видеокарта NVIDIA) для пре-пасса тяжелых видео")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2, help="Количество потоков")

    args = parser.parse_args()
    
    if not check_ffmpeg():
        print(" ❌ Ошибка: FFmpeg не найден в системе. Добавь его в PATH.")
        return

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
    print("GIF FACTORY v4.4")
    print("=" * 65)
    print(f" 📂 Сканирование: {source_dir}")
    print(f" 🎯 Целевой лимит: {args.target_size} МБ")
    print(f" 🕹️ Метод сжатия: {args.mode.upper()}")
    if args.ss or args.to:
        print(f" ✂️  Обрезка:       [{args.ss or 'СТАРТ'} -> {args.to or 'КОНЕЦ'}]")
    print(f" ⏱️  Модификатор:   {args.speed}x скорости")
    print(f" ⏪ Реверс:        {'ВКЛЮЧЕН' if args.reverse else 'ВЫКЛЮЧЕН'}")
    print(f" ☢️  Deepfry:       {'ВКЛЮЧЕН' if args.nuke else 'ВЫКЛЮЧЕН'}")
    print(f" 🖥️  Ускорение GPU: {'ВКЛЮЧЕНО (NVENC)' if args.gpu else 'ВЫКЛЮЧЕНО (CPU)'}")
    print(f" 🚀 Потоков:       {args.jobs}")
    print(f" 📑 Найдено файлов: {len(media_files)}")
    print("=" * 65)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = [
            executor.submit(compress_single_file, media, out_dir, args.target_size, args.ultra, args.speed, args.mode, args.gpu, args.reverse, args.nuke, args.ss, args.to)
            for media in media_files
        ]
        while not KILL_SWITCH:
            done, not_done = concurrent.futures.wait(futures, timeout=0.5)
            if not not_done:
                break

    if not KILL_SWITCH:
        print("\n" + "=" * 65)
        print("Готово!")


if __name__ == '__main__':
    main()