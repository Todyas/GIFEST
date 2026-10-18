import os
import glob
import shutil
import textwrap
import tempfile
import argparse
import subprocess
import concurrent.futures
import threading
import signal
import sys
from typing import List, Optional


# Обработка выключения утилиты
KILL_SWITCH = False
active_processes = []
process_lock = threading.Lock()
print_lock = threading.Lock()


def signal_handler(sig, frame):
    global KILL_SWITCH
    if not KILL_SWITCH:
        with print_lock:
            print("\n 🛑    [СИСТЕМА] Получен сигнал прерывания (Ctrl+C)")
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


# Авто-режим сжатия (-c auto)
AUTO_TARGET = -1.0           # Значение target_size_mb, означающее "подобрать лимит автоматически"
AUTO_MIN_MB = 1.0            # Короткие клипы не жмём сильнее этого
AUTO_LINEAR_SEC = 30.0       # До этой длины лимит растёт линейно, дальше — как корень (длинное жмём сильнее)
AUTO_EMOTE_MB = 0.25         # Эмодзи: 256 КБ
AUTO_UNKNOWN_MB = 10.0       # Если длительность узнать не удалось
AUTO_MB_PER_SEC = {"smooth": 0.6, "default": 0.4, "ultra": 0.15}


def parse_compress_arg(value: str) -> float:
    if value.strip().lower() in ("auto", "a"):
        return AUTO_TARGET
    try:
        return float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается число МБ или 'auto', получено: {value!r}")


def parse_time_to_seconds(t: Optional[str]) -> Optional[float]:
    if not t: return None
    try:
        seconds = 0.0
        for part in str(t).strip().split(":"):
            seconds = seconds * 60 + float(part)
        return seconds
    except ValueError:
        return None


def probe_duration(path: str) -> Optional[float]:
    if shutil.which("ffprobe") is None: return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30
        ).stdout.strip()
        return float(out) if out and out != "N/A" else None
    except Exception:
        return None


def probe_video_width(path: str) -> Optional[int]:
    if shutil.which("ffprobe") is None: return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30
        ).stdout.strip().splitlines()
        return int(out[0]) if out and out[0].isdigit() else None
    except Exception:
        return None


def auto_target_size(input_path: str, gif_mode: str, ultra: bool, speed: float,
                     ss: Optional[str], to: Optional[str]) -> tuple:
    """Подбирает адекватный лимит в МБ по итоговой длительности гифки (с учётом обрезки и скорости).
    Возвращает (лимит_МБ, длительность_сек или None)."""
    if gif_mode == "emote":
        return AUTO_EMOTE_MB, None

    duration = probe_duration(input_path)
    if duration is None:
        return AUTO_UNKNOWN_MB, None

    start = parse_time_to_seconds(ss) or 0.0
    end = parse_time_to_seconds(to)
    end = min(end, duration) if end is not None else duration
    duration = max(end - start, 0.1) / max(speed, 0.01)

    rate = AUTO_MB_PER_SEC["ultra" if ultra else gif_mode]
    if duration <= AUTO_LINEAR_SEC:
        target = duration * rate
    else:
        target = AUTO_LINEAR_SEC * rate * (duration / AUTO_LINEAR_SEC) ** 0.5
    target = max(target, AUTO_MIN_MB)
    return round(target, 2), duration


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


try:
    from PIL import ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# Константы вёрстки текста (единая система, чтобы перенос строк и расчёт
