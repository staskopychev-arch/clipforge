/* Separate website-upload controls; existing TikTok OAuth and YouTube remain. */
(() => {
  const form = document.getElementById("ttDirectForm");
  if (!form) return;
  const el = id => document.getElementById(id);
  const cookieDialog = el("ttCookieDialog");
  document.querySelectorAll("[data-paste-tiktok-cookies]").forEach(button => button.addEventListener("click", () => {
    const accounts = state.accounts.filter(a => a.platform === "tiktok_web");
    const target = el("ttCookieAccount");
    target.replaceChildren(new Option("Добавить новый аккаунт", ""));
    for (const account of accounts) target.append(new Option(account.display_name + (account.status === "reauth_required" ? " · нужен вход" : ""), account.id));
    const selected = accounts.find(a => state.automation?.config?.account_ids?.includes(a.id) && a.status === "reauth_required");
    target.value = selected?.id || "";
    el("ttCookieError").textContent = "";
    cookieDialog.showModal();
    el("ttCookieText").focus();
  }));
  const clearCookieDraft = () => {
    el("ttCookieText").value = "";
    el("ttCookieName").value = "";
    el("ttCookieOwns").checked = false;
  };
  cookieDialog.addEventListener("close", clearCookieDraft);
  cookieDialog.addEventListener("cancel", clearCookieDraft);
  el("ttCookieCancel").addEventListener("click", () => cookieDialog.close());
  el("ttCookieForm").addEventListener("submit", async event => {
    event.preventDefault();
    const save = el("ttCookieSave");
    save.disabled = true;
    el("ttCookieCancel").disabled = true;
    el("ttCookieError").textContent = "";
    try {
      const result = await api("/api/tiktok-web/cookies/paste", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
        text: el("ttCookieText").value, account_id: el("ttCookieAccount").value || null,
        display_name: el("ttCookieName").value.trim(), owns_account: el("ttCookieOwns").checked
      })});
      cookieDialog.close();
      await refresh();
      el("ttAccountSelect").value = result.account_id;
      el("ttDirectStatus").textContent = result.message;
      toast(result.message);
      el("ttCheckSession").scrollIntoView({behavior: "smooth", block: "center"});
      el("ttCheckSession").focus({preventScroll: true});
    } catch (error) { el("ttCookieError").textContent = error.message; }
    finally { save.disabled = false; el("ttCookieCancel").disabled = false; }
  });
  window.renderTikTokWeb = data => {
    const accounts = data.accounts.filter(a => a.platform === "tiktok_web");
    setOptions(el("ttAccountSelect"), accounts, "Импортируйте JSON cookie TikTok", a => a.display_name + (a.status === "ready" ? "" : a.status === "reauth_required" ? " · нужен вход" : " · ошибка, см. журнал"));
    const clips = data.clips.filter(c => c.status === "ready");
    const selected = el("ttClipSelect").value;
    setOptions(el("ttClipSelect"), clips, "Нет готовых клипов", c => `${shortId(c.id)} · ${c.duration_seconds} сек.`);
    el("ttClipSelect").prepend(new Option("Или выберите файл ниже", ""));
    el("ttClipSelect").value = clips.some(c => c.id === selected) ? selected : "";
  };
  window.renderTikTokWeb(state);
  const pipelineForm = el("ttPipelineForm");
  pipelineForm.addEventListener("submit", event => {
    event.preventDefault();
    withButton(pipelineForm, async () => {
      const accountId = el("ttAccountSelect").value;
      if (!accountId || !el("ttPipelineConsent").checked) throw new Error("Выберите аккаунт и подтвердите публикацию");
      const result = await api("/api/pipeline/start", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
        query: el("ttPipelineQuery").value.trim(), max_results: 10, clip_count: 1,
        clip_duration: Number(el("ttPipelineDuration").value), tiktok_web_account_id: accountId,
        visibility: el("ttPipelineVisibility").value, consent: true, fresh_source: true, topic: el("ttPipelineTopic").value
      })});
      el("ttPipelineStatus").textContent = `Запуск ${shortId(result.job_id)}: поиск → скачивание → монтаж → TikTok. Результат появится в журнале.`;
      el("ttPipelineConsent").checked = false;
      await refresh();
    });
  });
  el("ttCheckSession").addEventListener("click", async event => {
    const id = el("ttAccountSelect").value;
    if (!id) return toast("Сначала импортируйте cookie своего TikTok-аккаунта", true);
    const button = event.currentTarget;
    button.disabled = true;
    el("ttDirectStatus").textContent = "Проверяю вход в TikTok Studio…";
    try {
      await api(`/api/accounts/${id}/check-session`, {method: "POST"});
      el("ttDirectStatus").textContent = "Форма загрузки TikTok Studio доступна. Разрешение на отправку видео ещё не подтверждено; публикация не выполнялась.";
      await refresh();
    } catch (error) { el("ttDirectStatus").textContent = error.message; await refresh(); }
    finally { button.disabled = false; }
  });
  form.addEventListener("submit", event => {
    event.preventDefault();
    withButton(form, async () => {
      const accountId = el("ttAccountSelect").value;
      const clipId = el("ttClipSelect").value;
      const file = el("ttFileInput").files[0];
      const description = el("ttDescription").value.trim();
      const visibility = el("ttVisibility").value;
      const consent = el("ttConsent").checked;
      if (!accountId || (!clipId && !file) || !description || !consent) throw new Error("Выберите TikTok-аккаунт и видео, укажите описание и подтвердите отправку");
      el("ttDirectStatus").textContent = "Передаю задачу на сервер…";
      try {
        let result;
        if (file) {
          const data = new FormData();
          data.append("file", file);
          data.append("account_id", accountId);
          data.append("description", description);
          data.append("visibility", visibility);
          data.append("consent", "true");
          result = await api("/api/tiktok-web/direct-upload", {method: "POST", body: data});
        } else {
          result = await api("/api/tiktok-web/publish", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({account_id: accountId, clip_id: clipId, description, visibility, consent})});
        }
        el("ttDirectStatus").textContent = `Задача ${shortId(result.job_id)} в очереди. Итог — в журнале; пока это не подтверждение публикации.`;
        el("ttConsent").checked = false;
        el("ttFileInput").value = "";
        toast(result.message);
        await refresh();
      } catch (error) {
        el("ttDirectStatus").textContent = error.message;
        throw error;
      }
    });
  });
})();
