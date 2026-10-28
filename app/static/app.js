const $ = (selector) => document.querySelector(selector);
let state = { accounts: [], sources: [], clips: [], jobs: [] };

function toast(message, isError = false) {
  const element = $("#toast");
  element.textContent = message;
  element.style.borderColor = isError ? "#744049" : "#34404c";
  element.classList.add("visible");
  window.setTimeout(() => element.classList.remove("visible"), 4200);
}

async function api(url, options = {}) {
  if (options.method === 'POST' && ['/api/render','/api/discover','/api/pipeline/start','/api/automation/start','/api/automation/five-videos/start','/api/youtube/direct-upload','/api/tiktok-web/direct-upload'].includes(url)) {
    await window.ensureEditingSaved?.();
  }
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(payload.detail) ? payload.detail.map((item) => item.msg).join("; ") : payload.detail;
    throw new Error(detail || `Ошибка HTTP ${response.status}`);
  }
  return payload;
}

function setOptions(element, records, emptyLabel, label) {
  const selected = element.value;
  element.innerHTML = "";
  if (!records.length) {
    element.append(new Option(emptyLabel, ""));
    return;
  }
  for (const record of records) element.append(new Option(label(record), record.id));
  if (records.some((record) => record.id === selected)) element.value = selected;
}

function shortId(id) { return id ? id.slice(0, 8) : "—"; }

function renderState() {
  window.renderEditing?.(state);
  renderAutomation();
  window.renderFiveVideos?.(state);
  window.renderTikTokWeb?.(state);
  const readySources = state.sources.filter((item) => item.status === "ready");
  const readyClips = state.clips.filter((item) => item.status === "ready");
  const usableAccounts = state.accounts.filter((item) => item.status === "ready");
  // Failed sessions remain selectable for an explicit check, not automatic upload.
  const youtubeAccounts = state.accounts.filter((item) => item.platform === "youtube" && ["ready", "reauth_required"].includes(item.status));
  const pendingJobs = state.jobs.filter((item) => ["queued", "processing"].includes(item.status));
  $("#sourceCount").textContent = state.sources.length;
  $("#clipCount").textContent = readyClips.length;
  $("#accountCount").textContent = usableAccounts.length;
  $("#queueCount").textContent = pendingJobs.length;
  $("#systemStatus").textContent = pendingJobs.length ? `В работе: ${pendingJobs.length}` : "Система готова";

  setOptions($("#renderSource"), readySources, "Нет готовых источников", (item) => `${shortId(item.id)} · ${Math.round(item.duration_seconds || 0)} сек.`);
  const ytAccountSelect = $("#ytAccountSelect");
  if (ytAccountSelect) {
    setOptions(ytAccountSelect, youtubeAccounts, "Сначала импортируйте JSON аккаунтов", (item) => item.display_name);
  }
  if ($("#pipelineAccount")) setOptions($("#pipelineAccount"), youtubeAccounts, "Импортируйте аккаунт", (item) => item.display_name);
  if ($("#ytClipSelect")) {
    const selected = $("#ytClipSelect").value;
    setOptions($("#ytClipSelect"), readyClips, "Нет готовых клипов", (item) => `${shortId(item.id)} · ${item.duration_seconds} сек.`);
    $("#ytClipSelect").prepend(new Option("Или выберите файл ниже", ""));
    $("#ytClipSelect").value = readyClips.some((item) => item.id === selected) ? selected : "";
  }

  const rows = $("#jobRows");
  rows.innerHTML = "";
  if (!state.jobs.length) {
    rows.innerHTML = '<tr><td colspan="6" class="empty">Очередь пуста</td></tr>';
    return;
  }
  for (const job of state.jobs) {
    const tr = document.createElement("tr");
    tr.dataset.jobId = job.id;
    const labels = {discovery:"Поиск", download:"Скачивание", render:"Монтаж", youtube_publish:"YouTube", publish:"TikTok API", tiktok_web_publish:"TikTok · сайт"};
    let detail = job.result ? escapeHtml(JSON.stringify(job.result)) : "—";
    if (job.error) detail = `<span class="error-text">${escapeHtml(job.error)}</span>`;
    if (job.result?.video_url && /^https:\/\/www\.youtube\.com\/watch\?v=[A-Za-z0-9_-]{11}$/.test(job.result.video_url)) {
      detail += ` <a href="${escapeHtml(job.result.video_url)}" target="_blank" rel="noopener noreferrer">Открыть видео</a>`;
      const videoId = new URL(job.result.video_url).searchParams.get("v");
      detail += ` <a href="https://www.youtube.com/shorts/${videoId}" target="_blank" rel="noopener noreferrer">Открыть в Shorts</a>`;
    }
    const tiktokUrl = job.result?.video_url && /^https:\/\/www\.tiktok\.com\/@[A-Za-z0-9_.]{1,64}\/video\/[0-9]{10,25}$/.test(job.result.video_url) ? job.result.video_url : null;
    const created = !!tiktokUrl && job.result?.confirmed === true;
    let account = escapeHtml(job.account_name || "—");
    let status = job.status, statusClass = job.status;
    if (tiktokUrl) {
      const profile = tiktokUrl.split('/video/')[0];
      const link = `<a href="${escapeHtml(tiktokUrl)}" target="_blank" rel="noopener noreferrer">Открыть TikTok-видео</a>`;
      if (created) {
        const reviewed = job.result.moderation_status === 'under_review';
        const visibility = job.result.visibility === 'public' ? 'Для всех' : job.result.visibility === 'private' ? 'Только я' : 'Видимость не подтверждена';
        detail = `<div>${link}</div><div>${escapeHtml(visibility)}${reviewed ? ' · на проверке' : ''}</div>`;
        status = reviewed ? 'Создано · на проверке' : 'Создано'; statusClass = 'ready';
        account += `<br><a href="${escapeHtml(profile)}" target="_blank" rel="noopener noreferrer">${escapeHtml(new URL(profile).pathname.slice(1))}</a>`;
      } else detail += ` ${link}`;
    }
    if (job.result?.error_kind === 'session_expired') status = 'Сессия истекла';
    tr.innerHTML = `<td>${escapeHtml(labels[job.type] || job.type)} · ${shortId(job.id)}</td><td>${account}</td><td><span class="badge ${statusClass}">${escapeHtml(status)}</span></td><td>${job.attempts}</td><td>${detail}</td><td>${!created && ["error", "reauth_required"].includes(job.status) ? `<button class="text-button retry" data-id="${job.id}">Повторить</button>` : ""}</td>`;
    rows.append(tr);
  }
  document.querySelectorAll(".retry").forEach((button) => button.addEventListener("click", () => retryJob(button.dataset.id)));
}

