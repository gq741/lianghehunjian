# V2 current contract — 2026-10-23 flexible-reference revision

Existing local/Douyin libraries, real JD title, title-grounded script, explicit paid consent, Fish cache and FFmpeg remain.
Old E:/京东带货 is read-only. No automatic paid retries. Script interface and removal of mandatory fact review remain.

## Vision / settings
Default provider glm; model glm-5.3-flash; standard endpoint https://open.bigmodel.cn/api/paas/v4/chat/completions.
Input glm_api_key, DPAPI secrets['glm']; public glm_key_set and compatibility vision_key_set reveal booleans only.
Legacy Qwen key is preserved but never sent to GLM. Old config migrates in memory without rewriting user files.
3s windows, 3 frames, <=90 windows/source, <=2 windows/6 images per request (<=45 requests/source).
Uniform long-source sampling and partial tail; byte-identical tail frames are deduplicated and actual count reported.
Versioned prompt/sampling-aware cache keeps old files. Strictly verified old 5-frame observations may be migrated while preserving their actual sample times/quality provenance.

## Semantic candidates and timeline
match_segments returns candidates[{segment_id,clip_ids,confidence,reason}] and unmatched; IDs known usable only.
Ranked candidate lists may share IDs across sentences. Bounded batches cover all input scenes; no paid auto retry.
Manual saves replace the selected sentence candidate pool; unchecked old choices are not silently re-added. No blanket same-product semantic relevance.
plan_timeline(segments,scenes,candidates,target_seconds,transition_seconds,voice_seconds=None,phase='estimated'):
  version=1,status=ready|shortage,phase,speed,repeat_count,total_raw_seconds,total_seconds,transition_seconds,
  segments[{segment_id,raw_seconds,voice_seconds,required_seconds,available_seconds,missing_seconds,clip_ids}],
  windows[{clip_id,segment_id,unit_index,source_path,source_sha256,source_start,source_end,start,end,reuse_index}],
  shortages,warnings,search_strategy=bounded_multi_order.
Allocation priority: natural voice + relevant windows -> allowed reuse -> remove dissolve overlap -> mild picture slowdown where needed -> minimum necessary uniform voice speed <=1.5.
Each window keeps source-axis start/end and original-range identity. video_speed defaults to 1; mild slowdown is >=0.85. playback_seconds=(end-start)/video_speed. Long windows are trimmed; ample sentences retain normal picture speed. The renderer independently rechecks this mapping.
Original hash + ORIGINAL interval identity, including overlap, survives rename and internal recrop; no external re-encode fingerprinting.
Per final video peak original-range appearances <=2; total extras <=2. Trimming never resets identity.
Reference duration is an advisory writing/rhythm target, not a target+/-5 publication gate. Actual audio drives final timing; valid output remains 0.1..60s. No padding/truncating narration merely to fit the reference. A duration deviation alone never triggers new model requests.
Planner is bounded heuristic, not optimality proof. Report all unsatisfied sentences, don't randomly fill scenes.

## Copy timing readiness
Generation uses approximately 4 spoken units/s only as a writing guide. Character budgets are not hard gates for saving, matching or rendering. Valid nonempty narration may continue regardless of reference deviation.
Pre-match timing still rejects invalid inputs and inconsistent narration/segments. Estimated voice speed defaults to natural 1.0 instead of speeding up to chase the reference.
Actual measured Fish voice and footage are replanned, with original cache untouched, voice <=1.5, explicit semantic candidates, source identity/reuse and true audio/video coverage checks. No new model call on opening a saved draft.

## Orchestration / render
Estimated plan shared by automatic matching, manual candidate changes and preflight. Legacy ready drafts hydrate local response only.
Real Fish sentence lengths trigger fresh actual plan; preserve raw cached WAV, atempo work copy only, <=1.5, pitch preserved.
Captions follow measured processed WAV. Picture slowdown is local to covered source intervals; source video/audio caches remain untouched. Keep cache after errors; no implicit extra paid call.
media.render_video receives actual ready timeline_plan; independently validates source hashes, original ranges, repeat budget and timing.
Legacy V1 rendering path remains separately supported.

## UI / history
Automatic matching is default; manual preview/replacement optional. Dirty copy/candidates and missing plan clearly explain disabled controls.
Each of library/mix task areas renders <=20, active tasks first, bounded internal scroll, collapsed error details.
Backend retains separate recent histories plus every active task; full durable SQLite history unpruned.
POST /api/open-logs uses local session authentication and fixed runtime/logs path only.
Daily JSONL logs contain allowlisted status metadata, no request payloads/secrets/raw errors. Full task detail remains SQLite.

