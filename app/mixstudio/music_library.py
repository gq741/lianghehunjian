"""Persistent, content-deduplicated copies of explicitly selected local songs."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from . import media
from .jobs import Cancelled
from .media_identity import file_version
from .recycle import recycle_file


EXTENSIONS={'.mp3','.wav','.m4a','.flac','.aac','.ogg','.opus','.wma'}
SCAN_SECONDS=15*60
MAX_TRACKS=10000
MAX_MANIFEST_BYTES=4*1024*1024
_ID=re.compile(r'[0-9a-f]{64}')


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled('音乐导入已取消，已完成的歌曲继续保留')


def _safe(path):
    return not any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction())
                   for p in (path,*path.parents))


def _valid_name(name):
    return (isinstance(name,str) and 1<=len(name)<=255 and not any(c in name for c in '/\\')
            and not any(ord(c)<32 for c in name))


def _version(path):
    value=file_version(path)
    if value is not None:return value
    info=path.stat()
    return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns


def _digest(path,cancel=None):
    signature=hashlib.sha256()
    with path.open('rb') as source:
        while block:=source.read(1024*1024):
            _cancel(cancel);signature.update(block)
    return signature.hexdigest()


def _validate_tracks(tracks):
    if not isinstance(tracks,list) or len(tracks)>MAX_TRACKS:raise ValueError
    ids=set();files=set()
    for track in tracks:
        if not isinstance(track,dict):raise ValueError
        identifier=track.get('id');filename=track.get('file');name=track.get('name')
        if (not isinstance(identifier,str) or not _ID.fullmatch(identifier)
                or track.get('sha256')!=identifier or not isinstance(filename,str)
                or Path(filename).suffix not in EXTENSIONS or filename!=identifier+Path(filename).suffix
                or not _valid_name(name)
                or type(track.get('size')) is not int or track['size']<=0
                or type(track.get('duration')) not in (int,float) or not math.isfinite(track['duration']) or track['duration']<=0
                or type(track.get('imported_at')) not in (int,float) or not math.isfinite(track['imported_at'])
                or identifier in ids or filename in files):raise ValueError
        ids.add(identifier);files.add(filename)
    return tracks


class MusicLibrary:
    def __init__(self,root):
        self.root=Path(os.path.abspath(root))/'music-library'
        self.manifest=self.root/'manifest.json'
        self.lock=threading.RLock()
        self.import_lock=threading.Lock()
        self.scans={}
        self._manifest_version=None
        self._manifest_tracks=None
        self._tracks_by_file={}
        self._check_root()
        self.root.mkdir(parents=True,exist_ok=True)
        self._check_root()

    def _check_root(self):
        if not _safe(self.root):raise ValueError('音乐库目录包含链接，原文件保持不变')

    def _owned(self,filename):
        self._check_root()
        path=self.root/filename
        if path.parent!=self.root or not _safe(path):
            raise ValueError('音乐库文件路径无效或包含链接，原文件保持不变')
        return path

    def _load(self):
        self._check_root()
        if not _safe(self.manifest):raise ValueError('音乐库清单包含链接，已保留现有文件')
        if not self.manifest.exists():
            self._manifest_version=None;self._manifest_tracks=None;self._tracks_by_file={}
            return []
        try:
            version=file_version(self.manifest)
            if version is not None and version==self._manifest_version and self._manifest_tracks is not None:
                return self._manifest_tracks
            if self.manifest.stat().st_size>MAX_MANIFEST_BYTES:raise ValueError
            saved=json.loads(self.manifest.read_text('utf-8'))
            if not isinstance(saved,dict) or type(saved.get('version')) is not int or saved['version']!=1:raise ValueError
            tracks=_validate_tracks(saved.get('tracks'))
            after=file_version(self.manifest)
            if version is not None and after!=version:raise ValueError
            self._manifest_version=after if version is not None else None
            self._manifest_tracks=tracks
            self._tracks_by_file={track['file']:track for track in tracks}
            return tracks
        except (OSError,ValueError,TypeError,UnicodeError,RecursionError):
            self._manifest_version=None;self._manifest_tracks=None;self._tracks_by_file={}
            raise ValueError('音乐库清单损坏或暂时不可读取，已保留现有清单和歌曲；请先备份并检查清单') from None

    def _save(self,tracks):
        self._manifest_version=None;self._manifest_tracks=None;self._tracks_by_file={}
        self._check_root()
        if not _safe(self.manifest):raise ValueError('音乐库清单包含链接，已保留现有文件')
        try:
            _validate_tracks(tracks)
            payload=json.dumps({'version':1,'tracks':tracks},ensure_ascii=False,allow_nan=False).encode('utf-8')
            if len(payload)>MAX_MANIFEST_BYTES:raise ValueError
        except (ValueError,TypeError,UnicodeError,RecursionError):
            raise ValueError('音乐库清单达到容量上限或内容无效，原清单保持不变') from None
        temporary=self.root/('.manifest-'+uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('xb') as output:
                output.write(payload)
                output.flush();os.fsync(output.fileno())
            self._check_root()
            os.replace(temporary,self.manifest)
        finally:
            if temporary.exists():temporary.unlink()

    def _public(self,track):
        path=self.root/track['file']
        try:available=_safe(path) and path.is_file() and path.stat().st_size==track['size']
        except OSError:available=False
        return {k:track[k] for k in ('id','name','size','duration')}|{'path':str(path),'available':available}

    def list_tracks(self):
        with self.lock:
            return [self._public(t) for t in sorted(self._load(),key=lambda t:t['imported_at'],reverse=True)]

    def selected_track(self,path):
        """Check only the selected copy; manifest identity still refreshes each poll."""
        with self.lock:
            self._load()
            if not path:return None
            # Retain the registered path's identity even if its file has been
            # replaced by a link; _public must then report that copy unavailable.
            source=Path(os.path.abspath(path))
            track=self._tracks_by_file.get(source.name) if source.parent==self.root else None
            return self._public(track) if track is not None else None

    def resolve(self,track_id):
        """Resolve only a registered owned path; missing copies stay missing."""
        with self.lock:
            track=next((t for t in self._load() if t['id']==track_id),None)
            if track is None:raise ValueError('歌曲不存在，请刷新音乐库')
            return self._owned(track['file'])

    def scan_directory(self,directory):
        if not isinstance(directory,(str,Path)) or not str(directory):raise ValueError('请选择音乐目录')
        source=Path(directory)
        if not source.is_absolute() or not _safe(source) or not source.is_dir():
            raise ValueError('请选择不包含链接的本地音乐目录')
        source=Path(os.path.abspath(source))
        if source==self.root or self.root in source.parents:
            raise ValueError('请选择音乐库以外的原歌曲目录')
        candidates=[];private={};skipped=0
        for path in sorted(source.iterdir(),key=lambda p:p.name.casefold()):
            if path.suffix.lower() not in EXTENSIONS or not _safe(path) or not path.is_file() or not _valid_name(path.name):
                skipped+=1;continue
            try:
                version=_version(path)
                if version[2]<=0:skipped+=1;continue
                identifier=uuid.uuid4().hex
                candidates.append({'id':identifier,'name':path.name,'size':version[2]})
                private[identifier]={'path':path,'version':version}
            except OSError:
                skipped+=1;continue
            if len(candidates)>MAX_TRACKS:raise ValueError('目录音频文件较多，请选择更小的音乐目录')
        now=time.time();scan_id=uuid.uuid4().hex
        with self.lock:
            self.scans={k:v for k,v in self.scans.items() if 0<=now-v['created_at']<SCAN_SECONDS}
            self.scans[scan_id]={'created_at':now,'candidates':private}
            while len(self.scans)>8:self.scans.pop(next(iter(self.scans)))
        return {'scan_id':scan_id,'candidates':candidates,'skipped':skipped}

    def _copy_file(self,source,temporary,*,cancel=None):
        signature=hashlib.sha256();size=0
        with source.open('rb') as incoming,temporary.open('xb') as outgoing:
            while block:=incoming.read(1024*1024):
                _cancel(cancel);outgoing.write(block);signature.update(block);size+=len(block)
            _cancel(cancel);outgoing.flush();os.fsync(outgoing.fileno())
        return signature.hexdigest(),size

    def _publish(self,temporary,target,signature,size,cancel):
        if target.exists():
            if not target.is_file() or target.stat().st_size!=size or _digest(target,cancel)!=signature:
                raise ValueError('音乐库中同名副本已改变，未覆盖该文件')
            return
        # Windows rename and POSIX hard-link publish atomically without replacing
        # an existing file. Only our generated staging file is ever unlinked.
        if os.name=='nt':os.rename(temporary,target)
        else:os.link(temporary,target)

    def import_candidates(self,scan_id,candidate_ids,*,cancel=None,progress=None):
        if not isinstance(candidate_ids,list) or not 1<=len(candidate_ids)<=MAX_TRACKS or any(not isinstance(i,str) for i in candidate_ids):
            raise ValueError('请勾选要导入的歌曲')
        with self.lock:
            scan=self.scans.get(scan_id) if isinstance(scan_id,str) else None
            if scan is None or not 0<=time.time()-scan['created_at']<SCAN_SECONDS:
                raise ValueError('音乐扫描已过期，请重新扫描目录')
            identifiers=list(dict.fromkeys(candidate_ids))
            if any(i not in scan['candidates'] for i in identifiers):raise ValueError('歌曲候选无效，请重新扫描并勾选')
            selected=[(i,dict(scan['candidates'][i])) for i in identifiers]
            self._load()  # Corrupt manifests are never silently replaced.
        while not self.import_lock.acquire(timeout=.1):_cancel(cancel)
        result={'imported':[],'duplicates':[],'failed':[],'warnings':[]}
        try:
            for index,(identifier,candidate) in enumerate(selected):
                _cancel(cancel)
                source=candidate['path'];temporary=self.root/('.music-import-'+uuid.uuid4().hex+'.tmp')
                try:
                    if not _safe(source) or not source.is_file() or _version(source)!=candidate['version']:
                        raise ValueError('源歌曲已移动或改变，请重新扫描')
                    info=media.probe(source)
                    if (not info.get('has_audio') or type(info.get('duration')) not in (int,float)
                            or not math.isfinite(info['duration']) or info['duration']<=0):
                        raise ValueError('所选文件没有有效音轨')
                    self._check_root()
                    signature,size=self._copy_file(source,temporary,cancel=cancel)
                    if not _safe(source) or _version(source)!=candidate['version'] or size!=candidate['version'][2]:
                        raise ValueError('复制期间源歌曲发生改变，请重新扫描')
                    _cancel(cancel)
                    with self.lock:
                        tracks=self._load()
                        previous=next((t for t in tracks if t['id']==signature),None)
                        track=previous or {'id':signature,'sha256':signature,'name':source.name,
                            'file':signature+source.suffix.lower(),'size':size,'duration':float(info['duration']),
                            'imported_at':time.time()}
                        target=self._owned(track['file'])
                        self._publish(temporary,target,signature,size,cancel)
                        if previous is None:
                            self._save(tracks+[track])
                        result['duplicates' if previous else 'imported'].append(self._public(track))
                except (OSError,ValueError,media.MediaError) as exc:
                    error=str(exc) if isinstance(exc,ValueError) else '歌曲校验、复制或清单保存失败，请检查音频和磁盘后重试；原文件保持不变'
                    result['failed'].append({'id':identifier,'name':source.name,'error':error})
                finally:
                    try:
                        if _safe(temporary) and temporary.exists():temporary.unlink()
                    except OSError:
                        result['warnings'].append('音乐导入临时文件暂时被占用，已提交的歌曲不受影响')
                if progress:progress((index+1)/len(selected),f'音乐导入 {index+1}/{len(selected)}')
            result['tracks']=self.list_tracks()
            return result
        finally:self.import_lock.release()

    def delete(self,track_ids,*,protected_paths=()):
        if not isinstance(track_ids,list) or not 1<=len(track_ids)<=MAX_TRACKS or any(not isinstance(i,str) for i in track_ids):
            raise ValueError('请勾选要删除的歌曲')
        with self.lock:
            tracks=self._load();identifiers=list(dict.fromkeys(track_ids));by_id={t['id']:t for t in tracks}
            if any(i not in by_id for i in identifiers):raise ValueError('歌曲不存在，请刷新音乐库')
            protected=[Path(os.path.abspath(p)) for p in protected_paths if p]
            result={'removed_ids':[],'failed':[]}
            for identifier in identifiers:
                track=by_id[identifier];moved=False
                try:
                    path=self._owned(track['file'])
                    if any(path==p or p in path.parents for p in protected):raise ValueError('歌曲正在使用，已保留')
                    if path.exists():recycle_file(path);moved=True
                    remaining=[t for t in tracks if t['id']!=identifier]
                    self._save(remaining);tracks=remaining
                    result['removed_ids'].append(identifier)
                except (OSError,ValueError) as exc:
                    message=('歌曲已移入回收站，但清单保存失败；原记录暂保留，请刷新后重试' if moved else
                             str(exc) if isinstance(exc,ValueError) else '歌曲被占用或回收失败，已保留歌曲和清单')
                    result['failed'].append({'id':identifier,'name':track['name'],'error':message,'moved_to_recycle_bin':moved})
            result['tracks']=[self._public(t) for t in tracks]
            return result
