(function () {
  "use strict";

  var tbody = document.getElementById("url-table-body");
  var empty = document.getElementById("empty");
  var note = document.getElementById("note");
  var addButton = document.getElementById("add-url-button");
  var checkAllButton = document.getElementById("check-all-button");
  var dialog = document.getElementById("add-url-modal");
  var addForm = document.getElementById("add-form");
  var newUrl = document.getElementById("new-url");
  var addError = document.getElementById("add-error");
  var addSubmit = document.getElementById("add-submit");
  var addCancel = document.getElementById("add-cancel");

  var items = [];

  function api(method, path, body) {
    return fetch(path, {
      method: method,
      headers: { "Content-Type": "application/json", "X-URL-Follower": "1" },
      body: body ? JSON.stringify(body) : undefined
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          throw new Error((data.error && data.error.message) || "Operazione non riuscita. Riprova.");
        }
        return data;
      });
    });
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatDate(iso) {
    if (!iso) return "Mai controllato";
    var date = new Date(iso);
    if (isNaN(date.getTime())) return iso;
    return date.toLocaleString("it-IT", { dateStyle: "medium", timeStyle: "short" });
  }

  function showNote(message) {
    note.textContent = message || "";
    note.hidden = !message;
  }

  function badge(item) {
    if (item.status === "online") return el("span", "badge online", "ONLINE");
    if (item.status === "offline") return el("span", "badge offline", "OFFLINE");
    return el("span", "badge unknown", "NON CONTROLLATO");
  }

  function buildRow(item) {
    var row = el("tr");

    var urlCell = el("td");
    var link = el("a", "", item.url);
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    urlCell.appendChild(link);
    if (item.hops && item.hops.length) {
      var chain = el("p", "detail");
      chain.appendChild(el("span", "mono", item.hops.join(" → ")));
      if (item.final_url && item.final_url !== item.url) {
        chain.appendChild(document.createTextNode(" verso " + item.final_url));
      }
      urlCell.appendChild(chain);
    }
    if (item.error) urlCell.appendChild(el("p", "detail error", item.error));
    row.appendChild(urlCell);

    var statusCell = el("td");
    statusCell.appendChild(badge(item));
    row.appendChild(statusCell);

    row.appendChild(el("td", "nowrap-cell", formatDate(item.last_check)));

    var actions = el("td");
    var list = el("div", "btn-list nowrap");
    var check = el("button", "btn btn-outline-primary btn-sm", "Controlla");
    check.type = "button";
    check.addEventListener("click", function () { checkOne(item, check); });
    var remove = el("button", "btn btn-outline-danger btn-sm", "Elimina");
    remove.type = "button";
    remove.addEventListener("click", function () { removeOne(item, remove); });
    list.appendChild(check);
    list.appendChild(remove);
    actions.appendChild(list);
    row.appendChild(actions);
    return row;
  }

  function render() {
    tbody.textContent = "";
    items.forEach(function (item) { tbody.appendChild(buildRow(item)); });
    empty.hidden = items.length > 0;
    checkAllButton.disabled = items.length === 0;
  }

  function applyData(data) {
    items = data.urls || [];
    showNote(data.persistent === false
      ? "Attenzione: l'elenco non può essere salvato su disco e andrà perso al riavvio dell'app."
      : "");
    render();
  }

  function checkOne(item, button) {
    button.disabled = true;
    button.textContent = "Controllo…";
    api("POST", "api/urls/" + encodeURIComponent(item.id) + "/check")
      .then(function (updated) {
        items = items.map(function (it) { return it.id === updated.id ? updated : it; });
        render();
      })
      .catch(function (error) { showNote(error.message); render(); });
  }

  function removeOne(item, button) {
    if (!window.confirm("Eliminare " + item.url + " dall'elenco?")) return;
    button.disabled = true;
    api("DELETE", "api/urls/" + encodeURIComponent(item.id))
      .then(function () {
        items = items.filter(function (it) { return it.id !== item.id; });
        render();
      })
      .catch(function (error) { showNote(error.message); render(); });
  }

  checkAllButton.addEventListener("click", function () {
    checkAllButton.disabled = true;
    checkAllButton.textContent = "Controllo in corso…";
    api("POST", "api/check-all")
      .then(applyData)
      .catch(function (error) { showNote(error.message); })
      .then(function () {
        checkAllButton.textContent = "Controlla tutti";
        render();
      });
  });

  addButton.addEventListener("click", function () {
    addError.hidden = true;
    newUrl.value = "";
    dialog.showModal();
    newUrl.focus();
  });
  addCancel.addEventListener("click", function () { dialog.close(); });

  addForm.addEventListener("submit", function (event) {
    event.preventDefault();
    addError.hidden = true;
    addSubmit.disabled = true;
    addSubmit.textContent = "Aggiungo e controllo…";
    api("POST", "api/urls", { url: newUrl.value })
      .then(function (created) {
        items.push(created);
        render();
        dialog.close();
      })
      .catch(function (error) {
        addError.textContent = error.message;
        addError.hidden = false;
      })
      .then(function () {
        addSubmit.disabled = false;
        addSubmit.textContent = "Aggiungi";
      });
  });

  api("GET", "api/urls")
    .then(applyData)
    .catch(function () {
      showNote("Impossibile contattare l'app. Controlla che sia in esecuzione e ricarica la pagina.");
      render();
    });
})();