## Vision schema diagnostics
Response rejection retains a fixed, allowlisted field/rule and returned-item position; no model-supplied values or unknown keys are displayed. Item position refers to the returned array, never a guessed source window.
Diagnosis does not relax validation, auto-retry requests, change prompt/cache identity, or silently invent missing observations. Historical schema errors cannot identify a field when the original response was not retained.

## Audit repair — 2026-10-22
- Pending copy saves acknowledge only the submitted draft/editor version; later text edits survive old responses. Match jobs retain draft_id in public task state.
- Manual and automatic semantic candidate pools share the 4500-per-sentence bound; all source/range/usability and final repetition constraints remain.
- Visual usability uses its own 0.65 threshold; semantic relevance retains its separate 0.70 threshold. Vision windows use the real video track interval, not an audio-only container tail.
- Successful semantic batches persist as strict, atomically saved checkpoints keyed by draft revision, source/visual data, narration, model, endpoint and prompt. Only incomplete batches are submitted on an explicitly confirmed retry; invalid caches stop rather than silently trigger paid replays.
- Final combined 48kHz PCM audio corrects only cumulative sample quantization (<=one sample per sentence plus two endpoint samples); original Fish cache is untouched. The historical target+/-5 gate is superseded by the current soft-reference contract above; the 60s delivery ceiling remains.
- Hard-cut output uses cumulative frame-grid boundaries; final publication checks independent video/audio coverage. Narrow blur regions clamp both luma and chroma radii to their actual plane sizes.
- Per-render work directories are removed on success/failure/cancel without deleting persistent caches or outputs. Filesystem cleanup errors never retain a concurrency admission slot or mask the job result.
- External re-encode/recrop content lineage remains unimplemented; this repair does not claim to solve perceptual same-source detection.

## Second audit repair — 2026-10-22
- B01/B02: candidate saves are bound to draft ID and editor version; settings saves acknowledge only the submitted settings version. Later input, native-dialog changes and cross-draft edits remain dirty.
- B03/B04: successful renders carry published=true after validated media publication. A late cancel cannot discard that committed result. Queued Futures are explicitly cancelled and become terminal immediately, releasing draft guards without waiting for unrelated jobs.
- B05: assets retain historical records with availability/selectable and validation revisions. Completed scans reconcile missing/replaced/unreadable entries in the selected directory; old IDs never preview a different file's bytes. Restoring the original content and rescanning reactivates the original ID; other folders remain unaffected.
- B06: candidate saves support a bounded ordered clip_table plus per-segment clip_indices, with the unchanged business/source/reuse validation after decoding. Only the authenticated matches endpoint accepts up to 8 MiB; all other endpoints retain 512 KiB. Legacy clip_ids payloads remain supported within the route bound.
- B07/B08: non-hardlink publication finishes a destination-volume temporary copy before an atomic no-overwrite commit on Windows. Copy/commit failures remove only that temporary file. A quarter-turn rotation also reciprocates SAR before square-pixel normalization.
- B09: public settings expose only credentials_need_reset, never encrypted or plaintext credential data. Explicit confirmed recovery archives the unreadable ciphertext before replacing it with an empty DPAPI store. Healthy stores cannot be reset through this recovery route; other preferences and unsaved UI input remain unchanged. New keys must then be entered and saved by the user.
- These changes do not add provider requests, automatic paid retries, source-file edits, or history deletion. Real provider/network and native desktop/GPU acceptance are separate from the offline regression suite.


## Desktop whole-voice production — 2026-10-23
- POST /api/v2/preflight is a free cancellable job: keys/model configuration, actual local ASR/OCR model loading, filesystem space/write, source identity, HDR preparation, short video decode and CPU subtitle/filter encode smoke test. It is not a remote balance/availability check or full-file corruption guarantee.
- Desktop generate sends voice_first=true; preflight runs again before DeepSeek. Confirmed /api/v2/drafts/<id>/produce consents to Fish+GLM+DeepSeek matching, then independently checks resources before any paid request. Existing V2 drafts opt into this route in the state projection without rewriting history.
- Exactly one Fish utterance per video/copy+voice identity on cache miss; PCM frames/rate give real duration. Durable raw WAV and journal survive later failures/cancellation. Ambiguous submitted attempts need explicit retry consent, with attempt-bound retry guards. No automatic paid retries.
- Existing local faster-whisper model provides measured word onsets. Confirmed copy is matched to those words; sentence boundaries, captions and visual units share one timeline. This is ASR-assisted timing, NOT exact phoneme forced alignment; low coverage is reported before vision calls. Sentence-internal caption pagination remains approximate.
- Neighboring sentences with the same semantic candidate set may share a continuous visual unit. Captions retain original sentence boundaries. Voice remains 1.0; footage trims, transition reductions, bounded repeat and >=0.85 picture slowdown remain. Actual duration 0.1..60s, user reference is soft.
- Only used windows undergo local RapidOCR 3-frame subtitle-band detection, separately cached. Missing/unreliable detections are reported and not blindly erased. delogo/blur is region repair, NOT lossless AI inpainting. New captions have no black plate; original audio is excluded; optional BGM is mixed by the existing renderer.
- Three dedicated production workers do not occupy the common download/job pool. Only encoding takes render admission. Restart/retry resumes validated audio, alignment, vision, semantic and subtitle-location caches; text edits clear draft bindings, not paid files. Final output still requires independent stream checks/full decode before publication.
- Legacy render/match endpoints retain their contracts for historical clients and tests; the default desktop uses the new produce endpoint. Free checks and local OCR add no model API requests.


