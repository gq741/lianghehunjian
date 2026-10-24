"""Isolated local-only ASR worker. Parent owns timeout and cancellation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    source, model_path, output = Path(args.source), Path(args.model), Path(args.output)
    if (not source.is_absolute() or not source.is_file() or not model_path.is_absolute()
            or not all((model_path / n).is_file()
                       for n in ("model.bin", "config.json", "tokenizer.json"))
            or not output.is_absolute() or output.exists()):
        print("ASR_INVALID_INPUT", file=sys.stderr)
        return 2
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(
            str(model_path), device=args.device,
            compute_type="int8" if args.device == "cpu" else "float16",
            cpu_threads=min(4, os.cpu_count() or 1), num_workers=1,
            local_files_only=True,
        )
        # Word timestamps improve internal utterance timing, but are not a
        # guaranteed forced-alignment measurement. User correction remains needed.
        segments, _ = model.transcribe(
            str(source), beam_size=3, vad_filter=True, word_timestamps=True,
            condition_on_previous_text=False,
        )
        result = []
        for segment in segments:
            if segment.text.strip() and segment.end > segment.start:
                result.append({"start": round(segment.start, 3),
                               "end": round(segment.end, 3),
                               "text": segment.text.strip()})
            if len(result) > 10000:
                raise ValueError("too_many_segments")
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False)
        print("ASR_LOCAL_COMPLETE")
        return 0
    except ImportError:
        print("ASR_DEPENDENCY_MISSING", file=sys.stderr)
        return 3
    except Exception:
        # Never expose transcript/model environment diagnostics in the UI.
        print("ASR_LOCAL_FAILED", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