let automationHydrated = false;
let automationSearchDirty = false;
let automationSearchSignature = '';
let automationSubmitting = false;
let automationContentDraft = {caption: "", hashtags: ""};
function renderAutomation() {
  const form = $("#automationForm");
  if (!form) return;
  const plan = state.automation || {enabled: false, status: "stopped", config: {}};
  const config = plan.config || {};
  const searchDraft = config.five_video_draft || {};
  const search = {queries: searchDraft.queries ?? config.queries ?? [], topic: searchDraft.topic ?? config.topic ?? 'any'};
  const searchSignature = JSON.stringify(search);
  if (!automationHydrated || (!automationSearchDirty && searchSignature !== automationSearchSignature)) {
    $("#automationQueries").value = search.queries.join("\n");
    $("#automationTopic").value = search.topic;
    automationSearchSignature = searchSignature;
  }
  const platformField = $("#automationPlatform");
  if (!automationHydrated && config.platform) platformField.value = config.platform;
  const platform = platformField.value;
  const platformName = platform === "tiktok_web" ? "TikTok" : "YouTube";
  $("#automationPlatformLabel").textContent = `${platformName} · публикации по расписанию`;
  $("#automationTitle").textContent = `${platformName} · режим 24/7`;
  const accounts = $("#automationAccounts");
  const selected = new Set(automationHydrated ? [...accounts.selectedOptions].map(option => option.value) : (config.account_ids || []));
  accounts.replaceChildren();
  const accountOrder = id => config.account_ids?.includes(id) ? config.account_ids.indexOf(id) : 999;
  for (const account of state.accounts.filter(item => item.platform === platform).sort((a,b) => accountOrder(a.id)-accountOrder(b.id))) {
    const option = new Option(`${account.display_name}${account.status === "ready" ? "" : " · требуется вход"}`, account.id);
    option.disabled = platform !== "tiktok_web" && account.status !== "ready";
    option.selected = selected.has(account.id);
    accounts.append(option);
  }
  if (!automationHydrated) {
    for (const [id, key] of [["automationInterval", "interval_minutes"], ["automationDailyLimit", "daily_limit"], ["automationClipCount", "clip_count"], ["automationDuration", "clip_duration"], ["automationVisibility", "visibility"]]) {
      if (config[key] !== undefined) document.getElementById(id).value = config[key];
    }
    $("#automationMode").value = config.publish ? "publish" : "preview";
    automationContentDraft = {caption: config.caption || "", hashtags: config.hashtags || ""};
    automationHydrated = true;
  }
  for (const field of form.querySelectorAll("input,select,textarea,button")) field.disabled = plan.enabled || automationSubmitting;
  $("#automationClipCount").value = 1;
  $("#automationClipCount").disabled = plan.enabled || automationSubmitting || platform === "tiktok_web";
  $("#automationVisibility").querySelector('option[value="unlisted"]').disabled = platform === "tiktok_web";
  if (platform === "tiktok_web" && $("#automationVisibility").value === "unlisted") $("#automationVisibility").value = "private";
  const labels = {stopped: "Остановлен", paused: "Приостановлен", review_required: "Нужен просмотр фрагмента", scheduled: "Запланирован", running: "Включён", quota_wait: "Ожидание лимита", retry_wait: "Ожидание повторного цикла"};
  $("#automationStatus").textContent = labels[plan.status] || plan.status;
  $("#automationContentSummary").textContent = `Описание: ${automationContentDraft.caption || "не задано"} · Хештеги: ${automationContentDraft.hashtags || "не заданы"}`;
  const nextTime = plan.next_run_at ? new Date(plan.next_run_at + (/([zZ]|[+-]\d{2}:\d{2})$/.test(plan.next_run_at) ? "" : "Z")).toLocaleString() : "сейчас";
  $("#automationDetail").textContent = plan.last_error || (plan.enabled ? `Следующая проверка расписания: ${nextTime}. Загрузчики: ${(state.download_providers || []).join(" → ")}.` : "Режим не запускается сам после установки. Выберите настройки и нажмите «Запустить 24/7».");
  if (plan.enabled && platform === "tiktok_web" && config.account_ids?.length) {
    const names = config.account_ids.map(id => state.accounts.find(a => a.id === id)?.display_name || shortId(id));
    $("#automationDetail").textContent += ` Очередь: ${names.join(" → ")}. Следующий: ${names[(config.account_index || 0) % names.length]}. Интервал ${config.interval_minutes} мин.`;
  }
  $("#automationStop").disabled = !plan.enabled;
  $("#automationAccountsHint").textContent = plan.enabled ? "Чтобы изменить выбранные аккаунты, остановите 24/7. Cookie можно обновлять и проверять здесь." : "Выберите свои аккаунты. Непроверенный вход будет проверен автоматически перед запуском.";
  window.renderAutomationAccounts?.(state);
}

