/* Administrator-only, explicit owned-account login. Never stores credentials. */
(() => {
  const panel = document.getElementById("loginQueuePanel");
  if (!panel) return;
  let activeId = null;
  let waiting = false;
  let fetching = false;
  let imageUrl = null;
  let lastReady = 0;
  const el = id => document.getElementById(id);
  const labels = {queued: "Ожидает", processing: "Вход", awaiting_verification: "Нужна проверка", ready: "Вход сохранён", error: "Ошибка", cancelled: "Отменён", interrupted: "Прерван"};

  async function screen() {
    if (!waiting || !activeId || document.hidden) return;
    const expectedId = activeId;
    const response = await fetch(`/api/account-logins/${activeId}/screen`, {cache: "no-store"});
    if (!response.ok) return;
    const blob = await response.blob();
    if (activeId !== expectedId || !waiting) return;
    const next = URL.createObjectURL(blob);
    el("loginBrowserScreen").src = next;
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = next;
  }

  async function update() {
    if (fetching || document.hidden) return;
    fetching = true;
    try {
      const data = await api("/api/account-logins");
      activeId = data.active_id;
      waiting = data.waiting;
      el("loginQueueStatus").textContent = waiting ? "Ожидает вашей проверки" : data.running ? "Работает" : "Остановлена";
      el("loginQueueMessage").textContent = data.message;
      el("loginQueueStart").disabled = data.running || !data.records.some(r => r.status === "queued");
      el("loginQueueStop").disabled = !data.running;
      el("loginQueueClear").disabled = data.running || !data.records.some(r => r.status === "queued");
      el("loginManualPanel").hidden = !waiting;
      const rows = el("loginQueueRows");
      rows.replaceChildren();
      for (const record of data.records) {
        const tr = document.createElement("tr");
        for (const value of [record.display_name, labels[record.status] || record.status, record.message]) {
          const td = document.createElement("td");
          td.textContent = value;
          tr.append(td);
        }
        rows.append(tr);
      }
      const ready = data.records.filter(r => r.status === "ready").length;
      if (ready !== lastReady) { lastReady = ready; await refresh(); }
      if (waiting) await screen();
      else {
        el("loginBrowserScreen").removeAttribute("src");
        if (imageUrl) { URL.revokeObjectURL(imageUrl); imageUrl = null; }
        el("loginManualText").value = "";
      }
    } catch (error) { el("loginQueueMessage").textContent = error.message; }
    finally { fetching = false; }
  }

  async function action(payload) {
    if (!activeId || !waiting) return;
    await api(`/api/account-logins/${activeId}/action`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    if (payload.kind === "resume") { waiting = false; el("loginManualPanel").hidden = true; }
    else await screen();
  }

  el("loginCredentialsForm").addEventListener("submit", event => {
    event.preventDefault();
    withButton(event.currentTarget, async () => {
      const file = el("loginCredentialsFile").files[0];
      if (!file || !el("loginOwnsAccounts").checked) throw new Error("Выберите файл и подтвердите владение аккаунтами");
      const data = new FormData();
      data.append("file", file);
      data.append("owns_accounts", "true");
      const result = await api("/api/account-logins/import", {method: "POST", body: data});
      event.target.reset();
      toast(`Добавлено в очередь входа: ${result.imported}. Нажмите «Запустить очередь входа».`);
      await update();
    });
  });
  for (const [id, operation] of [["loginQueueStart", "start"], ["loginQueueStop", "stop"], ["loginQueueClear", "clear"]]) {
    el(id).addEventListener("click", async () => {
      el(id).disabled = true;
      try { await api(`/api/account-logins/${operation}`, {method: "POST"}); }
      catch (error) { toast(error.message, true); }
      finally { await update(); }
    });
  }
  el("loginBrowserScreen").addEventListener("click", async event => {
    const rect = event.currentTarget.getBoundingClientRect();
    try { await action({kind: "click", x: (event.clientX - rect.left) * 1100 / rect.width, y: (event.clientY - rect.top) * 760 / rect.height}); }
    catch (error) { toast(error.message, true); }
  });
  el("loginManualTextForm").addEventListener("submit", async event => {
    event.preventDefault();
    const value = el("loginManualText").value;
    el("loginManualText").value = "";
    if (!value) return;
    try { await action({kind: "text", text: value}); }
    catch (error) { toast(error.message, true); }
  });
  panel.querySelectorAll("[data-login-key]").forEach(button => button.addEventListener("click", async () => {
    try { await action({kind: "key", key: button.dataset.loginKey}); } catch (error) { toast(error.message, true); }
  }));
  panel.querySelectorAll("[data-login-scroll]").forEach(button => button.addEventListener("click", async () => {
    try { await action({kind: "scroll", dy: Number(button.dataset.loginScroll)}); } catch (error) { toast(error.message, true); }
  }));
  el("loginManualResume").addEventListener("click", async () => {
    try { await action({kind: "resume"}); await update(); } catch (error) { toast(error.message, true); }
  });
  window.addEventListener("beforeunload", () => { if (imageUrl) URL.revokeObjectURL(imageUrl); });
  update();
  window.setInterval(update, 2500);
})();
