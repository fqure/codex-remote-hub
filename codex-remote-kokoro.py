#!/usr/bin/env python3
"""Generate speech audio with Kokoro and write it to a WAV file."""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate TTS audio with Kokoro")
    parser.add_argument("--text", required=True, help="Text to synthesize")
    parser.add_argument("--output", required=True, help="Target WAV file path")
    parser.add_argument("--voice", default="af_heart", help="Kokoro voice id")
    parser.add_argument("--lang-code", default="a", help="Kokoro language code")
    parser.add_argument("--speed", default="1.0", help="Speech speed multiplier")
    args = parser.parse_args()

    try:
        from kokoro import KPipeline
        import numpy as np
        import soundfile as sf
    except Exception as exc:  # pragma: no cover - runtime dependency check
        print(f"Missing Kokoro runtime dependency: {exc}", file=sys.stderr)
        return 2

    try:
        speed = float(args.speed)
    except ValueError:
        print(f"Invalid speed: {args.speed}", file=sys.stderr)
        return 2

    try:
        pipeline = KPipeline(lang_code=args.lang_code)
        segments = []
        for _, _, audio in pipeline(args.text, voice=args.voice, speed=speed):
            segments.append(audio)
        if not segments:
            print("Kokoro did not return audio", file=sys.stderr)
            return 1
        merged = np.concatenate(segments)
        sf.write(args.output, merged, 24000)
    except Exception as exc:  # pragma: no cover - tool surface
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