const automationForm = $("#automationForm");
if (automationForm) {
  for (const id of ['automationQueries', 'automationTopic']) {
    document.getElementById(id).addEventListener('input', () => { automationSearchDirty = true; });
  }
  window.automationSearchSaved = () => { automationSearchDirty = false; renderAutomation(); };
  $("#automationPlatform").addEventListener("change", renderAutomation);
  $("#automationAccounts").addEventListener("change", () => {
    if ($("#automationPlatform").value === "tiktok_web") {
      const ids = [...$("#automationAccounts").selectedOptions].map(option => option.value);
      window.automationCheckAccounts?.(ids);
    }
  });
  automationForm.addEventListener("submit", event => {
    event.preventDefault();
    withButton(event.currentTarget, async () => {
      const publish = $("#automationMode").value === "publish";
      const consent = $("#automationConsent").checked;
      const accountIds = [...$("#automationAccounts").selectedOptions].map(option => option.value);
      if (publish && (!consent || !accountIds.length)) throw new Error("Выберите свои действующие каналы и подтвердите публикации");
      automationSubmitting = true;
      renderAutomation();
      try {
      if (publish && $("#automationPlatform").value === "tiktok_web" && window.automationCheckAccounts) {
        const checked = await window.automationCheckAccounts(accountIds);
        if (!checked) throw new Error("Вход не подтверждён. Причина указана рядом с аккаунтом в блоке 24/7.");
      }
      await api("/api/automation/start", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
        queries: $("#automationQueries").value.split(/\r?\n/).map(query => query.trim()).filter(Boolean),
        platform: $("#automationPlatform").value, topic: $("#automationTopic").value,
        account_ids: accountIds, interval_minutes: Number($("#automationInterval").value),
        daily_limit: Number($("#automationDailyLimit").value), clip_count: Number($("#automationClipCount").value),
        clip_duration: Number($("#automationDuration").value), visibility: $("#automationVisibility").value,
        caption: automationContentDraft.caption, hashtags: automationContentDraft.hashtags,
        publish, consent, owns_channels: consent,
      })});
      toast("Режим 24/7 включён на сервере");
      await refresh();
      } finally { automationSubmitting = false; renderAutomation(); }
    });
  });
  $("#automationStop").addEventListener("click", async () => {
    try {
      await api("/api/automation/stop", {method: "POST"});
      toast("Новые циклы остановлены. Уже начатое сохранение может завершиться.");
      await refresh();
    } catch (error) { toast(error.message, true); }
  });
  const contentDialog = $("#automationContentDialog");
  $("#automationContentOpen").addEventListener("click", () => {
    $("#automationCaption").value = automationContentDraft.caption;
    $("#automationHashtags").value = automationContentDraft.hashtags;
    $("#automationContentTitle").textContent = `Описание и хештеги · ${$("#automationPlatform").value === "tiktok_web" ? "TikTok" : "YouTube"}`;
    $("#automationContentError").textContent = "";
    contentDialog.showModal();
    $("#automationCaption").focus();
  });
  $("#automationContentCancel").addEventListener("click", () => contentDialog.close());
  $("#automationContentForm").addEventListener("submit", async event => {
    event.preventDefault();
    const save = $("#automationContentSave");
    save.disabled = true;
    $("#automationContentError").textContent = "";
    try {
      const content = {caption: $("#automationCaption").value.trim(), hashtags: $("#automationHashtags").value.trim()};
      const plan = await api("/api/automation/content", {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(content)});
      automationContentDraft = {caption: plan.config.caption || "", hashtags: plan.config.hashtags || ""};
      state.automation = plan;
      renderAutomation();
      contentDialog.close();
      toast("Описание и хештеги сохранены для следующих роликов 24/7");
    } catch (error) {
      $("#automationContentError").textContent = error.message;
    } finally { save.disabled = false; }
  });
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
}

