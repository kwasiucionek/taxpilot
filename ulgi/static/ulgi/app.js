/* TaxPilot — streaming RAG po SSE (fetch) + przełącznik trybów. */
const TaxPilot = (() => {
  "use strict";
  const csrf = (document.querySelector('meta[name=csrf]') || {}).content || "";
  let busy = false;

  const $ = (id) => document.getElementById(id);
  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  function esc(s) {
    return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  function mode(m) {
    const ask = m === "ask";
    $("panel-ask").classList.toggle("active", ask);
    $("panel-qual").classList.toggle("active", !ask);
    $("tab-ask").setAttribute("aria-selected", String(ask));
    $("tab-qual").setAttribute("aria-selected", String(!ask));
  }

  // Minimalny render: akapity + **pogrubienie**.
  function renderText(s) {
    const safe = esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
    return safe
      .split(/\n{2,}/)
      .map((p) => "<p>" + p.replace(/\n/g, "<br>") + "</p>")
      .join("");
  }

  function ulgaTag(s) {
    return s.ulga_label ? `<span class="ulga ${s.ulga_cls}">${esc(s.ulga_label)}</span>` : "";
  }
  function renderSources(turn, items) {
    if (!items || !items.length) return;
    const box = el("div", "sources");
    box.appendChild(el("h4", null, "Źródła"));
    items.forEach((s) => {
      const row = el("div", "src");
      row.innerHTML =
        `<span class="cit">${esc(s.cit)} <span class="suf">${esc(s.suf || "")}</span></span>` +
        ulgaTag(s) +
        `<span class="grow"></span>` +
        (s.url ? `<a class="lnk" href="${esc(s.url)}" target="_blank" rel="noopener">tekst ↗</a>` : "");
      box.appendChild(row);
    });
    turn.appendChild(box);
  }

  function parseSSE(chunk) {
    let ev = "message",
      data = "";
    chunk.split("\n").forEach((line) => {
      if (line.startsWith("event:")) ev = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    });
    let payload = {};
    try {
      payload = data ? JSON.parse(data) : {};
    } catch (_) {}
    return { ev, payload };
  }

  async function ask() {
    if (busy) return;
    const ta = $("q");
    const q = ta.value.trim();
    if (!q) return;
    const ulga = (document.querySelector("input[name=ulga]:checked") || {}).value || "";

    busy = true;
    $("ask-btn").disabled = true;
    const empty = $("ask-empty");
    if (empty) empty.remove();
    const thread = $("thread");

    const ut = el("div", "turn user");
    ut.appendChild(el("div", "who", "pytanie"));
    ut.appendChild(el("div", "bubble", esc(q)));
    thread.appendChild(ut);

    const turn = el("div", "turn bot");
    const who = el("div", "who", "TaxPilot");
    const bub = el("div", "bubble streaming");
    turn.appendChild(who);
    turn.appendChild(bub);
    thread.appendChild(turn);

    ta.value = "";
    turn.scrollIntoView({ behavior: "smooth", block: "end" });

    let acc = "";
    const onEvent = ({ ev, payload }) => {
      if (ev === "token") {
        acc += payload.t || "";
        bub.innerHTML = renderText(acc);
        bub.classList.add("streaming");
        turn.scrollIntoView({ block: "end" });
      } else if (ev === "meta") {
        if (payload.cache) who.innerHTML = `TaxPilot <span class="cachetag">cache · ${esc(payload.cache)}</span>`;
      } else if (ev === "sources") {
        bub.classList.remove("streaming");
        renderSources(turn, payload.items || []);
      } else if (ev === "error") {
        bub.classList.remove("streaming");
        bub.appendChild(el("div", "errline", esc(payload.m || "Błąd.")));
      } else if (ev === "done") {
        bub.classList.remove("streaming");
      }
    };

    try {
      const res = await fetch(window.TAXPILOT_ASK_URL, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf },
        body: new URLSearchParams({ q, ulga }),
      });
      if (!res.ok || !res.body) throw new Error("HTTP " + res.status);
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          onEvent(parseSSE(buf.slice(0, i)));
          buf = buf.slice(i + 2);
        }
      }
    } catch (e) {
      bub.classList.remove("streaming");
      bub.appendChild(el("div", "errline", "Błąd połączenia: " + esc(String(e))));
    }
    bub.classList.remove("streaming");
    busy = false;
    $("ask-btn").disabled = false;
  }

  return { mode, ask };
})();
