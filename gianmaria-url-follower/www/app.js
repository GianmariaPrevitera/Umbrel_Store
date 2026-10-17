(function () {
  "use strict";

  var form = document.getElementById("try");
  var input = document.getElementById("url");
  var button = document.getElementById("go");
  var alertBox = document.getElementById("alert");
  var results = document.getElementById("results");
  var title = document.getElementById("trace-title");
  var trace = document.getElementById("trace");

  var MEANINGS = {
    301: "Spostato in modo permanente",
    302: "Reindirizzamento temporaneo",
    303: "Rimandato a un'altra pagina",
    307: "Reindirizzamento temporaneo",
    308: "Spostato in modo permanente",
    401: "Richiede un accesso",
    403: "Accesso negato",
    404: "Pagina non trovata",
    410: "Pagina rimossa",
    429: "Troppe richieste al sito"
  };

  function meaning(hop, isLast) {
    if (MEANINGS[hop.status]) return MEANINGS[hop.status];
    if (hop.status >= 200 && hop.status < 300) return isLast ? "Destinazione raggiunta" : "Risposta riuscita";
    if (hop.status >= 500) return "Errore del sito";
    if (hop.status >= 400) return "Richiesta rifiutata dal sito";
    return hop.reason || "Risposta del sito";
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showAlert(message) {
    alertBox.textContent = message;
    alertBox.hidden = !message;
  }

  function setBusy(busy) {
    button.disabled = busy;
    button.textContent = busy ? "Sto seguendo il link…" : "Segui il link";
    results.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function render(data) {
    var hops = data.hops || [];
    trace.textContent = "";
    title.hidden = hops.length === 0;
    if (!hops.length) return;

    title.textContent = "Percorso trovato ";
    title.appendChild(el("span", "", "(" + hops.length + (hops.length === 1 ? " passaggio)" : " passaggi)")));

    hops.forEach(function (hop, i) {
      var isLast = i === hops.length - 1;
      var item = el("li", "hop");
      item.style.setProperty("--i", i);
      if (isLast && !data.error) item.classList.add(hop.status < 400 ? "final" : "failed");

      var body = el("div");
      body.appendChild(el("p", "url", hop.url));
      var meta = el("p", "meta");
      meta.appendChild(el("span", "code", String(hop.status)));
      meta.appendChild(document.createTextNode(" " + meaning(hop, isLast)));
      body.appendChild(meta);

      item.appendChild(el("span", "node", String(hop.n)));
      item.appendChild(body);
      trace.appendChild(item);
    });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var value = input.value.trim();
    showAlert("");
    if (!value) {
      showAlert("Incolla un indirizzo da seguire, per esempio https://bit.ly/esempio.");
      input.focus();
      return;
    }
    setBusy(true);
    fetch("api/follow?url=" + encodeURIComponent(value), { headers: { Accept: "application/json" } })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        render(data);
        if (data.error) showAlert(data.error.message);
      })
      .catch(function () {
        showAlert("Impossibile contattare l'app. Controlla che sia in esecuzione e riprova.");
      })
      .then(function () { setBusy(false); });
  });
})();