# отступов под текст всегда были синхронизированы друг с другом).
# ВАЖНО: межстрочный интервал мы больше НЕ отдаём на откуп встроенным метрикам
# шрифта (ffmpeg drawtext берёт line-gap из самого файла шрифта, а у многих
# жирных TTF он неадекватно большой) — вместо этого каждая строка рисуется
# отдельным drawtext с шагом по Y, который считаем сами по этим константам.
LINE_HEIGHT_FACTOR = 1.2     # "рабочая" высота одной строки (с запасом под выносные элементы) относительно fontsize
LINE_SPACING_FACTOR = 0.12   # доп. интервал МЕЖДУ строками, доля от fontsize (масштабируется вместе с текстом)
TOP_MARGIN_FACTOR = 0.9      # отступ НАД текстовым блоком относительно fontsize (сделан просторнее)
BOTTOM_MARGIN_FACTOR = 0.45  # отступ ПОД текстовым блоком относительно fontsize

_FONT_CACHE = {}
_FONT_WARNED = set()

FALLBACK_FONTS = ["DejaVu Sans Bold", "Liberation Sans Bold", "Arial Bold", "Verdana Bold", "DejaVu Sans"]

DEFAULT_FONT = "Impact"
DEMOTIVATOR_FONT = "Times New Roman"   # Классический шрифт демотиваторов

FONT_FILE_ALIASES = {
    "timesnewroman": ["times.ttf", "Times New Roman.ttf", "LiberationSerif-Regular.ttf", "DejaVuSerif.ttf"],
    "timesnewromanbold": ["timesbd.ttf", "Times New Roman Bold.ttf", "LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf"],
    "arialbold": ["arialbd.ttf"],
    "verdanabold": ["verdanab.ttf"],
}

COLOR_NAME_RGB = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0), "green": (0, 128, 0),
    "blue": (0, 0, 255), "yellow": (255, 255, 0), "gray": (128, 128, 128), "grey": (128, 128, 128),
    "orange": (255, 165, 0), "purple": (128, 0, 128), "pink": (255, 192, 203),
    "cyan": (0, 255, 255), "magenta": (255, 0, 255), "brown": (165, 42, 42),
}


def parse_color_to_rgb(color: str) -> Optional[tuple]:
    """Пытается распознать RGB для имени цвета или hex-строки (#rrggbb / 0xrrggbb)."""
    if not color: return None
    c = color.strip().lower()
    if c.startswith("0x"): c = "#" + c[2:]
    if c.startswith("#"):
        h = c.lstrip("#")
        if len(h) == 6:
            try:
                return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                return None
        return None
    return COLOR_NAME_RGB.get(c)


