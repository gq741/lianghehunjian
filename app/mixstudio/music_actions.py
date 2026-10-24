"""Workbench music controls; owned local copies, no provider requests."""
from pathlib import Path
import math
import sqlite3

from .music_library import _digest


class MusicActions:
    def music_state(self, *, include_tracks=False, _settings=None):
        settings = self.settings.public() if _settings is None else _settings
        path = settings['bgm_path']
        current = None
        if path:
            source = Path(path)
            current = {'id':None, 'name':source.name, 'path':str(source),
                       'available':source.is_file(), 'managed':False}
        error = ''
        try:
            rows = self.music_library.list_tracks() if include_tracks else []
            row = None
            if not include_tracks:
                row = self.music_library.selected_track(path)
            elif current:
                row = next((row for row in rows if Path(row['path']).resolve()==Path(path).resolve()), None)
            if current and row:
                current.update(id=row['id'], name=row['name'], available=row['available'], managed=True)
                if current['available'] and self._media_identity_cache.signature(Path(path), _digest) != row['id']:
                    current['available'] = False
                    error = '已选择的音乐库副本被修改，请重新导入或主动关闭音乐；不会自动换歌'
        except (OSError, ValueError) as exc:
            rows = []
            error = str(exc)[:300] or '音乐库读取失败，请检查本地文件'
        if current and not current['available'] and not error:
            error = '已选择的背景音乐文件不存在，请重新导入并选择，或主动选择不加音乐；不会自动换歌'
        value = {'current':current, 'gain':settings['bgm_gain'], 'error':error}
        if include_tracks:
            value['tracks'] = [{**{k:row[k] for k in ('id','name','size','duration','available')},
                                'selected':bool(current and current['id']==row['id'])} for row in rows]
        return value

    def scan_music(self, folder):
        return self.music_library.scan_directory(folder)

    def require_music(self, settings):
        status = self.music_state(_settings=settings)
        if status['current'] and status['current']['managed'] and not status['current']['available']:
            raise ValueError(status['error'])

    def import_music(self, scan_id, candidate_ids):
        if (not isinstance(scan_id, str) or not scan_id or not isinstance(candidate_ids, list)
                or not 1 <= len(candidate_ids) <= 10000
                or any(not isinstance(v, str) or not v for v in candidate_ids)
                or len(set(candidate_ids)) != len(candidate_ids)):
            raise ValueError('请先选择文件夹，再勾选需要导入的歌曲')
        with self.action_lock:
            for job in self.store.active_jobs():
                if job['kind'] == 'music_import':
                    if job.get('payload') == {'scan_id':scan_id,'candidate_ids':candidate_ids}:
                        return job
                    raise ValueError('音乐正在导入，请等待完成或取消后再导入')
            previous = self.settings.public()['bgm_path']
            def work(event, progress):
                result = self.music_library.import_candidates(scan_id, candidate_ids,
                    cancel=event, progress=progress)
                if result.get('failed'):
                    if not (result.get('imported') or result.get('duplicates')):
                        raise ValueError('歌曲未导入：'+result['failed'][0]['error'])
                    result.setdefault('warnings', []).append(f"{len(result['failed'])}首歌曲未导入；已成功入库的歌曲继续保留，请重新扫描后处理失败项")
                # Only an empty default may adopt the first explicitly imported
                # track. A later user selection always wins over this worker.
                with self.action_lock:
                    choices = result.get('imported', [])+result.get('duplicates', [])
                    if (not event.is_set() and not previous and choices
                            and not self.store.meta('music_choice_set')
                            and self.settings.public()['bgm_path'] == previous):
                        path = self.music_library.resolve(choices[0]['id'])
                        if path.is_file():
                            try:
                                self.settings.update({'bgm_path':str(path)})
                                self.store.meta('music_choice_set', True)
                            except (OSError, ValueError, sqlite3.Error):
                                result.setdefault('warnings', []).append('歌曲已入库，默认歌曲尚未保存，请在列表重新选择')
                result['committed'] = True
                return result
            return self.jobs.submit('music_import', {'scan_id':scan_id,'candidate_ids':candidate_ids}, work)

    def select_music(self, track_id):
        with self.action_lock:
            if track_id is None:
                path = ''
            else:
                if not isinstance(track_id, str):
                    raise ValueError('请选择已入库的歌曲')
                source = self.music_library.resolve(track_id)
                if not source.is_file():
                    raise ValueError('所选歌曲文件不存在，请重新导入；默认音乐未改变')
                if self._media_identity_cache.signature(source, _digest) != track_id:
                    raise ValueError('音乐库副本已改变，请重新导入；默认音乐未改变')
                path = str(source)
            self.store.meta('music_choice_set', True)
            self.settings.update({'bgm_path':path})
            return self.music_state(include_tracks=True)

    def music_volume(self, gain):
        if type(gain) not in (int, float) or not math.isfinite(gain) or not 0 <= gain <= 1:
            raise ValueError('音乐音量须为0～1之间的数字')
        with self.action_lock:
            self.settings.update({'bgm_gain':gain})
            return self.music_state(include_tracks=True)

    def delete_music(self, track_ids, confirm):
        if confirm is not True:
            raise ValueError('请确认删除音乐；原文件夹中的歌曲不会删除')
        if (not isinstance(track_ids, list) or not 1 <= len(track_ids) <= 10000
                or any(not isinstance(v, str) or not v for v in track_ids)
                or len(set(track_ids)) != len(track_ids)):
            raise ValueError('请选择要删除的已入库歌曲')
        with self.action_lock:
            if any(j['kind'] in {'render','script','match','alignment','preflight','media_prepare','music_import'}
                   for j in self.store.active_jobs()):
                raise ValueError('正在生成、预检或导入，请等待任务结束后再删除音乐')
            before = self.music_state()['current']
            result = self.music_library.delete(track_ids)
            if (before and before['id'] in result['removed_ids']
                    and self.settings.public()['bgm_path'] == before['path']):
                try:
                    self.store.meta('music_choice_set', True)
                    self.settings.update({'bgm_path':''})
                except (OSError, ValueError, sqlite3.Error):
                    result.setdefault('warnings', []).append('歌曲已删除，默认选择尚未清除，请重新选歌或选择不加音乐')
            result.update(self.music_state(include_tracks=True))
            return result
