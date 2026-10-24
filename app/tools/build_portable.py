"""Build a relocatable Windows folder. No venv/runtime user data or model APIs."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess


MODEL_FILES = ('model.bin','config.json','tokenizer.json','vocabulary.txt')
SKIP = {'__pycache__','.git','.venv','.pytest_cache','.mypy_cache','verification'}
SECRET = re.compile(rb'\bsk-[A-Za-z0-9_-]{20,}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while data:=stream.read(4*1024*1024):h.update(data)
    return h.hexdigest()


def safe_path(root,name):
    if not isinstance(name,str) or not name or '\\' in name:
        raise ValueError('invalid relative distribution path')
    root=Path(root).resolve();rel=Path(name)
    if rel.is_absolute() or rel.drive or '..' in rel.parts or ':' in name:
        raise ValueError('invalid relative distribution path')
    p=root/rel
    if not p.resolve().is_relative_to(root):raise ValueError('path outside distribution')
    return p


def verify_sources(source,manifest):
    rows=json.loads(Path(manifest).read_text('utf8'))
    if not isinstance(rows,list) or len(rows)<165:raise ValueError('source manifest incomplete')
    seen=set()
    for row in rows:
        name=row['path'];p=safe_path(source,name)
        if name.casefold() in seen or not p.is_file() or p.is_symlink() or digest(p)!=row['sha256']:
            raise ValueError('source hash mismatch: '+name)
        if p.stat().st_size>8*1024**2:raise ValueError('unexpected source payload')
        if SECRET.search(p.read_bytes()):raise ValueError('source requires credential review: '+name)
        seen.add(name.casefold())
    required={'app.py','web/brand-icon.ico','web/app.js','prompts/narration-v2.md',
        'mixstudio/voice_alignment_worker.py','mixstudio/subtitle_worker.py',
        'tools/portable_boot.py','tools/portable_check.py'}
    if not required <= {r['path'] for r in rows}:raise ValueError('required application resource missing')
    return rows


def write(path,text,bom=False):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text,encoding='utf-8-sig' if bom else 'utf8',newline='\r\n' if path.suffix.lower() in ('.cmd','.vbs','.ps1') else '\n')


def launchers(root):
    source='''using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
class PortableLauncher {
  [STAThread] static int Main() {
    try {
      string root = AppDomain.CurrentDomain.BaseDirectory;
      string python = Path.Combine(root, "runtime", "pythonw.exe");
      string boot = Path.Combine(root, "app", "tools", "portable_boot.py");
      if (!File.Exists(python) || !File.Exists(boot)) throw new Exception();
      ProcessStartInfo start = new ProcessStartInfo(python,
          "-I -B -X utf8 \\"" + boot + "\\"");
      start.WorkingDirectory = root;
      start.UseShellExecute = false;
      start.CreateNoWindow = true;
      Process.Start(start);
      return 0;
    } catch {
      MessageBox.Show("请完整复制量禾混剪文件夹，并运行免费自检。", "量禾混剪", MessageBoxButtons.OK, MessageBoxIcon.Error);
      return 1;
    }
  }
}
'''
    if (root/'app/web/brand-icon.ico').is_file():
        cs=root/'app/tools/PortableLauncher.cs';write(cs,source,bom=True)
        compiler=Path(os.environ.get('WINDIR','C:/Windows'))/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        command=[str(compiler),'/nologo','/target:winexe','/platform:anycpu',
            '/reference:System.Windows.Forms.dll','/win32icon:'+str(root/'app/web/brand-icon.ico'),
            '/out:'+str(root/'量禾混剪.exe'),str(cs)]
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=30)
        if result.returncode or not (root/'量禾混剪.exe').is_file():raise ValueError('Native launcher build failed: '+result.stdout)
    write(root/'启动量禾混剪.vbs','''Option Explicit
Dim fso, shell, folder, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
command = Chr(34) & folder & "\\runtime\\pythonw.exe" & Chr(34) & " -I -B -X utf8 " & Chr(34) & folder & "\\app\\tools\\portable_boot.py" & Chr(34)
shell.Run command, 0, False
''')
    write(root/'启动量禾混剪.cmd','''@echo off
setlocal
"%~dp0runtime\\python.exe" -I -B -X utf8 "%~dp0app\\tools\\portable_boot.py" %*
if errorlevel 1 pause
''')
    write(root/'免费自检.cmd','''@echo off
setlocal
chcp 65001 >nul
"%~dp0runtime\\python.exe" -I -B -X utf8 "%~dp0app\\tools\\portable_check.py" --output "%~dp0logs\\selfcheck.json"
set "CHECK_RESULT=%ERRORLEVEL%"
echo.
echo Report: %~dp0logs\\selfcheck.json
pause
exit /b %CHECK_RESULT%
''')
    write(root/'安装系统组件.ps1','''$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$items = @(
    @{Name='Visual C++ x64'; File='vc_redist.x64.exe'; Args=@('/install','/passive','/norestart')},
    @{Name='WebView2'; File='MicrosoftEdgeWebView2RuntimeInstallerX64.exe'; Args=@('/install')}
)
foreach ($item in $items) {
    $file = Join-Path $root ('installers\\' + $item.File)
    $sig = Get-AuthenticodeSignature -LiteralPath $file
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { throw 'Microsoft installer signature check failed' }
    Write-Host ('Installing ' + $item.Name)
    $process = Start-Process -FilePath $file -ArgumentList $item.Args -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -notin @(0,3010,1638)) { throw ('Installer exit status: ' + $process.ExitCode) }
    if ($process.ExitCode -eq 3010) { Write-Host 'Windows restart is required before the next check.' }
}
Write-Host 'Run free self-check after installation.'
''',bom=True)
    write(root/'安装系统组件.cmd','''@echo off
setlocal
chcp 65001 >nul
"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0安装系统组件.ps1"
set "INSTALL_RESULT=%ERRORLEVEL%"
pause
exit /b %INSTALL_RESULT%
''')


def documents(root):
    write(root/'先看这里.txt','''量禾混剪 Windows x64 文件夹版

1. 请把整个文件夹复制到朋友电脑本地可写目录，例如 D:\\量禾混剪；保留完整目录，不只发送启动文件。
2. 第一次先双击“免费自检.cmd”。它只检测本地环境，不调用 DeepSeek、GLM 或 Fish。
3. 如果缺少 VC++ / WebView2，运行“安装系统组件.cmd”，同意微软安装器；提示重启时先重启再自检。
4. 自检全部 PASS 后，双击“量禾混剪.exe”。若没有窗口，用“启动量禾混剪.cmd”查看错误。
5. 首次在软件设置里填入自己的三个 API Key，确认音色，选择下载、输出目录，再导入自己的素材。
6. 本包包含 Python、依赖、本地语音/OCR模型、FFmpeg 与开源中文字体，无需另装 Python 或 CUDA。

适配目标：Windows 10 22H2（19045）/ Windows 11 x64、Intel/AMD、支持 SSE4.1 的 CPU，默认CPU推理。
系统仍需 .NET Framework 4.6.2+、Windows媒体组件；N/KN版如缺媒体功能，按系统可选功能补齐 Media Feature Pack。
云模型仍需互联网与自己的账号额度；本地依赖齐全不代表云服务离线可用。
请不要发送自己的 AppData 数据目录、密钥、登录状态、历史素材或缓存音频。
移动软件文件夹后请重新运行免费自检；随包模型和字体会根据新位置重新定位。
用户数据保存在当前 Windows 用户的 %LOCALAPPDATA%\\JDMixStudio\\v1，不随软件文件夹移动。
收到更新包时不要删除该用户数据目录。日志看软件内“打开日志文件夹”或本包 logs。
给 WorkBuddy 的说明见 WORKBUDDY.md；文件清单/版本见 RELEASE.json 和 FILES.sha256.json。
''')
    write(root/'WORKBUDDY.md','''# 量禾混剪：接收电脑安装与验收

本工程是完整运行环境文件夹版，不要重新创建 .venv，不要升级 requirements-lock.txt 或安装 CUDA。
Python 必须用 `本包/runtime/python.exe`，通过 `-I -B -X utf8` 执行。

## 顺序
1. 确认 Windows 10 22H2（19045）/ Windows 11 Intel/AMD x64，软件在本地可写文件夹，整包完整。
2. 运行 `免费自检.cmd`，读取 `logs/selfcheck.json`；该步骤没有收费调用。
3. 如报告系统原生组件缺失，运行官方签名的 `安装系统组件.cmd`；需要用户确认或重启时按提示处理。
4. 再次自检，全部通过后运行 `量禾混剪.exe`；源码入口是 app/tools/portable_boot.py。
5. 请用户自己在设置中填写 DeepSeek、GLM、Fish Key，不把密钥粘进聊天、源码、终端或报告。
6. 让用户选择素材/下载/输出目录。检查空白素材库、读取商品、文案保存和免费预检。
7. 如需真实出片，先让用户确认本次收费范围，再进行；不要把启动、免费预检或过去授权当成新收费许可。

## 定位
- app：应用源码、web、prompts 和离线回归测试；runtime：独立Python及依赖；bin：媒体程序。
- models/faster-whisper-small：本地时间对齐模型；fonts：开源中文字幕字体。
- installers：微软离线系统组件；licenses：第三方原声明；logs：启动/自检日志。\n- third-party-sources：随附媒体组件精确来源归档，保留即可，无需解压或安装。
- 用户数据：当前用户 %LOCALAPPDATA%/JDMixStudio/v1（或显式 JDMIX_DATA_DIR）。
- 不复制开发者的 settings.json、secrets.dpapi、数据库或浏览器 profile。

## 原则
- 先复制备份、保留原哈希，在副本适配；不改混剪业务顺序/文案/重复限制。
- 不修改全局PATH；启动器只给自身和子进程配置 bin/runtime、离线本地模型与Noto字体。
- 旧收费结果不确定时不自动重提；优先复用已验证音频与识别缓存。
- 全新电脑的网络、账号额度、代理与素材可用性需分别验证。诊断要区分本地环境和云服务失败。
- app/HANDOFF.md 等是开发历史记录；安装与路径以本文件和根目录说明为准。

## 可直接交给 WorkBuddy 的操作提示
```text
请在这份量禾混剪软件文件夹内操作。先读 WORKBUDDY.md 与先看这里.txt，
检查 FILES.sha256.json，再运行随包免费自检，缺少原生组件时用随包官方安装器。
不要重建虚拟环境或升级依赖，不要读取开发电脑路径。先完成免费启动与本地验收，
让用户自己填写 API Key；付费出片须先确认本次费用范围。保留用户数据与已有缓存，
记录实际命令、退出状态、失败字段；只修已复现的问题，并在副本测试。
```
''')
    write(root/'AGENTS.md','''# 量禾混剪文件夹版
请先读取本目录 WORKBUDDY.md 和先看这里.txt。本目录即软件包根，所有路径使用相对根定位。
只在包副本中进行安装适配；不要假定开发电脑路径；不要调用旧工程或用户机器上其他项目。
优先运行免费自检；使用随包 runtime/python.exe，不复制旧 .venv、不升级锁定依赖。
用户密钥只经软件设置由用户输入，不写入代码、日志或报告；任何收费模型调用需本次明确确认。
''')
    # Override the development-machine-only instruction file for recipient agents.
    write(root/'app/AGENTS.md','''# 量禾混剪发布副本
本目录是程序源码；软件包根是上一级目录。请读取 ../WORKBUDDY.md 与 ../AGENTS.md。
路径、运行方式和安装适配以发布包根说明为准，不使用开发电脑绝对路径。
不要修改混剪业务逻辑；先备份和免费自检；收费调用需用户本次明确确认；密钥不得进入源码和报告。
''')


def verify_package(root):
    root=Path(root).resolve();rows=json.loads((root/'FILES.sha256.json').read_text('utf8'))
    for row in rows:
        p=safe_path(root,row['path'])
        if not p.is_file() or p.is_symlink() or digest(p)!=row['sha256']:raise ValueError('package integrity mismatch: '+row['path'])
    return {'status':'PACKAGE_HASHES_PASS','files_verified':len(rows),'bytes':sum(r['bytes'] for r in rows)}


def build(args):
    source=Path(args.source).resolve();out=Path(args.output).resolve()
    if out.exists():raise FileExistsError('Output already exists; choose a fresh directory')
    if out.is_relative_to(source) or source.is_relative_to(out):raise ValueError('Output and source overlap')
    rows=verify_sources(source,args.source_manifest)
    home,site,model=map(lambda p:Path(p).resolve(),(args.python_home,args.site_packages,args.model))
    if (home/'pyvenv.cfg').exists() or not (home/'python312.dll').is_file():raise ValueError('Need full Python home, not a venv')
    if not all((model/n).is_file() for n in MODEL_FILES):raise ValueError('incomplete local ASR model')
    for name in ('MicrosoftEdgeWebView2RuntimeInstallerX64.exe','vc_redist.x64.exe'):
        p=Path(args.components)/name
        if not p.is_file() or p.stat().st_size<10*1024**2 or p.open('rb').read(2)!=b'MZ':raise ValueError('offline installer missing')
    out.mkdir(parents=True,exist_ok=False)
    for row in rows:
        target=safe_path(out/'app',row['path']);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(safe_path(source,row['path']),target)
    shutil.copyfile(source/'requirements-lock.txt',out/'requirements-lock.txt')
    def ignored(directory,names):return [n for n in names if n in SKIP or n.endswith(('.pyc','.pyo')) or (Path(directory)==home and n=='Scripts')]
    shutil.copytree(home,out/'runtime',ignore=ignored)
    shutil.copytree(site,out/'runtime/Lib/site-packages',dirs_exist_ok=True,ignore=ignored)
    for n in ('bin','models/faster-whisper-small','installers','fonts','licenses','logs'):(out/n).mkdir(parents=True,exist_ok=True)
    if Path(args.ffmpeg).resolve().parent != Path(args.ffprobe).resolve().parent:
        raise ValueError('Media programs must belong to the same distribution')
    for src,name in ((args.ffmpeg,'ffmpeg.exe'),(args.ffprobe,'ffprobe.exe')):shutil.copyfile(src,out/'bin'/name)
    for dll in Path(args.ffmpeg).resolve().parent.glob('*.dll'):shutil.copyfile(dll,out/'bin'/dll.name)
    for name in MODEL_FILES:shutil.copyfile(model/name,out/'models/faster-whisper-small'/name)
    for name in ('MicrosoftEdgeWebView2RuntimeInstallerX64.exe','vc_redist.x64.exe'):shutil.copyfile(Path(args.components)/name,out/'installers'/name)
    shutil.copyfile(Path(args.components)/'NotoSansCJKsc-Regular.otf',out/'fonts/NotoSansCJKsc-Regular.otf')
    shutil.copyfile(Path(args.components)/'NotoSansCJK-OFL.txt',out/'licenses/NotoSansCJK-OFL.txt')
    shutil.copytree(args.licenses,out/'licenses/supplement',dirs_exist_ok=True)
    shutil.copyfile(args.notices,out/'THIRD_PARTY_NOTICES.md')
    for names,label in ((('LICENSE','LICENSE.txt','COPYING.GPLv3'),'FFmpeg-LICENSE.txt'),(('README.txt','README.md'),'FFmpeg-README.txt')):
        doc=next((Path(args.ffmpeg_docs)/n for n in names if (Path(args.ffmpeg_docs)/n).is_file()),None)
        if doc is None:
            if label!='FFmpeg-README.txt':raise ValueError('Media distribution notices missing')
            write(out/'licenses'/label,'FFmpeg distribution identity and source references: see FFmpeg-buildconf.txt and THIRD_PARTY_NOTICES.md.\n')
        else:shutil.copyfile(doc,out/'licenses'/label)
    if (Path(args.ffmpeg_docs)/'doc').is_dir():shutil.copytree(Path(args.ffmpeg_docs)/'doc',out/'licenses/FFmpeg-doc')
    conf=subprocess.run([str(out/'bin/ffmpeg.exe'),'-hide_banner','-buildconf'],capture_output=True,text=True,encoding='utf8',errors='replace',timeout=15)
    if conf.returncode:raise ValueError('Bundled FFmpeg failed to execute')
    write(out/'licenses/FFmpeg-buildconf.txt',conf.stdout+conf.stderr)
    launchers(out);documents(out)
    release={'product':'量禾混剪','edition':'Windows x64 folder','date':'2026-10-24','python':'3.12.14','default_inference':'cpu',
        'source_manifest_sha256':digest(args.source_manifest),'source_files':len(rows),'paid_api_calls':0,
        'personal_runtime_included':False,'compression':False,'generated_source_overrides':['app/AGENTS.md']}
    write(out/'RELEASE.json',json.dumps(release,ensure_ascii=False,indent=2))
    entries=[]
    for p in sorted(out.rglob('*')):
        if p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()):raise ValueError('Distribution must not contain links')
        if p.is_file():entries.append({'path':p.relative_to(out).as_posix(),'bytes':p.stat().st_size,'sha256':digest(p)})
    write(out/'FILES.sha256.json',json.dumps(entries,ensure_ascii=False,indent=2))
    return {**release,**verify_package(out),'path':str(out)}


def main():
    p=argparse.ArgumentParser();p.add_argument('--verify',type=Path)
    for n in ('source','source-manifest','output','python-home','site-packages','model','ffmpeg','ffprobe','components','licenses','notices','ffmpeg-docs'):p.add_argument('--'+n,type=Path)
    args=p.parse_args()
    if args.verify:result=verify_package(args.verify)
    else:
        if any(getattr(args,n.replace('-','_')) is None for n in ('source','source-manifest','output','python-home','site-packages','model','ffmpeg','ffprobe','components','licenses','notices','ffmpeg-docs')):p.error('build paths required')
        result=build(args)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
