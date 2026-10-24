"""Per-used-window subtitle bands. Sparse OCR is advisory, never invisible repair."""
import math
import struct
import os
from pathlib import Path
import sys
import tempfile
import json
import time
import importlib.metadata
import importlib.util


VERSION = 2
NEGATIVE_TTL = 3600


def probe_fingerprint():
    """Dependency/model versions without importing or loading the OCR engine."""
    try:
        version=importlib.metadata.version('rapidocr_onnxruntime')
        spec=importlib.util.find_spec('rapidocr_onnxruntime')
        root=Path(spec.origin).parent if spec and spec.origin else None
        models=tuple((str(p.relative_to(root)),p.stat().st_size,p.stat().st_mtime_ns)
                     for p in sorted(root.rglob('*.onnx'))) if root else ()
        return version,models
    except (OSError,ValueError,ImportError,importlib.metadata.PackageNotFoundError):
        return 'unavailable',()


def caption_band(results,width,height):
    boxes=[]
    if not isinstance(results,list) or width<=0 or height<=0:return None
    for index,frame in enumerate(results):
        if not isinstance(frame,list):continue
        for item in frame:
            if not isinstance(item,list) or len(item)!=3:continue
            points,text,score=item
            try:
                if (not isinstance(text,str) or len(''.join(c for c in text if c.isalnum()))<4
                        or type(score) not in (int,float) or not math.isfinite(score) or not .6<=score<=1):continue
                xs,ys=zip(*points)
                left,right,top,bottom=min(xs),max(xs),min(ys),max(ys)
                if not all(math.isfinite(v) for v in (left,right,top,bottom)):continue
                if (top>=height*.5 and bottom<=height*.99 and 0<bottom-top<=height*.18
                        and 0<=left<right<=width
                        and left<width*.65 and right>width*.35 and right-left>width*.2):
                    boxes.append((index,left,right,top,bottom))
            except (TypeError,ValueError):continue
    if not boxes:return None
    # At least two samples must agree when several frames were sampled. Never
    # let a lone logo/parameter label widen the repair to the entire picture.
    groups=[]
    for seed in boxes:
        group=[b for b in boxes if abs(b[3]-seed[3])<=height*.045
               and min(b[2],seed[2])-max(b[1],seed[1])>min(b[2]-b[1],seed[2]-seed[1])*.5]
        if len({b[0] for b in group})>=min(2,len(results)):groups.append(group)
    if not groups:return None
    group=max(groups,key=lambda g:(len({b[0] for b in g}),sum(b[4] for b in g)/len(g)))
    # Adjacent, independently supported lines belong to the same caption block.
    # A distant product label or one-frame outlier is still excluded.
    for other in sorted(groups,key=lambda g:max(b[4] for b in g),reverse=True):
        top0,bottom0=min(b[3] for b in group),max(b[4] for b in group)
        top1,bottom1=min(b[3] for b in other),max(b[4] for b in other)
        if (max(top0,top1)-min(bottom0,bottom1)<=height*.05
                and max(bottom0,bottom1)-min(top0,top1)<=height*.35):
            group=list(dict.fromkeys(group+other))
    left=max(1.,min(b[1] for b in group)-width*.01)
    right=min(width-2.,max(b[2] for b in group)+width*.01)
    top=max(1.,min(b[3] for b in group)-height*.015)
    bottom=min(height-2.,max(b[4] for b in group)+height*.015)
    if bottom-top>height*.38:return None
    return {'top_pct':top/height*100,'height_pct':(bottom-top)/height*100,
            'left_pct':left/width*100,'width_pct':(right-left)/width*100}


def _valid_band(band):
    if not isinstance(band,dict) or set(band)!={'top_pct','height_pct','left_pct','width_pct'}:return False
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in band.values()):return False
    return (0<=band['top_pct']<99 and 0<band['height_pct']<=38
        and band['top_pct']+band['height_pct']<=99.9 and 0<=band['left_pct']<100
        and 0<band['width_pct']<=100 and band['left_pct']+band['width_pct']<=100)


def _failure(code):
    if not isinstance(code,str) or code not in {'missing_dependency','model_load','out_of_memory','timeout','invalid_output',
                    'frame_invalid','process_start','process_output','recognition'}:code='invalid_output'
    reason={'missing_dependency':'缺少RapidOCR依赖','model_load':'OCR模型加载失败',
            'out_of_memory':'OCR内存不足','timeout':'OCR处理超时',
            'invalid_output':'OCR返回数据不完整','frame_invalid':'抽帧图片无效'}.get(code,'OCR本地处理失败')
    return ValueError(f'本地旧字幕定位失败（{code}）：{reason}；配音、画面匹配结果已保留，请修复本地环境后重试，不会自动跳过去旧字幕')


def _error_code(value):
    error=value.get('error') if isinstance(value,dict) else None
    return error.get('code','invalid_output') if isinstance(error,dict) else 'invalid_output'