async function refresh() {
  try {
    state = await api("/api/state");
    renderState();
  } catch (error) {
    $("#systemStatus").textContent = "Нет связи";
    toast(error.message, true);
  }
}

async function withButton(form, task) {
  const button = form.querySelector('button[type="submit"]');
  if (!button) return task();
  const originalText = button.textContent;
  button.disabled = true;
  try { await task(); } catch (error) { toast(error.message, true); }
  finally {
    button.disabled = false;
    button.textContent = originalText;
    if (form.id === "automationForm") renderAutomation();
  }
}

$("#sourceForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  withButton(form, async () => {
    const urls = $("#urls").value.split(/\r?\n/).map((url) => url.trim()).filter(Boolean);
    await api("/api/sources", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({urls, rights_confirmed: $("#rights").checked}) });
    form.reset();
    toast(`${urls.length} источников добавлено`);
    await refresh();
  });
});

const discoveryForm = $("#discoveryForm");
if (discoveryForm) {
  discoveryForm.addEventListener("submit", (event) => {
    event.preventDefault();
    withButton(event.currentTarget, async () => {
      const publish = $("#pipelinePublish").checked;
      const accountId = $("#pipelineAccount").value;
      if (publish && !accountId) throw new Error("Сначала импортируйте и выберите YouTube-аккаунт");
      const button = $("#startPipelineButton");
      if (button) button.textContent = "Запускаю…";
      const result = await api("/api/pipeline/start", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          query: $("#discoveryQuery").value.trim(),
          max_results: Number($("#discoveryAmount").value),
          clip_count: Number($("#discoveryClipCount").value),
          clip_duration: Number($("#discoveryClipDuration").value),
          caption: $("#discoveryCaption").value.trim(),
          hashtags: $("#discoveryHashtags").value.trim(),
          duration_filter: $("#discoveryDurationFilter").value,
          youtube_account_id: publish ? accountId : null,
          visibility: $("#pipelineVisibility").value,
          consent: publish,
        }),
      });
      toast(`${result.message}. Задача: ${shortId(result.job_id)}`);
      await refresh();
    });
  });
}

$("#uploadForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  withButton(form, async () => {
    const data = new FormData();
    data.append("file", $("#localFile").files[0]);
    data.append("rights_confirmed", $("#uploadRights").checked ? "true" : "false");
    await api("/api/sources/upload", {method: "POST", body: data});
    form.reset();
    toast("Исходник загружен");
    await refresh();
  });
});

$("#accountImportForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  withButton(form, async () => {
    const data = new FormData();
    data.append("file", $("#accountJson").files[0]);
    const result = await api("/api/accounts/import", {method: "POST", body: data});
    form.reset();
    toast(`Подключено аккаунтов: ${result.imported}`);
    await refresh();
    await window.automationCheckAccounts?.(result.tiktok_account_ids || [], {force: true});
  });
});

