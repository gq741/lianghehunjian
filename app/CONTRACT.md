# V1 implementation contract

Python package `mixstudio`, Flask HTTP on 127.0.0.1 ephemeral port, pywebview WebView2 desktop.
UI files `web/index.html`, `web/app.js`, `web/styles.css`. No Node runtime.
HTTP JSON envelopes `{ok:true,data:...}` or `{ok:false,error:"user-readable"}`.
All `/api/` calls require `X-Local-Token` from `<meta name="local-token" content="__LOCAL_TOKEN__">`.
Media URLs same-origin, allowlisted asset/output IDs, authorized by session cookie.
Native JS bridge: `window.pywebview.api.choose_folder()` / `choose_bgm()` -> absolute path or null.

## REST
- GET `/api/state` -> `{assets, jobs, drafts, settings, environment, folder}`.
- GET `/api/settings` -> public settings + `deepseek_key_set`, `fish_key_set`, never key values.
- PUT `/api/settings` -> validated settings fields; optional `deepseek_api_key`,`fish_api_key` on explicit save, blank preserves.
- POST `/api/library/scan` `{folder}` -> job.
- POST `/api/downloads` `{links:[...], folder}` -> jobs; 1–50 full share texts or HTTPS Douyin share/video URLs, normalize/dedupe and validate the entire batch before enqueue. Two active downloads maximum. Completion includes MP4 validation and automatic asset import; no scan request required. Enter queues one pasted share text; multiline paste queues a batch.
- POST `/api/prepare` `{asset_ids, product_name, target_seconds:25,count:1,allow_paid:true}` -> job.
- PUT `/api/drafts/<id>` `{segments:[{clip_id,text}]}` -> updated draft; no invented clip IDs.
- POST `/api/drafts/<id>/render` `{allow_paid:true}` -> job; separate confirmation for Fish paid generation.
- POST `/api/jobs/<id>/cancel` -> job. POST `/api/jobs/<id>/retry` `{allow_paid:false|true}` -> new job (explicit).
- POST `/api/assets/<id>/transcript` `{segments:[{start:0,end:2.5,text:"..."}]}` -> asset (manual import/correction).
- GET `/media/assets/<id>` / `/media/thumbs/<id>` / `/media/outputs/<job_id>` -> authorized local file.
- POST `/api/open-output` `{job_id}` -> opens known output folder via native desktop, never arbitrary command.

Asset: `{id,name,path,duration,width,height,imported_at,thumbnail_url,preview_url,transcript_status,clips:[]}`.
Clip: `{clip_id,asset_id,source_path,start,end,text}`. Times in seconds from original.
Draft: `{id,product_name,title,segments:[{clip_id,text}],clips:[Clip],target_seconds,revision,status,voiceover}`. Each draft contains 1–64 segments.
Job: `{id,kind,status,progress,message,error,created_at,result}` statuses queued/running/cancelling/cancelled/failed/completed/interrupted.
Environment: `{ffmpeg,ffprobe,asr_available,desktop_available,download_available,download_route,notes}`. Availability fields are booleans; `download_route` is `mobile_feed`, requiring FFmpeg/ffprobe.
Settings:
`output_dir`, `download_dir`, `asr_model_path`(existing local model folder), `asr_device` cpu|cuda,
`voice_id` default above, `deepseek_model`, `bgm_path`, `bgm_gain` .15,
`target_seconds`25, `output_count`1, `render_concurrency`1 (1..3),
`encoder` cpu|nvenc|qsv, `preset` fast|balanced|quality,
`width`720,`height`1280,`fps`30,`transition` fade|none,`transition_seconds`.2,
`clean_mode` none|blur|delogo, `clean_top_pct`75,`clean_height_pct`18,
`caption_plate`true, `subtitle_font_size`42, `subtitle_margin_pct`12.
Expose basic task duration/count on mix page; advanced defaults in settings.
AI inpainting is not yet validated: do not label blur/delogo as AI repair.

## media.py API (owned by media agent)
`probe(path:Path, ffprobe="ffprobe")->dict` `{duration,width,height,fps,has_audio}`.
`thumbnail(source:Path,dest:Path, ffmpeg="ffmpeg")->None`.
`render_video(clips:list[dict], voice_path:Path, output_path:Path, settings:dict, *, cancel=None, progress=None)->dict`
Each render clip `{source_path,start,end,text}` (text attached to this voice unit if supplied).
settings includes `subtitle_segments:[{start,end,text}]` on final voice timeline;
optional actual `duration_seconds`, otherwise probe voice.
Need extend coverage from bound same-product selected clips if voice longer; calculate overlaps.
Return `{path,duration,width,height,warnings:[]}`. Cancel is threading.Event; progress(float,message).
Must fail if insufficient permitted source coverage rather than truncate speech or loop silently.

## providers.py API (owned by providers agent)
`ProviderError`; `transcribe_video(path:Path, model_path:str, *, device="cpu",cancel=None,progress=None)->list[{start,end,text}]` subprocess isolated local faster-whisper, no auto model download.
`rewrite_clips(clips:list[dict], *, api_key:str, model:str, product_name:str, target_seconds:float,count:int, cancel=None)->list[dict]`
Return drafts each `{title,segments:[{clip_id,text}]}` with known IDs, no new facts; explicit input prompt constraints.
`synthesize_voice(text:str, output:Path, *, api_key:str, voice_id:str,cancel=None)->dict` `{path,duration?}`.
Fish first-party text-to-speech API, never log keys/raw error bodies. No uncertain-result automatic paid retries.
`download_douyin(url:str, folder:Path, *,cancel=None,progress=None)->dict` `{path,title,warning?}` using bounded HTTPS mobile Feed metadata for the exact video ID, then validated media URLs. No login extraction or overwrite; rejects other hosts/playlists. Downloads to an owned partial file, verifies video/audio and full decode before publishing; cleans failed partials.
Providers use standard library except optional faster-whisper/msgpack packages; all network mockable. Download does not invoke yt-dlp.

## responsibilities / test rules
Each agent adds unit tests only for its owned module, stdlib unittest for no extra install.
No paid network calls or user videos in tests. Root adds integration tests using synthetic lavfi+tones and mock providers.
Root owns settings encryption, SQLite store, jobs, orchestrator, HTTP, native launch, documentation, packaging.