def relative_luminance(rgb: tuple) -> float:
    r, g, b = [x / 255.0 for x in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def is_light_color(color: str, default_is_light: bool = False) -> bool:
    rgb = parse_color_to_rgb(color)
    if rgb is None:
        return default_is_light
    return relative_luminance(rgb) > 0.55


def contrasting_bw(color: str, default_is_light: bool = False) -> str:
    """Возвращает 'black' или 'white' — что бы дало максимальный контраст с color."""
    return "black" if is_light_color(color, default_is_light) else "white"


def resolve_font_path(font: str) -> Optional[str]:
    """Находит реальный файл шрифта по имени/пути. Возвращает None, если не нашли —
    тогда используем font=<имя> и полагаемся на fontconfig внутри ffmpeg (менее надёжно)."""
    if not font:
        return None
    if font in _FONT_CACHE:
        return _FONT_CACHE[font]

    result = None

    if os.path.isfile(font):
        result = os.path.abspath(font)
    else:
        # Пробуем fontconfig (есть почти везде на Linux/macOS, даёт точный путь к файлу)
        try:
            proc = subprocess.run(
                ["fc-match", "--format=%{file}", font],
                capture_output=True, text=True, timeout=3
            )
            candidate = proc.stdout.strip()
            if candidate and os.path.isfile(candidate):
                result = candidate
        except Exception:
            pass

        if not result:
            # Ручной обход стандартных каталогов шрифтов на случай отсутствия fontconfig
            search_dirs = []
            if sys.platform.startswith("win"):
                search_dirs.append(os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts"))
            elif sys.platform == "darwin":
                search_dirs += ["/System/Library/Fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
            else:
                search_dirs += ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts")]

            name_key = "".join(font.lower().split())
            # Имена файлов, которые не совпадают с названием шрифта (Windows: times.ttf и т.п.)
            for alias in FONT_FILE_ALIASES.get(name_key, []):
                for d in search_dirs:
                    if os.path.isfile(os.path.join(d, alias)):
                        result = os.path.join(d, alias)
                        break
                if result: break

            for d in ([] if result else search_dirs):
                if not os.path.isdir(d): continue
                for root, _, filenames in os.walk(d):
                    for fn in filenames:
                        if fn.lower().endswith((".ttf", ".otf")) and name_key in "".join(fn.lower().split()):
                            result = os.path.join(root, fn)
                            break
                    if result: break
                if result: break

    _FONT_CACHE[font] = result
    return result


def resolve_font_with_fallback(font: str, base_name: str) -> Optional[str]:
    """Ищет запрошенный шрифт, а если не находит — перебирает набор надёжных резервных шрифтов."""
    path = resolve_font_path(font)
    if path:
        return path

    for fb in FALLBACK_FONTS:
        path = resolve_font_path(fb)
        if path:
            if font not in _FONT_WARNED:
                _FONT_WARNED.add(font)
                tprint(f" ⚠️ [{base_name}] Шрифт '{font}' не найден в системе, использую запасной: {fb}")
            return path

    if font not in _FONT_WARNED:
        _FONT_WARNED.add(font)
        tprint(f" ⚠️ [{base_name}] Не удалось найти файл шрифта для '{font}' — рассчитываю на поиск fontconfig внутри ffmpeg (может дать нечитаемый/дефолтный шрифт)")
    return None


def measure_text_width(text: str, font_path: Optional[str], font_size: float) -> float:
    """Реальная ширина текста в пикселях (через Pillow), либо приближённая оценка."""
    if PIL_AVAILABLE and font_path:
        try:
            key = (font_path, int(round(font_size)))
            font_obj = _FONT_CACHE.get(("pil", key))
            if font_obj is None:
                font_obj = ImageFont.truetype(font_path, max(int(round(font_size)), 1))
                _FONT_CACHE[("pil", key)] = font_obj
            bbox = font_obj.getbbox(text)
            return bbox[2] - bbox[0]
        except Exception:
            pass
    # Приближённая оценка, если Pillow недоступен или шрифт не резолвится
    return len(text) * font_size * 0.55


def wrap_text_precise(text: str, font_path: Optional[str], font_size: float, max_width: float) -> List[str]:
    """Разбивает строку на строки так, чтобы каждая физически помещалась в max_width,
    используя реальные метрики шрифта (если доступен Pillow), а не оценку по числу символов."""
    words = text.split()
    if not words:
        return [""]

    lines: List[str] = []
    current = ""

    for word in words:
        # Слово само по себе шире доступной ширины — рубим его по символам
        if measure_text_width(word, font_path, font_size) > max_width and len(word) > 1:
            if current:
                lines.append(current)
                current = ""
            chunk = ""
            for ch in word:
                trial = chunk + ch
                if measure_text_width(trial, font_path, font_size) <= max_width or not chunk:
                    chunk = trial
                else:
                    lines.append(chunk)
                    chunk = ch
            current = chunk
            continue

        trial = f"{current} {word}".strip()
        if measure_text_width(trial, font_path, font_size) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word

    if current:
        lines.append(current)
    return lines


def wrap_text_lines(text: str, font_size: float, max_width: float,
                     font_path: Optional[str] = None) -> List[str]:
    """Считает перенос строк по РЕАЛЬНЫМ метрикам шрифта. Возвращает список готовых
    строк (без записи в файл — под каждую строку файл создаётся отдельно на этапе
    построения фильтров, см. build_line_drawtext_filters)."""
    if not text:
        return []

    text = text.replace('\\n', '\n')
    usable_width = max(max_width * 0.9, 10)

    result_lines: List[str] = []
    for raw_line in text.split('\n'):
        if raw_line.strip() == "":
            result_lines.append("")
        else:
            result_lines.extend(wrap_text_precise(raw_line, font_path, font_size, usable_width))
    return result_lines


def compute_text_block(font_size: float, line_count: int) -> tuple:
    """Возвращает (высота_блока_px, шаг_между_строками_px). Шаг между строками мы
    считаем сами (а не полагаемся на встроенный line-gap шрифта, который у многих
    жирных TTF огромный и непредсказуемый) — поэтому итоговый интервал стабилен
    и одинаков независимо от того, какой шрифт в итоге подставится."""
    if line_count <= 0:
        return 0, 0
    single_line_h = font_size * LINE_HEIGHT_FACTOR
    pitch = single_line_h + font_size * LINE_SPACING_FACTOR
    block_h = (line_count - 1) * pitch + single_line_h
    return int(round(block_h)), int(round(pitch))


def compute_margins(font_size: float) -> tuple:
    """Возвращает (отступ_сверху_px, отступ_снизу_px) для текстового блока."""
    return int(round(font_size * TOP_MARGIN_FACTOR)), int(round(font_size * BOTTOM_MARGIN_FACTOR))


def build_line_drawtext_filters(lines: List[str], font_param: str, font_size: float, color: str,
                                 line_pitch: int, y_top_expr: str,
                                 border_color: Optional[str] = None, extra: str = "") -> tuple:
    """Строит цепочку из одного drawtext НА КАЖДУЮ строку с явным Y = y_top_expr + i*line_pitch.
    Так интервал между строками не зависит от "родного" line-gap шрифта — только от
    line_pitch, который мы сами вычислили в compute_text_block(). Возвращает
    (список_фильтров, список_временных_файлов_для_очистки)."""
    filters: List[str] = []
    temp_files: List[str] = []
    border_part = f":bordercolor={border_color}:borderw=3" if border_color else ""

    for i, line in enumerate(lines):
        tf = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', delete=False)
        tf.write(line)
        tf.close()
        temp_files.append(tf.name)

        ff_path = tf.name.replace('\\', '/').replace(':', '\\:')
        y_expr = f"{y_top_expr}+{i * line_pitch}" if i > 0 else y_top_expr
        filters.append(
            f"drawtext=textfile='{ff_path}':{font_param}:fontcolor={color}:fontsize={font_size}:"
            f"x=(w-text_w)/2:y={y_expr}{border_part}{extra}"
        )

    return filters, temp_files


def even(n: int) -> int:
    """Округляет вверх до чётного числа (немного подстраховки для видео-фильтров)."""
    return n if n % 2 == 0 else n + 1


def compress_single_file(
    input_path: str, output_dir: str, target_size_mb: float = 0.0, ultra: bool = False, 
    speed: float = 1.0, gif_mode: str = "smooth", use_gpu: bool = False, reverse: bool = False, nuke: bool = False,
    ss: Optional[str] = None, to: Optional[str] = None,
    upper_text: Optional[str] = None, bottom_text: Optional[str] = None,
    text_layout: str = "pad", bg_color: str = "black", text_color: str = "auto", font: Optional[str] = None
):
    if not font:
        font = DEMOTIVATOR_FONT if text_layout == "demotivator" else DEFAULT_FONT
    if KILL_SWITCH: return

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    
    has_text = bool(upper_text or bottom_text or text_layout == "demotivator")
    has_effects = speed != 1.0 or gif_mode != "smooth" or ultra or reverse or nuke or ss or to or has_text
    
    name_parts = []
    if ss or to: name_parts.append("cut")
    if has_text: name_parts.append("meme")
    if ultra: name_parts.append("ultra")
    if gif_mode != "smooth": name_parts.append(gif_mode)
    if nuke: name_parts.append("nuke")
    if reverse: name_parts.append("rev")
    if speed != 1.0: name_parts.append(f"{speed}x")

    suffix = "_" + "_".join(name_parts) if name_parts else ""
    output_path = os.path.join(output_dir, f"{base_name}{suffix}.gif")
    
    initial_size = os.path.getsize(input_path) / (1024 * 1024)

    auto_info = ""
    if target_size_mb == AUTO_TARGET:
        target_size_mb, auto_duration = auto_target_size(input_path, gif_mode, ultra, speed, ss, to)
        auto_info = f" (авто, {auto_duration:.1f} сек)" if auto_duration is not None else " (авто)"

        # Пропускаем, если это уже GIF, нет эффектов, и размер либо в лимите, либо лимит отключен (0.0)
    if input_path.lower().endswith(".gif") and not has_effects:
        if target_size_mb <= 0.0 or initial_size <= target_size_mb:
            tprint(f" ⏩    [{base_name}] Пропуск: Файл уже GIF и не требует обработки ({initial_size:.2f} МБ)")
            return

    limit_str = f"{target_size_mb:.2f} МБ{auto_info}" if target_size_mb > 0 else "Без лимита"
    tprint(f"\n 🎬   [{base_name}] Обработка -> Лимит: {limit_str}")
    
    is_video = not input_path.lower().endswith(".gif")
    use_prepass = False if gif_mode == "emote" else (ultra or initial_size > 50.0 or is_video or has_effects)

    actual_input = input_path
    temp_prepass = None


    try:
        if use_prepass:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
                temp_prepass = tf.name
            
            tprint(f" 🚀   [{base_name}] Быстрый рендер прокси-файла H.264...")
            if create_fast_prepass(input_path, temp_prepass, ultra=ultra, speed_factor=speed, mode=gif_mode, use_gpu=use_gpu, reverse=reverse, nuke=nuke, ss=ss, to=to):
                actual_input = temp_prepass
            else:
                if not KILL_SWITCH:
                    tprint(f" ⚠️ [{base_name}] Пре-пасс не удался, пробуем напрямую из исходника...")

        if gif_mode == "smooth":
            steps = [
                {"fps": 30, "width": 540, "colors": 256, "dither": "none"},
                {"fps": 30, "width": 480, "colors": 128, "dither": "none"},
                {"fps": 24, "width": 480, "colors": 128, "dither": "none"},
                {"fps": 24, "width": 380, "colors": 96,  "dither": "none"},
                {"fps": 20, "width": 360, "colors": 64,  "dither": "none"},
                {"fps": 18, "width": 320, "colors": 64,  "dither": "none"},
                {"fps": 15, "width": 280, "colors": 48,  "dither": "none"},
                {"fps": 12, "width": 240, "colors": 32,  "dither": "none"},
            ]
        elif gif_mode == "emote":
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
            ]


        # Подготовка шрифта: резолвим имя/путь в реальный файл (fontfile надёжнее, чем
        # font=<имя>, который зависит от того, собран ли ffmpeg с libfontconfig)
        resolved_font_path = resolve_font_with_fallback(font, base_name)
        if resolved_font_path:
            ff_font_path = resolved_font_path.replace('\\', '/').replace(':', '\\:')
            font_param = f"fontfile='{ff_font_path}'"
        else:
            font_param = f"font='{font}'"

        # Автоконтраст: если text_color не задан явно ("auto"), подбираем его так,
        # чтобы он гарантированно читался на фоне bg_color. Обводка всегда берётся
        # противоположным цветом от итогового текста — это и решает проблему
        # "чёрный текст на белом фоне становится нечитаемым".
        effective_text_color = contrasting_bw(bg_color, default_is_light=False) if text_color == "auto" else text_color
        border_color = contrasting_bw(effective_text_color, default_is_light=False)

        # Реальная ширина входного кадра: scale='min(width,iw)' не растягивает маленькие видео,
        # поэтому вся вёрстка текста должна считаться от фактической ширины, а не от width шага
        src_width = probe_video_width(actual_input) if has_text else None

        success = False

        for idx, step in enumerate(steps, 1):
            if KILL_SWITCH: break
            
            fps, width, colors = step["fps"], step["width"], step["colors"]
            dither = step.get("dither", "none")
            scale_algo = step.get("scale_algo", "lanczos")
            alpha = step.get("alpha", 128)
            
            tprint(f" ⚙️    [{base_name}] Шаг {idx}: {fps} FPS, {width}px, {colors} цветов...")
            
            # Динамическая обработка текста для видео
            temp_text_files = []
            text_filters = []

            if has_text:
                frame_w = even(min(width, src_width)) if src_width else width

                if text_layout == "demotivator":
                    # Классическая рамка: картинка -> чёрный зазор -> тонкая белая линия -> чёрное поле
                    gap = max(int(frame_w * 0.012), 2)
                    line = max(int(frame_w * 0.006), 2)
                    side_margin = even(int(frame_w * 0.1))
                    top_frame_margin = even(int(frame_w * 0.1))
                    demot_w = frame_w + 2 * (gap + line) + 2 * side_margin

                    fs_ut = max(demot_w * 0.065, 12)
                    fs_bt = max(demot_w * 0.035, 10)

                    ut_lines = wrap_text_lines(upper_text, fs_ut, demot_w, resolved_font_path)
                    bt_lines = wrap_text_lines(bottom_text, fs_bt, demot_w, resolved_font_path)

                    # Область подписи считаем по фактическому числу строк и по нашему
                    # собственному шагу между ними (а не по проценту высоты кадра или
                    # встроенному line-gap шрифта) — иначе при коротком тексте остаётся
                    # пустой зазор под текстом, а при длинном текст обрезается/наезжает.
                    ut_block_h, ut_pitch = compute_text_block(fs_ut, len(ut_lines))
                    bt_block_h, bt_pitch = compute_text_block(fs_bt, len(bt_lines))
                    ut_top_m, ut_bot_m = compute_margins(fs_ut)
                    bt_top_m, bt_bot_m = compute_margins(fs_bt)

                    caption_area = ut_top_m + ut_block_h + (ut_bot_m if ut_lines else 0) + bt_top_m + bt_block_h + bt_bot_m
                    caption_area = even(max(caption_area, int(frame_w * 0.15)))

                    text_filters.append(f"pad=w=iw+{2 * gap}:h=ih+{2 * gap}:x={gap}:y={gap}:color=black")
                    text_filters.append(f"pad=w=iw+{2 * line}:h=ih+{2 * line}:x={line}:y={line}:color=white")
                    text_filters.append(
                        f"pad=w=iw+{2 * side_margin}:h=ih+{top_frame_margin}+{caption_area}:"
                        f"x={side_margin}:y={top_frame_margin}:color=black"
                    )

                    if ut_lines:
                        y_top = f"h-{caption_area}+{ut_top_m}"
                        filters, files = build_line_drawtext_filters(ut_lines, font_param, fs_ut, "white", ut_pitch, y_top)
                        text_filters.extend(filters)
                        temp_text_files.extend(files)
                    if bt_lines:
                        y_top = f"h-{bt_bot_m}-{bt_block_h}"
                        filters, files = build_line_drawtext_filters(bt_lines, font_param, fs_bt, "white", bt_pitch, y_top)
                        text_filters.extend(filters)
                        temp_text_files.extend(files)

                else:
                    fs_ut = max(frame_w * 0.07, 12)
                    fs_bt = max(frame_w * 0.07, 12)

                    ut_lines = wrap_text_lines(upper_text, fs_ut, frame_w, resolved_font_path)
                    bt_lines = wrap_text_lines(bottom_text, fs_bt, frame_w, resolved_font_path)

                    # Реальный размер отступа под текст считаем по числу строк и НАШЕМУ
                    # шагу между ними — вместо фиксированных ih*0.2, которые либо оставляли
                    # пустой зазор под короткой надписью, либо обрезали длинную.
                    ut_block_h, ut_pitch = compute_text_block(fs_ut, len(ut_lines))
                    bt_block_h, bt_pitch = compute_text_block(fs_bt, len(bt_lines))
                    ut_top_m, ut_bot_m = compute_margins(fs_ut)
                    bt_top_m, bt_bot_m = compute_margins(fs_bt)

                    pad_top = pad_bot = 0
                    if text_layout == "pad":
                        pad_top = even(ut_top_m + ut_block_h + ut_bot_m) if ut_lines else 0
                        pad_bot = even(bt_top_m + bt_block_h + bt_bot_m) if bt_lines else 0
                        text_filters.append(f"pad=w=iw:h=ih+{pad_top}+{pad_bot}:x=0:y={pad_top}:color={bg_color}")

                    box_part = f":box=1:boxcolor={border_color}@0.35:boxborderw=10" if text_layout == "overlay" else ""

                    if ut_lines:
                        y_top = str(ut_top_m) if text_layout == "pad" else f"h*0.05+{ut_top_m}"
                        filters, files = build_line_drawtext_filters(
                            ut_lines, font_param, fs_ut, effective_text_color, ut_pitch, y_top,
                            border_color=border_color, extra=box_part
                        )
                        text_filters.extend(filters)
                        temp_text_files.extend(files)

                    if bt_lines:
                        if text_layout == "pad":
                            y_top = f"h-{pad_bot}+{bt_top_m}"
                        else:
                            y_top = f"h-{bt_block_h}-{bt_bot_m}-h*0.05"
                        filters, files = build_line_drawtext_filters(
                            bt_lines, font_param, fs_bt, effective_text_color, bt_pitch, y_top,
                            border_color=border_color, extra=box_part
                        )
                        text_filters.extend(filters)
                        temp_text_files.extend(files)

            text_filter_str = (",".join(text_filters) + ",") if text_filters else ""

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
                f"{text_filter_str}"
                f"split[s0][s1];[s0]palettegen=max_colors={colors}:reserve_transparent=1[p];"
                f"[s1][p]paletteuse=dither={dither}:alpha_threshold={alpha}"
            )

            cmd.extend(["-vf", vf_filter, output_path])

            step_success = run_ffmpeg(cmd)

            # Чистим временные текстовые файлы текущего шага
            for tf in temp_text_files:
                if os.path.exists(tf):
                    try: os.remove(tf)
                    except: pass

            if not step_success and not KILL_SWITCH:
                tprint(f" ❌ [{base_name}] Ошибка кодирования на шаге {idx}.")
                continue

            if not KILL_SWITCH and os.path.exists(output_path):
                new_size = os.path.getsize(output_path) / (1024 * 1024)
                
                if target_size_mb <= 0.0 or new_size <= target_size_mb:
                    tprint(f" ✅   [{base_name}] УСПЕХ: Сохранено ({new_size:.2f} МБ) -> {os.path.basename(output_path)}")
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
        description="GIF FACTORY v5.3 - NOW WITH MEMES!",
        formatter_class=argparse.RawTextHelpFormatter
    )

    parser.add_argument("path", nargs="?", default=".", help="Путь к файлу или папке")
    parser.add_argument("--compress", "--target-size", "-c", dest="target_size", type=parse_compress_arg, default=0.0,
                        help="Лимит в МБ, или 'auto' — подобрать лимит по длине гифки\n"
                             f"(≈{AUTO_MB_PER_SEC['smooth']} МБ/сек в smooth, после {AUTO_LINEAR_SEC:g} сек растёт медленнее; emote — {AUTO_EMOTE_MB} МБ)")
    parser.add_argument("--ultra", "-u", action="store_true", help="Ultra-сжатие (очень длинные фильмы)")
    parser.add_argument("--mode", "-m", choices=["default", "smooth", "emote"], default="smooth", 
                        help="Метод: default (баланс), smooth (GD/геймплей), emote (микро-пиксели)")
    parser.add_argument("--speed", type=float, default=1.0, help="Фактор скорости (10.0 = в 10 раз быстрее)")
    
    # Тримминг
    parser.add_argument("-ss", type=str, default=None, help="✂️ Старт обрезки (напр. '00:01:23' или '83')")
    parser.add_argument("-to", type=str, default=None, help="✂️ Конец обрезки (напр. '00:01:30' или '90')")

    parser.add_argument("--reverse", "-rv", action="store_true", help="⏪ Воспроизведение задом наперед")
    parser.add_argument("--nuke", action="store_true", help="☢️ Эффект 'Deepfry'")
    
    # Текст и демотиваторы
    parser.add_argument("--upper-text", "-ut", type=str, default=None, help="Текст сверху (в демотиваторе — крупный заголовок)")
    parser.add_argument("--bottom-text", "-bt", type=str, default=None, help="Текст снизу (в демотиваторе — мелкий подзаголовок)")
    parser.add_argument("--text-layout", "-tl", choices=["overlay", "pad", "demotivator"], default="pad", 
                        help="Стиль текста: overlay (поверх), pad (расширить фон), demotivator (черная рамка)")
    parser.add_argument("--bg-color", "-bc", type=str, default="black", help="Цвет фона (для pad)")
    parser.add_argument("--text-color", "-tc", type=str, default="auto",
                        help="Цвет текста (для overlay/pad). 'auto' — подбирается автоматически "
                             "для контраста с --bg-color, чтобы текст не сливался с фоном")
    parser.add_argument("--font", "-f", type=str, default=None,
                        help=f"Шрифт: системное имя (ищется через fontconfig/системные каталоги) "
                             f"или полный путь к .ttf/.otf. По умолчанию {DEFAULT_FONT}, "
                             f"в демотиваторе — {DEMOTIVATOR_FONT}. При отсутствии — автоматический запасной шрифт")

    parser.add_argument("--output", "-o", default="GIFs", help="Папка выгрузки")
    parser.add_argument("--gpu", action="store_true", help="Включить NVENC (видеокарта NVIDIA)")
    parser.add_argument("--jobs", "-j", type=int, default=os.cpu_count() or 2, help="Количество потоков")

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
    print("GIF FACTORY v5.3 - NOW WITH MEMES!")
    print("=" * 65)
    print(f" 📂   Сканирование: {source_dir}")
    target_str = "АВТО (по длительности)" if args.target_size == AUTO_TARGET else (
        f"{args.target_size} МБ" if args.target_size > 0 else "Без лимита")
    print(f" 🎯   Целевой лимит: {target_str}")
    print(f" 🕹️    Метод сжатия: {args.mode.upper()}")
    if args.ss or args.to:
        print(f" ✂️   брезка: [{args.ss or 'СТАРТ'} -> {args.to or 'КОНЕЦ'}]")
    if args.upper_text or args.bottom_text or args.text_layout == "demotivator":
        print(f" 📝   Текст/Мемы: ВКЛЮЧЕНО (Режим: {args.text_layout})")
    print(f" 🖥️    Ускорение GPU: {'ВКЛЮЧЕНО (NVENC)' if args.gpu else 'ВЫКЛЮЧЕНО (CPU)'}")
    print(f" 📑   Найдено файлов: {len(media_files)}")
    print("=" * 65)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = [
            executor.submit(
                compress_single_file, media, out_dir, args.target_size, args.ultra, args.speed, args.mode, 
                args.gpu, args.reverse, args.nuke, args.ss, args.to,
                args.upper_text, args.bottom_text, args.text_layout, args.bg_color, args.text_color, args.font
            )
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