(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var loginPage = $("login-page"), adminPage = $("admin-page");
  var users = [], urls = [], me = "";

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function api(method, path, body) {
    return fetch(path, {
      method: method,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-URL-Follower": "1" },
      body: body ? JSON.stringify(body) : undefined
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          var error = new Error(data.error || "Operazione non riuscita. Riprova.");
          error.status = response.status;
          throw error;
        }
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

  function fail(error) {
    if (error.status === 401) return showLogin("La sessione è scaduta: accedi di nuovo.");
    showMessage(error.message, "error");
  }

  function showLogin(error) {
    adminPage.hidden = true;
    loginPage.hidden = false;
    $("login-error").textContent = error || "";
    $("login-error").hidden = !error;
    $("api-key").value = "";
    $("api-key").focus();
  }

  function showAdmin() {
    loginPage.hidden = true;
    adminPage.hidden = false;
    $("logged-user").textContent = me;
  }

  /* ---------- Utenti ---------- */
  function yesNo(value) { return el("span", "badge" + (value ? " yes" : ""), value ? "Sì" : "No"); }

  function renderUsers() {
    var body = $("user-table-body");
    body.textContent = "";
    users.forEach(function (user) {
      var row = el("tr");
      row.appendChild(el("td", "", user.nomeutente));
      var keyCell = el("td");
      keyCell.appendChild(el("code", "mono", user.apikey));
      row.appendChild(keyCell);
      var adminCell = el("td"); adminCell.appendChild(yesNo(user.admin === 1)); row.appendChild(adminCell);
      var availCell = el("td"); availCell.appendChild(yesNo(user.available === 1)); row.appendChild(availCell);

      var actions = el("td"), list = el("div", "btn-list nowrap");
      var toggleAvail = el("button", "btn btn-outline-primary btn-sm", user.available === 1 ? "Rendi non disponibile" : "Rendi disponibile");
      toggleAvail.type = "button";
      toggleAvail.addEventListener("click", function () { patchUser(user, { available: user.available === 1 ? 0 : 1 }); });
      var toggleAdmin = el("button", "btn btn-outline-primary btn-sm", user.admin === 1 ? "Rimuovi admin" : "Rendi admin");
      toggleAdmin.type = "button";
      toggleAdmin.addEventListener("click", function () { patchUser(user, { admin: user.admin === 1 ? 0 : 1 }); });
      var remove = el("button", "btn btn-outline-danger btn-sm", "Elimina");
      remove.type = "button";
      remove.addEventListener("click", function () { removeUser(user); });
      list.appendChild(toggleAvail); list.appendChild(toggleAdmin); list.appendChild(remove);
      actions.appendChild(list); row.appendChild(actions);
      body.appendChild(row);
    });
  }

  function loadUsers() {
    return api("GET", "api/users").then(function (data) { users = data.users || []; renderUsers(); });
  }
  function patchUser(user, change) {
    api("PATCH", "api/users/" + user.id, change).then(loadUsers).then(function () { showMessage(""); }).catch(fail);
  }
  function removeUser(user) {
    if (!window.confirm("Eliminare l'utente " + user.nomeutente + "?")) return;
    api("DELETE", "api/users/" + user.id).then(loadUsers).then(function () { showMessage(""); }).catch(fail);
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
        api("POST", "api/urls/" + item.id + "/check").then(loadUrls).catch(function (e) { fail(e); loadUrls(); });
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
        .catch(function (e) {
          if (e.status === 401) { dialog.close(); return fail(e); }
          error.textContent = e.message; error.hidden = false;
        })
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
        admin: $("new-user-admin").checked ? 1 : 0,
        available: $("new-user-available").checked ? 1 : 0
      }).then(function (created) {
        showMessage("Utente " + created.username + " creato. Chiave API: " + created.apikey +
          ". Copiala adesso: se è un amministratore non verrà più mostrata.", "success");
        return loadUsers();
      });
    });

  /* ---------- Accesso ---------- */
  $("login-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var button = $("login-button");
    button.disabled = true;
    $("login-error").hidden = true;
    api("POST", "api/login", { password: $("api-key").value })
      .then(function (data) { me = data.username; return start(); })
      .catch(function (e) { showLogin(e.message); })
      .then(function () { button.disabled = false; });
  });

  $("logout-button").addEventListener("click", function (event) {
    event.preventDefault();
    api("POST", "api/logout").catch(function () {}).then(function () { showLogin(""); });
  });

  function start() {
    return Promise.all([loadUsers(), loadUrls()]).then(function () { showMessage(""); showAdmin(); });
  }

  // All'apertura: se la sessione è ancora valida si entra direttamente.
  api("GET", "api/users").then(function (data) {
    users = data.users || [];
    var self = users.filter(function (u) { return u.apikey !== "*********" && u.admin === 1; });
    return api("GET", "api/urls").then(function (list) {
      urls = list.urls || [];
      me = self.length ? self[0].nomeutente : "";
      renderUsers(); renderUrls(); showAdmin();
    });
  }).catch(function () { showLogin(""); });
})();