## Whole-voice audit repair — 2026-10-24
- Local alignment v2 preserves measured word onsets, labels word-internal / high-coverage neighboring-anchor estimates, and coalesces sub-frame sentences into caption/visual units without changing confirmed speech or raw audio. Global mismatch still stops before paid vision.
- One canonical grouped timeline now drives automatic/manual matching and final rendering. Grouping cannot conceal a sentence with no semantic candidates. Whole-voice drafts entering the old render API are routed to the whole-voice implementation; genuine legacy drafts retain their contract.
- Raw local ASR is cached independently of successful sentence alignment and segmentation. Invalid derived caches are locally recomputed; a cache write error does not discard an in-memory local result. POST /api/v2/drafts/<id>/realign reuses a verified existing utterance, never invokes paid providers, and supports refresh=true for local ASR only. The original audio's voice identity survives a change to default settings.
- Pending Fish attempts distinguish definite rejection/pre-submit cancellation from submitted uncertain results. Oversized, truncated, or invalid completed audio responses remain fenced for explicit retry; no automatic paid retry is introduced. Over-60s copy gets an advisory in the paid confirmation, not a hard character gate. Already measured over-60s audio is retained for preview, not repeatedly regenerated.
- Free preflight remains before paid stages; it identifies the missing service and exposes checked_at and limitations. ASR and OCR probes have independent fingerprints; unchanged source/binary/encode settings can reuse successful smoke checks while source identity, permissions, disk space and current configuration are rechecked.
- Subtitle OCR cache v2 includes OCR dependency/model identity, bounds negative results to one hour, and treats malformed records as misses. Multi-frame geometric agreement limits repair to a horizontal/vertical rectangle; short-lived or moving subtitles may be missed and are reported, not claimed erased. Local OCR errors remain recoverable with paid audio/matching retained; old subtitles are never silently skipped as a success workaround.
- HDR original-mode decisions include a fresh probe version and a change-sensitive source version. Rendering passes cancellation and per-request identity reuse through original/SDR checks. Subtitle caches and abandoned subtitle-locate work join existing guarded storage cleanup.
- UI marks task kinds and migration costs, prevents edits while a draft is busy, provides local-only realignment, and shows the actual preflight conclusion. Audio Range previews reuse a completed SHA check only for an unchanged file version; URL identity still must match the current binding.


## Workbench UI and owned music library — 2026-10-24
- Task errors/details occupy one ellipsized line after the task title. Full text opens a separate scrollable dialog, never enlarging the task row; both task areas share the renderer.
- JD link Clear/Paste only changes the input and its dirty state. Reading the product still requires the explicit Read button. Native clipboard reads occur only on click; no clipboard content is logged or used in tests.
- Background music controls moved from Settings to a collapsed workbench panel between video parameters and copy. The collapsed summary shows the current song name. Missing/changed selected copies open a local panel error, never silently select a replacement.
- Choose a local folder (direct files only), check songs, then import: valid audio is copied into runtime/music-library, content-SHA deduplicated, atomically catalogued, cancellable and independent of the original folder afterwards. Library scanning/import/selection/deletion has no model API calls.
- The first explicit import can initialize an empty never-chosen default. Thereafter explicit selection or No Music persists across restart and further imports. User selection wins over a completing import worker. Volume is independently saved without overwriting unrelated settings or later edits.
- Deletion requires explicit confirmation and only recycles registered owned copies, never the scanned originals. Deleting the selected song switches to No Music without randomly picking another. Active production/preflight/import tasks block deletion. Corrupt catalogs and failed recycle operations preserve prior records; partial import success remains visible.
- Current owned song identity is checked using a completed SHA for an unchanged file version before selection and paid production. Generic cache cleanup does not delete the persistent music library. Older external bgm_path values are retained until the user explicitly imports/selects a managed copy.
