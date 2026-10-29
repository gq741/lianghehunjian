"use strict";

(() => {
  // Pure helpers are exported only under Node, for offline contract tests.
  function formatTime(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
    const whole = Math.floor(seconds);
    return `${Math.floor(whole / 60).toString().padStart(2, "0")}:${(whole % 60).toString().padStart(2, "0")}`;
  }
  function parseTranscript(value, duration) {
    const rows = String(value).split(/\r?\n/).filter((line) => line.trim());
    if (!rows.length) throw new Error("请至少填写一段文案和时间码。");
    let previousEnd = 0;
    return rows.map((line, index) => {
      const parts = line.split("|");
      if (parts.length < 3 || !parts[0].trim() || !parts[1].trim()) {
        throw new Error(`第 ${index + 1} 行请使用：开始秒数 | 结束秒数 | 文案。`);
      }
      const start = Number(parts[0].trim());
      const end = Number(parts[1].trim());
      const text = parts.slice(2).join("|").trim();
      if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start || !text) {
        throw new Error(`第 ${index + 1} 行的时间码或文案不完整。结束时间需要大于开始时间。`);
      }
      if (Number.isFinite(Number(duration)) && Number(duration) > 0 && end > Number(duration) + 0.05) {
        throw new Error(`第 ${index + 1} 行超过源视频时长 ${Number(duration).toFixed(2)} 秒。`);
      }
      if (start < previousEnd) throw new Error(`第 ${index + 1} 行的时间与前一段重叠，请按原视频顺序填写。`);
      previousEnd = end;
      return { start, end, text };
    });
  }
  function normalizeLinkPaste(value) {
    // Preserve one link per line in the batch editor; Enter never submits.
    return String(value).replace(/\r\n?/g, "\n");
  }
  function normalizeDouyinUrl(value) {
    const candidates = String(value).match(/https?:\/\/[^\s<>"，。；！]+/g) || [];
    if (candidates.length !== 1) throw new Error("请粘贴包含 https:// 的单条抖音视频分享链接。");
    const candidate = candidates[0].replace(/[),;!）]+$/, "");
    if (candidate.length > 4096) throw new Error("抖音链接过长，请重新复制视频分享链接。");
    // Validate the original path, not URL's dot-segment/backslash-normalized path.
    const match = /^https:\/\/(douyin\.com|www\.douyin\.com|v\.douyin\.com)(?::(\d+))?([^?#]*)(?:\?[^#]*)?(?:#.*)?$/i.exec(candidate);
    if (!match || (match[2] && Number(match[2]) !== 443)) {
      throw new Error("这里只接受 HTTPS 抖音视频或短链接，不接受其他网站、账号信息或自定义端口。");
    }
    const host = match[1].toLowerCase();
    const path = match[3];
    if (/^\/note(?:\/|$)/.test(path)) throw new Error("第一版暂不下载图文笔记，请使用单条视频链接。");
    const valid = host === "v.douyin.com"
      ? /^\/[A-Za-z0-9_-]{3,100}\/?$/.test(path)
      : /^\/video\/\d{5,30}\/?$/.test(path);
    if (!valid) throw new Error("第一版仅接收抖音单条视频或短分享链接，不接收主页、合集或其他页面。");
    return `https://${host}${path}`;
  }
  function extractDouyinLinks(value) {
    const candidates = String(value).match(/https?:\/\/[^\s<>"，。；！]+/g) || [];
    if (!candidates.length) throw new Error("请粘贴包含 https:// 的抖音视频分享链接。");
    const links = [];
    for (const candidate of candidates) {
      const link = normalizeDouyinUrl(candidate);
      if (!links.includes(link)) links.push(link);
    }
    return links;
  }
  function timestamp(value) {
    if (typeof value === "number") return value < 1e12 ? value * 1000 : value;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  function sortedAssets(assets) {
    return [...assets].sort((a, b) => timestamp(b.imported_at) - timestamp(a.imported_at));
  }
  function safeMediaUrl(value, prefix, base) {
    if (!value) return "";
    try {
      const baseUrl = new URL(base);
      const url = new URL(String(value), baseUrl);
      return url.origin === baseUrl.origin && url.pathname.startsWith(prefix) && !url.username && !url.password
        ? `${url.pathname}${url.search}` : "";
    } catch {
      return "";
    }
  }
  function assetIdentity(asset) { return asset.sha256 ? `sha:${asset.sha256}` : `id:${asset.id}`; }
  function selectedAssets(assets, ids) {
    const byId = new Map(assets.filter((item) => item.selectable !== false).map((item) => [item.id, item]));
    const unique = new Map();
    for (const id of ids) { const asset = byId.get(id); if (asset && !unique.has(assetIdentity(asset))) unique.set(assetIdentity(asset), asset); }
    return [...unique.values()];
  }
  function changeSelection(assets, ids, id, checked) {
    const target = assets.find((asset) => asset.id === id);
    if (!target || target.selectable === false) return new Set(selectedAssets(assets, ids).map((asset) => asset.id));
    const key = assetIdentity(target);
    const result = new Set(selectedAssets(assets, ids).filter((asset) => assetIdentity(asset) !== key).map((asset) => asset.id));
    if (checked) result.add(id);
    return result;
  }
  function encodeMatches(matches) {
    const clip_table = [], clipIndex = new Map();
    const rows = matches.map((row) => ({ segment_id: row.segment_id,
      clip_indices: row.clip_ids.map((id) => {
        if (!clipIndex.has(id)) { clipIndex.set(id, clip_table.length); clip_table.push(id); }
        return clipIndex.get(id);
      }) }));
    return { clip_table, matches: rows };
  }
  function normalizedPath(value) { return String(value || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase(); }
  function libraryAssets(assets, library, folder) {
    return assets.filter((asset) => {
      if (asset.selectable === false) return false;
      const libraries = Array.isArray(asset.libraries) ? asset.libraries : ["local"];
      if (!libraries.includes(library)) return false;
      if (library === "douyin") return true;
      const path = normalizedPath(asset.path || asset.source_path);
      return Boolean(folder) && path.slice(0, path.lastIndexOf("/")) === normalizedPath(folder);
    });
  }
  function durationBounds(value) {
    const target = Number(value);
    if (!Number.isInteger(target) || target < 5 || target > 60) throw new Error("参考时长请填写 5～60 的整数。");
    return { min: 0.1, max: 60 };
  }
  function isV2Draft(draft) { return draft?.workflow === "script_first_v2"; }
  function narrationTiming(draft, editedText) {
    // Recount the actual text; legacy V2 drafts may lack metadata, and cached estimates can be stale.
    const voice = editedText !== undefined ? editedText : typeof draft?.voiceover === "string" ? draft.voiceover : (draft?.segments || []).map((segment) => segment.text || "").join("");
    const units = (String(voice).match(/[\p{L}\p{N}]/gu) || []).length;
    const estimated = units / 4;
    let bounds;
    try { bounds = durationBounds(draft?.target_seconds); }
    catch { return { units, estimated, ready: false, mode: "needs_adjustment", message: "参考时长无效，请填写 5～60 的整数。" }; }
    const referenceSeconds = Number(draft.target_seconds), deviation = estimated - referenceSeconds;
    // A text estimate informs the user; only measured audio determines output duration.
    const ready = units > 0, mode = ready ? "reference_only" : "needs_adjustment";
    const message = ready
      ? `当前 ${units} 字，原速预估 ${estimated.toFixed(2)} 秒；参考 ${referenceSeconds} 秒，与参考相差 ${Math.abs(deviation).toFixed(2)} 秒（仅作提示）。可继续匹配画面；成片按实测配音与镜头校准，最长 60 秒。`
      : "请填写有效的口播文案后保存。";
    return { units, estimated, min: bounds.min, max: bounds.max, minUnits: 1, maxUnits: 240,
      maxAdaptableUnits: 360, estimatedVoiceSpeed: 1, referenceSeconds, deviation, mode, ready, message };
  }
  function canMatchDraft(draft) { return isV2Draft(draft) && narrationTiming(draft).ready; }
  function timelineReady(draft) {
    const plan = draft?.whole_timeline_plan || draft?.timeline_plan;
    return plan?.version === 1 && plan.status === "ready" && Array.isArray(plan.shortages) && plan.shortages.length === 0;
  }
  function usesWholeVoice(draft) { return isV2Draft(draft) && (draft.production_mode === "whole_voice_v1" || draft.desktop_production_mode === "whole_voice_v1"); }
  function isWholeVoiceUpgrade(draft) { return usesWholeVoice(draft) && draft.production_mode !== "whole_voice_v1" && !draft.whole_audio && !draft.whole_voice; }
  const wholeVoiceUpgradeNotice = "历史文案将使用整条配音新流程；旧分段配音缓存不复用。只有点击生成并确认费用后才会调用配音，打开或查看文案不会收费。";
  function canRenderDraft(draft) { return Boolean(draft) && (!isV2Draft(draft) || (usesWholeVoice(draft) ? canMatchDraft(draft) : canMatchDraft(draft) && draft.match_status === "ready" && timelineReady(draft))); }
  function analysisProgress(draft) {
    const summary = draft?.analysis_summary;
    if (!isV2Draft(draft) || !summary) return null;
    const { planned, completed, pending } = summary;
    if (![planned, completed, pending].every(Number.isSafeInteger) || planned <= 0 || completed < 0 || pending < 0 || completed + pending !== planned) return null;
    return { pending, text: `已分析 ${completed}/${planned} 个窗口` + (pending ? `${completed ? "；已保存的结果可用于匹配" : ""}，剩余 ${pending} 个可稍后继续。` : "，画面分析已完成。") };
  }
  function selectionBudget(clips, groups, assets = []) {
    const byId = new Map(clips.map((clip) => [clip.clip_id, clip]));
    const byAsset = new Map(assets.map((asset) => [asset.id, asset]));
    const windows = []; let repeatCount = 0, maxUses = 0;
    for (const ids of groups) for (const id of ids) {
      const clip = byId.get(id); if (!clip) return { ready: false, repeatCount, maxUses, message: "候选片段已变化，请重新匹配画面。" };
      const source = clip.source_sha256 || byAsset.get(clip.asset_id)?.sha256 || clip.asset_id;
      const start = Number(clip.source_start ?? clip.start), end = Number(clip.source_end ?? clip.end);
      if (!source || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) return { ready: false, repeatCount, maxUses, message: "片段时间信息缺失，请重新匹配画面。" };
      if (windows.some((item) => item.source === source && Math.min(item.end, end) > Math.max(item.start, start) + 0.000001)) repeatCount++;
      windows.push({ source, start, end });
    }
    for (const source of new Set(windows.map((item) => item.source))) {
      const points = windows.filter((item) => item.source === source).flatMap((item) => [[item.start, 1], [item.end, -1]]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      let count = 0; for (const [, delta] of points) { count += delta; maxUses = Math.max(maxUses, count); }
    }
    return { ready: maxUses <= 2 && repeatCount <= 2, repeatCount, maxUses, message: maxUses > 2 ? "同一源画面最多使用 2 次，请选择其他片段。" : repeatCount > 2 ? "本条视频额外复用最多 2 次，请减少重复画面。" : "" };
  }
  function jobPage(jobs, page = 0) {
    const active = (job) => ["queued", "running", "cancelling"].includes(job.status);
    const ordered = [...jobs].sort((a, b) => Number(active(b)) - Number(active(a)) || timestamp(b.created_at) - timestamp(a.created_at));
    const total = ordered.length, pages = Math.max(1, Math.ceil(total / 20));
    const index = Math.max(0, Math.min(pages - 1, Math.floor(Number(page) || 0)));
    return { items: ordered.slice(index * 20, (index + 1) * 20), total, pages, index, active: ordered.filter(active).length };
  }
  if (typeof document === "undefined") {
    if (typeof module !== "undefined") module.exports = { formatTime, parseTranscript, normalizeLinkPaste, normalizeDouyinUrl, extractDouyinLinks, timestamp, sortedAssets, safeMediaUrl, assetIdentity, selectedAssets, changeSelection, libraryAssets, encodeMatches, durationBounds, isV2Draft, narrationTiming, timelineReady, selectionBudget, jobPage, canMatchDraft, canRenderDraft };
    return;
  }

  const $ = (id) => document.getElementById(id);
  const token = document.querySelector('meta[name="local-token"]')?.content || "";
  const state = {
    jobPages: { download: 0, mix: 0 }, assets: [], jobs: [], drafts: [], settings: {}, environment: {}, folder: "", product: null, preflight: null, library: "local",
    selectionSources: new Map(), matchesDirty: false, reviewMatches: new Map(), requestedProductUrl: "", productInputDirty: false, productInputVersion: 0, productRequestVersion: null, productJobId: null,
    selected: new Set(), activeDraftId: null, draftDirty: false, draftEditorVersion: 0, settingsDirty: false, settingsEditorVersion: 0,
    settingsLoaded: false, currentPage: "library", previewAssetId: null,
    assetsSignature: "", jobsSignature: "", draftsSignature: "", fetching: false,
    mutationEpoch: 0, pendingMutations: 0, refreshPending: false,
    draftsToken: "", connectionFailures: 0, pollTimer: null, pollDelay: 0,
    pendingDrafts: false, pendingDraftId: null, connected: false, busy: new Set(), activePreview: null,
    music: { tracks: [], current: null, gain: .15, error: "" }, musicLoaded: false, musicLoading: false, musicTracksSignature: "",
    musicScan: null, musicScanVersion: 0, musicChecks: new Set(), musicImportId: null, musicFinishedKey: "",
    musicGainDirty: false, musicGainVersion: 0, musicGainPending: new Set(),
    musicUiError: "", musicLastError: "", musicEpoch: 0, musicWrites: 0, musicWriteTail: Promise.resolve(), musicGetVersion: 0, musicImportSummary: "",
  };
  const names = { library: "素材库", mix: "混剪工作台", settings: "设置" };
  const statusNames = {
    queued: "等待中", running: "处理中", cancelling: "正在取消", cancelled: "已取消",
    failed: "失败", completed: "已完成", interrupted: "已中断",
  };
  const jobNames = { scan: "读取素材", library_scan: "读取素材", download: "视频下载", music_import: "音乐导入", prepare: "历史文案任务", media_prepare: "素材准备", preflight: "免费预检", alignment: "本地语音对齐（免费）", product: "读取京东商品", script: "生成口播文案", match: "分析并匹配画面", render: "配音与视频合成" };
  const numericSettings = new Set([
    "bgm_gain", "target_seconds", "output_count", "render_concurrency", "width", "height",
    "fps", "transition_seconds", "clean_top_pct", "clean_height_pct", "subtitle_font_size",
    "subtitle_margin_pct",
  ]);
  const settingKeys = [
    "output_dir", "download_dir", "asr_model_path", "asr_device", "voice_id", "voice_provider", "minimax_model", "minimax_voice_id", "deepseek_model", "vision_endpoint", "vision_model",
    "target_seconds", "output_count", "render_concurrency", "encoder",
    "preset", "width", "height", "fps", "transition", "transition_seconds", "clean_mode",
    "clean_top_pct", "clean_height_pct", "caption_plate", "subtitle_font_size", "subtitle_margin_pct",
  ];
  const defaultSettings = {
    output_dir: "", download_dir: "", asr_model_path: "", asr_device: "cpu",
    voice_id: "be40aff9ee7b4d0cba3bb35e924d85b2", deepseek_model: "deepseek-flash",
    voice_provider: "fish", minimax_model: "speech-2.8-hd", minimax_voice_id: "male-qn-qingse",
    bgm_path: "", bgm_gain: 0.15, target_seconds: 25, output_count: 1, render_concurrency: 1,
    encoder: "cpu", preset: "balanced", width: 720, height: 1280, fps: 30,
    transition: "fade", transition_seconds: 0.2, clean_mode: "none",
    clean_top_pct: 75, clean_height_pct: 18, caption_plate: false,
    subtitle_font_size: 42, subtitle_margin_pct: 12,
    vision_endpoint: "https://open.bigmodel.cn/api/paas/v4/chat/completions", vision_model: "glm-5.3-flash",
  };

  // Production always uses saved settings; changing the selector never submits work.
  function voiceProvider() { return state.settings.voice_provider === "minimax" ? "minimax" : "fish"; }
  function voiceProviderName(short = false) { return voiceProvider() === "minimax" ? "MiniMax" : short ? "Fish" : "Fish Audio"; }
  function syncVoiceProviderFields() {
    const provider = $("set-voice_provider").value;
    document.querySelectorAll("[data-voice-provider]").forEach((field) => {
      field.hidden = field.getAttribute("data-voice-provider") !== provider;
      field.querySelectorAll("input").forEach(input => { input.disabled = field.hidden; });
      field.querySelectorAll("select").forEach(input => { input.disabled = field.hidden; });
    });
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }
  function action(text, handler, className = "button button-small") {
    const button = element("button", className, text);
    button.type = "button";
    button.addEventListener("click", () => withBusy(button, handler));
    return button;
  }
  function toast(message, error = false) {
    const node = element("div", `toast${error ? " error" : ""}`, message);
    $("toast-region").replaceChildren(node);
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => node.remove(), error ? 8500 : 4500);
  }
  async function api(path, method = "GET", body, extraHeaders = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ["/api/library/remove", "/api/storage/cleanup"].includes(path) ? 120000 : 30000);
    // Every write invalidates reads started before it or while it was in flight.
    const mutation = method !== "GET";
    if (mutation) { state.mutationEpoch++; state.pendingMutations++; }
    try {
      const response = await fetch(path, {
        method, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { "X-Local-Token": token, ...extraHeaders, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      });
      let result;
      try { result = await response.json(); } catch { throw new Error("本地服务返回了无法读取的响应，请同步状态后重试。"); }
      if (!response.ok || !result.ok) throw new Error(typeof result.error === "string" ? result.error : `操作失败（${response.status}）`);
      return result.data;
    } catch (error) {
      if (error.name === "AbortError") {
        throw new Error("本地请求超时。请先同步任务状态，确认没有已提交任务，再决定是否重试。");
      }
      if (error instanceof TypeError) throw new Error("未连接到本地服务，请检查桌面程序是否仍在运行。");
      throw error;
    } finally {
      clearTimeout(timer);
      if (mutation) {
        state.mutationEpoch++; state.pendingMutations--;
        if (!state.pendingMutations && !state.fetching && state.refreshPending) refresh();
      }
    }
  }
  async function withBusy(button, handler) {
    if (button && state.busy.has(button)) return;
    if (button) {
      state.busy.add(button);
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.classList.add("busy");
    }
    try { return await handler(); } catch (error) {
      const message = error.message || "操作失败，请检查任务详情。";
      if ($("asset-dialog").open) {
        $("transcript-error").textContent = message;
        $("transcript-error").hidden = false;
      } else toast(message, true);
    }
    finally {
      if (button) {
        state.busy.delete(button);
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.classList.remove("busy");
      }
      syncButtons();
    }
  }
  function confirmAction(title, description, acceptText) {
    const dialog = $("confirm-dialog");
    if (dialog.open) return Promise.resolve(false);
    $("confirm-title").textContent = title;
    $("confirm-description").textContent = description;
    $("confirm-accept").textContent = acceptText;
    dialog.returnValue = "";
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "accept"), { once: true });
      dialog.showModal();
      $("confirm-cancel").focus();
    });
  }
  $("confirm-accept").addEventListener("click", () => $("confirm-dialog").close("accept"));
  $("confirm-cancel").addEventListener("click", () => $("confirm-dialog").close("cancel"));

  function setPage(page) {
    if (!names[page]) return;
    if (state.currentPage === "mix" && page !== "mix") $("mix-preview").pause();
    state.currentPage = page;
    document.querySelectorAll(".page").forEach((node) => { node.hidden = node.id !== `page-${page}`; });
    document.querySelectorAll(".nav-button").forEach((node) => {
      const active = node.dataset.page === page;
      node.classList.toggle("active", active);
      if (active) node.setAttribute("aria-current", "page");
      else node.removeAttribute("aria-current");
    });
    $("page-name").textContent = names[page];
    document.title = `${names[page]} · 量禾混剪`;
    if (location.hash !== `#${page}`) history.replaceState(null, "", `#${page}`);
    window.scrollTo({ top: 0, behavior: "instant" });
  }
  document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => setPage(button.dataset.page)));
  window.addEventListener("hashchange", () => setPage(location.hash.slice(1)));
  $("go-mix").addEventListener("click", () => withBusy($("go-mix"), async () => {
    setPage("mix");
    $("preflight-status").textContent = "正在提交免费预检，不调用收费接口。";
    syncButtons();
    try {
      await api("/api/v2/preflight", "POST", { asset_ids: [...state.selected] });
      $("product-url").focus();
      toast("正在免费预检素材、模型配置和输出环境；不会调用收费接口。");
      await refresh();
    } catch (error) { $("preflight-status").textContent = error.message; throw error; }
  }));

  function visibleAssets() { return libraryAssets(state.assets, state.library, state.folder); }
  function checkedLibraryAssets() {
    const keys = new Set(selectedAssets(state.assets, state.selected).map(assetIdentity));
    return visibleAssets().filter(asset => keys.has(assetIdentity(asset)));
  }
  $("delete-selected-assets").addEventListener("click", () => withBusy($("delete-selected-assets"), async () => {
    const targets = checkedLibraryAssets(), library = state.library;
    if (!targets.length) return;
    const names = targets.slice(0, 8).map(asset => asset.name).join("\n");
    const accepted = await confirmAction("确认删除选中的素材？",
      `将删除当前${library === "douyin" ? "抖音" : "本地"}素材库勾选的 ${targets.length} 个素材，并将原视频文件移到回收站。\n\n${names}${targets.length > 8 ? "\n……" : ""}\n\n引用同一文件的其他库条目也会移除，未完成的草稿可能需要补充素材；已有成片不受影响。取消不会删除。`, "确认");
    if (!accepted) return;
    const result = await api("/api/library/remove", "POST", { asset_ids: targets.map(asset => asset.id), library, confirmed: true });
    const removed = new Set(result.removed_ids || []);
    state.selected = new Set([...state.selected].filter(id => !removed.has(id)));
    for (const id of removed) state.selectionSources.delete(id);
    state.assets = state.assets.map(asset => removed.has(asset.id) ? {...asset, availability:"trashed", selectable:false} : asset);
    if (removed.has(state.previewAssetId) && $("asset-dialog").open) $("asset-dialog").close();
    renderAssets(); updateSelection();
    const failures = Array.isArray(result.failed) ? result.failed : [];
    const detail = failures.slice(0, 3).map(item => `${item.name}：${item.error}`).join("；");
    const warnings = (result.warnings || []).join("；");
    toast(`已将 ${result.removed_count || 0} 个原视频移到回收站。${failures.length ? ` ${failures.length} 个删除失败：${detail}` : ""}${warnings}`, failures.length > 0 || Boolean(warnings));
    await refresh();
  }));
  function setLibrary(library) {
    if (!["local", "douyin"].includes(library)) return;
    state.library = library;
    for (const name of ["local", "douyin"]) {
      const active = name === library;
      const tab = $(`library-tab-${name}`);
      tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1;
      $(`library-panel-${name}`).hidden = !active;
    }
    $("empty-choose-folder").hidden = library === "douyin";
    $("library-empty-title").textContent = library === "local" ? "从一组同商品的视频开始" : "还没有下载的抖音素材";
    $("library-empty-description").textContent = library === "local" ? "选择本地素材文件夹，勾选这次混剪需要的视频。" : "在上方粘贴分享链接，点击“解析并下载”。下载完成后会自动显示在这里。";
    renderAssets();
  }
  document.querySelectorAll("[data-library]").forEach((tab) => {
    tab.addEventListener("click", () => setLibrary(tab.dataset.library));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const library = event.key === "Home" ? "local" : event.key === "End" ? "douyin" : state.library === "local" ? "douyin" : "local";
      setLibrary(library); $(`library-tab-${library}`).focus();
    });
  });
  function selectAsset(id, checked) {
    state.selected = changeSelection(state.assets, state.selected, id, checked);
    if (checked) state.selectionSources.set(id, state.library);
    updateSelection();
  }
  function updateSelection() {
    const selected = selectedAssets(state.assets, state.selected);
    state.selected = new Set(selected.map((asset) => asset.id));
    const keys = new Set(selected.map(assetIdentity));
    const visible = visibleAssets();
    const visibleKeys = new Set(visible.map(assetIdentity));
    const visibleCount = [...visibleKeys].filter((key) => keys.has(key)).length;
    const count = selected.length;
    const local = selected.filter((asset) => (state.selectionSources.get(asset.id) || (asset.libraries?.includes("local") ? "local" : "douyin")) === "local").length;
    $("selection-count").textContent = `已选 ${count} 个视频`;
    $("selection-sources").textContent = `本地 ${local} 个｜抖音 ${count - local} 个 · 同一内容只加入一次`;
    $("mix-selection-count").textContent = count ? `本次使用 ${count} 个视频` : "未选择素材";
    $("mix-selection-names").textContent = count ? selected.map((asset) => asset.name).join("、") : "请先回素材库勾选同商品的视频";
    $("select-all").checked = visibleKeys.size > 0 && visibleCount === visibleKeys.size;
    $("select-all").indeterminate = visibleCount > 0 && visibleCount < visibleKeys.size;
    $("select-all").disabled = visible.length === 0;
    document.querySelectorAll(".asset-card").forEach((card) => {
      const asset = state.assets.find((item) => item.id === card.dataset.id);
      const checked = asset && keys.has(assetIdentity(asset));
      card.classList.toggle("selected", checked);
      card.querySelector('input[type="checkbox"]').checked = checked;
    });
    syncButtons();
  }
  function syncButtons() {
    const draft = activeDraft();
    if (!state.busy.has($("delete-selected-assets"))) $("delete-selected-assets").disabled = !state.connected || checkedLibraryAssets().length === 0;
    const running = state.jobs.filter((job) => ["queued", "running", "cancelling"].includes(job.status));
    const draftTask = running.find(job => ["match", "render", "alignment"].includes(job.kind) && (!job.draft_id || job.draft_id === state.activeDraftId));
    const draftLocked = Boolean(draftTask);
    if (!state.busy.has($("run-preflight"))) $("run-preflight").disabled = !state.connected || state.selected.size === 0 || state.busy.has($("go-mix")) || running.some(job => ["preflight", "media_prepare", "script", "render", "alignment"].includes(job.kind));
    if (!state.busy.has($("reset-credentials"))) $("reset-credentials").disabled = !state.connected || state.busy.has($("save-settings"));
    if (!state.busy.has($("save-settings"))) $("save-settings").disabled = state.busy.has($("reset-credentials"));
    if (!state.busy.has($("go-mix"))) $("go-mix").disabled = state.selected.size === 0;
    if (!state.busy.has($("prepare-button"))) $("prepare-button").disabled = state.selected.size === 0 || !state.connected || !state.product?.title || state.product.status !== "read" || state.requestedProductUrl !== "" || state.productInputDirty || running.some((job) => ["product", "media_prepare", "preflight", "script"].includes(job.kind));
    if (!state.busy.has($("save-draft"))) {
      $("save-draft").disabled = !state.draftDirty || !state.activeDraftId || draftLocked;
      $("save-draft").textContent = "保存修改";
    }
    const rendering = new Set(running.filter((job) => ["render", "alignment"].includes(job.kind)).map((job) => job.result?.draft_id || job.draft_id));
    $("draft-segments").querySelectorAll("textarea").forEach(input => { input.readOnly = draftLocked; });
    $("realign-whole").hidden = !draft?.whole_audio;
    if (!state.busy.has($("realign-whole"))) $("realign-whole").disabled = !state.connected || !draft?.whole_audio || Number(draft.whole_audio.duration) > 60 || draftLocked || state.draftDirty || state.matchesDirty;
    const blockingTask = draftTask || running.find(job => ["media_prepare", "preflight"].includes(job.kind));
    const matching = Boolean(blockingTask);
    if (!state.busy.has($("match-draft"))) {
      $("match-draft").hidden = !isV2Draft(draft) || (usesWholeVoice(draft) && !draft?.whole_voice);
      $("match-draft").disabled = !canMatchDraft(draft) || state.draftDirty || state.matchesDirty || matching || !state.connected;
      $("match-draft").textContent = analysisProgress(draft)?.pending ? "继续分析并匹配" : "自动匹配画面";
    }
    if (!state.busy.has($("save-matches"))) $("save-matches").disabled = !canMatchDraft(draft) || !state.matchesDirty || state.draftDirty || matching || !state.connected;
    const overLimitAudio = Number(draft?.whole_audio?.duration) > 60;
    if (!state.busy.has($("render-draft"))) $("render-draft").disabled = !canRenderDraft(draft) || overLimitAudio || state.draftDirty || state.matchesDirty || !state.connected || rendering.has(state.activeDraftId) || matching;
    if (!state.busy.has($("render-all"))) $("render-all").disabled = state.draftDirty || state.matchesDirty || !state.connected || batchDrafts().length < 2 || batchDrafts().some((item) => !canRenderDraft(item) || Number(item.whole_audio?.duration) > 60 || rendering.has(item.id));
    const operationReason = blockingTask ? ({ media_prepare: "素材正在本地准备，请等待完成。", preflight: "免费预检正在进行，请等待完成。", match: "画面匹配正在进行，请等待完成。", render: "本条视频正在生成，完成后可修改文案或镜头。", alignment: "本地对齐正在进行，完成后可修改文案或镜头。" }[blockingTask.kind] || "任务正在进行，请等待完成。") : "";
    const commonReason = !state.connected ? "本地服务未连接，请同步状态。" : !draft ? "请先生成口播文案。" : draftLocked ? operationReason : state.draftDirty ? "文案修改未保存，请先点击“保存修改”。" : state.matchesDirty ? "画面选择未保存，请先点击下方“保存画面选择”。" : !canMatchDraft(draft) && isV2Draft(draft) ? narrationTiming(draft).message : operationReason;
    const shortages = Array.isArray(draft?.timeline_plan?.shortages) ? draft.timeline_plan.shortages : [];
    const shortageReason = shortages.length ? `时间线仍有 ${shortages.length} 处缺口，请查看下方各句净画面秒数。` : !timelineReady(draft) && isV2Draft(draft) ? "时间线尚未完成，请自动匹配画面，或保存补充的画面选择。" : draft?.match_status !== "ready" && isV2Draft(draft) ? "画面匹配尚未完成，请自动匹配画面。" : "";
    const renderReason = commonReason || (overLimitAudio ? "已保存的整条配音实测超过 60 秒，音频可试听且已保留；请先精简文案，再确认是否重新配音。" : rendering.has(state.activeDraftId) ? "本条视频正在生成。" : usesWholeVoice(draft) ? "" : shortageReason);
    $("match-draft").title = commonReason; $("render-draft").title = renderReason;
    $("save-draft").title = draftLocked ? operationReason : "";
    $("match-action-reason").textContent = commonReason; $("match-action-reason").hidden = !commonReason;
    $("render-action-reason").textContent = renderReason; $("render-action-reason").hidden = !renderReason;
    $("save-matches-reason").textContent = state.matchesDirty ? "保存后按完整时间线重新计算；不会调用配音或模型。" : "自动匹配结果已保存。仅在手动替换画面后需要保存。";
    const step = draftTask && ["render", "alignment"].includes(draftTask.kind) ? 2 : running.some(job => ["preflight", "media_prepare"].includes(job.kind)) ? 0 : draft?.whole_audio || draft?.status === "rendered" || draft?.match_status === "ready" ? 2 : draft || state.product || running.some(job => ["script", "product"].includes(job.kind)) ? 1 : 0;
    document.querySelectorAll(".workflow li").forEach((item, index) => item.classList.toggle("current", index === step));
    syncMusicButtons();
  }
  $("select-all").addEventListener("change", () => {
    const checked = $("select-all").checked;
    for (const asset of visibleAssets()) { state.selected = changeSelection(state.assets, state.selected, asset.id, checked); if (checked) state.selectionSources.set(asset.id, state.library); }
    updateSelection();
  });

  const assetCards = new Map();
  function renderAssets() {
    const assets = sortedAssets(visibleAssets()), grid = $("asset-grid");
    const visibleIds = new Set(assets.map((asset) => asset.id));
    for (const [id, view] of assetCards) {
      if (!visibleIds.has(id)) { view.card.remove(); assetCards.delete(id); }
    }
    for (const [index, asset] of assets.entries()) {
      let view = assetCards.get(asset.id);
      if (!view) {
        const card = element("article", "asset-card");
        card.dataset.id = asset.id;
        const thumb = element("div", "asset-thumbnail");
        const checkLabel = element("label", "asset-select");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.addEventListener("change", () => {
          selectAsset(asset.id, checkbox.checked);
        });
        checkLabel.append(checkbox);
        const preview = document.createElement("button");
        preview.type = "button";
        preview.addEventListener("click", () => showAsset(asset.id));
        const placeholder = element("div", "asset-no-thumb");
        placeholder.append(icon("film"));
        preview.append(placeholder);
        const play = element("span", "asset-play");
        play.append(icon("play"));
        const duration = element("span", "asset-duration");
        preview.append(play, duration);
        thumb.append(preview, checkLabel);
        const body = element("div", "asset-body");
        const name = element("h3", "asset-name");
        const info = element("div", "asset-info");
        const dimensions = element("span"), transcript = element("span");
        info.append(dimensions, transcript);
        body.append(name, info);
        card.append(thumb, body);
        view = { card, checkbox, preview, placeholder, play, duration, name, dimensions, transcript, image: null, imageKey: "" };
        assetCards.set(asset.id, view);
      }
      view.checkbox.setAttribute("aria-label", `选择素材：${asset.name}`);
      view.preview.setAttribute("aria-label", `预览 ${asset.name}，并校对转写`);
      view.name.textContent = asset.name || "未命名视频"; view.name.title = asset.name || "";
      view.duration.textContent = formatTime(asset.duration);
      view.dimensions.textContent = asset.width && asset.height ? `${asset.width} × ${asset.height}` : "待检测";
      const ready = asset.clips?.length > 0 || asset.transcript_status === "ready" || asset.transcript_status === "completed";
      view.transcript.textContent = ready ? "有转写参考" : "待画面分析";
      const url = safeMediaUrl(asset.thumbnail_url, "/media/thumbs/", location.href);
      const imageKey = JSON.stringify([url, asset.sha256 || ""]);
      if (view.imageKey !== imageKey) {
        view.imageKey = imageKey;
        view.image?.remove(); view.image = null; view.placeholder.hidden = false;
        if (url) {
          const img = document.createElement("img");
          img.src = url; img.alt = ""; img.loading = "lazy"; img.decoding = "async";
          view.image = img;
          img.addEventListener("load", () => { if (view.image === img) view.placeholder.hidden = true; });
          img.addEventListener("error", () => {
            img.remove(); if (view.image === img) { view.image = null; view.imageKey = ""; view.placeholder.hidden = false; }
          }, { once: true });
          view.preview.insertBefore(img, view.play);
        }
      }
      // Insert/move only the card whose position changed; unchanged image nodes stay attached.
      if (grid.children[index] !== view.card) grid.insertBefore(view.card, grid.children[index] || null);
    }
    $("asset-count").textContent = String(state.assets.filter(asset => asset.availability !== "trashed").length);
    $("library-summary").textContent = visibleAssets().length ? `共 ${visibleAssets().length} 个视频` : "当前库还没有素材";
    $("library-empty").hidden = visibleAssets().length > 0;
    updateSelection();
  }
  async function nativePath(method, fieldId) {
    const bridge = window.pywebview?.api;
    if (!bridge || typeof bridge[method] !== "function") {
      $(fieldId).focus();
      toast("当前窗口未连接文件选择器，请在输入框粘贴本地完整路径。");
      return null;
    }
    const result = await bridge[method]();
    if (typeof result === "string" && result) {
      $(fieldId).value = result;
      // Native bridge assignments must count even before DOM input bubbling.
      if (fieldId.startsWith("set-")) markSettingsEdited();
      $(fieldId).dispatchEvent(new Event("input", { bubbles: true }));
      return result;
    }
    return null;
  }
  async function scanFolder() {
    const folder = $("library-folder").value.trim();
    if (!folder) { $("library-folder").focus(); throw new Error("请先选择或填写素材文件夹。"); }
    await api("/api/library/scan", "POST", { folder });
    toast("素材读取任务已加入队列。");
    $("library-queue").open = true;
    await refresh();
  }
  $("folder-form").addEventListener("submit", (event) => {
    event.preventDefault(); withBusy($("scan-folder"), scanFolder);
  });
  for (const id of ["choose-folder", "empty-choose-folder"]) {
    $(id).addEventListener("click", () => withBusy($(id), async () => {
      if (await nativePath("choose_folder", "library-folder")) await scanFolder();
    }));
  }
  $("download-link").addEventListener("paste", (event) => {
    const text = event.clipboardData?.getData("text/plain");
    if (!text || !/[\r\n]/.test(text)) return;
    event.preventDefault();
    const input = $("download-link");
    input.setRangeText(normalizeLinkPaste(text), input.selectionStart, input.selectionEnd, "end");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  $("clear-download").addEventListener("click", () => { $("download-link").value = ""; $("download-link").focus(); });
  // Textarea default keyboard handling intentionally keeps Enter as a newline.
  $("download-form").addEventListener("submit", (event) => {
    event.preventDefault();
    withBusy($("queue-download"), async () => {
      const submittedText = $("download-link").value;
      const links = extractDouyinLinks(submittedText);
      const folder = state.settings.download_dir;
      if (!folder) throw new Error("请先在设置中填写抖音素材保存目录。");
      await api("/api/downloads", "POST", { links, folder });
      // Do not erase the next link typed while the local request was pending.
      if ($("download-link").value === submittedText) $("download-link").value = "";
      $("library-queue").open = true;
      toast(`${links.length} 条链接已加入下载队列。`);
      await refresh();
      $("download-link").focus();
    });
  });

  function showAsset(id) {
    const asset = state.assets.find((item) => item.id === id);
    if (!asset) return;
    state.previewAssetId = id;
    $("asset-dialog-title").textContent = asset.name;
    $("asset-dialog-meta").textContent = `${formatTime(asset.duration)}  ·  ${asset.width || "?"} × ${asset.height || "?"}`;
    const source = safeMediaUrl(asset.preview_url || `/media/assets/${encodeURIComponent(id)}`, "/media/assets/", location.href);
    $("asset-preview").src = source;
    $("transcript-text").value = (asset.clips || []).map((clip) => `${Number(clip.start).toFixed(2)} | ${Number(clip.end).toFixed(2)} | ${clip.text}`).join("\n");
    $("transcript-error").hidden = true;
    $("transcript-status").textContent = "";
    $("asset-dialog").showModal();
  }
  $("close-asset-dialog").addEventListener("click", () => $("asset-dialog").close());
  $("asset-dialog").addEventListener("close", () => {
    $("asset-preview").pause();
    $("asset-preview").removeAttribute("src");
    $("asset-preview").load();
    state.previewAssetId = null;
  });
  $("save-transcript").addEventListener("click", () => withBusy($("save-transcript"), async () => {
    $("transcript-error").hidden = true;
    $("transcript-status").textContent = "";
    const asset = state.assets.find((item) => item.id === state.previewAssetId);
    if (!asset) throw new Error("该素材已不在当前素材库中，请重新导入。");
    let segments;
    try { segments = parseTranscript($("transcript-text").value, asset.duration); }
    catch (error) {
      $("transcript-error").textContent = error.message;
      $("transcript-error").hidden = false;
      $("transcript-text").focus();
      return;
    }
    await api(`/api/assets/${encodeURIComponent(asset.id)}/transcript`, "POST", { segments });
    $("transcript-status").textContent = `已保存 ${segments.length} 段转写文案，可直接用于改写。`;
    toast("转写文案已保存，可直接用于改写。");
    await refresh();
  }));

  function renderProduct() {
    const product = state.product;
    $("product-card").hidden = !product?.title;
    $("product-title").textContent = product?.title || "";
    const latest = [...state.jobs].filter((job) => job.kind === "product").sort((a, b) => timestamp(b.created_at) - timestamp(a.created_at))[0];
    if (state.requestedProductUrl && latest?.id === state.productJobId && ["completed", "failed", "cancelled", "interrupted"].includes(latest.status)) {
      state.productInputDirty = latest.status !== "completed" || state.productInputVersion !== state.productRequestVersion || !product?.source;
      if (!state.productInputDirty && product?.source) $("product-url").value = product.source;
      state.requestedProductUrl = ""; state.productRequestVersion = null;
    }
    const readStatus = $("product-read-status");
    readStatus.textContent = latest && ["queued", "running", "cancelling"].includes(latest.status)
      ? (latest.message || "正在打开商品页面并读取标题，请在独立窗口完成可能出现的登录。")
      : latest?.error || "";
    readStatus.hidden = !readStatus.textContent;
  }
  $("run-preflight").addEventListener("click", () => withBusy($("run-preflight"), async () => {
    setPage("mix");
    try { await api("/api/v2/preflight", "POST", { asset_ids: [...state.selected] }); await refresh(); }
    catch (error) { $("preflight-status").textContent = error.message; throw error; }
  }));
  $("realign-whole").addEventListener("click", () => withBusy($("realign-whole"), async () => {
    const draft = activeDraft();
    if (!draft?.whole_audio || state.draftDirty || state.matchesDirty) throw new Error("请先保存文案和画面修改，并确认整条音频仍可用。");
    await api(`/api/v2/drafts/${encodeURIComponent(draft.id)}/realign`, "POST", { refresh: true });
    toast("正在重新本地对齐；复用整条音频，不调用收费接口。"); await refresh();
  }));
  $("product-url").addEventListener("input", () => { state.productInputDirty = true; state.productInputVersion++; syncButtons(); });
  $("clear-product").addEventListener("click", () => {
    $("product-url").value = "";
    $("product-url").dispatchEvent(new Event("input", { bubbles: true }));
    $("product-url").focus();
  });
  $("paste-product").addEventListener("click", () => withBusy($("paste-product"), async () => {
    const bridge = window.pywebview?.api;
    const version = state.productInputVersion;
    const text = typeof bridge?.read_clipboard === "function" ? await bridge.read_clipboard()
      : typeof navigator !== "undefined" && navigator.clipboard?.readText ? await navigator.clipboard.readText() : null;
    if (typeof text !== "string") throw new Error("剪贴板未读取，请在商品链接框内按 Ctrl+V 粘贴。");
    if (version !== state.productInputVersion) return;
    $("product-url").value = text;
    $("product-url").dispatchEvent(new Event("input", { bubbles: true }));
    $("product-url").focus();
  }));
  $("product-form").addEventListener("submit", (event) => {
    event.preventDefault();
    withBusy($("read-product"), async () => {
      const url = $("product-url").value.trim();
      if (!url) throw new Error("请粘贴京东商品链接。");
      const version = state.productInputVersion;
      const submitted = await api("/api/product/read", "POST", { url });
      state.productJobId = submitted?.id || submitted?.job_id;
      state.productInputDirty = true; state.requestedProductUrl = url; state.productRequestVersion = version;
      toast("商品读取已提交，请查看商品窗口及下方任务进度。");
      await refresh();
    });
  });
  function showDurationHint() {
    try { durationBounds($("target-seconds").value); $("duration-hint").textContent = "参考时长，成片按实测配音与镜头校准，最长 60 秒"; }
    catch { $("duration-hint").textContent = "参考时长请填写 5～60 秒"; }
  }
  $("target-seconds").addEventListener("input", showDurationHint);
  $("prepare-form").addEventListener("submit", (event) => {
    event.preventDefault();
    withBusy($("prepare-button"), async () => {
      if (!state.selected.size || state.selected.size > 50) throw new Error("请选择 1～50 个同商品的视频素材。");
      if (!state.product?.title || state.product.status !== "read" || state.productInputDirty || state.requestedProductUrl) { $("product-url").focus(); throw new Error("请先读取并核对京东商品标题。"); }
      if (!state.settings.deepseek_key_set) { setPage("settings"); $("set-deepseek_api_key").focus(); throw new Error("请先保存 DeepSeek API Key，再生成文案。"); }
      const count = Number($("output-count").value);
      const targetSeconds = Number($("target-seconds").value);
      durationBounds(targetSeconds);
      if (!Number.isInteger(count) || count < 1 || count > 5) throw new Error("生成数量请填写 1～5 的整数。");
      const accepted = await confirmAction("生成口播文案？",
        `根据“${state.product.title}”生成 ${count} 条文案，参考时长 ${targetSeconds} 秒，成片按实测配音与镜头校准，最长 60 秒。\n\n本次只调用 DeepSeek，可能产生接口费用；不会自动转写、识别画面或调用 ${voiceProviderName(true)} 配音。`,
        "确认调用 DeepSeek");
      if (!accepted) return;
      await api("/api/v2/generate", "POST", { asset_ids: [...state.selected], target_seconds: targetSeconds, count, voice_first: true, allow_paid: true });
      state.pendingDrafts = true;
      toast("文案任务已提交，完成后会显示在下方。");
      await refresh();
    });
  });
  function activeDraft() { return state.drafts.find((draft) => draft.id === state.activeDraftId); }
  function recentDrafts() {
    return [...state.drafts].sort((a, b) => timestamp(b.created_at) - timestamp(a.created_at)).slice(0, 10);
  }
  function batchDrafts() {
    const job = state.jobs.find((item) => ["prepare", "script"].includes(item.kind) && item.result?.draft_ids?.includes(state.activeDraftId));
    const ids = new Set(job?.result?.draft_ids || [state.activeDraftId]);
    return state.drafts.filter((draft) => ids.has(draft.id));
  }
  function renderDraftOptions() {
    const select = $("draft-select");
    const fragment = document.createDocumentFragment();
    recentDrafts().forEach((draft, index) => {
      const timing = narrationTiming(draft);
      const option = element("option", "", `${isV2Draft(draft) ? (!timing.ready ? "文案待完善 · " : "") : "历史 · "}${draft.title || `${draft.product_name || "文案"} ${index + 1}`}`);
      option.value = draft.id;
      fragment.append(option);
    });
    select.replaceChildren(fragment);
    select.value = state.activeDraftId || "";
    $("render-all").hidden = batchDrafts().length < 2;
    $("render-all").textContent = `生成本组全部 ${batchDrafts().length} 条视频`;
  }
  function previewClip(clip) {
    const asset = state.assets.find((item) => item.id === clip.asset_id);
    const video = $("mix-preview");
    video.pause();
    const url = safeMediaUrl(asset?.preview_url || `/media/assets/${encodeURIComponent(clip.asset_id)}`, "/media/assets/", location.href);
    const sourceOffset = Number(asset?.prepared_media?.time_offset) || 0;
    state.activePreview = { kind: "clip", id: clip.clip_id, end: Number(clip.end) - sourceOffset };
    $("mix-preview-heading").textContent = "源片段预览";
    $("mix-preview-empty").hidden = true;
    video.hidden = false;
    video.src = url;
    video.onloadedmetadata = () => { video.currentTime = Math.max(0, (Number(clip.start) || 0) - sourceOffset); video.play().catch(() => {}); };
    $("mix-preview-caption").textContent = `${asset?.name || "源视频"}  ${formatTime(clip.start)}—${formatTime(clip.end)} · 此处为原片段，不是最终成片。`;
  }
  $("mix-preview").addEventListener("timeupdate", () => {
    if (state.activePreview?.kind === "clip" && $("mix-preview").currentTime >= state.activePreview.end) $("mix-preview").pause();
  });
  function renderMatches(draft) {
    const v2 = isV2Draft(draft);
    $("match-draft").hidden = !v2;
    $("match-review").hidden = false;
    $("save-matches").hidden = !v2;
    $("match-review-label").textContent = v2 ? "预览与替换画面（可选）" : "预览历史画面";
    $("match-review-hint").textContent = v2 ? "程序会自动配齐时间线，无需逐句手选。手动选择将替换本句候选；清空会保留为画面缺口。最终同一源画面最多用 2 次，全片额外复用最多 2 次。" : "以下为历史文案原有关联片段，保留原始时间码。";
    $("match-status").textContent = !v2 ? "历史文案" : !narrationTiming(draft).ready ? "先填写有效文案，再匹配画面" : { pending: "等待匹配画面", ready: timelineReady(draft) ? "画面已匹配，可继续生成" : "时间线尚未完整", needs_review: "时间线仍有画面缺口" }[draft.match_status] || "等待匹配画面";
    const usable = (draft.clips || []).filter((clip) => clip.usable === true);
    const rejected = (draft.clips || []).filter((clip) => clip.usable === false);
    const analysis = analysisProgress(draft);
    const detail = !v2 ? "保留历史文案与原有片段关联；新文案使用脚本先行流程。" : draft.match_status === "pending" && !draft.clips?.length ? "程序筛选黑屏、模糊和无关信息，并按文案自动匹配商品画面。" : `${usable.length} 个可用片段，${rejected.length} 个已排除或待核对。是否能生成，以各句净画面秒数和完整时间线为准。`;
    $("match-detail").textContent = analysis ? `${analysis.text} ${detail}` : detail;
    $("match-count").textContent = usable.length ? `${usable.length} 个候选片段` : "尚无候选";
    const fragment = document.createDocumentFragment();
    if (!v2) {
      const clipsById = new Map((draft.clips || []).map((clip) => [clip.clip_id, clip]));
      for (const [index, segment] of (draft.segments || []).entries()) {
        const row = element("div", "match-row"); row.append(element("p", "match-copy", segment.text));
        const clip = clipsById.get(segment.clip_id);
        if (clip) row.append(action(`预览第 ${index + 1} 段 · ${formatTime(clip.start)}–${formatTime(clip.end)}`, () => previewClip(clip)));
        else row.append(element("p", "inline-error", "历史源片段信息缺失"));
        fragment.append(row);
      }
      $("match-rows").replaceChildren(fragment); $("match-count").textContent = "历史关联片段";
      $("match-save-status").textContent = "历史文案保留原片段关联，不参与新版自动匹配。";
      return;
    }
    // Manual edits replace the candidate pool, not merely the final windows chosen by the planner.
    const candidateRows = Array.isArray(draft.match_candidates) ? draft.match_candidates : (draft.matches || []);
    const matches = new Map(candidateRows.map((match) => [match.segment_id, match]));
    state.reviewMatches = new Map((draft.segments || []).map((segment) => [segment.segment_id, new Set(matches.get(segment.segment_id)?.clip_ids || [])]));
    for (const [index, segment] of (draft.segments || []).entries()) {
      const row = element("fieldset", "match-row"); row.dataset.segmentId = segment.segment_id;
      row.append(element("legend", "", `文案 ${index + 1}`), element("p", "match-copy", segment.text));
      const selectedIds = state.reviewMatches.get(segment.segment_id);
      const status = element("p", selectedIds.size ? "field-hint" : "inline-error", selectedIds.size ? `已选 ${selectedIds.size} 个片段` : "尚未找到合适画面，请核对候选或补充同商品素材。");
      const filterLabel = element("label", "field-hint", "筛选候选画面");
      const filter = document.createElement("input"); filter.type = "search"; filter.placeholder = "搜索文件名或画面内容"; filter.setAttribute("aria-label", `筛选第 ${index + 1} 句的候选画面`); filterLabel.append(filter);
      const options = element("div", "match-options");
      function drawCandidates() {
        const query = filter.value.trim().toLowerCase();
        // Keep saved choices plus a bounded unselected page, even when the pool exceeds 60.
        const filtered = usable.filter((clip) => selectedIds.has(clip.clip_id) || `${state.assets.find((asset) => asset.id === clip.asset_id)?.name || ""} ${clip.text || ""} ${(clip.tags || []).join(" ")}`.toLowerCase().includes(query));
        const ordered = [...filtered.filter((clip) => selectedIds.has(clip.clip_id)), ...filtered.filter((clip) => !selectedIds.has(clip.clip_id))];
        const nodes = document.createDocumentFragment();
        if (!usable.length) nodes.append(element("p", "field-hint", "先运行画面分析；仅展示分析后确认可用的候选。"));
        else if (!ordered.length) nodes.append(element("p", "field-hint", "没有符合筛选条件的画面。"));
        for (const clip of ordered.slice(0, selectedIds.size + 60)) {
          const choice = element("div", "match-option");
          const label = element("label", "checkbox-label");
          const check = document.createElement("input"); check.type = "checkbox"; check.value = clip.clip_id; check.checked = selectedIds.has(clip.clip_id); check.dataset.clipId = clip.clip_id;
          const asset = state.assets.find((item) => item.id === clip.asset_id);
          const description = `${asset?.name || "源视频"} ${formatTime(clip.start)}–${formatTime(clip.end)}${clip.text ? ` · ${clip.text}` : ""}`;
          label.append(check, element("span", "", description));
          check.addEventListener("change", () => {
            if (check.checked) {
              if (selectedIds.size >= 4500) { check.checked = false; toast("每句最多保留 4500 个候选片段。", true); return; }
              const groups = [...state.reviewMatches].map(([segmentId, ids]) => segmentId === segment.segment_id ? [...ids, clip.clip_id] : [...ids]);
              const budget = selectionBudget(usable, groups, state.assets);
              if (!budget.ready) toast("部分候选画面有重复；保存后会自动选取，最终仍按同画面最多 2 次、额外复用最多 2 次规划。");
              selectedIds.add(clip.clip_id);
            } else selectedIds.delete(clip.clip_id);
            status.className = selectedIds.size ? "field-hint" : "inline-error";
            status.textContent = selectedIds.size ? `已选 ${selectedIds.size} 个片段` : "这句还没有选中画面。";
            state.draftEditorVersion++; state.matchesDirty = true; $("match-save-status").textContent = "画面选择未保存"; renderTimeline(draft); syncButtons();
          });
          choice.append(label, action("预览", () => previewClip(clip), "button button-small button-quiet"));
          nodes.append(choice);
        }
        if (ordered.length > selectedIds.size + 60) nodes.append(element("p", "field-hint", `符合条件 ${ordered.length} 个，当前展示已选候选及前 60 个未选候选；请用上方搜索缩小范围。`));
        options.replaceChildren(nodes);
      }
      filter.addEventListener("input", drawCandidates);
      row.append(status, filterLabel); drawCandidates();
      row.append(options); fragment.append(row);
    }
    if (rejected.length) {
      const rejectedPanel = element("details", "rejected-scenes");
      rejectedPanel.append(element("summary", "", `查看 ${rejected.length} 个排除原因`));
      for (const clip of rejected) rejectedPanel.append(element("p", "field-hint", `${formatTime(clip.start)}–${formatTime(clip.end)}：${clip.reason || "识别结果不确定，未用于自动匹配"}`));
      fragment.append(rejectedPanel);
    }
    $("match-rows").replaceChildren(fragment);
    $("match-save-status").textContent = "画面选择已同步";
  }
  function renderTimeline(draft) {
    const node = $("timeline-summary"); node.replaceChildren(); node.hidden = !isV2Draft(draft);
    if (!isV2Draft(draft)) return;
    const plan = draft.whole_timeline_plan || draft.timeline_plan;
    node.append(element("h3", "", "自动时间线"));
    if (!plan || plan.version !== 1) { node.append(element("p", "field-hint", usesWholeVoice(draft) ? "确认文案并生成视频后，程序先生成整条配音，再自动匹配画面；预览与替换为可选操作。" : "点击“自动匹配画面”，程序按每句时长配齐画面；预览与替换为可选操作。")); return; }
    const seconds = (value) => Number.isFinite(Number(value)) ? Number(value).toFixed(2) : "—";
    const current = !state.draftDirty && !state.matchesDirty;
    const phase = plan.phase === "actual" ? "实测配音" : "预计配音";
    const head = element("div", "timeline-overview");
    head.append(element("strong", timelineReady(draft) && current ? "timeline-ready" : "timeline-shortage", !current ? "选择已变更，保存后更新计划" : timelineReady(draft) ? "时间线完整" : "时间线有缺口"));
    head.append(element("span", "", `${phase}倍速 ${seconds(plan.speed)}×`), element("span", "", `额外复用 ${Number(plan.repeat_count) || 0}/2 次`), element("span", "", `净画面 ${seconds(plan.total_seconds)} 秒`)); node.append(head);
    const table = element("table", "timeline-table");
    const header = element("tr", ""); for (const text of ["口播", "所需净画面", "可用净画面", "缺口"]) header.append(element("th", "", text));
    const thead = element("thead", ""); thead.append(header); table.append(thead);
    const body = element("tbody", "");
    const planSegments = new Map((Array.isArray(plan.segments) ? plan.segments : []).map((item) => [item.segment_id, item]));
    const shortages = new Map((Array.isArray(plan.shortages) ? plan.shortages : []).map((item) => [item.segment_id, item]));
    const visualGroups = Array.isArray(plan.visual_groups) ? plan.visual_groups : draft.segments || [];
    for (const [index, segment] of visualGroups.entries()) {
      const item = planSegments.get(segment.segment_id) || shortages.get(segment.segment_id), shortage = shortages.get(segment.segment_id);
      const row = element("tr", shortage ? "has-shortage" : "");
      row.append(element("th", "", `文案组 ${index + 1}${segment.sentence_ids?.length > 1 ? `（${segment.sentence_ids.length}句共用画面）` : ""}`), element("td", "", item ? `${seconds(item.required_seconds)} 秒` : "未规划"), element("td", "", item ? `${seconds(item.available_seconds)} 秒` : "未规划"), element("td", "", shortage ? `缺 ${seconds(shortage.missing_seconds)} 秒` : item ? "已覆盖" : "待计算"));
      body.append(row);
    }
    table.append(body); node.append(table);
    if (Array.isArray(plan.shortages)) for (const item of plan.shortages) {
      const index = (draft.segments || []).findIndex((segment) => segment.segment_id === item.segment_id);
      node.append(element("p", "timeline-shortage-note", `${index < 0 ? "全片" : `文案 ${index + 1}`}：${item.reason || "净画面时长不足"}`));
    }
    if (current && plan.transition_adjustment?.reason === "transition_overlap_shortage" && plan.transition_seconds === 0) {
      node.append(element("p", "field-hint", "当前计划已自动改为直切，避免淡化转场重叠造成时长缺口。"));
    }
    if (current && Number.isFinite(plan.video_speed) && plan.video_speed > 0 && plan.video_speed < 1) {
      node.append(element("p", "field-hint", `部分镜头已适度慢放以覆盖口播（画面 ${plan.video_speed.toFixed(2)}×，非配音倍速）。`));
    }
    node.append(element("p", "field-hint", plan.phase === "actual" ? "按本次实测配音校准；时长与复用限制通过后才输出。" : "当前按预估配音计算，实际配音后自动校准。净画面已计入转场重叠。"));
  }
  function renderDraftTiming(draft, editedText) {
    const node = $("draft-timing-status");
    node.hidden = !isV2Draft(draft);
    if (!isV2Draft(draft)) return;
    const timing = narrationTiming(draft, editedText);
    node.className = `draft-timing${timing.ready ? "" : " needs-adjustment"}`;
    node.textContent = `${timing.message} 汉字、字母、数字各计 1 字，标点不计；按每秒 4 字预估。`;
    $("draft-meta").textContent = `参考 ${draft.target_seconds} 秒 · 原速预估 ${timing.estimated.toFixed(2)} 秒`;
    $("draft-save-status").textContent = editedText !== undefined ? "文案未保存 · 保存后需要重新匹配画面" : timing.ready ? "文案已保存" : "文案待完善";
  }
  function renderDraft() {
    // A rebuilt editor is a new editing session, even after switching back to the same draft.
    state.draftEditorVersion++;
    const draft = activeDraft();
    $("draft-workspace").hidden = !draft;
    $("mix-empty").hidden = !!draft;
    if (!draft) { syncButtons(); return; }
    $("draft-editor-help").textContent = isV2Draft(draft) ? "这里仅显示成片口播。保存修改后需要重新匹配画面，内部脚本与分镜不展示。" : "历史文案保留原片段关联。可编辑口播，并在下方展开预览原片段。";
    $("whole-migration-notice").hidden = !isWholeVoiceUpgrade(draft);
    $("whole-migration-notice").textContent = isWholeVoiceUpgrade(draft) ? wholeVoiceUpgradeNotice : "";
    const audio = $("whole-audio");
    const whole = draft.whole_audio;
    audio.hidden = !whole;
    if (whole) {
      const src = `/media/voices/${encodeURIComponent(draft.id)}?key=${encodeURIComponent(whole.key)}`;
      if (audio.getAttribute("src") !== src) audio.src = src;
    } else { audio.pause(); audio.removeAttribute("src"); }
    $("whole-duration").textContent = whole ? `整条配音：${Number(whole.duration).toFixed(3)} 秒（音频实测）${Number(whole.duration) > 60 ? "，超过 60 秒交付上限；音频已保留，可试听。请先精简文案，不要反复重试配音。" : ""}` : "确认文案后生成整条语音，再自动匹配画面";
    const target = Number(draft.target_seconds) || 25;
    $("draft-meta").textContent = `${isV2Draft(draft) ? "" : "历史文案 · "}参考 ${target} 秒${draft.estimated_seconds ? ` · 预估 ${Number(draft.estimated_seconds).toFixed(1)} 秒` : ""} · 实际时长以配音为准`;
    const fragment = document.createDocumentFragment();
    if (isV2Draft(draft)) {
      const row = element("div", "voiceover-editor");
      const label = element("label", "sr-only", "可编辑的口播文案"); label.htmlFor = "draft-voiceover";
      const input = document.createElement("textarea"); input.id = "draft-voiceover"; input.rows = 10; input.maxLength = 2000;
      input.value = draft.voiceover || (draft.segments || []).map((segment) => segment.text).join("");
      input.setAttribute("aria-describedby", "draft-timing-status");
      input.addEventListener("input", () => { state.draftEditorVersion++; state.draftDirty = true; renderDraftTiming(draft, input.value); renderTimeline(draft); syncButtons(); });
      row.append(label, input); fragment.append(row);
    } else {
      // Preserve editable old drafts and their clip identity; no new V1 creation.
      for (const [index, segment] of (draft.segments || []).entries()) {
        const row = element("div", "segment-row");
        const label = element("label", "sr-only", `第 ${index + 1} 段历史文案`); label.htmlFor = `segment-text-${index}`;
        const input = document.createElement("textarea"); input.id = `segment-text-${index}`; input.rows = 3; input.value = segment.text; input.dataset.clipId = segment.clip_id;
        input.addEventListener("input", () => { state.draftEditorVersion++; state.draftDirty = true; $("draft-save-status").textContent = "有未保存的修改"; syncButtons(); });
        row.append(label, input); fragment.append(row);
      }
    }
    $("draft-segments").replaceChildren(fragment);
    $("draft-save-status").textContent = "文案已保存";
    renderDraftTiming(draft);
    state.matchesDirty = false;
    renderMatches(draft);
    renderTimeline(draft);
    syncButtons();
  }
  $("draft-select").addEventListener("change", async () => {
    const nextId = $("draft-select").value;
    if ((state.draftDirty || state.matchesDirty) && !(await confirmAction("切换文案？", "当前文案或画面选择还有未保存的修改。切换后会丢弃这些修改，已保存的内容保持不变。", "丢弃修改并切换"))) {
      $("draft-select").value = state.activeDraftId;
      return;
    }
    state.activeDraftId = nextId; state.draftDirty = false; renderDraftOptions(); renderDraft();
  });
  async function saveDraft() {
    const draft = activeDraft();
    if (!draft) throw new Error("请先选择一份文案。");
    const submittedEditorVersion = state.draftEditorVersion;
    let updated;
    if (isV2Draft(draft)) {
      const voiceover = document.getElementById("draft-voiceover").value.trim();
      if (!voiceover) throw new Error("请填写口播文案。");
      updated = await api(`/api/v2/drafts/${encodeURIComponent(draft.id)}`, "PUT", { voiceover });
    } else {
      const segments = [...$("draft-segments").querySelectorAll("textarea")].map((input) => ({ clip_id: input.dataset.clipId, text: input.value.trim() }));
      if (segments.some((segment) => !segment.text)) throw new Error("每段文案都需要填写内容。");
      updated = await api(`/api/drafts/${encodeURIComponent(draft.id)}`, "PUT", { segments });
    }
    const index = state.drafts.findIndex((item) => item.id === draft.id);
    if (index >= 0 && updated?.id === draft.id) state.drafts[index] = updated;
    // An older response may update its saved draft, never acknowledge or replace later edits.
    const editorUnchanged = state.activeDraftId === draft.id && state.draftEditorVersion === submittedEditorVersion;
    if (editorUnchanged) {
      state.draftDirty = false;
      state.matchesDirty = false;
      renderDraftOptions(); renderDraft();
      toast(isV2Draft(draft) ? canMatchDraft(updated) ? "文案已保存，请重新匹配画面。" : "文案已保留，请完善口播内容；无需重新调用模型。" : "文案修改已保存。");
    } else {
      toast("已保存刚才提交的版本；当前编辑内容保持不变，新增修改请继续保存。");
      syncButtons();
    }
    await refresh();
  }
  $("save-draft").addEventListener("click", () => withBusy($("save-draft"), saveDraft));
  const visionCostNotice = "GLM-5.3-Flash 每条源视频最多 45 次视觉请求，每批最多 2 个片段、6 张图。按 3 秒窗口、每窗最多 3 帧抽样，每条视频最多 90 个窗口；长视频均匀抽样。已有成功分析的有效缓存会复用，包含兼容的旧五帧结果；仅未完成的画面分析会新调用 GLM。DeepSeek 每批最多 128 个候选窗，一次比较全部文案段落（最多64段），字符预算可能进一步拆批；实际批数会显示在任务进度中。候选池变化时，DeepSeek 匹配可能再次调用；不会自动重试收费请求。";
  async function matchingBudgetNotice(drafts, operation = "match") {
    const notices = [];
    for (const draft of drafts.filter(isV2Draft)) {
      const budget = await api(`/api/v2/drafts/${encodeURIComponent(draft.id)}/matching-budget?operation=${operation}`);
      notices.push(budget.message || (budget.exact ? `已有 ${budget.available_scene_count} 个可用画面，DeepSeek 预计新增 ${budget.new_request_count} 次请求，复用 ${budget.cached_request_count} 个成功检查点。` : "尚未完成画面分析，匹配次数将在识别后确定；已有成功缓存会复用。"));
    }
    return notices.join("\n");
  }
  $("match-draft").addEventListener("click", () => withBusy($("match-draft"), async () => {
    const draft = activeDraft();
    if (!isV2Draft(draft) || state.draftDirty || state.matchesDirty) throw new Error("请先保存当前文案及画面选择。");
    if (!canMatchDraft(draft)) throw new Error(`请先完善文案并保存。${narrationTiming(draft).message}`);
    if (!state.settings.glm_key_set || !state.settings.deepseek_key_set) { setPage("settings"); (!state.settings.glm_key_set ? $("set-glm_api_key") : $("set-deepseek_api_key")).focus(); throw new Error("请先在设置中保存 GLM 视觉识别及 DeepSeek 密钥。"); }
    const budgetNotice = await matchingBudgetNotice([draft]);
    const accepted = await confirmAction("分析素材并匹配画面？", `将未分析素材的关键帧发送至视觉模型，分类后按文案自动匹配画面；部分批次失败时，已保存结果仍可参与匹配。${visionCostNotice}\n\n${budgetNotice}\n\n可能产生接口费用，本次不调用 ${voiceProviderName(true)} 配音。预览与手动替换为可选操作。`, "确认分析与匹配费用");
    if (!accepted) return;
    await api(`/api/v2/drafts/${encodeURIComponent(draft.id)}/match`, "POST", { allow_paid: true });
    toast("画面分析与匹配已提交，可在下方查看进度。"); await refresh();
  }));
  $("save-matches").addEventListener("click", () => withBusy($("save-matches"), async () => {
    const draft = activeDraft();
    if (!isV2Draft(draft) || state.draftDirty) throw new Error("请先保存文案并重新匹配画面。");
    if (!canMatchDraft(draft)) throw new Error("请先完善有效口播文案并保存，再匹配画面。");
    const submittedEditorVersion = state.draftEditorVersion;
    const matches = (draft.segments || []).map((segment) => ({ segment_id: segment.segment_id, clip_ids: [...(state.reviewMatches.get(segment.segment_id) || [])] }));
    const updated = await api(`/api/v2/drafts/${encodeURIComponent(draft.id)}/matches`, "PUT", encodeMatches(matches));
    const index = state.drafts.findIndex((item) => item.id === draft.id);
    if (index >= 0 && updated?.id === draft.id) state.drafts[index] = updated;
    if (state.activeDraftId === draft.id && state.draftEditorVersion === submittedEditorVersion) {
      state.matchesDirty = false;
      toast("画面选择已保存，时间线结果已更新。");
    } else toast("已保存刚才提交的画面选择；当前新增修改保持不变，请继续保存。");
    await refresh();
  }));
  async function renderVideos(drafts) {
    if (state.draftDirty || state.matchesDirty) throw new Error("请先保存文案和画面选择，再生成视频。");
    if (drafts.some((draft) => isV2Draft(draft) && !narrationTiming(draft).ready)) throw new Error("本组有空白或无效文案，请先完善并保存。");
    if (drafts.some((draft) => !canRenderDraft(draft))) throw new Error("请先为每条文案匹配完整画面，并预览核对。");
    const provider = voiceProvider();
    if ($("set-voice_provider").value !== provider) { setPage("settings"); $("save-settings").focus(); throw new Error("配音服务已更改，请先保存设置，再确认生成视频。"); }
    if (!state.settings[`${provider}_key_set`]) { setPage("settings"); $(`set-${provider}_api_key`).focus(); throw new Error(`请先保存 ${voiceProviderName()} API Key。`); }
    if (!drafts.length) throw new Error("请先生成并确认文案。");
    // Keep the shown paid consent and its request on the same saved voice snapshot.
    const voice = provider === "minimax"
      ? { provider, model: state.settings.minimax_model || defaultSettings.minimax_model, voice_id: state.settings.minimax_voice_id || defaultSettings.minimax_voice_id }
      : { provider, model: "s2.1-pro", voice_id: state.settings.voice_id || defaultSettings.voice_id };
    const voiceName = provider === "minimax" ? "MiniMax" : "Fish Audio";
    const shortVoiceName = provider === "minimax" ? "MiniMax" : "Fish";
    const budgetNotice = await matchingBudgetNotice(drafts, "produce");
    const migrationNotice = drafts.some(isWholeVoiceUpgrade) ? `\n\n${wholeVoiceUpgradeNotice}` : "";
    const longCopyNotice = drafts.some(draft => usesWholeVoice(draft) && !draft.whole_audio && !draft.whole_voice && narrationTiming(draft).estimated > 60) ? "\n\n文案字数只作估算，真实配音可能超过 60 秒交付上限；不会自动截断或重新购买配音。建议先精简文案，再决定是否付费生成。" : "";
    const accepted = await confirmAction("确认文案并生成视频？",
      `先免费预检，然后为 ${drafts.length} 条文案各生成一整条 ${shortVoiceName} 配音，按实测语音分析、匹配画面并导出。\n\n这会产生 ${voiceName} 配音、GLM 画面识别和 DeepSeek 匹配费用。${visionCostNotice}\n\n${budgetNotice}\n\n请确认文案与音色。成功缓存会复用。参考时长仅作参考，成片按实测配音与镜头校准，最长 60 秒。画面偏长自动裁短，偏短时可适度慢放；必要时使用允许的复用额度和自然语速整条配音，不为了凑参考时长加速口播。确有相关画面不足或超过 60 秒上限时会保留缓存，不自动追加配音。若接口结果不确定，不会自动重复提交收费请求。${migrationNotice}${longCopyNotice}`,
      `确认生成 ${drafts.length} 条`);
    if (!accepted) return;
    let submitted = 0;
    try {
      for (const draft of drafts) {
        await api(usesWholeVoice(draft) ? `/api/v2/drafts/${encodeURIComponent(draft.id)}/produce` : `/api/drafts/${encodeURIComponent(draft.id)}/render`, "POST", { allow_paid: true, voice });
        submitted++;
      }
      toast(`${submitted} 条视频任务已提交。`);
    } catch (error) {
      throw new Error(`已提交 ${submitted} 条任务。${error.message} 请先查看任务列表，避免重复生成。`);
    } finally { await refresh(); }
  }
  $("render-draft").addEventListener("click", () => withBusy($("render-draft"), () => renderVideos(activeDraft() ? [activeDraft()] : [])));
  $("render-all").addEventListener("click", () => withBusy($("render-all"), () => renderVideos(batchDrafts())));

  function showOutput(job) {
    setPage("mix");
    $("draft-workspace").hidden = false;
    $("mix-empty").hidden = true;
    const video = $("mix-preview");
    video.pause();
    state.activePreview = { kind: "output", id: job.id };
    video.onloadedmetadata = null;
    video.src = `/media/outputs/${encodeURIComponent(job.id)}`;
    video.hidden = false;
    $("mix-preview-empty").hidden = true;
    $("mix-preview-heading").textContent = "成片预览";
    $("mix-preview-caption").textContent = `已完成的输出视频 · ${job.result?.duration ? formatTime(job.result.duration) : job.id.slice(0, 8)}`;
    video.play().catch(() => {});
    video.scrollIntoView({ block: "center", behavior: "auto" });
  }
  async function retryJob(job) {
    const paid = ["prepare", "script", "match", "render"].includes(job.kind);
    const accepted = await confirmAction(paid ? "确认重新提交任务？" : "重试任务？",
      paid ? `这将重新执行${{ render: `整条 ${voiceProviderName()} 配音、画面分析匹配及合成（成功缓存复用）`, prepare: "历史转写与 DeepSeek 改写", script: "DeepSeek 文案生成", match: "视觉分析与 DeepSeek 匹配" }[job.kind]}，可能再次产生接口费用。${job.kind === "match" ? `${visionCostNotice}本次不调用 ${voiceProviderName(true)} 配音。` : ""}若上次请求结果不确定，请先核对服务端状态再重试。` : "将重新提交这个任务，已有原素材保持不变。",
      paid ? "确认重试及可能的费用" : "重试");
    if (!accepted) return;
    const draft = state.drafts.find(item => item.id === job.draft_id);
    const route = job.kind === "render" && usesWholeVoice(draft) ? `/api/v2/jobs/${encodeURIComponent(job.id)}/retry` : `/api/jobs/${encodeURIComponent(job.id)}/retry`;
    const productVersion = state.productInputVersion;
    const submitted = await api(route, "POST", { allow_paid: paid });
    if (job.kind === "product") {
      state.productJobId = submitted?.id || submitted?.job_id;
      state.productInputDirty = true; state.requestedProductUrl = $("product-url").value.trim() || "retry";
      state.productRequestVersion = productVersion;
    }
    toast("重试任务已提交。");
    await refresh();
  }
  let jobDetailVersion = 0;
  function fullJobMessage(job, fallback = "") {
    const messages = [job.error || job.message || fallback];
    if (Array.isArray(job.result?.warnings) && job.result.warnings.length) messages.push(`处理提示：${job.result.warnings.join("；")}`);
    if (job.diagnostic && Object.keys(job.diagnostic).length) messages.push(`诊断信息：\n${JSON.stringify(job.diagnostic, null, 2)}`);
    return messages.filter(Boolean).join("\n\n") || fallback;
  }
  async function showJobDetails(job, fallback) {
    const version = ++jobDetailVersion;
    $("job-detail-title").textContent = `${jobNames[job.kind] || "后台任务"} · ${String(job.id).slice(0, 8)} · ${job.error ? "错误详情" : "任务详情"}`;
    $("job-detail-content").textContent = fullJobMessage(job, fallback);
    if (!$("job-detail-dialog").open) $("job-detail-dialog").showModal();
    $("close-job-detail").focus();
    try {
      const detail = await api(`/api/jobs/${encodeURIComponent(job.id)}`);
      if (version === jobDetailVersion && $("job-detail-dialog").open && (detail?.error || detail?.message || detail?.result?.warnings?.length)) {
        $("job-detail-content").textContent = fullJobMessage(detail, fallback);
      }
    } catch { /* The full text already received remains visible even offline. */ }
  }
  $("close-job-detail").addEventListener("click", () => { jobDetailVersion++; $("job-detail-dialog").close(); });
  function jobRow(job) {
    const row = element("article", "job-row");
    const kind = element("div", "job-kind");
    kind.append(icon(job.kind === "render" ? "film" : ["prepare", "script", "match"].includes(job.kind) ? "text" : job.kind === "download" ? "link" : job.kind === "music_import" ? "music" : "folder"));
    const body = element("div", "job-body");
    const heading = element("div", "job-heading");
    const adjustmentIds = job.kind === "script" && job.status === "completed" && Array.isArray(job.result?.needs_adjustment_ids)
      ? job.result.needs_adjustment_ids.filter((id) => { const draft = state.drafts.find((item) => item.id === id); return !draft || !narrationTiming(draft).ready; }) : [];
    const pendingLabel = adjustmentIds.length ? "文案待完善" : "";
    heading.append(element("span", "job-name", `${jobNames[job.kind] || "后台任务"} · ${String(job.id).slice(0, 8)}`), element("span", `job-state ${pendingLabel ? "needs-adjustment" : job.status}`, pendingLabel || statusNames[job.status] || job.status));
    body.append(heading);
    // Obsolete review metadata does not impose an action on completed script jobs.
    const staleTimingMessage = job.kind === "script" && job.status === "completed" && job.result?.needs_adjustment_ids?.length && !adjustmentIds.length;
    const jobMessage = staleTimingMessage ? "文案已生成，按当前倍速规则可继续匹配画面。"
      : job.kind === "script" && job.status === "completed" && job.result?.needs_fact_review_ids?.length
        ? "文案已生成，可继续匹配画面。" : job.message;
    const warningText = Array.isArray(job.result?.warnings) && job.result.warnings.length ? `处理提示：${job.result.warnings.join("；")}` : "";
    const adjustmentText = adjustmentIds.length ? `已保留全部文案，其中 ${adjustmentIds.length} 条需调整时长。请在上方编辑文案并保存，无需重新生成；未自动追加模型调用。` : "";
    const message = [String(job.error || adjustmentText || jobMessage || ""), warningText].filter(Boolean).join("\n\n");
    if (message) {
      if (job.error || message.length > 120 || warningText) {
        const detail = action(message.replace(/\s+/g, " "), () => showJobDetails(job, message), `job-inline-detail${job.error ? " job-error" : ""}`);
        detail.setAttribute("aria-label", `查看${job.error ? "完整错误" : "完整任务详情"}：${jobNames[job.kind] || "后台任务"}`);
        detail.title = "点击查看完整内容";
        heading.append(detail);
      } else body.append(element("p", "job-message", message));
    }
    if (["queued", "running", "cancelling"].includes(job.status)) {
      const progress = document.createElement("progress");
      progress.className = "job-progress";
      progress.max = 1;
      progress.value = Math.max(0, Math.min(1, Number(job.progress) || 0));
      progress.setAttribute("aria-label", `${jobNames[job.kind] || "任务"}进度`);
      body.append(progress);
    }
    const actions = element("div", "job-actions");
    if (["queued", "running"].includes(job.status)) {
      actions.append(action("取消", async () => {
        await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, "POST");
        toast("已请求取消，正在结束后台步骤。"); await refresh();
      }, "button button-small button-quiet"));
    }
    if (["failed", "cancelled", "interrupted"].includes(job.status)) actions.append(action("重试", () => retryJob(job)));
    if (job.kind === "render" && job.status === "completed") {
      actions.append(action("预览成片", () => showOutput(job)), action("打开目录", () => api("/api/open-output", "POST", { job_id: job.id })));
    }
    row.append(kind, body, actions);
    return row;
  }
  function renderJobs() {
    const sorted = [...state.jobs].sort((a, b) => timestamp(b.created_at) - timestamp(a.created_at));
    const downloads = sorted.filter((job) => !["prepare", "product", "script", "match", "render", "media_prepare", "preflight"].includes(job.kind) && job.kind !== "alignment");
    const mixing = sorted.filter((job) => ["prepare", "product", "script", "match", "render", "media_prepare", "preflight"].includes(job.kind) || job.kind === "alignment");
    function fill(id, key, jobs, message) {
      const page = jobPage(jobs, state.jobPages[key]); state.jobPages[key] = page.index;
      const fragment = document.createDocumentFragment();
      page.items.forEach((job) => fragment.append(jobRow(job)));
      if (!jobs.length) fragment.append(element("p", "jobs-empty", message));
      $(id).replaceChildren(fragment);
      $(`${key}-page-info`).textContent = page.total ? `${page.index * 20 + 1}–${page.index * 20 + page.items.length} / ${page.total} 条 · 进行中 ${page.active}` : "暂无任务";
      $(`${key}-prev`).disabled = page.index === 0; $(`${key}-next`).disabled = page.index + 1 >= page.pages;
    }
    fill("download-jobs", "download", downloads, "导入和下载任务会显示在这里。");
    fill("mix-jobs", "mix", mixing, "提交文案或视频任务后，在这里查看进度。");
    $("download-count").textContent = String(downloads.length);
    const active = sorted.filter((job) => ["queued", "running", "cancelling"].includes(job.status)).length;
    $("active-count").textContent = String(active);
    $("active-count").hidden = !active;
    const completed = mixing.filter((job) => job.kind === "render" && job.status === "completed").length;
    $("task-summary").textContent = mixing.length ? `${mixing.length} 个任务 · ${completed} 条成片` : "暂无任务";
  }

  for (const key of ["download", "mix"]) {
    $(`${key}-prev`).addEventListener("click", () => { state.jobPages[key]--; renderJobs(); });
    $(`${key}-next`).addEventListener("click", () => { state.jobPages[key]++; renderJobs(); });
    $(`open-${key}-logs`).addEventListener("click", () => withBusy($(`open-${key}-logs`), () => api("/api/open-logs", "POST")));
  }
  function fillSettings() {
    if (state.settingsDirty) return;
    const values = { ...defaultSettings, ...state.settings };
    settingKeys.forEach((key) => {
      if (key === "caption_plate") $(`set-${key}`).checked = Boolean(values[key]);
      else $(`set-${key}`).value = values[key] ?? "";
    });
    syncVoiceProviderFields();
    $("settings-status").textContent = "设置已与本机同步";
  }
  function renderEnvironment() {
    const recovery = state.settings.settings_recovery;
    $("settings-recovery").hidden = !recovery?.needed;
    $("settings-recovery").textContent = recovery?.message || "";
    $("credential-recovery").hidden = state.settings.credentials_need_reset !== true;
    const fragment = document.createDocumentFragment();
    [
      ["ffmpeg", "FFmpeg"], ["ffprobe", "FFprobe"], ["asr_available", "本地转写"],
      ["desktop_available", "桌面组件"],
    ].forEach(([key, label]) => {
      const ready = Boolean(state.environment[key]);
      fragment.append(element("span", `environment-chip ${ready ? "ready" : "missing"}`, `${label} ${ready ? "就绪" : "待配置"}`));
    });
    $("environment-status").replaceChildren(fragment);
    if (Array.isArray(state.environment.notes) && state.environment.notes.length) {
      $("environment-status").append(element("p", "field-hint", state.environment.notes.join("；")));
    }
    for (const name of ["deepseek", "fish", "minimax", "glm"]) {
      const configured = Boolean(state.settings[`${name}_key_set`]);
      $(`${name}-key-status`).textContent = configured ? "已保存" : "未配置";
      $(`${name}-key-status`).classList.toggle("configured", configured);
    }
    $("voice-provider-label").textContent = voiceProviderName();
    $("render-cost-hint").textContent = `确认后先免费预检，再生成整条配音、自动匹配画面并导出。会确认 ${voiceProviderName()}、GLM 和 DeepSeek 的可能费用，成功缓存复用；不保留素材原始口播。`;
    $("cleaning-label").textContent = { none: "不处理原字幕", blur: "区域模糊", delogo: "区域修补" }[state.settings.clean_mode] || "不处理原字幕";
    $("transition-label").textContent = state.settings.transition === "none" ? "直切" : `轻柔淡化 ${state.settings.transition_seconds ?? 0.2}s`;
  }
  function markSettingsEdited() {
    state.settingsEditorVersion++;
    state.settingsDirty = true; $("settings-status").textContent = "有未保存的设置";
  }
  $("settings-form").addEventListener("input", markSettingsEdited);
  $("settings-form").addEventListener("change", markSettingsEdited);
  $("set-voice_provider").addEventListener("change", () => { syncVoiceProviderFields(); markSettingsEdited(); });
  document.querySelectorAll(".choose-setting-folder").forEach((button) => button.addEventListener("click", () => withBusy(button, () => nativePath("choose_folder", button.dataset.for))));
  function musicImportRunning() {
    const ownJob = state.jobs.find(job => job.id === state.musicImportId);
    return Boolean(state.musicImportId && (!ownJob || ["queued", "running", "cancelling"].includes(ownJob.status))) || state.jobs.some(job => job.kind === "music_import" && ["queued", "running", "cancelling"].includes(job.status));
  }
  function finishOwnMusicImport(job) {
    if (!job || job.id !== state.musicImportId || !["completed", "failed", "cancelled", "interrupted"].includes(job.status)) return false;
    state.musicImportId = null;
    const result = job.result || {}, count = key => Number(result[`${key}_count`]) || (Array.isArray(result[key]) ? result[key].length : Number(result[key]) || 0);
    state.musicImportSummary = job.status === "completed" ? `导入完成：新增 ${count("imported")} 首，已有 ${count("duplicate") || count("duplicates")} 首，失败 ${count("failed")} 首。` : "音乐导入未完成，原始歌曲保持不变。";
    if (job.error) state.musicUiError = job.error;
    renderMusic();
    return true;
  }
  let musicImportLookupId = null;
  function reconcileMissingMusicImport() {
    const identifier = state.musicImportId;
    if (!identifier || musicImportLookupId === identifier) return;
    musicImportLookupId = identifier;
    // Compact state intentionally omits older completed jobs. Read the owned
    // import once by ID instead of treating its absence as perpetual activity.
    api(`/api/jobs/${encodeURIComponent(identifier)}`).then(job => {
      if (state.musicImportId === identifier && finishOwnMusicImport(job)) {
        state.musicFinishedKey = `${job.id}:${job.status}`;
        return refreshMusic();
      }
    }).catch(error => {
      if (state.musicImportId === identifier) { state.musicUiError = `导入状态暂未同步：${error.message}`; renderMusicError(); }
    }).finally(() => { if (musicImportLookupId === identifier) musicImportLookupId = null; });
  }
  function syncMusicButtons() {
    const importing = musicImportRunning();
    if (!state.busy.has($("import-music"))) $("import-music").disabled = !state.connected || importing || !state.musicScan?.scan_id || state.musicChecks.size === 0;
    if (!state.busy.has($("scan-music"))) $("scan-music").disabled = !state.connected || importing;
    if (!state.busy.has($("choose-bgm"))) $("choose-bgm").disabled = !state.connected || importing;
    if (!state.busy.has($("refresh-music"))) $("refresh-music").disabled = !state.connected || state.musicLoading;
    $("disable-bgm").disabled = !state.connected || !state.music.current || state.musicWrites > 0;
    $("save-bgm-gain").disabled = !state.connected || !state.musicGainDirty || state.musicGainPending.has(state.musicGainVersion);
    $("music-tracks").querySelectorAll("input").forEach(input => {
      const track = state.music.tracks.find(item => item.id === input.value);
      input.disabled = !state.connected || track?.available === false || state.musicWrites > 0;
    });
  }
  function renderMusicError() {
    const error = state.musicUiError || state.music.error || (state.music.current?.available === false ? "当前默认音乐文件已丢失，请重新导入或关闭背景音乐。" : "");
    $("music-error").textContent = error;
    $("music-error").hidden = !error;
    if (error && error !== state.musicLastError) $("music-panel").open = true;
    state.musicLastError = error;
  }
  function applyMusicData(data) {
    if (!data || typeof data !== "object") return;
    state.music = { ...state.music, ...data, tracks: Array.isArray(data.tracks) ? data.tracks : state.music.tracks };
    if (Array.isArray(data.tracks)) state.musicLoaded = true;
    renderMusic();
  }
  function musicWrite(path, body, committed) {
    state.musicEpoch++;
    state.musicWrites++;
    state.musicUiError = "";
    state.musicImportSummary = "";
    syncMusicButtons();
    const operation = state.musicWriteTail.then(async () => {
      const result = await api(path, "POST", body);
      if (committed) committed(result); else applyMusicData(result);
      return result;
    });
    // Ordering protects independent song/volume writes without blocking typing.
    state.musicWriteTail = operation.catch(() => {});
    return operation.catch(error => { state.musicUiError = error.message; throw error; }).finally(() => {
      state.musicWrites--; renderMusic();
    });
  }
  async function refreshMusic() {
    const request = ++state.musicGetVersion, epoch = state.musicEpoch;
    state.musicLoading = true; syncMusicButtons();
    try {
      const result = await api("/api/music");
      if (request === state.musicGetVersion && epoch === state.musicEpoch) {
        applyMusicData(result);
      }
    } catch (error) {
      if (request === state.musicGetVersion) { state.musicUiError = error.message; renderMusicError(); }
      throw error;
    } finally {
      if (request === state.musicGetVersion) { state.musicLoading = false; syncMusicButtons(); }
    }
  }
  function renderMusicCandidates() {
    const fragment = document.createDocumentFragment();
    for (const candidate of state.musicScan?.candidates || []) {
      const label = element("label", "checkbox-label music-candidate");
      const box = document.createElement("input"); box.type = "checkbox"; box.value = candidate.id;
      box.checked = state.musicChecks.has(candidate.id);
      box.addEventListener("change", () => { box.checked ? state.musicChecks.add(candidate.id) : state.musicChecks.delete(candidate.id); syncMusicButtons(); });
      label.append(box, element("span", "", candidate.name)); fragment.append(label);
    }
    if (state.musicScan && !state.musicScan.candidates?.length) fragment.append(element("p", "field-hint", "当前文件夹没有可导入的歌曲。"));
    $("music-candidates").replaceChildren(fragment);
    syncMusicButtons();
  }
  function renderMusic() {
    const current = state.music.current;
    $("music-current-name").textContent = current?.name || "未选择";
    $("set-bgm_path").value = current?.path || "";
    if (!state.musicGainDirty) $("set-bgm_gain").value = String(state.music.gain ?? .15);
    $("music-status").textContent = musicImportRunning() ? "正在复制并校验歌曲，原始歌曲保持不变。导入完成后将更新音乐库。" : state.musicImportSummary || (
      current ? current.managed === false ? "继续使用原来选择的外部音乐，不会自动更换。可从下方文件夹导入副本后选择库内歌曲。" : "已固定为默认音乐；后续成片继续使用这首，直到你手动更换。"
        : "尚未选择音乐。首次导入的第一首可设为默认；已有默认歌曲不会因导入而自动更换。");
    renderMusicError();
    const signature = JSON.stringify([state.musicLoaded, (state.music.tracks || []).map(track => [track.id, track.name, track.duration, track.available])]);
    if (signature === state.musicTracksSignature) {
      $("music-tracks").querySelectorAll("input").forEach(input => { input.checked = current?.id === input.value; });
      syncMusicButtons(); return;
    }
    state.musicTracksSignature = signature;
    const fragment = document.createDocumentFragment();
    for (const track of state.music.tracks || []) {
      const row = element("div", "music-track"); row.dataset.id = track.id;
      const label = element("label", "checkbox-label music-track-label");
      const radio = document.createElement("input"); radio.type = "radio"; radio.name = "default-music"; radio.value = track.id;
      radio.checked = current?.id === track.id;
      radio.addEventListener("change", () => { if (radio.checked) musicWrite("/api/music/select", { track_id: track.id }).catch(() => {}); });
      const name = element("span", "music-track-name", track.name); name.title = track.name;
      label.append(radio, name);
      const duration = Number(track.duration);
      const meta = element("span", "music-track-meta", track.available === false ? "文件丢失" : Number.isFinite(duration) && duration > 0 ? `${duration.toFixed(1)} 秒` : "");
      const remove = action("删除", async () => {
        const isCurrent = state.music.current?.id === track.id;
        const confirmed = await confirmAction("删除这首库内音乐？", `“${track.name}”\n\n只删除软件音乐库中的副本，原始歌曲保持不变。${isCurrent ? "\n\n这首是当前默认歌曲。确认后将关闭背景音乐，不会自动选择另一首。" : ""}`, "确认删除");
        if (!confirmed) return;
        const result = await musicWrite("/api/music/delete", { track_ids: [track.id], confirm: true });
        if (result.failed?.length) { state.musicUiError = result.failed.map(item => item.error || item.message || "部分歌曲未删除").join("；"); renderMusicError(); }
      }, "button button-small button-danger");
      row.append(label, meta, remove); fragment.append(row);
    }
    if (!state.music.tracks?.length) fragment.append(element("p", "field-hint", state.musicLoaded ? "音乐库暂无歌曲，请从文件夹勾选导入。" : "展开后读取音乐库。"));
    $("music-tracks").replaceChildren(fragment);
    syncMusicButtons();
  }
  async function scanMusicFolder() {
    const folder = $("music-folder").value.trim();
    if (!folder) throw new Error("请选择或填写音乐文件夹。");
    const version = ++state.musicScanVersion;
    state.musicScan = null; state.musicChecks.clear(); renderMusicCandidates();
    state.musicUiError = "";
    try {
      const result = await api("/api/music/scan", "POST", { folder });
      if (version !== state.musicScanVersion || $("music-folder").value.trim() !== folder) return;
      state.musicScan = result;
      state.musicChecks.clear(); renderMusicCandidates(); renderMusicError();
    } catch (error) {
      if (version !== state.musicScanVersion || $("music-folder").value.trim() !== folder) return;
      state.musicUiError = error.message; renderMusicError(); throw error;
    }
  }
  $("music-folder").addEventListener("input", () => { state.musicScanVersion++; state.musicScan = null; state.musicChecks.clear(); renderMusicCandidates(); });
  $("choose-bgm").addEventListener("click", () => withBusy($("choose-bgm"), async () => {
    const bridge = window.pywebview?.api;
    if (typeof bridge?.choose_music_folder !== "function") {
      $("music-folder").focus(); throw new Error("请在输入框填写音乐文件夹的完整路径，再点击扫描歌曲。");
    }
    const folder = await bridge.choose_music_folder();
    if (typeof folder !== "string" || !folder) return;
    $("music-folder").value = folder;
    $("music-folder").dispatchEvent(new Event("input", { bubbles: true }));
    await scanMusicFolder();
  }));
  $("scan-music").addEventListener("click", () => withBusy($("scan-music"), scanMusicFolder));
  $("import-music").addEventListener("click", () => withBusy($("import-music"), async () => {
    if (musicImportRunning() || !state.musicScan?.scan_id || !state.musicChecks.size) return;
    try {
      const job = await api("/api/music/import", "POST", { scan_id: state.musicScan.scan_id, candidate_ids: [...state.musicChecks] });
      state.musicImportId = job.id; state.musicChecks.clear(); state.musicUiError = "";
      renderMusicCandidates(); renderMusic(); await refresh();
    } catch (error) { state.musicUiError = error.message; renderMusicError(); throw error; }
  }));
  $("refresh-music").addEventListener("click", () => withBusy($("refresh-music"), () => { state.musicUiError = ""; return refreshMusic(); }));
  $("music-panel").addEventListener("toggle", () => { if ($("music-panel").open && !state.musicLoading) refreshMusic().catch(() => {}); });
  $("disable-bgm").addEventListener("click", () => { musicWrite("/api/music/select", { track_id: null }).catch(() => {}); });
  $("set-bgm_gain").addEventListener("input", () => {
    state.musicGainDirty = true; state.musicGainVersion++; state.musicUiError = ""; renderMusicError(); syncMusicButtons();
  });
  async function saveMusicGain() {
    const raw = String($("set-bgm_gain").value).trim(), gain = Number(raw);
    if (!raw || !Number.isFinite(gain) || gain < 0 || gain > 1) {
      state.musicUiError = "音乐音量请填写 0～1 的数字，留空不会静音。"; renderMusicError(); return;
    }
    const version = state.musicGainVersion;
    if (state.musicGainPending.has(version)) return;
    state.musicGainPending.add(version); syncMusicButtons();
    try {
      await musicWrite("/api/music/volume", { gain }, result => {
        if (state.musicGainVersion === version) state.musicGainDirty = false;
        applyMusicData(result);
      });
    } catch { /* Keep the edited number and the inline error for a manual retry. */ }
    finally { state.musicGainPending.delete(version); syncMusicButtons(); }
  }
  $("set-bgm_gain").addEventListener("change", saveMusicGain);
  $("save-bgm-gain").addEventListener("click", saveMusicGain);
  $("settings-form").addEventListener("submit", (event) => {
    event.preventDefault();
    withBusy($("save-settings"), async () => {
      if (state.busy.has($("reset-credentials"))) throw new Error("凭据重建正在进行，请完成后再保存设置。");
      syncButtons();
      const submittedSettingsVersion = state.settingsEditorVersion;
      const body = {};
      settingKeys.forEach((key) => {
        const input = $(`set-${key}`);
        if (numericSettings.has(key) && !String(input.value).trim()) throw new Error("数字设置不能为空，请填写后再保存。");
        body[key] = key === "caption_plate" ? input.checked : numericSettings.has(key) ? Number(input.value) : input.value.trim();
      });
      for (const key of ["deepseek_api_key", "fish_api_key", "minimax_api_key", "glm_api_key"]) {
        const value = $(`set-${key}`).value.trim();
        if (value) body[key] = value;
      }
      await api("/api/settings", "PUT", body);
      if (state.settingsEditorVersion === submittedSettingsVersion) {
        $("set-deepseek_api_key").value = "";
        $("set-fish_api_key").value = "";
        $("set-minimax_api_key").value = "";
        $("set-glm_api_key").value = "";
        state.settingsDirty = false;
      }
      toast(state.settingsDirty ? "已保存刚才提交的设置；后续输入仍未保存，请继续保存。" : "设置已保存。密钥不会回显。");
      await refresh();
    });
  });
  $("reset-credentials").addEventListener("click", () => withBusy($("reset-credentials"), async () => {
    if (state.settings.credentials_need_reset !== true) return;
    if (state.busy.has($("save-settings"))) throw new Error("设置正在保存，请完成后再重建凭据。");
    syncButtons();
    const accepted = await confirmAction("重建本机凭据？",
      "程序将备份旧加密凭据文件，并重建空凭据。原来保存的服务密钥将重置，需要重新输入 DeepSeek、Fish Audio、MiniMax 和 GLM 密钥。其他设置不变，页面上已填但未保存的内容会保留。本操作不会调用模型。",
      "备份并重建空凭据");
    if (!accepted) return;
    state.settings = await api("/api/settings/reset-credentials", "POST", { confirm: true });
    // A reset changes stored credentials, not the user's current form edits.
    fillSettings(); renderEnvironment(); syncButtons();
    toast("旧加密凭据已备份，空凭据已重建。请填写并保存各服务密钥；当前未保存输入已保留。");
    await refresh();
  }));
  function formatBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  }
  function renderStorage(data) {
    $("storage-summary").textContent = `程序缓存与日志共 ${formatBytes(data.total_bytes)}；默认可清理 ${formatBytes(data.eligible_bytes)}。`;
    const fragment = document.createDocumentFragment();
    (data.categories || []).forEach(category => {
      fragment.append(element("div", "storage-row", `${category.label}：${formatBytes(category.bytes)} · ${category.files || 0} 个文件`));
    });
    $("storage-usage").replaceChildren(fragment);
  }
  $("refresh-storage").addEventListener("click", () => withBusy($("refresh-storage"), async () => {
    renderStorage(await api("/api/storage"));
  }));
  $("clean-storage").addEventListener("click", () => withBusy($("clean-storage"), async () => {
    const include = $("storage-include-caches").checked;
    const accepted = await confirmAction("确认清理程序存储？",
      `默认将过期临时文件（7天以上）和旧任务日志（30天以上）移到回收站。${include ? "\n\n同时清理未被文案引用的可复用缓存；以后再次使用可能需要重新转码、识别或配音，并产生接口费用。" : "\n\n本次不清理配音、识别和转码缓存。"}\n\n源素材、文案记录、已导出成片、设置和密钥均保留。有任务运行时会先停止清理。`, "确认清理");
    if (!accepted) return;
    const result = await api("/api/storage/cleanup", "POST", { confirmed: true, include_caches: include });
    renderStorage(result.inventory || result);
    toast(`已移入回收站 ${result.recycled_files || 0} 个文件。${result.failed?.length ? "部分文件未清理，请检查占用后重试。" : ""}`, Boolean(result.failed?.length));
  }));
  function applyState(data) {
    const firstLoad = !state.settingsLoaded;
    const oldDraftIds = new Set(state.drafts.map((draft) => draft.id));
    state.assets = Array.isArray(data.assets) ? data.assets : [];
    state.jobs = Array.isArray(data.jobs) ? data.jobs : [];
    if (Array.isArray(data.drafts)) state.drafts = data.drafts;
    else if (!data.compact) state.drafts = [];
    if (data.drafts_token) state.draftsToken = data.drafts_token;
    state.settings = data.settings || {};
    if (data.music && typeof data.music === "object") state.music = { ...state.music, ...data.music, tracks: state.music.tracks };
    else if (!state.musicLoaded && !state.musicWrites) {
      const path = state.settings.bgm_path || "";
      state.music = { ...state.music, gain: state.settings.bgm_gain ?? .15, current: path ? { id: null, name: path.split(/[\\/]/).pop(), path, managed: false, available: true } : null };
    }
    state.environment = data.environment || {};
    state.product = data.product || null;
    state.preflight = data.preflight || null;
    state.folder = data.folder || "";
    if (firstLoad) {
      $("target-seconds").value = Math.max(5, Math.min(60, Number(state.settings.target_seconds) || 25));
      $("output-count").value = Math.max(1, Math.min(5, Number(state.settings.output_count) || 1));
      if (!$("library-folder").value) $("library-folder").value = state.folder || "";
      if (!$("product-url").value && state.product?.source) $("product-url").value = state.product.source;
      state.settingsLoaded = true;
    }
    const check = [...state.jobs].filter(j => j.kind === "preflight").sort((a,b) => timestamp(b.created_at)-timestamp(a.created_at))[0];
    const checkActive = check && ["queued", "running", "cancelling"].includes(check.status);
    const savedChecks = [state.preflight, check?.status === "completed" ? check.result : null].filter(result => result?.message).sort((a, b) => timestamp(b.checked_at) - timestamp(a.checked_at));
    const latestCheck = savedChecks[0];
    const preflightResult = !checkActive && (!check || check.status === "completed" || timestamp(latestCheck?.checked_at) > timestamp(check.created_at)) ? latestCheck : null;
    const checkedAt = Number(preflightResult?.checked_at);
    $("preflight-status").textContent = preflightResult?.message ? `上次检查结论：${preflightResult.message}${Number.isFinite(checkedAt) && checkedAt > 0 ? ` · 检查时间：${new Date(timestamp(checkedAt)).toLocaleString("zh-CN", { hour12: false })}` : ""}` : check ? `${statusNames[check.status] || check.status} · ${check.error || check.message || ""}` : "进入工作台先运行免费预检；生成前会再次核查";
    const assetsSignature = JSON.stringify([state.assets, state.folder]);
    if (assetsSignature !== state.assetsSignature) {
      state.assetsSignature = assetsSignature; renderAssets();
    }
    const jobsSignature = JSON.stringify(state.jobs) + (data.drafts_token || JSON.stringify(state.drafts));
    if (jobsSignature !== state.jobsSignature) {
      state.jobsSignature = jobsSignature; renderJobs();
    }
    const newDraft = state.drafts.find((draft) => !oldDraftIds.has(draft.id));
    if (state.pendingDrafts && newDraft) state.pendingDraftId = newDraft.id;
    if (state.pendingDraftId && !state.draftDirty && !state.matchesDirty) {
      state.activeDraftId = state.pendingDraftId; state.pendingDraftId = null; state.pendingDrafts = false;
    }
    if (!state.draftDirty && !state.matchesDirty && !recentDrafts().some(draft => draft.id === state.activeDraftId)) {
      state.activeDraftId = recentDrafts()[0]?.id || null;
    }
    $("draft-history-warning").textContent = data.draft_history?.warning || "";
    $("draft-history-warning").hidden = !data.draft_history?.warning;
    const draftsSignature = data.drafts_token || JSON.stringify(state.drafts);
    if (draftsSignature !== state.draftsSignature && !state.draftDirty && !state.matchesDirty) {
      state.draftsSignature = draftsSignature; renderDraftOptions(); renderDraft();
    }
    fillSettings(); renderEnvironment(); renderProduct(); showDurationHint(); renderMusic(); updateSelection(); syncButtons();
    const ownImport = state.jobs.find(job => job.id === state.musicImportId);
    const ownImportFinished = finishOwnMusicImport(ownImport);
    if (state.musicImportId && !ownImport) reconcileMissingMusicImport();
    const finishedImport = [...state.jobs].filter(job => job.kind === "music_import" && !["queued", "running", "cancelling"].includes(job.status)).sort((a, b) => timestamp(b.created_at) - timestamp(a.created_at))[0];
    const importKey = finishedImport ? `${finishedImport.id}:${finishedImport.status}` : "";
    if (importKey && importKey !== state.musicFinishedKey) {
      state.musicFinishedKey = importKey;
      if (ownImportFinished || state.musicLoaded || $("music-panel").open) refreshMusic().catch(() => {});
    }
    $("download-destination").textContent = state.settings.download_dir ? `保存到 ${state.settings.download_dir}` : "请先在设置中指定保存目录";
    updatePolling();
  }
  function updatePolling() {
    const active = state.jobs.some(job => ["queued", "running", "cancelling"].includes(job.status));
    const delay = active || state.connectionFailures ? 1000 : 15000;
    if (state.pollDelay === delay) return;
    if (state.pollTimer && typeof clearInterval === "function") clearInterval(state.pollTimer);
    state.pollDelay = delay;
    state.pollTimer = setInterval(() => { if (!document.hidden) refresh(); }, delay);
  }
  async function refresh() {
    if (state.fetching || state.pendingMutations) { state.refreshPending = true; return; }
    state.fetching = true; state.refreshPending = false;
    const requestedEpoch = state.mutationEpoch;
    try {
      const data = await api("/api/state", "GET", undefined, {
        "X-State-Mode": "compact", "X-Drafts-Token": state.draftsToken,
        "X-Active-Draft": encodeURIComponent(state.activeDraftId || ""),
      });
      if (requestedEpoch !== state.mutationEpoch || state.pendingMutations) {
        state.refreshPending = true; return;
      }
      state.connected = true;
      state.connectionFailures = 0;
      $("connection-status").className = "connection connected";
      $("connection-text").textContent = "本地服务已连接";
      $("connection-error").hidden = true;
      applyState(data);
    } catch (error) {
      if (requestedEpoch !== state.mutationEpoch || state.pendingMutations) {
        state.refreshPending = true; return;
      }
      state.connectionFailures++;
      if (state.connected && state.connectionFailures < 2) { updatePolling(); return; }
      state.connected = false;
      $("connection-status").className = "connection disconnected";
      $("connection-text").textContent = "本地服务未连接";
      $("connection-error").textContent = `${error.message} 点击右上角“同步状态”重试；未保存内容仍保留在当前页面。`;
      $("connection-error").hidden = false;
      syncButtons();
      updatePolling();
    } finally {
      state.fetching = false;
      if (state.refreshPending && !state.pendingMutations) refresh();
    }
  }
  $("refresh-state").addEventListener("click", () => withBusy($("refresh-state"), refresh));
  window.addEventListener("pywebviewready", () => {
    document.documentElement.dataset.nativeReady = "true";
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.draftDirty || state.matchesDirty || state.settingsDirty || state.musicGainDirty) { event.preventDefault(); event.returnValue = ""; }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { $("asset-preview").pause(); $("mix-preview").pause(); }
    else refresh();
  });
  setPage(names[location.hash.slice(1)] ? location.hash.slice(1) : "library");
  renderAssets(); renderJobs(); fillSettings(); syncButtons(); refresh();
  updatePolling();
})();
