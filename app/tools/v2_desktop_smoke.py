"""Native V2 smoke: actual isolated Studio + WebView2, generated fixture media only.

No product/model/download requests are submitted. HTTP writes are rejected before
route execution; only UI selection/navigation/textarea events are exercised.
Timing drafts and one completed script job are seeded in the isolated store before
startup, not created by paid requests or injected into JavaScript application state.
Run with the existing studio virtualenv. The window closes itself.
"""
from __future__ import annotations

import importlib.metadata
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT.parent
sys.path.insert(0, str(ROOT))
SHORT_VOICE = "本地测试文案。" * 5  # 30 spoken units / 4 = 7.5 seconds.
READY_VOICE = "本地测试文案。" * 16  # 96 spoken units / 4 = 24 seconds.
SPEED_VOICE = "本地测试文案。" * 20 + "显示"  # 122 units / 4 = 30.5 seconds; fits at 1.017x.
OLD_REVIEW_VOICE = "本地测试文案。" * 18 + "显示器。"  # 111 units / 4 = 27.75 seconds; obsolete review metadata.
LONG_VOICE = "本地测试文案。" * 45  # 270 spoken units / 4 = 67.5 seconds.

import webview
from flask import jsonify, request
from werkzeug.serving import make_server
from mixstudio.desktop import DialogBridge
from mixstudio.http_app import create_app
from mixstudio.service import Studio


