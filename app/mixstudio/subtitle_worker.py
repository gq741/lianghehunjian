"""Offline OCR locator, using bundled RapidOCR models; no video is modified."""
import argparse
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input')
    parser.add_argument('--output')
    parser.add_argument('--probe',action='store_true')
    args=parser.parse_args()
    stage='missing_dependency'
    try:
        from rapidocr_onnxruntime import RapidOCR
        stage='model_load'
        engine=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=1)
        if args.probe:
            print('SUBTITLE_OCR_READY');return 0
        stage='recognition'
        images=json.loads(Path(args.input).read_text(encoding='utf-8'))
        results=[]
        for image in images:
            found,_=engine(image)
            results.append(found or [])
        with Path(args.output).open('x',encoding='utf-8') as out:
            json.dump(results,out,ensure_ascii=False,allow_nan=False)
        return 0
    except Exception as exc:
        code='out_of_memory' if isinstance(exc,MemoryError) or 'out of memory' in str(exc).lower() else stage
        if args.output:
            try:
                with Path(args.output).open('x',encoding='utf-8') as out:
                    json.dump({'error':{'code':code}},out)
            except OSError:pass
        print('SUBTITLE_OCR_FAILED:'+code);return 1


if __name__=='__main__':raise SystemExit(main())
