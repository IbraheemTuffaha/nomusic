"""Generate an original audiovisual fixture for controlled browser checks.

Harmonic pulses and a generated video pattern exercise media plumbing, chunk
boundaries, and export timing. They are not a speech-quality evaluation corpus.
Only the synthesized samples are deterministic across FFmpeg versions; the
manifest records the actual encoded files rather than promising fixed hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from pathlib import Path


DURATION = 12.0
SAMPLE_RATE = 44100
FIXTURE_ID = "nomusic_e2e_fixture"
FIXTURE_URL = f"https://www.youtube.com/watch?v={FIXTURE_ID}"


def generate(output: Path, duration: float = DURATION) -> None:
    import numpy as np
    import soundfile as sf

    if not math.isfinite(duration) or not 1 <= duration <= 180:
        raise ValueError("Fixture duration must be between 1 and 180 seconds")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Fixture output must be an empty directory; choose a fresh --output")

    # Bound synthesis working memory even for the three-minute playback run.
    frames = round(duration * SAMPLE_RATE)
    phase_offset = 0.0
    with sf.SoundFile(output / "soundtrack.wav", "w", samplerate=SAMPLE_RATE,
                      channels=2, subtype="PCM_16") as soundtrack:
        for first in range(0, frames, 10 * SAMPLE_RATE):
            t = np.arange(first, min(first + 10 * SAMPLE_RATE, frames), dtype=np.float64) / SAMPLE_RATE
            # Generated harmonics and accompaniment, not recognizable speech.
            pitch = 155 + 25 * np.sin(2 * np.pi * 0.65 * t)
            phase = phase_offset + 2 * np.pi * np.cumsum(pitch) / SAMPLE_RATE
            phase_offset = phase[-1]
            envelope = np.sin(np.pi * np.mod(t * 3.3, 1.0)) ** 2
            envelope *= (np.mod(t, 2.8) < 2.2).astype(np.float64)
            voice = sum(np.sin(n * phase) / n for n in range(1, 9))
            accompaniment = 0.06 * np.sin(2 * np.pi * 110 * t)
            accompaniment += 0.04 * np.sin(2 * np.pi * 330 * t)
            mono = 0.18 * envelope * voice + accompaniment
            fade = np.minimum(1.0, np.minimum(t / 0.04, (duration - t) / 0.04))
            stereo = np.column_stack((mono, 0.98 * mono)) * fade[:, None]
            soundtrack.write(stereo.astype(np.float32))

    video = (f"testsrc2=size=640x360:rate=24:duration={duration}" if duration <= DURATION
             else f"color=c=0x243447:size=640x360:rate=2:duration={duration}")

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-f", "lavfi", "-i", video,
            "-i", "soundtrack.wav", "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-t", str(duration), "-movflags", "+faststart", "clip.mp4",
        ],
        cwd=output, check=True, timeout=60,
    )
    probe = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", "clip.mp4"],
        cwd=output, text=True, timeout=15,
    ))
    if abs(float(probe["format"]["duration"]) - duration) > 0.05:
        raise RuntimeError("Generated fixture has an unexpected duration")
    if {s["codec_type"] for s in probe["streams"]} != {"audio", "video"}:
        raise RuntimeError("Generated fixture must contain both audio and video")
    hashes = {}
    for name in ("soundtrack.wav", "clip.mp4"):
        with (output / name).open("rb") as source:
            hashes[name] = hashlib.file_digest(source, "sha256").hexdigest()
    manifest = {
        "fixture_url": FIXTURE_URL,
        "duration_seconds": duration,
        "description": "Original generated video and harmonic pulses; no real speech",
        "sha256": hashes,
        "ffprobe": probe,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New or empty fixture directory")
    parser.add_argument("--duration", type=int, default=DURATION, help="Whole seconds, 1–180 (default: 12)")
    args = parser.parse_args()
    try:
        generate(args.output.resolve(), args.duration)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Fixture generation failed: {exc}\n")
    print(json.dumps({"fixture": str(args.output.resolve() / "clip.mp4"), "duration_seconds": args.duration}))


if __name__ == "__main__":
    main()
