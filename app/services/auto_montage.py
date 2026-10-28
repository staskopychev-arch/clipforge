"""Versioned automatic presentation choices, persisted once before encoding."""
import csv
import io
import math
import random
import subprocess
import tempfile
import textwrap
import unicodedata
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from ..config import settings
from .media import MediaError, _run_media_command


class Presentation(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    zoom: float = Field(ge=1.01, le=1.03)
    mirror: bool = False
    text_check: str = Field(pattern=r'^(text|clear|unknown)$')
    contrast: float = Field(ge=.97, le=1.04)
    saturation: float = Field(ge=.96, le=1.06)
    brightness: float = Field(ge=-.012, le=.012)
    warmth: float = Field(ge=-.015, le=.015)
    grain_seed: int = Field(ge=1, le=2147483647)
    pitch_semitones: float = Field(ge=-.12, le=.12)
    audio_noise: float = Field(ge=.01, le=.02)
    crf: int = Field(ge=20, le=22)
    heading: str = Field(max_length=100)


def title_card(title):
    # Render literal text via a UTF-8 file, never as FFmpeg expressions.
    clean = ' '.join(''.join(c for c in str(title or '') if not unicodedata.category(c).startswith('C')).split())
    clean = clean[:82]
    return '\n'.join(textwrap.wrap(clean, width=28, max_lines=3, placeholder='…'))


def detect_excerpt_text(source, start, source_duration):
    """Sample the whole chosen excerpt. Unavailable/uncertain OCR disables mirroring.

    OCR cannot prove that every frame is text-free; keep the check result in the
    job record, including failures, rather than claiming perfect detection.
    """
    count = max(1, math.ceil(source_duration * 2))
    try:
        with tempfile.TemporaryDirectory(prefix='clipforge-ocr-') as directory:
            root = Path(directory)
            result = _run_media_command(['ffmpeg','-hide_banner','-loglevel','error','-y',
                '-ss',str(start),'-t',str(source_duration),'-i',str(source),'-an',
                '-vf',"fps=2,scale=960:960:force_original_aspect_ratio=decrease",
                '-frames:v',str(count),str(root/'frame-%03d.png')],timeout=60)
            frames = sorted(root.glob('frame-*.png'))
            if result.returncode or not frames:
                return 'unknown'
            for frame in frames:
                result = subprocess.run(['tesseract',str(frame),'stdout','-l','eng+rus','--psm','11','tsv'],
                    capture_output=True, text=True, encoding='utf-8', errors='replace',timeout=5)
                if result.returncode:
                    return 'unknown'
                rows = csv.DictReader(io.StringIO(result.stdout),delimiter='\t')
                if not rows.fieldnames or 'conf' not in rows.fieldnames or 'text' not in rows.fieldnames:
                    return 'unknown'
                for row in rows:
                    word = (row.get('text') or '').strip()
                    if any(c.isalnum() for c in word) and float(row['conf']) >= 20:
                        return 'text'
            return 'clear'
    except (OSError, ValueError, subprocess.TimeoutExpired, MediaError):
        return 'unknown'


def resolve_presentation(source, resolved, title, *, rng=None):
    rng = rng or random.SystemRandom()
    check = detect_excerpt_text(source, resolved['start_seconds'],resolved['duration_seconds']*resolved['speed'])
    return Presentation(zoom=round(rng.uniform(1.01,1.03),4),mirror=check=='clear',text_check=check,
        contrast=round(rng.uniform(.97,1.04),4),saturation=round(rng.uniform(.96,1.06),4),
        brightness=round(rng.uniform(-.012,.012),4),warmth=round(rng.uniform(-.015,.015),4),
        grain_seed=rng.randint(1,2147483647),pitch_semitones=rng.choice([-.12,-.08,.08,.12]),
        audio_noise=round(rng.uniform(.01,.02),4),crf=rng.randint(20,22),heading=title_card(title)).model_dump()
