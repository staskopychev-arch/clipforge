/* TikTok account setup and automatic checks, entirely inside the 24/7 panel. */
(() => {
  const el = id => document.getElementById(id);
  const dialog = el("ttCookieDialog");
  if (!dialog) return;
  const checks = new Map();
  let queue = Promise.resolve();

  window.renderAutomationAccounts = data => {
    const list = el("automationAccountStatus");
    list.replaceChildren();
    for (const account of data.accounts.filter(item => item.platform === "tiktok_web")) {
      const savedCheck = checks.get(account.id);
      const checked = savedCheck?.state === "ready" && account.status !== "ready" ? null : savedCheck;
      const row = document.createElement("div");
      row.className = "automation-account-row";
      const name = document.createElement("strong");
      name.textContent = account.display_name;
      const detail = document.createElement("span");
      detail.textContent = checked?.message || (account.status === "ready" ? "Вход подтверждён" : account.status === "error" ? "Требуется проверка аккаунта TikTok или обновление cookie" : "Вход ещё не подтверждён. Выберите аккаунт или обновите cookie.");
      detail.className = checked?.state === "error" ? "error-text" : "";
      row.append(name, detail);
      list.append(row);
    }
    if (!list.childElementCount) list.textContent = "Добавьте cookie своего TikTok-аккаунта.";
  };

  window.automationCheckAccounts = (ids, {force = false} = {}) => {
    const requested = [...new Set(ids)];
    const tasks = requested.filter(id => {
      const account = state.accounts.find(item => item.id === id && item.platform === "tiktok_web");
      if (!account || (!force && account.status === "ready")) return false;
      const previous = checks.get(id);
      return force || !previous || previous.state === "ready";
    });
    for (const id of tasks) checks.set(id, {state: "queued", message: "Ожидает автоматической проверки входа…"});
    renderAutomation();
    const batch = queue.then(async () => {
      for (const id of tasks) {
        checks.set(id, {state: "checking", message: "Проверяю вход в TikTok…"});
        renderAutomation();
        try {
          await api(`/api/accounts/${id}/check-session`, {method: "POST"});
          checks.set(id, {state: "ready", message: "Вход подтверждён"});
          const account = state.accounts.find(item => item.id === id);
          if (account) account.status = "ready";
        } catch (error) {
          checks.set(id, {state: "error", message: error.message});
        }
        await refresh();
      }
      return requested.every(id => state.accounts.some(item => item.id === id && item.status === "ready") && checks.get(id)?.state !== "error");
    });
    queue = batch.catch(() => {});
    return batch;
  };

  document.querySelectorAll("[data-paste-tiktok-cookies]").forEach(button => button.addEventListener("click", () => {
    const accounts = state.accounts.filter(item => item.platform === "tiktok_web");
    const target = el("ttCookieAccount");
    target.replaceChildren(new Option("Добавить новый аккаунт", ""));
    for (const account of accounts) target.append(new Option(account.display_name + (account.status === "ready" ? "" : " · требуется вход"), account.id));
    target.value = accounts.find(item => state.automation?.config?.account_ids?.includes(item.id) && item.status === "reauth_required")?.id || "";
    el("ttCookieError").textContent = "";
    dialog.showModal();
    el("ttCookieText").focus();
  }));
  const clearDraft = () => {
    el("ttCookieText").value = "";
    el("ttCookieName").value = "";
    el("ttCookieOwns").checked = false;
  };
  dialog.addEventListener("close", clearDraft);
  dialog.addEventListener("cancel", clearDraft);
  el("ttCookieCancel").addEventListener("click", () => dialog.close());
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
      dialog.close();
      clearDraft();
      await refresh();
      toast("Cookie сохранены. Проверяю вход в режиме 24/7…");
      el("automationAccountStatus").scrollIntoView({behavior: "smooth", block: "center"});
      const checked = await window.automationCheckAccounts([result.account_id], {force: true});
      toast(checked ? "Аккаунт проверен и доступен в режиме 24/7" : "Cookie сохранены. Причина ошибки входа показана в блоке 24/7.", !checked);
    } catch (error) {
      if (dialog.open) el("ttCookieError").textContent = error.message;
      else toast(error.message, true);
    } finally { save.disabled = false; el("ttCookieCancel").disabled = false; }
  });
  window.renderAutomationAccounts(state);
})();
