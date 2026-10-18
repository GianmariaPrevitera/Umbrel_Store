(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var users = [], urls = [];

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // L'accesso è gestito da Umbrel. Se la sessione di Umbrel scade, la
  // richiesta riceve la pagina di accesso di Umbrel invece di un JSON.
  var SESSION_MESSAGE = "La sessione di Umbrel potrebbe essere scaduta. Ricarica la pagina e accedi di nuovo.";

  function api(method, path, body) {
    return fetch(path, {
      method: method,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-URL-Follower": "1" },
      body: body ? JSON.stringify(body) : undefined
    }).then(function (response) {
      var type = response.headers.get("Content-Type") || "";
      if (response.redirected || type.indexOf("application/json") === -1) throw new Error(SESSION_MESSAGE);
      return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.error || "Operazione non riuscita. Riprova.");
        return data;
      });
    });
  }

  function formatDate(iso) {
    if (!iso) return "Mai controllato";
    var date = new Date(iso);
    if (isNaN(date.getTime())) return iso;
    return date.toLocaleString("it-IT", { timeZone: "Europe/Rome", dateStyle: "medium", timeStyle: "short" });
  }

  function showMessage(text, kind) {
    var box = $("message");
    $("message-text").textContent = text || "";
    box.className = "alert" + (kind ? " " + kind : "");
    box.hidden = !text;
  }
  $("message-close").addEventListener("click", function () { showMessage(""); });
  function fail(error) { showMessage(error.message, "error"); }

  /* ---------- Utenti ---------- */
  function renderUsers() {
    var body = $("user-table-body");
    body.textContent = "";
    $("user-empty").hidden = users.length > 0;
    users.forEach(function (user) {
      var row = el("tr");
      row.appendChild(el("td", "", user.nomeutente));
      var keyCell = el("td");
      keyCell.appendChild(el("code", "mono", user.apikey));
      row.appendChild(keyCell);
      var availCell = el("td");
      availCell.appendChild(el("span", "badge" + (user.available === 1 ? " yes" : ""), user.available === 1 ? "Sì" : "No"));
      row.appendChild(availCell);

      var actions = el("td"), list = el("div", "btn-list nowrap");
      var toggle = el("button", "btn btn-outline-primary btn-sm", user.available === 1 ? "Rendi non disponibile" : "Rendi disponibile");
      toggle.type = "button";
      toggle.addEventListener("click", function () {
        api("PATCH", "api/users/" + user.id, { available: user.available === 1 ? 0 : 1 }).then(loadUsers).catch(fail);
      });
      var remove = el("button", "btn btn-outline-danger btn-sm", "Elimina");
      remove.type = "button";
      remove.addEventListener("click", function () {
        if (!window.confirm("Eliminare l'utente " + user.nomeutente + "?")) return;
        api("DELETE", "api/users/" + user.id).then(loadUsers).catch(fail);
      });
      list.appendChild(toggle); list.appendChild(remove);
      actions.appendChild(list); row.appendChild(actions);
      body.appendChild(row);
    });
  }
  function loadUsers() {
    return api("GET", "api/users").then(function (data) { users = data.users || []; renderUsers(); });
  }

  /* ---------- URL ---------- */
  function renderUrls() {
    var body = $("url-table-body");
    body.textContent = "";
    $("url-empty").hidden = urls.length > 0;
    $("check-all-button").disabled = urls.length === 0;
    urls.forEach(function (item) {
      var row = el("tr");
      var urlCell = el("td");
      var link = el("a", "", item.url);
      link.href = item.url; link.target = "_blank"; link.rel = "noopener noreferrer";
      urlCell.appendChild(link); row.appendChild(urlCell);

      var statusCell = el("td");
      statusCell.appendChild(el("span", "badge " + (item.stato === 1 ? "online" : (item.last_check ? "offline" : "")),
        item.stato === 1 ? "ONLINE" : (item.last_check ? "OFFLINE" : "NON CONTROLLATO")));
      row.appendChild(statusCell);
      row.appendChild(el("td", "nowrap-cell", formatDate(item.last_check)));

      var actions = el("td"), list = el("div", "btn-list nowrap");
      var check = el("button", "btn btn-outline-primary btn-sm", "Controlla");
      check.type = "button";
      check.addEventListener("click", function () {
        check.disabled = true; check.textContent = "Controllo…";
        api("POST", "api/urls/" + item.id + "/check").then(loadUrls).catch(function (e) { fail(e); loadUrls().catch(function () {}); });
      });
      var remove = el("button", "btn btn-outline-danger btn-sm", "Elimina");
      remove.type = "button";
      remove.addEventListener("click", function () {
        if (!window.confirm("Eliminare " + item.url + " dall'elenco?")) return;
        api("DELETE", "api/urls/" + item.id).then(loadUrls).catch(fail);
      });
      list.appendChild(check); list.appendChild(remove);
      actions.appendChild(list); row.appendChild(actions);
      body.appendChild(row);
    });
  }
  function loadUrls() {
    return api("GET", "api/urls").then(function (data) { urls = data.urls || []; renderUrls(); });
  }

  $("check-all-button").addEventListener("click", function () {
    var button = $("check-all-button");
    button.disabled = true; button.textContent = "Controllo in corso…";
    api("POST", "api/urls/check-all")
      .then(function (data) { urls = data.urls || []; })
      .catch(fail)
      .then(function () { button.textContent = "Controlla tutti"; renderUrls(); });
  });

  /* ---------- Finestre ---------- */
  function wireDialog(openId, dialogId, cancelId, formId, errorId, submitId, labelBusy, labelIdle, run) {
    var dialog = $(dialogId), error = $(errorId), submit = $(submitId);
    $(openId).addEventListener("click", function () {
      error.hidden = true;
      $(formId).reset();
      dialog.showModal();
      dialog.querySelector("input[type=text]").focus();
    });
    $(cancelId).addEventListener("click", function () { dialog.close(); });
    $(formId).addEventListener("submit", function (event) {
      event.preventDefault();
      error.hidden = true;
      submit.disabled = true; submit.textContent = labelBusy;
      run().then(function () { dialog.close(); })
        .catch(function (e) { error.textContent = e.message; error.hidden = false; })
        .then(function () { submit.disabled = false; submit.textContent = labelIdle; });
    });
  }

  wireDialog("add-url-button", "add-url-modal", "add-url-cancel", "add-url-form", "add-url-error",
    "add-url-submit", "Aggiungo e controllo…", "Aggiungi", function () {
      return api("POST", "api/urls", { url: $("new-url").value }).then(loadUrls);
    });

  wireDialog("add-user-button", "add-user-modal", "add-user-cancel", "add-user-form", "add-user-error",
    "add-user-submit", "Creo…", "Crea", function () {
      return api("POST", "api/users", {
        username: $("new-username").value,
        available: $("new-user-available").checked ? 1 : 0
      }).then(function (created) {
        showMessage("Utente " + created.username + " creato. Chiave API: " + created.apikey, "success");
        return loadUsers();
      });
    });

  Promise.all([loadUsers(), loadUrls()]).catch(fail);
})();
