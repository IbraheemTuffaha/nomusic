"""Generate an original 12-second audiovisual fixture for the controlled smoke.

Harmonic pulses and a generated video pattern exercise media plumbing, chunk
boundaries, and export timing. They are not a speech-quality evaluation corpus.
Only the synthesized samples are deterministic across FFmpeg versions; the
manifest records the actual encoded files rather than promising fixed hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


DURATION = 12.0
SAMPLE_RATE = 44100
FIXTURE_ID = "nomusic_e2e_fixture"
FIXTURE_URL = f"https://www.youtube.com/watch?v={FIXTURE_ID}"


def generate(output: Path) -> None:
    import numpy as np
    import soundfile as sf

    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Fixture output must be an empty directory; choose a fresh --output")

    t = np.arange(round(DURATION * SAMPLE_RATE), dtype=np.float64) / SAMPLE_RATE
    # Changing harmonics with syllable-shaped envelopes plus accompaniment.
    # These are generated signals, not recordings or recognizable speech.
    pitch = 155 + 25 * np.sin(2 * np.pi * 0.65 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / SAMPLE_RATE
    envelope = np.sin(np.pi * np.mod(t * 3.3, 1.0)) ** 2
    envelope *= (np.mod(t, 2.8) < 2.2).astype(np.float64)
    voice = sum(np.sin(n * phase) / n for n in range(1, 9))
    accompaniment = 0.06 * np.sin(2 * np.pi * 110 * t)
    accompaniment += 0.04 * np.sin(2 * np.pi * 330 * t)
    mono = 0.18 * envelope * voice + accompaniment
    fade = np.minimum(1.0, np.minimum(t / 0.04, (DURATION - t) / 0.04))
    stereo = np.column_stack((mono, 0.98 * mono)) * fade[:, None]
    sf.write(output / "soundtrack.wav", stereo.astype(np.float32), SAMPLE_RATE, subtype="PCM_16")

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=12",
            "-i", "soundtrack.wav", "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-t", "12", "-movflags", "+faststart", "clip.mp4",
        ],
        cwd=output, check=True, timeout=60,
    )
    probe = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", "clip.mp4"],
        cwd=output, text=True, timeout=15,
    ))
    if abs(float(probe["format"]["duration"]) - DURATION) > 0.05:
        raise RuntimeError("Generated fixture has an unexpected duration")
    if {s["codec_type"] for s in probe["streams"]} != {"audio", "video"}:
        raise RuntimeError("Generated fixture must contain both audio and video")
    hashes = {}
    for name in ("soundtrack.wav", "clip.mp4"):
        with (output / name).open("rb") as source:
            hashes[name] = hashlib.file_digest(source, "sha256").hexdigest()
    manifest = {
        "fixture_url": FIXTURE_URL,
        "duration_seconds": DURATION,
        "description": "Original generated test pattern and harmonic pulses; no real speech",
        "sha256": hashes,
        "ffprobe": probe,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New or empty fixture directory")
    args = parser.parse_args()
    try:
        generate(args.output.resolve())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Fixture generation failed: {exc}\n")
    print(json.dumps({"fixture": str(args.output.resolve() / "clip.mp4"), "duration_seconds": DURATION}))


if __name__ == "__main__":
    main()
