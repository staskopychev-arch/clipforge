import asyncio
import json
import logging
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import settings


class MediaError(RuntimeError):
    pass


log = logging.getLogger("clipforge.media")


def _resolve_tools(command: list[str]) -> list[str]:
    executable = {"ffmpeg": settings.ffmpeg_executable, "ffprobe": settings.ffprobe_executable}
    return [executable.get(command[0], command[0]), *command[1:]]


@dataclass(frozen=True)
class NormalizedMedia:
    path: Path
    duration_seconds: float
    width: int
    height: int
    speed_factor: float


def _run_media_command(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    command = _resolve_tools(command)
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    except FileNotFoundError as exc:
        raise MediaError(f"Required media tool is not installed: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{command[0]} exceeded the {timeout}s processing limit") from exc


def probe_duration(path: Path) -> float:
    if not path.is_file() or path.stat().st_size == 0:
        raise MediaError(f"Video file does not exist or is empty: {path}")
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = _run_media_command(command, timeout=30)
    if result.returncode != 0:
        raise MediaError(result.stderr.strip() or "ffprobe could not read the source video")
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration must be positive")
        return duration
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaError("Could not determine source duration") from exc


def probe_video_stream(path: Path) -> tuple[int, int, bool]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,width,height",
        "-of",
        "json",
        str(path),
    ]
    result = _run_media_command(command, timeout=30)
    if result.returncode != 0:
        raise MediaError(result.stderr.strip() or "ffprobe could not inspect video streams")
    try:
        streams = json.loads(result.stdout).get("streams", [])
        video = next(item for item in streams if item.get("codec_type") == "video")
        width, height = int(video["width"]), int(video["height"])
        has_audio = any(item.get("codec_type") == "audio" for item in streams)
    except (KeyError, TypeError, ValueError, StopIteration, json.JSONDecodeError) as exc:
        raise MediaError("Input does not contain a readable video stream") from exc
    if width <= 0 or height <= 0:
        raise MediaError("Input video dimensions are invalid")
    return width, height, has_audio


def validate_shorts_file(path: Path) -> dict:
    """Check actual media, not extension/title. YouTube decides classification."""
    duration = probe_duration(path)
    width, height, _ = probe_video_stream(path)
    if duration > 180:
        raise MediaError("Для Shorts нужен ролик не длиннее 180 секунд; сначала нарежьте исходник")
    if width > height:
        raise MediaError("Для Shorts нужен квадратный или вертикальный кадр; сначала создайте вертикальный клип")
    return {"duration": round(duration, 3), "width": width, "height": height, "shorts_eligible": True}


