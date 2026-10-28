(() => {
  const panel = document.querySelector('#fiveVideoPanel');
  if (!panel) return;
  const find = id => document.getElementById(id);
  let initialized = false, dirty = false, busy = false, savedSignature = '';
  let busyPhase = '';
  let selected = new Set();
  let latest = null;
  const fields = [];
  for (let i = 0; i < 5; i++) {
    const card = document.createElement('article');
    card.className = 'five-video-card';
    card.innerHTML = `<h3>Видео ${i + 1} <span class="helper">· круг ${i + 1}</span></h3>
      <label for="fiveDescription${i}">Описание и хештеги<textarea id="fiveDescription${i}" rows="4" maxlength="1600" placeholder="Ваше описание этого ролика на филиппинском и хештеги"></textarea></label>
      <label for="fiveClip${i}">Ролик<select id="fiveClip${i}"><option value="">Создать автоматически</option></select></label>
      <p class="helper" id="fiveProgress${i}">Ещё не отправлен</p>`;
    find('fiveVideoCards').append(card);
    fields.push({description: find(`fiveDescription${i}`), clip: find(`fiveClip${i}`)});
  }
  const searchSettings = () => ({queries: find('automationQueries').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean), topic: find('automationTopic').value});
  const read = () => ({account_ids: [...selected], videos: fields.map(f => ({description: f.description.value.trim(), clip_id: f.clip.value || null})), ...searchSettings()});
  const currentReview = () => {
    const batch = latest?.automation?.config?.five_video_batch;
    const round = batch?.account_ids?.length ? Math.floor(batch.position / batch.account_ids.length) : 5;
    const cid = round < 5 ? batch?.clip_ids?.[round] : null;
    return {batch, round, cid, review: batch?.clip_reviews?.[cid]};
  };
  function startBlocker() {
    if (busy) return busyPhase || 'Сохраняем изменения…';
    if (!initialized) return 'Загружаем настройки партии…';
    if (latest?.automation?.enabled) return 'Очередь уже включена. Для изменения партии сначала остановите 24/7.';
    const pipeline = latest?.automation?.last_pipeline_id;
    const active = pipeline && latest.jobs?.find(j => (j.id === pipeline || j.pipeline_id === pipeline) && ['queued', 'processing'].includes(j.status));
    if (active) return active.status === 'queued' ? 'Осталось ожидающее задание предыдущей партии. Нажмите «Остановить 24/7», чтобы отменить его.' : 'Предыдущая операция ещё выполняется. Дождитесь её завершения.';
    if (!selected.size) return 'Выберите аккаунты для партии.';
    if (selected.size > 30) return 'Выберите не больше 30 аккаунтов.';
    const values = read().videos.map(v => v.description);
    const empty = values.map((v, i) => v ? null : i + 1).filter(Boolean);
    if (empty.length) return `Заполните описания роликов: ${empty.join(', ')}.`;
    if (new Set(values).size !== 5) return 'Для пяти роликов нужны пять разных описаний.';
    const queries = searchSettings().queries;
    if (!queries.length) return 'Добавьте поисковый запрос в поле «Поисковые запросы» выше.';
    if (queries.length > 10 || queries.some(q => q.length < 2 || q.length > 180)) return 'Укажите до 10 запросов, каждый длиной от 2 до 180 символов.';
    const {batch, cid, review} = currentReview();
    if (cid && batch.status !== 'rejected' && review?.decision !== 'approved') return 'Просмотрите и подтвердите подготовленный фрагмент перед продолжением партии.';
    if (!find('fiveConsent').checked) return 'Поставьте галочку «Разрешаю отправить эти пять роликов».';
    return '';
  }
  function buttons() {
    const {batch, cid, review} = currentReview();
    find('fiveSave').disabled = busy;
    fields.forEach(f => { f.description.disabled = busy; f.clip.disabled = busy || !!batch && !['complete', 'rejected'].includes(batch.status); });
    for (const input of find('fiveAccounts').querySelectorAll('input')) input.disabled = busy || !!latest?.automation?.enabled;
    find('fiveConsent').disabled = busy || !!latest?.automation?.enabled;
    for (const id of ['automationQueries', 'automationTopic']) find(id).disabled = busy || !!latest?.automation?.enabled;
    const blocker = startBlocker();
    const start = find('fiveStart');
    start.disabled = !!blocker;
    start.setAttribute('aria-busy', String(busy));
    find('fiveSave').setAttribute('aria-busy', String(busy));
    start.title = blocker || (dirty ? 'Сохранить изменения и запустить партию' : 'Запустить партию');
    const label = batch?.status === 'rejected' ? 'Начать новую партию' : cid ? 'Продолжить партию' : 'Запустить партию из 5 видео';
    start.textContent = busy ? blocker : dirty ? `Сохранить и ${label[0].toLowerCase()}${label.slice(1)}` : label;
    const unchecked = latest?.accounts?.filter(a => selected.has(a.id) && a.status !== 'ready') || [];
    find('fiveStartHint').textContent = blocker || (dirty ? 'Изменения сохранятся при нажатии кнопки. ' : 'Настройки сохранены. ') + (unchecked.length ? `Перед запуском проверим вход для ${unchecked.length} аккаунтов. Если сессия истекла, потребуется обновить cookie.` : 'Можно запускать партию.');
    find('fiveReviewApprove').disabled = busy || !!latest?.automation?.enabled || !cid || !!review || !find('fiveReviewWatched').checked;
    find('fiveReviewReject').disabled = busy || !!latest?.automation?.enabled || !cid || review?.decision === 'rejected';
    find('fiveSaveState').textContent = dirty ? 'Есть несохранённые изменения' : initialized ? 'Описания и поиск сохранены на сервере' : '';
    const skipCount = latest?.automation?.config?.five_video_batch?.skipped?.length || 0;
    find('fiveTarget').textContent = `${selected.size} аккаунтов × 5 роликов = ${selected.size * 5} ${skipCount ? `отправок · пропущено: ${skipCount}` : 'публикаций'}`;
  }
  for (const f of fields) for (const element of [f.description, f.clip]) element.addEventListener('input', () => {dirty = true; buttons();});
  for (const id of ['automationQueries', 'automationTopic']) find(id).addEventListener('input', () => {dirty = true; buttons();});
  find('fiveConsent').addEventListener('change', buttons);
  find('fiveReviewWatched').addEventListener('change', buttons);
  window.renderFiveVideos = data => {
    latest = data;
    const config = data.automation?.config || {};
    const draft = config.five_video_draft || {videos: Array.from({length: 5}, () => ({description: '', clip_id: null})), account_ids: []};
    const batch = config.five_video_batch;
    const signature = JSON.stringify(draft);
    if (!initialized || (!dirty && signature !== savedSignature)) {
      selected = new Set(draft.account_ids);
      fields.forEach((f, i) => { f.description.value = draft.videos[i]?.description || ''; f.clip.dataset.chosen = draft.videos[i]?.clip_id || ''; });
      savedSignature = signature; initialized = true;
    }
    const accounts = data.accounts.filter(a => a.platform === 'tiktok_web');
    const order = draft.account_ids || [];
    accounts.sort((a, b) => (order.includes(a.id) ? order.indexOf(a.id) : 999) - (order.includes(b.id) ? order.indexOf(b.id) : 999) || a.display_name.localeCompare(b.display_name));
    const list = find('fiveAccounts'); list.replaceChildren();
    accounts.forEach(a => {
      const label = document.createElement('label'); label.className = 'check five-account';
      const check = document.createElement('input'); check.type = 'checkbox'; check.checked = selected.has(a.id);
      check.disabled = !!data.automation?.enabled;
      const name = document.createElement('span'); name.textContent = `${a.display_name}${a.status === 'ready' ? '' : ' · требуется вход'}`;
      check.addEventListener('change', () => {
        if (check.checked && selected.size >= 30) {check.checked = false; toast('Можно выбрать не больше 30 аккаунтов', true); return;}
        check.checked ? selected.add(a.id) : selected.delete(a.id); dirty = true; buttons();
      });
      label.append(check, name); list.append(label);
    });
    fields.forEach((f, i) => {
      const chosen = f.clip.dataset.chosen !== undefined ? f.clip.dataset.chosen : f.clip.value;
      delete f.clip.dataset.chosen;
      const options = [new Option('Создать автоматически', '')];
      data.clips.filter(c => c.status === 'ready').forEach(c => options.push(new Option(`${shortId(c.id)} · ${c.duration_seconds} сек. · ${c.start_seconds} сек. источника`, c.id)));
      if (chosen && !options.some(o => o.value === chosen)) options.push(new Option(`Сохранённый клип ${shortId(chosen)}`, chosen));
      f.clip.replaceChildren(...options); f.clip.value = chosen;
      f.clip.disabled = !!batch && !['complete', 'rejected'].includes(batch.status);
      const skipped = batch?.skipped?.filter(s => s.round === i + 1).length || 0;
      const n = batch ? Math.max(0, Math.min(batch.account_ids.length, batch.position - i * batch.account_ids.length) - skipped) : 0;
      find(`fiveProgress${i}`).textContent = batch ? `${n} / ${batch.account_ids.length} аккаунтов${skipped ? ` · пропущено: ${skipped}` : ''} · ${batch.clip_ids[i] ? `клип ${shortId(batch.clip_ids[i])}` : 'ролик будет создан перед кругом'}` : 'Один ролик для всех выбранных аккаунтов';
    });
    find('fiveBatchStatus').textContent = batch ? `Создано: ${batch.completed?.length || 0}. Пропущено: ${batch.skipped?.length || 0}. Обработано ${batch.position} / ${batch.account_ids.length * 5}. ${data.automation.enabled ? 'Партия работает' : batch.status === 'complete' ? 'Партия завершена' : 'Партия остановлена'}.` : 'Черновик · публикации не запущены';
    const current = currentReview();
    find('fiveReview').hidden = !current.cid;
    if (current.cid) {
      const video = find('fiveReviewVideo');
      if (video.dataset.clip !== current.cid) {
        video.dataset.clip = current.cid;
        video.src = `/api/clips/${encodeURIComponent(current.cid)}/preview`;
        find('fiveReviewWatched').checked = false;
      }
      const clip = data.clips.find(c => c.id === current.cid);
      const source = data.sources.find(s => s.id === clip?.source_id);
      find('fiveReviewSource').textContent = source ? `Источник: ${source.title} · ${clip?.duration_seconds || ''} сек. фрагмента` : `Видео ${current.round + 1} · ${shortId(current.cid)}`;
      find('fiveReviewStatus').textContent = current.review?.decision === 'rejected' ? 'Отклонён. Этот исходник исключён из нового поиска. История прежних отправок сохранена.' : current.review?.decision === 'approved' ? 'Фрагмент подтверждён. Для отправки нажмите «Продолжить партию».' : 'Публикации этого фрагмента ждут просмотра и подтверждения.';
    }
    find('fiveAccountRows').replaceChildren(...accounts.filter(a => selected.has(a.id)).map((a, i) => {
      const tr = document.createElement('tr');
      const skipped = batch?.skipped?.filter(s => s.account_id === a.id).length || 0;
      const n = batch && batch.account_ids.includes(a.id) ? Math.max(0, Math.min(5, Math.floor(batch.position / batch.account_ids.length) + (batch.position % batch.account_ids.length > batch.account_ids.indexOf(a.id) ? 1 : 0)) - skipped) : 0;
      const profileJob = data.jobs.find(j => j.account_id === a.id && /^https:\/\/www\.tiktok\.com\/@[A-Za-z0-9_.]{1,64}\/video\/\d+$/.test(j.result?.video_url || ''));
      const name = document.createElement('td'); name.textContent = a.display_name;
      if (profileJob) { const link = document.createElement('a'); link.href = profileJob.result.video_url.split('/video/')[0]; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = ` · ${new URL(link.href).pathname.slice(1)}`; name.append(link); }
      const number = document.createElement('td'); number.textContent = i + 1;
      const count = document.createElement('td'); count.textContent = `${n} / 5${skipped ? ` · пропущено: ${skipped}` : ''}`;
      const status = document.createElement('td'); status.textContent = a.status === 'ready' ? 'Вход подтверждён' : 'Требуется вход';
      tr.append(number, name, count, status); return tr;
    }));
    buttons();
  };
  async function reviewClip(decision) {
    const {batch, cid} = currentReview();
    if (!cid) return;
    busy = true; buttons(); find('fiveError').textContent = '';
    try {
      state.automation = await api('/api/automation/five-videos/review', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({batch_id: batch.id, clip_id: cid, decision, watched: find('fiveReviewWatched').checked})});
      window.renderFiveVideos(state);
      toast(decision === 'approved' ? 'Фрагмент подтверждён. Отправки не запущены.' : 'Исходник исключён. Партия остановлена.');
    } catch (e) {find('fiveError').textContent = e.message;} finally {busy = false; buttons();}
  }
  find('fiveReviewApprove').addEventListener('click', () => reviewClip('approved'));
  find('fiveReviewReject').addEventListener('click', () => reviewClip('rejected'));
  find('fiveVideoForm').addEventListener('submit', async event => {
    event.preventDefault(); busy = true; buttons(); find('fiveError').textContent = '';
    try {
      const plan = await api('/api/automation/five-videos', {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(read())});
      dirty = false; state.automation = plan; window.automationSearchSaved?.(); window.renderFiveVideos(state); toast('Пять описаний и поисковые запросы сохранены. Публикации не запущены.');
    } catch (e) {find('fiveError').textContent = e.message;} finally {busy = false; buttons();}
  });
  find('fiveStart').addEventListener('click', async () => {
    if (startBlocker()) return;
    busy = true; busyPhase = dirty ? 'Сохраняем настройки…' : 'Проверяем вход в аккаунты…'; buttons(); find('fiveError').textContent = '';
    try {
      if (dirty) {
        const plan = await api('/api/automation/five-videos', {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(read())});
        dirty = false; state.automation = plan; window.automationSearchSaved?.(); window.renderFiveVideos(state);
      }
      busyPhase = 'Проверяем вход в аккаунты…'; buttons();
      const ids = [...selected];
      if (window.automationCheckAccounts && !await window.automationCheckAccounts(ids)) throw new Error('Вход подтверждён не для всех выбранных аккаунтов.');
      busyPhase = 'Запускаем партию…'; buttons();
      state.automation = await api('/api/automation/five-videos/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
        consent: true, interval_minutes: Number(find('automationInterval').value), daily_limit: Number(find('automationDailyLimit').value),
        clip_duration: Number(find('automationDuration').value), ...searchSettings(), visibility: find('automationVisibility').value,
      })});
      toast('Партия включена. После пяти роликов на каждый аккаунт очередь остановится.'); await refresh();
    } catch (e) {find('fiveError').textContent = e.message;} finally {busy = false; busyPhase = ''; buttons();}
  });
})();
