"""Explicit, credential-free GitHub update checks for the folder edition."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = 'gq741/lianghehunjian'
API = 'https://api.github.com/repos/' + REPOSITORY + '/releases?per_page=20'
RELEASES = 'https://github.com/' + REPOSITORY + '/releases'
VERSION = '2026.11.23'
MAX_DOWNLOAD = 32 * 1024 * 1024


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def version(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}(?:\.\d+)?',value):
        raise ValueError('发布版本格式无效')
    return tuple(int(x) for x in value.split('.')) + ((0,) if value.count('.')==2 else ())


def package_root(source=None):
    source = Path(source or __file__).resolve()
    root = source.parents[2]
    return root if (root/'RELEASE.json').is_file() and (root/'runtime/python.exe').is_file() else None


def current(root):
    if root is None:
        return {'version':VERSION,'portable':False,'release_url':RELEASES}
    release = json.loads((root/'RELEASE.json').read_text('utf-8-sig'))
    return {'version':release.get('version') or release['date'].replace('-','.'),
            'portable':True,'release_url':RELEASES}


def trusted_url(url, *, redirect=False):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None,443):
        return False
    if p.hostname == 'api.github.com':
        return p.path.startswith('/repos/'+REPOSITORY+'/')
    if p.hostname == 'github.com':
        return p.path.startswith('/'+REPOSITORY+'/releases/download/')
    return redirect and p.hostname in {'release-assets.githubusercontent.com','objects.githubusercontent.com'}


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if not trusted_url(newurl,redirect=True):
            raise ValueError('更新下载跳转地址不属于 GitHub 发行文件服务')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def fetch(url, limit):
    if not trusted_url(url):
        raise ValueError('更新地址不属于指定仓库')
    request = urllib.request.Request(url,headers={'User-Agent':'LiangHeMix-Updater',
                                  'Accept':'application/vnd.github+json'})
    try:
        with urllib.request.build_opener(Redirect()).open(request,timeout=25) as response:
            data = response.read(limit+1)
        if len(data)>limit:
            raise ValueError('更新下载超过大小限制')
        return data
    except (urllib.error.URLError,TimeoutError,OSError):
        raise ValueError('暂时连接不上 GitHub，请检查网络后重试；当前软件不受影响') from None


def check(root):
    info = current(root)
    releases = json.loads(fetch(API, 2*1024*1024))
    if not isinstance(releases,list):
        raise ValueError('更新服务返回格式异常')
    offers=[]
    for release in releases:
        if release.get('draft') or release.get('prerelease'):
            continue
        tag=release.get('tag_name','')
        if not re.fullmatch(r'v\d{4}\.\d{2}\.\d{2}(?:\.\d+)?',tag):
            continue
        assets={a.get('name'):a for a in release.get('assets',[])}
        feed=assets.get('UPDATE_FEED.json')
        if feed and version(tag[1:])>version(info['version']):
            offers.append((version(tag[1:]),tag,assets,feed))
    if not offers:
        return {**info,'available':False,'message':'当前没有更新版本'}
    _,tag,assets,asset=max(offers,key=lambda x:x[0])
    raw=fetch(asset['browser_download_url'],1024*1024)
    if asset.get('digest')!='sha256:'+hashlib.sha256(raw).hexdigest():
        raise ValueError('更新清单校验失败，请稍后重试')
    feed=json.loads(raw)
    if feed.get('format')!=1 or feed.get('version')!=tag[1:]:
        raise ValueError('更新清单与发布版本不一致')
    archive=feed.get('archive',{})
    published=assets.get(archive.get('name'),{})
    if (not re.fullmatch(r'LiangHeMix-Update-[0-9.]+\.zip',archive.get('name',''))
            or type(archive.get('bytes')) is not int or not 0<archive['bytes']<=MAX_DOWNLOAD
            or published.get('size')!=archive['bytes']
            or not re.fullmatch(r'[0-9a-f]{64}',archive.get('sha256',''))
            or published.get('digest')!='sha256:'+archive['sha256']
            or not trusted_url(published.get('browser_download_url',''))):
        raise ValueError('更新包清单无效')
    compatible=bool(root and digest(root/'FILES.sha256.json') in feed.get('base_manifests',[]))
    return {**info,'available':True,'compatible':compatible,'latest':feed['version'],
            'release_url':RELEASES+'/tag/'+tag,
            'message':('可下载更新；个人数据保留' if compatible else '此安装版本需使用对应更新包，请查看发布说明'),
            'archive':{**archive,'url':published['browser_download_url']}}


def download(offer, folder):
    archive=offer['archive']; data=fetch(archive['url'],MAX_DOWNLOAD)
    if len(data)!=archive['bytes'] or hashlib.sha256(data).hexdigest()!=archive['sha256']:
        raise ValueError('更新包下载不完整或校验失败；原程序未修改')
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='update-',dir=folder))
    path=stage/'download.zip'; path.write_bytes(data)
    # Extract only normalized regular files; never use untrusted extractall.
    try:
        with zipfile.ZipFile(path) as z:
            rows=z.infolist(); seen=set(); total=0
            if len(rows)>1000: raise ValueError('更新包文件过多')
            for row in rows:
                parts=row.filename.split('/')
                if (row.is_dir() or any(not x or x in ('.','..') or x.rstrip(' .')!=x for x in parts)
                        or '\\' in row.filename or ':' in row.filename
                        or row.filename.casefold() in seen or (row.external_attr>>16)&0o170000==0o120000):
                    raise ValueError('更新包路径无效')
                if any(re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',x,re.I) for x in parts):
                    raise ValueError('更新包路径包含保留名称')
                seen.add(row.filename.casefold()); total+=row.file_size
                if row.file_size>8*1024*1024 or total>64*1024*1024:
                    raise ValueError('更新包展开大小异常')
                out=stage/'bundle'/Path(*parts)
                out.parent.mkdir(parents=True,exist_ok=True); out.write_bytes(z.read(row))
        bundle=stage/'bundle'
        if not (bundle/'UPDATE.json').is_file() or not (bundle/'apply_update.py').is_file():
            raise ValueError('更新包缺少入口文件')
        return bundle
    except Exception:
        # This is our newly-created staging directory only, never install/data.
        if stage.parent.resolve()==folder.resolve() and stage.name.startswith('update-'):
            shutil.rmtree(stage)
        raise


class UpdateSession:
    def __init__(self,root=None):
        self.root=root or package_root()
        self.offer=None
        self.prepared=None
        self.lock=threading.Lock()

    def status(self):
        return current(self.root)

    def check(self):
        with self.lock:
            self.prepared=None; self.offer=None
            self.offer=check(self.root)
            return {k:v for k,v in self.offer.items() if k!='archive'}

    def prepare(self):
        with self.lock:
            if not self.root or not self.offer or not self.offer.get('compatible'):
                raise ValueError('请先检查更新并确认安装版本兼容')
            self.prepared=None
            folder=self.root/'.lianghe-downloads'
            if folder.is_symlink() or (folder.exists() and folder.resolve().parent!=self.root.resolve()):
                raise ValueError('更新暂存目录异常')
            bundle=download(self.offer,folder)
            # Check downloaded code/data only; validation never executes payload.
            from importlib.util import spec_from_file_location,module_from_spec
            installed=self.root/'app/tools/apply_update.py'
            spec=spec_from_file_location('_lianghe_installed_updater',installed)
            updater=module_from_spec(spec);spec.loader.exec_module(updater)
            updater.validate(self.root,bundle)
            self.prepared=bundle
            return {'version':self.offer['latest'],'ready':True}

    def launch(self):
        with self.lock:
            if not self.prepared: raise ValueError('请先下载并校验更新包')
            command=[str(self.root/'runtime/python.exe'),'-I','-B','-X','utf8',
                     str(self.prepared/'apply_update.py'),'--target',str(self.root),
                     '--bundle',str(self.prepared),'--wait','120','--notify']
            subprocess.Popen(command,cwd=self.root,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.prepared=None