async def _run_async_media_command(command: list[str], timeout: int) -> tuple[int, str, str]:
    command = _resolve_tools(command)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise MediaError(f"Required media tool is not installed: {command[0]}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise MediaError(f"{command[0]} exceeded the {timeout}s processing limit") from exc
    return process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def normalize_for_validation(source: Path, destination: Path) -> NormalizedMedia:
    """Create a deterministic, metadata-free company-standard validation asset.

    The transform intentionally uses fixed values so identical QA inputs produce
    equivalent outputs and test runs remain reproducible.
    """
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise MediaError("Source and destination must be different files")
    if destination.suffix.lower() != ".mp4":
        raise MediaError("Normalized validation media must use the .mp4 extension")

    speed_factor = 1.03
    output_width, output_height = 1920, 1080
    source_duration = await asyncio.to_thread(probe_duration, source)
    _, _, has_audio = await asyncio.to_thread(probe_video_stream, source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.normalizing{destination.suffix}")
    temporary.unlink(missing_ok=True)

    video_filter = (
        "[0:v]"
        "crop=trunc(iw*0.98/2)*2:trunc(ih*0.98/2)*2:(iw-ow)/2:(ih-oh)/2,"
        f"scale={output_width}:{output_height}:force_original_aspect_ratio=increase,"
        f"crop={output_width}:{output_height},"
        f"setpts=PTS/{speed_factor}[outv]"
    )
    filters = video_filter
    if has_audio:
        filters += f";[0:a]atempo={speed_factor},aresample=48000[outa]"

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-filter_complex",
        filters,
        "-map",
        "[outv]",
    ]
    if has_audio:
        command.extend(["-map", "[outa]", "-c:a", "aac", "-b:a", "160k", "-shortest"])
    else:
        command.append("-an")
    command.extend(
        [
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-metadata",
            "title=",
            "-metadata",
            "comment=",
            "-r",
            "30",
            "-fps_mode",
            "cfr",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-b:v",
            "4500k",
            "-maxrate",
            "5000k",
            "-bufsize",
            "9000k",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
    )

    log.info("Normalizing validation media source=%s destination=%s", source.name, destination.name)
    timeout = max(300, math.ceil(source_duration * 15))
    try:
        return_code, _, stderr = await _run_async_media_command(command, timeout)
        if return_code != 0:
            raise MediaError(stderr.strip() or "FFmpeg normalization failed")
        output_duration = await asyncio.to_thread(probe_duration, temporary)
        width, height, _ = await asyncio.to_thread(probe_video_stream, temporary)
        expected_duration = source_duration / speed_factor
        if (width, height) != (output_width, output_height):
            raise MediaError(f"Unexpected output dimensions: {width}x{height}")
        if abs(output_duration - expected_duration) > max(1.0, expected_duration * 0.05):
            raise MediaError(
                f"Unexpected output duration: expected {expected_duration:.2f}s, got {output_duration:.2f}s"
            )
        temporary.replace(destination)
        log.info("Validation media normalized successfully destination=%s", destination.name)
        return NormalizedMedia(destination, output_duration, width, height, speed_factor)
    except Exception:
        temporary.unlink(missing_ok=True)
        log.exception("Validation media normalization failed source=%s", source.name)
        raise


def clip_start_times(total: float, count: int, clip_length: float) -> list[float]:
    if count < 1 or clip_length <= 0:
        raise MediaError("Clip count and duration must be positive")
    if total < clip_length:
        raise MediaError("Source video is shorter than the requested clip")
    if count == 1:
        return [max(0.0, (total - clip_length) / 2)]
    if total >= count * clip_length:
        gap = (total - count * clip_length) / (count + 1)
        return [round((index + 1) * gap + index * clip_length, 3) for index in range(count)]
    available = total - clip_length
    step = available / count
    return [round(min(available, (index + 0.5) * step), 3) for index in range(count)]


QUALITY_PRESETS = {'1080p': (1080, 1920, 21), '720p': (720, 1280, 23), '480p': (480, 854, 26)}


def prepare_music(source: Path, destination: Path) -> float:
    duration = probe_duration(source)
    if duration > 1800:
        raise MediaError('Музыка должна быть не длиннее 30 минут')
    command = ['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(source),
        '-vn','-map','0:a:0','-map_metadata','-1','-c:a','aac','-b:a','160k','-ar','48000','-ac','2',str(destination)]
    result = _run_media_command(command, timeout=180)
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise MediaError('Не удалось прочитать аудиодорожку. Загрузите MP3, WAV, M4A, AAC, OGG или FLAC.')
    return probe_duration(destination)


def render_vertical_clip(source: Path, destination: Path, start: float, duration: float,
                         *, speed=1.0, quality='1080p', music=None, presentation=None) -> None:
    if not all(math.isfinite(v) for v in (start, duration, speed)) or start < 0 or duration <= 0 or not 0.5 <= speed <= 2:
        raise MediaError('Некорректное начало, длина или скорость клипа')
    if quality not in QUALITY_PRESETS:
        raise MediaError('Выберите качество 1080p, 720p или 480p')
    total = probe_duration(source)
    if start + duration * speed > total + 0.25:
        raise MediaError('Выбранный фрагмент выходит за длину исходника')
    _, _, has_audio = probe_video_stream(source)
    width, height, crf = QUALITY_PRESETS[quality]
    if presentation is not None:
        from .auto_montage import Presentation
        presentation = Presentation.model_validate(presentation)
        if quality != '1080p':
            raise MediaError('Новый автомонтаж использует вертикальный формат 1080×1920')
        crf = presentation.crf
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f'.{destination.stem}.rendering{destination.suffix}')
    temporary.unlink(missing_ok=True)
    heading_file = destination.with_name(f'.{destination.stem}.heading.txt')
    transform = ''
    if presentation:
        p = presentation
        transform = (f'crop=trunc(iw/{p.zoom}/2)*2:trunc(ih/{p.zoom}/2)*2,'
            + ('hflip,' if p.mirror and p.text_check=='clear' else '')
            + f'eq=contrast={p.contrast}:brightness={p.brightness}:saturation={p.saturation},'
            + f'colorbalance=rs={p.warmth}:bs={-p.warmth},')
    filters = (
        f'[0:v]setpts=(PTS-STARTPTS)/{speed},{transform}split=2[background][foreground];'
        f'[background]scale={width}:{height}:force_original_aspect_ratio=increase,'
        f'crop={width}:{height},boxblur=24:12[blurred];'
        f'[foreground]scale={width}:{height}:force_original_aspect_ratio=decrease'
        + (',drawbox=x=0:y=0:w=iw:h=ih:color=white@0.35:t=2' if presentation else '') + '[fit];'
        '[blurred][fit]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p')
    if presentation:
        filters += f',noise=alls=1:allf=t+u:all_seed={presentation.grain_seed}'
        if presentation.heading:
            heading_file.write_text(presentation.heading,encoding='utf-8')
            path = str(heading_file.resolve()).replace('\\','/').replace(':','\\:').replace("'", "'\\''")
            filters += (f",drawtext=textfile='{path}':expansion=none:font=DejaVuSans:fontsize=44:"
                "fontcolor=white:box=1:boxcolor=black@0.38:boxborderw=20:"
                "x=(w-tw)/2:y=160:line_spacing=10:"
                "alpha='if(lt(t,0.25),t/0.25,if(lt(t,2.7),1,if(lt(t,3.2),(3.2-t)/0.5,0)))'")
    filters += '[outv]'
    command = ['ffmpeg','-hide_banner','-loglevel','error','-y',
        '-ss',str(start),'-t',str(duration * speed),'-i',str(source)]
    if music:
        command += ['-stream_loop','-1','-i',str(music)]
        filters += f';[1:a]atrim=duration={duration},asetpts=PTS-STARTPTS,aresample=48000'
    elif has_audio:
        if presentation:
            pitch = 2**(presentation.pitch_semitones/12)
            # Compensate pitch-rate duration so video and audio stay synchronized.
            filters += f';[0:a]aresample=48000,asetrate={48000*pitch:.6f},aresample=48000,atempo={speed/pitch:.8f},asetpts=PTS-STARTPTS'
        else:
            filters += f';[0:a]atempo={speed},asetpts=PTS-STARTPTS,aresample=48000'
    if music or has_audio:
        if presentation:
            filters += ',loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo[clean_audio]'
            filters += (f';anoisesrc=color=white:amplitude={presentation.audio_noise}:sample_rate=48000:'
                f'duration={duration}:seed={presentation.grain_seed},aformat=channel_layouts=stereo[air];'
                '[clean_audio][air]amix=inputs=2:duration=first:normalize=0,'
                f'afade=t=in:d=0.12,afade=t=out:st={max(0,duration-.25)}:d=0.25,'
                'alimiter=limit=0.95:level=0,apad[outa]')
        else:
            filters += '[outa]'
    command += ['-filter_complex',filters,'-map','[outv]']
    if music or has_audio:
        command += ['-map','[outa]','-c:a','aac','-b:a','160k']
    else:
        command += ['-an']
    command += ['-t',str(duration),'-r','30','-c:v','libx264','-preset','medium','-crf',str(crf),
        '-map_metadata','-1','-map_metadata:s','-1','-map_chapters','-1',
        '-metadata','title=','-metadata','comment=','-pix_fmt','yuv420p',
        '-movflags','+faststart',str(temporary)]
    try:
        result = _run_media_command(command, timeout=max(180, math.ceil(duration * 12)))
        if result.returncode != 0:
            raise MediaError(result.stderr.strip() or 'FFmpeg render failed')
        rendered_duration = probe_duration(temporary)
        if abs(rendered_duration - duration) > max(0.3, duration * 0.05):
            raise MediaError(f'Rendered clip duration is invalid: expected {duration:.2f}s, got {rendered_duration:.2f}s')
        actual_width, actual_height, _ = probe_video_stream(temporary)
        if (actual_width, actual_height) != (width, height):
            raise MediaError('Размер кадра после монтажа не совпадает с выбранным качеством')
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        heading_file.unlink(missing_ok=True)


def choose_chunk_plan(size: int) -> tuple[int, int]:
    if size <= 0:
        raise MediaError("Video file is empty")
    five_mb = 5 * 1024 * 1024
    ten_mb = 10 * 1024 * 1024
    if size < ten_mb:
        return size, 1
    count = max(1, math.floor(size / ten_mb))
    chunk_size = ten_mb
    # TikTok accepts a larger final chunk; count intentionally uses floor.
    if chunk_size < five_mb:
        chunk_size = five_mb
    return chunk_size, count