$("#renderForm").addEventListener("submit", (event) => {
  event.preventDefault();
  withButton(event.currentTarget, async () => {
    await api("/api/render", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ source_id: $("#renderSource").value, count: Number($("#clipAmount").value), duration: Number($("#clipDuration").value), caption: $("#caption").value, hashtags: $("#hashtags").value }) });
    toast("Монтаж добавлен в очередь");
    await refresh();
  });
});

async function retryJob(id) {
  try { await api(`/api/jobs/${id}/retry`, {method: "POST"}); toast("Задача возвращена в очередь"); await refresh(); }
  catch (error) { toast(error.message, true); }
}

$("#refreshButton").addEventListener("click", refresh);
refresh();
window.setInterval(refresh, 5000);

// Пользовательская кнопка быстрой загрузки видео в ClipForge (ОРИГИНАЛЬНАЯ - НЕ МЕНЯЕМ)
document.addEventListener("DOMContentLoaded", () => {
  const uploadBtn = document.getElementById("uploadVideoBtn");
  const fileInput = document.getElementById("videoFileInput");
  const uploadStatus = document.getElementById("uploadStatus");
  if (!uploadBtn || !fileInput || !uploadStatus) return;

  uploadBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    const formData = new FormData();
    formData.append("file", file);
    uploadBtn.disabled = true;
    uploadStatus.textContent = "Загрузка…";
    try {
      const result = await api("/api/upload-video", {method: "POST", body: formData});
      uploadStatus.textContent = "Готово";
      toast(`${result.message}: ${result.filename}`);
      await refresh();
    } catch (error) {
      uploadStatus.textContent = "Ошибка";
      toast(error.message, true);
    } finally {
      fileInput.value = "";
      uploadBtn.disabled = false;
    }
  });
});

// НОВЫЙ БЛОК: Изолированный обработчик для новой панели YouTube (НИКАК НЕ МЕШАЕТ ПРЕДЫДУЩЕЙ КНОПКЕ)
document.addEventListener("DOMContentLoaded", () => {
  const ytDirectForm = document.getElementById("ytDirectForm");
  const ytFileInput = document.getElementById("ytFileInput");
  const ytAccountSelect = document.getElementById("ytAccountSelect");
  const ytDirectStatus = document.getElementById("ytDirectStatus");

  if (ytDirectForm && ytFileInput && ytAccountSelect) {
    ytDirectForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      
      const file = ytFileInput.files?.[0];
      const clipId = $("#ytClipSelect").value;
      const accountId = ytAccountSelect.value;
      
      if (!file && !clipId) {
        toast("Выберите готовый клип или видеофайл", true);
        return;
      }
      if (!accountId) {
        toast("Сначала импортируйте и выберите YouTube-аккаунт", true);
        return;
      }

      const submitBtn = ytDirectForm.querySelector('button[type="submit"]');
      const originalText = submitBtn.textContent;
      submitBtn.disabled = true;
      ytDirectStatus.textContent = "Отправка на сервер (передача файлов)...";

      const formData = new FormData();
      if (file) formData.append("file", file);
      formData.append("account_id", accountId);
      formData.append("title", $("#ytTitle").value.trim());
      formData.append("visibility", $("#ytVisibility").value);
      formData.append("made_for_kids", $("#ytMadeForKids").value);
      formData.append("consent", $("#ytConsent").checked ? "true" : "false");

      try {
        const result = file ? await api("/api/youtube/direct-upload", {method: "POST", body: formData}) : await api("/api/youtube/publish", {
          method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
            account_id: accountId, clip_id: clipId, title: $("#ytTitle").value.trim(),
            visibility: $("#ytVisibility").value, made_for_kids: $("#ytMadeForKids").value === "true", consent: $("#ytConsent").checked,
          }),
        });
        
        ytDirectStatus.textContent = `В очереди: ${shortId(result.job_id)}. Результат появится в журнале.`;
        toast(result.message);
        
        // Очищаем форму после успеха
        ytFileInput.value = "";
        $("#ytConsent").checked = false;
        await refresh();
      } catch (error) {
        ytDirectStatus.textContent = "Произошла ошибка.";
        toast(error.message, true);
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = originalText;
      }
    });
  }
});

$("#ytCheckSession")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const id = $("#ytAccountSelect").value;
  if (!id) return toast("Выберите YouTube-аккаунт", true);
  button.disabled = true;
  const status = $("#ytDirectStatus");
  status.textContent = "Проверяю доступ к YouTube Studio…";
  try {
    await api(`/api/accounts/${id}/check-session`, {method:"POST"});
    status.textContent = "Вход в YouTube Studio подтверждён";
    toast("Вход в YouTube Studio подтверждён");
  } catch (error) {
    status.textContent = error.message;
    toast(error.message, true);
  }
  finally { button.disabled = false; await refresh(); }
});

