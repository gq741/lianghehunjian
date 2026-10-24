"""Isolated offline word timestamp worker, sharing the installed ASR model."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    for name in ('source', 'model', 'output', 'text'):
        parser.add_argument('--'+name, required=name=='model')
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(args.model, device=args.device,
            compute_type='int8' if args.device == 'cpu' else 'float16',
            cpu_threads=min(4, os.cpu_count() or 1), num_workers=1, local_files_only=True)
        if args.probe:
            print('VOICE_MODEL_READY');return 0
        # Recognize independently. Supplying the entire expected copy as a
        # decoder prompt can make short Chinese utterances hallucinate instead
        # of transcribing. The parent maps measured words to confirmed copy.
        segments, _ = model.transcribe(args.source, language='zh', beam_size=3,
            vad_filter=True, word_timestamps=True,
            condition_on_previous_text=False)
        words = []
        for segment in segments:
            for word in segment.words or []:
                words.append({'word': word.word, 'start': float(word.start), 'end': float(word.end)})
                if len(words) > 20000:
                    raise ValueError('too_many_words')
        with Path(args.output).open('x', encoding='utf-8') as stream:
            json.dump(words, stream, ensure_ascii=False, allow_nan=False)
        print('VOICE_ALIGNMENT_COMPLETE')
        return 0
    except Exception:
        print('VOICE_ALIGNMENT_FAILED')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