def main():
    run_root = EVIDENCE / ("desktop-v2-runtime-" + uuid.uuid4().hex[:10])
    run_root.mkdir(parents=True)
    report = EVIDENCE / "desktop-v2.json"
    started = time.monotonic()
    result = {
        "test": "real isolated pywebview/WebView2 + production V2 HTML/JS/HTTP",
        "status": "running", "python": sys.version, "platform": platform.platform(),
        "versions": {name: importlib.metadata.version(name) for name in ("pywebview", "pythonnet", "Flask", "Werkzeug")},
        "project": str(ROOT), "runtime": str(run_root), "production_runtime_used": False,
        "private_mode": True, "paid_api_calls": 0,
        "fixture_kind": "locally generated color media and synthetic timeline/job UI fixtures; no real matching or rendering",
        "fixture_job_ids": ["native-script-timing-fixture"],
        "events": [], "requests": [], "assertions": [], "errors": [],
    }
    lock = threading.RLock()
    complete = threading.Event()
    server = studio = window = None

    def save():
        with lock:
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def event(name):
        with lock:
            result["events"].append({"name": name, "seconds": round(time.monotonic() - started, 3)})

    def check(name, passed):
        result["assertions"].append({"name": name, "passed": bool(passed)})
        if not passed:
            raise AssertionError(name)

    class Bridge(DialogBridge):
        def smoke_ping(self, value):
            event("python_bridge_called")
            return {"echo": value, "python": True}

    def exercise():
        try:
            check("actual document loaded within 25 seconds", window.events.loaded.wait(25))
            deadline = time.monotonic() + 15
            snapshot = {}
            while time.monotonic() < deadline:
                snapshot = window.evaluate_js("""(() => ({
                    title:document.title,ready:document.readyState,
                    connection:document.querySelector('#connection-text')?.textContent,
                    nav:Array.from(document.querySelectorAll('[data-page]')).map(x=>x.dataset.page),
                    bridge_methods:Object.keys(window.pywebview?.api || {}),
                    token_present:!!document.querySelector('meta[name="local-token"]')?.content,
                    user_agent:navigator.userAgent, dimensions:[innerWidth,innerHeight],
                    asset_cards:document.querySelectorAll('#asset-grid .asset-card').length,
                    local_tab:document.querySelector('#library-tab-local')?.textContent,
                    douyin_tab:document.querySelector('#library-tab-douyin')?.textContent,
                    scripts:Array.from(document.scripts).map(x=>x.src).filter(Boolean)
                }))()""")
                if snapshot.get("connection") == "本地服务已连接" and snapshot.get("asset_cards") == 1 and "smoke_ping" in snapshot.get("bridge_methods", []):
                    break
                time.sleep(.1)
            result["initial_document"] = snapshot
            check("production JavaScript connected to isolated HTTP", snapshot.get("connection") == "本地服务已连接")
            check("actual application title", str(snapshot.get("title", "")).endswith("京东自动混剪"))
            check("all three main pages exist", set(snapshot.get("nav", [])) >= {"library", "mix", "settings"})
            check("session token injected without disclosure", snapshot.get("token_present"))
            check("bridge exposes only choose_folder/choose_bgm/smoke_ping", set(snapshot.get("bridge_methods", [])) == {"choose_folder", "choose_bgm", "smoke_ping"})
            check("actual Edge WebView runtime", "Edg/" in snapshot.get("user_agent", ""))
            check("both requested library tabs exist", snapshot.get("local_tab") == "本地素材库" and snapshot.get("douyin_tab") == "抖音素材库")
            check("real local fixture imported and rendered", snapshot.get("asset_cards") == 1)
            window.evaluate_js("""window.__smokeReply=null; window.pywebview.api.smoke_ping('v2-native').then(x=>window.__smokeReply=x); true""")
            ping = None
            for _ in range(100):
                ping = window.evaluate_js("window.__smokeReply")
                if ping is not None:
                    break
                time.sleep(.05)
            result["bridge_reply"] = ping
            check("JavaScript to Python bridge actually called", ping == {"echo": "v2-native", "python": True})

            local = window.evaluate_js("""(() => {
                const check=document.querySelector('#asset-grid input[type=checkbox]');check.click();
                return {selected:document.querySelector('#selection-count').textContent,source:document.querySelector('#selection-sources').textContent};
            })()""")
            result["local_selection"] = local
            check("local fixture selected through real click handler", local["selected"] == "已选 1 个视频" and "本地 1 个" in local["source"])
            combined = window.evaluate_js("""(() => {
                document.querySelector('#library-tab-douyin').click();
                const cards=document.querySelectorAll('#asset-grid .asset-card').length;
                document.querySelector('#asset-grid input[type=checkbox]').click();
                return {cards,selected:document.querySelector('#selection-count').textContent,source:document.querySelector('#selection-sources').textContent,
                    local_hidden:document.querySelector('#library-panel-local').hidden,douyin_hidden:document.querySelector('#library-panel-douyin').hidden};
            })()""")
            result["combined_selection"] = combined
            check("download fixture is separate library, no actual download", combined["cards"] == 1 and combined["local_hidden"] and not combined["douyin_hidden"])
            check("shared cross-library selection", combined["selected"] == "已选 2 个视频" and "本地 1 个" in combined["source"] and "抖音 1 个" in combined["source"])

            before_requests = len(result["requests"])
            textarea = window.evaluate_js("""(() => {
                const box=document.querySelector('#download-link');box.focus();box.value='https://v.douyin.com/fixture_one/';
                box.setSelectionRange(box.value.length,box.value.length);
                const enter=new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true});
                const accepted=box.dispatchEvent(enter);
                // Synthetic keyboard events do not perform the browser default action.
                // Use browser editing command to verify actual textarea newline retention.
                const inserted=document.execCommand('insertText',false,'\\nhttps://v.douyin.com/fixture_two/');
                box.dispatchEvent(new Event('input',{bubbles:true}));
                return {tag:box.tagName,lines:box.value.split('\\n').length,has_newline:box.value.includes('\\n'),enter_unprevented:accepted,editing_command:inserted};
            })()""")
            time.sleep(.2)
            result["textarea"] = textarea
            check("real multiline textarea retains newline", textarea["tag"] == "TEXTAREA" and textarea["lines"] == 2 and textarea["has_newline"])
            check("Enter handler does not submit or prevent newline", textarea["enter_unprevented"] and all(item["method"] == "GET" for item in result["requests"][before_requests:]))
            cleared = window.evaluate_js("""(() => {
                document.querySelector('#clear-download').click();
                return {text:document.querySelector('#download-link').value,selected:document.querySelector('#selection-count').textContent};
            })()""")
            check("clear empties only text, keeps both selected assets", cleared == {"text": "", "selected": "已选 2 个视频"})
            check("clear and Enter never submit downloads", not any(item["path"] == "/api/downloads" for item in result["requests"]))
            local_again = window.evaluate_js("""(() => {
                document.querySelector('#library-tab-local').click();
                return {checked:document.querySelector('#asset-grid input[type=checkbox]').checked,selected:document.querySelector('#selection-count').textContent};
            })()""")
            check("tab switch preserves local selection", local_again == {"checked": True, "selected": "已选 2 个视频"})
            mix = window.evaluate_js("""(() => {
                document.querySelector('#go-mix').click();
                return {page:document.querySelector('#page-name').textContent,visible:!document.querySelector('#page-mix').hidden,
                    selected:document.querySelector('#mix-selection-count').textContent,
                    product_input:!!document.querySelector('#product-url'),old_name_input:!!document.querySelector('#product-name'),
                    seconds_max:document.querySelector('#target-seconds').max,count_max:document.querySelector('#output-count').max,
                    seconds:document.querySelector('#target-seconds').value,count:document.querySelector('#output-count').value};
            })()""")
            result["mix_page"] = mix
            check("next enters actual mix page with both assets", mix["page"] == "混剪工作台" and mix["visible"] and mix["selected"] == "本次使用 2 个视频")
            check("JD URL replaces old product name input", mix["product_input"] and not mix["old_name_input"])
            check("limits are 60 seconds and five outputs", mix["seconds_max"] == "60" and mix["count_max"] == "5")
            check("defaults remain 25 seconds and one output", mix["seconds"] == "25" and mix["count"] == "1")
            timing = window.evaluate_js("""(() => ({
                active:document.querySelector('#draft-select').value,
                voice:document.querySelector('#draft-voiceover').value,
                visible:!document.querySelector('#draft-workspace').hidden,
                meta:document.querySelector('#draft-meta').textContent,
                warning:document.querySelector('#draft-timing-status').textContent,
                warning_visible:!document.querySelector('#draft-timing-status').hidden,
                match_disabled:document.querySelector('#match-draft').disabled,
                render_disabled:document.querySelector('#render-draft').disabled,
                save_disabled:document.querySelector('#save-draft').disabled,
                badges:Array.from(document.querySelectorAll('#mix-jobs .job-state')).map(x=>x.textContent),
                job_copy:document.querySelector('#mix-jobs').textContent
            }))()""")
            result["timing_initial"] = timing
            check("out-of-budget saved fixture is visible without regeneration", timing["visible"] and timing["active"] == "native-timing-short" and timing["voice"] == SHORT_VOICE)
            check("short narration shows measured estimate and actionable budget", timing["warning_visible"] and "7.50" in timing["meta"] and "80～120" in timing["warning"] and "至少补充 50 字" in timing["warning"])
            check("saved short narration blocks matching and rendering even with stale ready match status", timing["match_disabled"] and timing["render_disabled"] and timing["save_disabled"])
            check("completed script job is clearly marked pending duration adjustment", timing["badges"] == ["待调整时长"] and "已保留全部文案" in timing["job_copy"] and "无需重新生成" in timing["job_copy"])

            def edit_voice(text):
                return window.evaluate_js("""(() => {
                    const box=document.querySelector('#draft-voiceover');box.value=""" + json.dumps(text, ensure_ascii=False) + """;
                    box.dispatchEvent(new Event('input',{bubbles:true}));
                    return {voice:box.value,meta:document.querySelector('#draft-meta').textContent,
                        warning:document.querySelector('#draft-timing-status').textContent,
                        status:document.querySelector('#draft-save-status').textContent,
                        match_disabled:document.querySelector('#match-draft').disabled,
                        render_disabled:document.querySelector('#render-draft').disabled,
                        save_disabled:document.querySelector('#save-draft').disabled};
                })()""")

            before_timing_requests = len(result["requests"])
            ready_edit = edit_voice(READY_VOICE)
            result["timing_ready_unsaved"] = ready_edit
            check("textarea input updates spoken count and estimate immediately", ready_edit["voice"] == READY_VOICE and "24.00" in ready_edit["meta"] and "当前 96 字" in ready_edit["warning"] and "字数符合预估范围" in ready_edit["warning"])
            check("corrected but unsaved text cannot start matching or rendering", ready_edit["match_disabled"] and ready_edit["render_disabled"] and not ready_edit["save_disabled"] and "未保存" in ready_edit["status"])
            long_edit = edit_voice(LONG_VOICE)
            result["timing_long_unsaved"] = long_edit
            check("overlong edit shows real estimate and reduction beyond 1.5x allowance", "67.50" in long_edit["meta"] and "当前 270 字" in long_edit["warning"] and "至少删减 90 字" in long_edit["warning"] and "1.5 倍" in long_edit["warning"])
            check("overlong edit remains blocked before paid steps", long_edit["match_disabled"] and long_edit["render_disabled"] and not long_edit["save_disabled"])
            check("live narration editing makes no business HTTP request", all(item["method"] == "GET" for item in result["requests"][before_timing_requests:]))

            discard = window.evaluate_js("""(() => {
                const select=document.querySelector('#draft-select');select.value='native-timing-ready';
                select.dispatchEvent(new Event('change',{bubbles:true}));
                const dialog=document.querySelector('#confirm-dialog');
                const opened=dialog.open;document.querySelector('#confirm-accept').click();
                return {opened};
            })()""")
            check("switching dirty narration uses the actual discard confirmation", discard["opened"])
            ready_saved = {}
            for _ in range(100):
                ready_saved = window.evaluate_js("""(() => ({
                    active:document.querySelector('#draft-select').value,
                    voice:document.querySelector('#draft-voiceover').value,
                    meta:document.querySelector('#draft-meta').textContent,
                    match_disabled:document.querySelector('#match-draft').disabled,
                    save_disabled:document.querySelector('#save-draft').disabled,
                    status:document.querySelector('#draft-save-status').textContent
                }))()""")
                if ready_saved.get("active") == "native-timing-ready" and ready_saved.get("save_disabled"):
                    break
                time.sleep(.05)
            result["timing_ready_saved"] = ready_saved
            check("saved in-budget draft allows matching without changing stored text", ready_saved.get("voice") == READY_VOICE and not ready_saved.get("match_disabled", True) and ready_saved.get("save_disabled") and "24.00" in ready_saved.get("meta", ""))
            check("discarded UI edits never overwrite saved fixture", studio.store.draft("native-timing-short")["voiceover"] == SHORT_VOICE and studio.store.draft("native-timing-short")["revision"] == 1)
            legacy_review_ui = window.evaluate_js("""(() => {
                const select=document.querySelector('#draft-select');select.value='native-fact-review';
                select.dispatchEvent(new Event('change',{bubbles:true}));
                return {active:select.value,voice:document.querySelector('#draft-voiceover').value,
                    review_present:!!document.querySelector('#draft-fact-status'),
                    meta:document.querySelector('#draft-meta').textContent,
                    match_disabled:document.querySelector('#match-draft').disabled,
                    render_disabled:document.querySelector('#render-draft').disabled,
                    save_disabled:document.querySelector('#save-draft').disabled,
                    save_label:document.querySelector('#save-draft').textContent,
                    match_title:document.querySelector('#match-draft').title,
                    match_status:document.querySelector('#match-status').textContent,
                    option:select.selectedOptions[0].textContent};
            })()""")
            result["legacy_review_metadata"] = legacy_review_ui
            check("27.75-second saved fixture with old needs_review permits direct matching", legacy_review_ui["active"] == "native-fact-review" and legacy_review_ui["voice"] == OLD_REVIEW_VOICE and "27.75" in legacy_review_ui["meta"] and not legacy_review_ui["match_disabled"])
            check("obsolete copy-review panel and pending-review selector label are absent", not legacy_review_ui["review_present"] and legacy_review_ui["option"] == "显示器口播" and "商品信息" not in legacy_review_ui["match_title"])
            check("ordinary save button needs no review action on unchanged draft", legacy_review_ui["save_disabled"] and legacy_review_ui["save_label"] == "保存修改")
            check("render still waits for complete matching", legacy_review_ui["render_disabled"] and legacy_review_ui["match_status"] == "等待匹配画面")
            legacy_edit = edit_voice("新" + OLD_REVIEW_VOICE[1:])
            result["legacy_review_unsaved"] = legacy_edit
            check("unsaved text still blocks downstream while ordinary saving is enabled", not legacy_edit["save_disabled"] and legacy_edit["match_disabled"] and legacy_edit["render_disabled"] and "未保存" in legacy_edit["status"])
            stored_legacy = studio.store.draft("native-fact-review")
            check("rendering UI does not migrate old review metadata or saved text", stored_legacy["voiceover"] == OLD_REVIEW_VOICE and stored_legacy["fact_status"] == "needs_review" and stored_legacy["fact_issues"][0]["term"] == "刷新率")
            check("ordinary editing with obsolete review metadata makes no business request", all(item["method"] == "GET" for item in result["requests"]))
            # Synthetic timeline plans exercise the real UI, not a paid match or render.
            def select_saved(identifier, expected_voice=READY_VOICE):
                window.evaluate_js("""(() => {
                    const select=document.querySelector('#draft-select');select.value=""" + json.dumps(identifier) + """;
                    select.dispatchEvent(new Event('change',{bubbles:true}));
                    if(document.querySelector('#confirm-dialog').open)document.querySelector('#confirm-accept').click();
                })()""")
                for _ in range(100):
                    ready = window.evaluate_js("document.querySelector('#draft-voiceover').value === " + json.dumps(expected_voice) + " && document.querySelector('#save-draft').disabled")
                    if ready:
                        time.sleep(.2)  # Let the 150 ms button color transition finish before measuring.
                        break
                    time.sleep(.03)

            select_saved("native-timing-speed", SPEED_VOICE)
            speed_saved = window.evaluate_js("""(() => ({
                voice:document.querySelector('#draft-voiceover').value,
                warning:document.querySelector('#draft-timing-status').textContent,
                warning_class:document.querySelector('#draft-timing-status').className,
                option:document.querySelector('#draft-select').selectedOptions[0].textContent,
                status:document.querySelector('#draft-save-status').textContent,
                meta:document.querySelector('#draft-meta').textContent,
                match_disabled:document.querySelector('#match-draft').disabled,
                render_disabled:document.querySelector('#render-draft').disabled,
                save_disabled:document.querySelector('#save-draft').disabled
            }))()""")
            result["timing_speed_saved"] = speed_saved
            check("122 units at 30.5 seconds unlock direct matching with stale needs_adjustment metadata", speed_saved["voice"] == SPEED_VOICE and not speed_saved["match_disabled"] and speed_saved["save_disabled"])
            check("speed-aware banner explains approximately 1.017x and measured final calibration", "30.50" in speed_saved["meta"] and "1.017" in speed_saved["warning"] and "可继续匹配" in speed_saved["warning"] and "实测配音" in speed_saved["warning"])
            check("old delete-two instruction is not shown and selector uses speed-ready label", "待调整" not in speed_saved["warning"] and "删减" not in speed_saved["warning"] and "needs-adjustment" not in speed_saved["warning_class"] and "可倍速适配" in speed_saved["option"])
            check("speed readiness does not bypass missing timeline or require resaving", speed_saved["render_disabled"] and speed_saved["save_disabled"] and speed_saved["status"] == "文案已保存")
            check("legacy timing metadata remains unchanged in isolated storage", studio.store.draft("native-timing-speed")["timing_status"] == "needs_adjustment" and studio.store.draft("native-timing-speed")["voiceover"] == SPEED_VOICE)
            speed_job = "speed-ready-native"
            result["fixture_job_ids"].append(speed_job)
            studio.store.create_job(speed_job, "script", {"target_seconds": 25, "count": 1})
            studio.store.update_job(speed_job, status="completed", progress=1, message="文案已保留，待调整时长。请删减 2 字。", result={
                "draft_ids": ["native-timing-speed"], "needs_adjustment_ids": ["native-timing-speed"],
            })
            window.evaluate_js("document.querySelector('#refresh-state').click()")
            speed_job_ui = {}
            for _ in range(100):
                speed_job_ui = window.evaluate_js("""(() => {
                    const row=[...document.querySelectorAll('#mix-jobs .job-row')].find(x=>x.textContent.includes('speed-re'));
                    return row ? {badge:row.querySelector('.job-state').textContent,text:row.textContent} : {};
                })()""")
                if speed_job_ui:
                    break
                time.sleep(.05)
            result["timing_speed_legacy_job"] = speed_job_ui
            check("legacy completed 122-unit task no longer displays stale duration review or deletion instruction", speed_job_ui.get("badge") == "已完成" and "待调整时长" not in speed_job_ui.get("text", "") and "删减 2" not in speed_job_ui.get("text", "") and "可继续匹配" in speed_job_ui.get("text", ""))
            speed_edit = edit_voice("中" * 181)
            result["timing_speed_over_limit"] = speed_edit
            check("181-unit edit exceeds 1.5x cap and stays blocked with exact one-unit reduction", "45.25" in speed_edit["meta"] and "1.5 倍" in speed_edit["warning"] and "删减 1 字" in speed_edit["warning"] and speed_edit["match_disabled"] and speed_edit["render_disabled"] and not speed_edit["save_disabled"])
            check("speed readiness and edits submit no business writes", all(item["method"] == "GET" for item in result["requests"]))

            select_saved("native-timeline-shortage")
            timeline_short = window.evaluate_js("""(() => ({
                text:document.querySelector('#timeline-summary').textContent,
                rows:document.querySelectorAll('#timeline-summary tbody tr').length,
                review_open:document.querySelector('#match-review').open,
                match_disabled:document.querySelector('#match-draft').disabled,
                render_disabled:document.querySelector('#render-draft').disabled,
                reason:document.querySelector('#render-action-reason').textContent,
                label:document.querySelector('#match-draft').textContent,
                primary:getComputedStyle(document.querySelector('#match-draft')).backgroundColor
            }))()""")
            result["timeline_shortage"] = timeline_short
            check("all sentence net requirements and deficits visible without opening manual editor", timeline_short["rows"] == 2 and "所需净画面" in timeline_short["text"] and "可用净画面" in timeline_short["text"] and "3.00" in timeline_short["text"] and not timeline_short["review_open"])
            check("automatic matching is primary and shortage explains disabled rendering", timeline_short["label"] == "自动匹配画面" and timeline_short["primary"] == "rgb(8, 125, 118)" and not timeline_short["match_disabled"] and timeline_short["render_disabled"] and "缺口" in timeline_short["reason"])
            check("estimated speed and repeat budget are explicit", "预计配音倍速 1.25" in timeline_short["text"] and "额外复用 1/2" in timeline_short["text"])
            select_saved("native-timeline-ready")
            timeline_ready = window.evaluate_js("""(() => ({
                text:document.querySelector('#timeline-summary').textContent,
                render_disabled:document.querySelector('#render-draft').disabled,
                reason_hidden:document.querySelector('#render-action-reason').hidden
            }))()""")
            result["timeline_ready"] = timeline_ready
            check("complete actual timeline unlocks render without requesting new recognition", "时间线完整" in timeline_ready["text"] and "实测配音" in timeline_ready["text"] and not timeline_ready["render_disabled"] and timeline_ready["reason_hidden"])

            for i in range(27):
                identifier = f"native-task-{i}"
                result["fixture_job_ids"].append(identifier)
                studio.store.create_job(identifier, "download", {})
                studio.store.update_job(identifier, status="completed", progress=1, message="本地预置任务")
            result["fixture_job_ids"].extend(["native-active-download", "native-task-error"])
            studio.store.create_job("native-active-download", "download", {})
            studio.store.update_job("native-active-download", status="running", progress=.3, message="预置进行中任务，无实际下载", created_at=time.time() - 1000)
            studio.store.create_job("native-task-error", "match", {})
            studio.store.update_job("native-task-error", status="failed", error="本地预置错误详情。" * 100)
            window.evaluate_js("document.querySelector('#refresh-state').click()")
            for _ in range(100):
                if window.evaluate_js("document.querySelector('#download-jobs .job-row')?.textContent.includes('处理中') && !!document.querySelector('#mix-jobs .job-details')"): break
                time.sleep(.05)
            # Make each tray visible before measuring its actual layout.
            window.evaluate_js("document.querySelector('[data-page=library]').click();document.querySelector('#library-queue').open=true")
            tasks = window.evaluate_js("""(() => {
                const list=document.querySelector('#download-jobs'),rows=[...list.querySelectorAll('.job-row')];
                const result={count:rows.length,height:list.getBoundingClientRect().height,scroll:list.scrollHeight>list.clientHeight,
                    row_heights:rows.map(x=>x.getBoundingClientRect().height),
                    first:rows[0].textContent,cancel:!!rows[0].querySelector('button'),
                    overflow:getComputedStyle(list).overflowY};
                document.querySelector('#download-next').click();result.next_count=list.querySelectorAll('.job-row').length;
                document.querySelector('#download-prev').click();return result;
            })()""")
            result["compact_task_tray"] = tasks
            check("download tray renders at most 20 records with internal scrolling", tasks["count"] == 20 and tasks["height"] == 320 and tasks["scroll"] and tasks["overflow"] == "auto")
            check("ordinary task rows stay within 44 to 52 pixels", all(44 <= height <= 52 for height in tasks["row_heights"]))
            check("active task cancellation remains first and older tasks remain paged", "处理中" in tasks["first"] and tasks["cancel"] and tasks["next_count"] == 8)
            window.evaluate_js("document.querySelector('[data-page=mix]').click()")
            error_ui = window.evaluate_js("""(() => {
                const details=document.querySelector('#mix-jobs .job-details');
                return {exists:!!details,collapsed:details&&!details.open,full_length:details?.querySelector('p').textContent.length,
                    logs_button:document.querySelector('#open-mix-logs').textContent,
                    height:document.querySelector('#mix-jobs').getBoundingClientRect().height};
            })()""")
            result["collapsed_error"] = error_ui
            check("long errors are collapsed but full text remains available", error_ui["exists"] and error_ui["collapsed"] and error_ui["full_length"] > 500)
            check("mix tray is fixed height with local logs access", error_ui["height"] == 320 and error_ui["logs_button"] == "打开日志文件夹")
            layout = window.evaluate_js("""(() => {
                const product=document.querySelector('.product-panel').getBoundingClientRect(),params=document.querySelector('.prepare-panel').getBoundingClientRect();
                return {page_width:document.documentElement.scrollWidth,viewport:innerWidth,
                    separated:product.bottom <= params.top,
                    output_border:getComputedStyle(document.querySelector('.preview-pane')).borderTopStyle,
                    main_label:document.querySelector('#match-draft').textContent};
            })()""")
            result["layout_boundaries"] = layout
            check("desktop page has no horizontal overflow", layout["page_width"] <= layout["viewport"])
            check("product parameters and output have separate visual boundaries", layout["separated"] and layout["output_border"] == "solid")
            check("timeline and compact task inspection submit no business writes", all(item["method"] == "GET" for item in result["requests"]))
            duration = window.evaluate_js("""(() => {
                const input=document.querySelector('#target-seconds');input.value='60';input.dispatchEvent(new Event('input',{bubbles:true}));
                return document.querySelector('#duration-hint').textContent;
            })()""")
            check("60-second reference gives 55 to 60-second tolerance", "55～60" in duration)
            settings = window.evaluate_js("""(() => {
                document.querySelector('[data-page=settings]').click();
                const vision=document.querySelector('#set-glm_api_key');
                return {page:document.querySelector('#page-name').textContent,visible:!document.querySelector('#page-settings').hidden,
                    vision_type:vision.type,vision_value:vision.value,vision_status:document.querySelector('#glm-key-status').textContent,
                    deepseek_empty:document.querySelector('#set-deepseek_api_key').value==='',fish_empty:document.querySelector('#set-fish_api_key').value==='',
                    model:document.querySelector('#set-vision_model').value,endpoint:document.querySelector('#set-vision_endpoint').value,
                    max_seconds:document.querySelector('#set-target_seconds').max,max_count:document.querySelector('#set-output_count').max};
            })()""")
            result["settings_page"] = settings
            check("settings navigation works", settings["page"] == "设置" and settings["visible"])
            check("all keys empty; visual key is password field", settings["vision_type"] == "password" and settings["vision_value"] == "" and settings["vision_status"] == "未配置" and settings["deepseek_empty"] and settings["fish_empty"])
            check("visual defaults are GLM-5.3-Flash endpoint/model", settings["model"] == "glm-5.3-flash" and settings["endpoint"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions")
            check("settings limits agree with workbench", settings["max_seconds"] == "60" and settings["max_count"] == "5")
            result["renderer"] = getattr(webview, "renderer", None)
            check("renderer is edgechromium", result["renderer"] == "edgechromium")
        except Exception as exc:
            result["errors"].append({"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()})
        finally:
            event("destroy_requested")
            save()
            window.destroy()
            complete.set()

    def watchdog():
        if not complete.wait(60):
            result["errors"].append({"type": "TimeoutError", "message": "native smoke exceeded 60 seconds"})
            save()
            if window:
                try:
                    window.destroy()
                except Exception:
                    pass

    try:
        ffmpeg = shutil.which("ffmpeg")
        check("local FFmpeg available for tiny fixture videos", bool(ffmpeg))
        paths = []
        for name, color in (("local-fixture", "red"), ("douyin-fixture", "blue")):
            folder = run_root / name
            folder.mkdir()
            path = folder / (name + ".mp4")
            completed = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:s=160x240:r=10:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(path)], capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            check(name + " generated locally", completed.returncode == 0 and path.is_file())
            paths.append(path)
        studio = Studio(run_root / "app-data")
        studio.settings.update({"download_dir": str(paths[1].parent), "output_dir": str(run_root / "output")})
        fixtures = []
        for path, library in zip(paths, ("local", "douyin")):
            asset = studio._import_one(path, threading.Event())
            studio._mark_library(asset, library)
            fixtures.append({"library": library, "path": str(path), "asset_id": asset["id"], "sha256": asset["sha256"]})
        studio.store.meta("folder", str(paths[0].parent))
        result["fixtures"] = fixtures
        fixture_product = {"sku": "123456789", "title": "本地隔离验收样例", "status": "read"}
        for index, (identifier, voice, seconds, timing_status, match_status) in enumerate((
            ("native-timing-ready", READY_VOICE, 24, "ready", "pending"),
            ("native-timing-short", SHORT_VOICE, 7.5, "needs_adjustment", "ready"),
        )):
            studio.store.put_draft({
                "id": identifier, "workflow": "script_first_v2", "title": identifier,
                "product": fixture_product, "product_name": fixture_product["title"],
                "voiceover": voice, "segments": [{"segment_id": "s1", "text": voice, "visual_hint": "隔离测试合成画面"}],
                "asset_ids": [item["asset_id"] for item in fixtures], "target_seconds": 25,
                "estimated_seconds": seconds, "timing_status": timing_status,
                "timing_message": "本地预置文案，供离线桌面时长校验使用",
                "revision": 1, "status": "draft", "created_at": time.time() + index,
                "clips": [], "matches": [], "match_status": match_status, "analysis_warnings": [],
            })
        studio.store.put_draft({
            "id": "native-fact-review", "workflow": "script_first_v2", "title": "显示器口播",
            "product": fixture_product, "product_name": fixture_product["title"],
            "voiceover": OLD_REVIEW_VOICE,
            "segments": [{"segment_id": "s1", "text": OLD_REVIEW_VOICE, "visual_hint": "INTERNAL_HINT_MUST_NOT_RENDER"}],
            "asset_ids": [item["asset_id"] for item in fixtures], "target_seconds": 25,
            "estimated_seconds": 27.75, "timing_status": "ready", "fact_status": "needs_review",
            "fact_issues": [{"field": "voiceover", "segment_id": "s1", "term": "刷新率", "message": "旧版本校验结果"}],
            "revision": 1, "status": "draft", "created_at": time.time() - 10,
            "clips": [], "matches": [], "match_status": "pending", "analysis_warnings": [],
        })
        studio.store.put_draft({
            "id": "native-timing-speed", "workflow": "script_first_v2", "title": "122字倍速适配样例",
            "product": fixture_product, "product_name": fixture_product["title"],
            "voiceover": SPEED_VOICE,
            "segments": [{"segment_id": "s1", "text": SPEED_VOICE, "visual_hint": "隔离测试合成画面"}],
            "asset_ids": [item["asset_id"] for item in fixtures], "target_seconds": 25,
            "estimated_seconds": 30.5, "timing_status": "needs_adjustment",
            "timing_message": "文案已保留，待调整时长。请至少删减 2 字后保存。",
            "revision": 1, "status": "draft", "created_at": time.time() - 11,
            "clips": [], "matches": [], "match_status": "pending", "analysis_warnings": [],
        })
        for identifier, ready in (("native-timeline-shortage", False), ("native-timeline-ready", True)):
            studio.store.put_draft({
                "id": identifier, "workflow": "script_first_v2", "title": "本地时间线界面样例",
                "product": fixture_product, "product_name": fixture_product["title"],
                "voiceover": READY_VOICE, "segments": [{"segment_id": "s1", "text": READY_VOICE[:56]}, {"segment_id": "s2", "text": READY_VOICE[56:]}],
                "asset_ids": [item["asset_id"] for item in fixtures], "target_seconds": 25,
                "estimated_seconds": 24, "timing_status": "ready", "fact_status": "not_required",
                "revision": 1, "status": "draft", "created_at": time.time() - 20,
                "clips": [], "matches": [], "match_status": "ready" if ready else "needs_review",
                "timeline_plan": {"version": 1, "status": "ready" if ready else "shortage", "phase": "actual" if ready else "estimated", "speed": 1.25, "repeat_count": 1, "total_seconds": 24 if ready else 21, "transition_seconds": .2,
                    "segments": [{"segment_id": "s1", "required_seconds": 12, "available_seconds": 12}, {"segment_id": "s2", "required_seconds": 12, "available_seconds": 12 if ready else 9}],
                    "shortages": [] if ready else [{"segment_id": "s2", "required_seconds": 12, "available_seconds": 9, "missing_seconds": 3, "reason": "本地预置时长缺口，仅用于界面测试"}], "windows": [], "warnings": []},
                "analysis_warnings": [],
            })
        fixture_job = result["fixture_job_ids"][0]
        studio.store.create_job(fixture_job, "script", {"target_seconds": 25, "count": 2})
        studio.store.update_job(fixture_job, status="completed", progress=1, message="本地预置验收任务", result={
            "draft_ids": ["native-timing-short", "native-timing-ready"],
            "needs_adjustment_ids": ["native-timing-short"],
        })
        app = create_app(studio)
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

        @app.before_request
        def audit():
            with lock:
                result["requests"].append({"method": request.method, "path": request.path})
            if request.method != "GET":
                return jsonify(ok=False, error="Native smoke rejects all business write requests"), 403

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        bridge = Bridge()
        window = webview.create_window("京东自动混剪 V2 · 隔离原生验收（自动关闭）", f"http://127.0.0.1:{server.server_port}", js_api=bridge, width=1200, height=820, min_size=(1024, 700), focus=False, text_select=True, background_color="#f4f6f8")
        bridge._window = window
        for name in ("shown", "loaded", "closing", "closed"):
            getattr(window.events, name).__iadd__(lambda *args, name=name: event(name))
        threading.Thread(target=watchdog, daemon=True).start()
        save()
        webview.start(exercise, gui="edgechromium", debug=False, private_mode=True, storage_path=str(run_root / "webview-private"))
        event("gui_loop_returned")
    except Exception as exc:
        result["errors"].append({"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()})
    finally:
        complete.set()
        if server:
            server.shutdown()
            server.server_close()
        if studio:
            result["business_jobs_created"] = len([job for job in studio.store.jobs() if job["id"] not in result["fixture_job_ids"]])
            studio.close()
        event("server_and_store_closed")
        names = {item["name"] for item in result["events"]}
        result["assertions"].extend([
            {"name": "native shown/loaded/closed lifecycle", "passed": {"shown", "loaded", "closed", "gui_loop_returned"} <= names},
            {"name": "only read-only local HTTP requests", "passed": bool(result["requests"]) and all(item["method"] == "GET" for item in result["requests"])},
            {"name": "no model/product/download business job submitted", "passed": result.get("business_jobs_created") == 0},
            {"name": "isolated runtime resides under evidence directory", "passed": run_root.resolve().is_relative_to(EVIDENCE.resolve())},
        ])
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["status"] = "passed" if not result["errors"] and all(item["passed"] for item in result["assertions"]) else "failed"
        result["exit_code"] = 0 if result["status"] == "passed" else 1
        save()
    print(json.dumps({"status": result["status"], "assertions": len(result["assertions"]), "paid_api_calls": result["paid_api_calls"], "elapsed_seconds": result["elapsed_seconds"], "report": str(report)}, ensure_ascii=False))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