def locate(studio,plan,scenes,event,progress,*,refresh=False):
    from . import media,providers
    from .whole_voice import _key,_atomic
    cache=studio.root/'subtitle-cache'
    by_id={s['clip_id']:s for s in scenes}
    pending=[];warnings=[];infos={};fingerprint=probe_fingerprint()
    cache_warning='旧字幕定位缓存保存失败；已保留本次识别区域并继续本地合成，下次可能重新定位'
    try:cache.mkdir(exist_ok=True)
    except OSError:warnings.append(cache_warning)
    with tempfile.TemporaryDirectory(prefix='subtitle-locate-',dir=studio.root/'work') as tmp:
        root=Path(tmp);images=[]
        for window in plan['windows']:
            scene=by_id[window['clip_id']]
            record=scene.get('prepared_media',{})
            key=_key(['subtitle_ocr',VERSION,fingerprint,scene['source_sha256'],window['start'],window['end'],record.get('sha256')])
            path=cache/(key+'.json')
            if path.exists() and not refresh:
                try:
                    if path.stat().st_size>65536:raise ValueError
                    saved=json.loads(path.read_text(encoding='utf-8'))
                    if not isinstance(saved,dict) or saved.get('version')!=VERSION or 'band' not in saved:raise ValueError
                    band=saved['band'];checked=saved.get('checked_at')
                    if band is None:
                        if type(checked) not in (int,float) or not 0<=time.time()-checked<NEGATIVE_TTL:raise ValueError
                    elif not _valid_band(band):raise ValueError
                    window['cleanup_band']=band
                    continue
                except (OSError,ValueError,TypeError,RecursionError):
                    pass  # Corrupt/old/expired local cache is a miss, not a paid retry.
            source=Path(record.get('path',scene['source_path']))
            if source not in infos:infos[source]=media.probe(source)
            info=infos[source];offset=record.get('time_offset',0.)
            indices=[]
            for fraction in (.1,.5,.9):
                image=root/f'frame-{len(images)}.png'
                at=window['start']+(window['end']-window['start'])*fraction-offset
                media._run(['ffmpeg','-hide_banner','-v','error','-nostdin','-n','-ss',f'{at:.6f}',
                    '-i',source,'-frames:v','1','-vf','scale=640:-2','-threads','1',image],cancel=event,timeout=30)
                images.append(str(image));indices.append(len(images)-1)
            pending.append((window,path,indices,info))
        if images:
            progress(.1,'按已选镜头本地定位旧字幕（每镜头3张抽样，不调用收费接口）')
            inputs=root/'images.json';outputs=root/'boxes.json'
            inputs.write_text(json.dumps(images,ensure_ascii=False),encoding='utf-8')
            try:
                code,_=providers._run_process([sys.executable,'-I','-B','-X','utf8',
                    Path(__file__).with_name('subtitle_worker.py'),'--input',inputs,'--output',outputs],
                    cancel=event,env=dict(os.environ),timeout=600)
            except providers.ProviderError as exc:
                if exc.code=='cancelled':raise
                raise _failure(exc.code) from None
            if code or not outputs.is_file() or outputs.stat().st_size>8*1024**2:
                try:
                    error=_error_code(json.loads(outputs.read_text(encoding='utf-8'))) if outputs.is_file() and outputs.stat().st_size<=65536 else 'invalid_output'
                except (OSError,ValueError,AttributeError):error='invalid_output'
                raise _failure(error)
            try:found=json.loads(outputs.read_text(encoding='utf-8'))
            except (OSError,ValueError):raise _failure('invalid_output') from None
            if isinstance(found,dict):raise _failure(_error_code(found))
            if not isinstance(found,list) or len(found)!=len(images) or any(not isinstance(f,list) for f in found):raise _failure('invalid_output')
            for window,path,indices,info in pending:
                # FFmpeg scaled frames preserve storage aspect; original mapping
                # uses normalized vertical fractions (rotation already normalized).
                try:
                    with Path(images[indices[0]]).open('rb') as png:header=png.read(24)
                except OSError:raise _failure('frame_invalid') from None
                if len(header)<24 or header[:8]!=b'\x89PNG\r\n\x1a\n':raise _failure('frame_invalid')
                width,height=struct.unpack('>II',header[16:24])
                if width<=0 or height<=0:raise _failure('frame_invalid')
                band=caption_band([found[i] for i in indices],width,height)
                window['cleanup_band']=band
                try:
                    _atomic(path,{'version':VERSION,'band':band,'checked_at':time.time(),
                                  'source_sha256':by_id[window['clip_id']]['source_sha256']})
                except OSError:
                    if cache_warning not in warnings:warnings.append(cache_warning)
    missing=sum(w.get('cleanup_band') is None for w in plan['windows'])
    if missing:warnings.append(f'{missing}个镜头未定位到可信旧字幕区域，未盲目抹除；请预览确认')
    warnings.append('旧字幕按关键帧OCR定位并局部修补，非逐帧无痕修复；短暂或移动字幕可能漏检，复杂背景需预览校对')
    return warnings
