"use strict";

const state = { items: [], selectedId: null, card: null, view: "waiting", query: "", config: null, stats: null };

const VIEWS = [
  ["waiting", "Ждут ответа", "waiting"],
  ["hot", "Горячие", "hot_waiting"],
  ["review", "На проверку", "needs_review"],
  ["all", "Все", "total"],
  ["archived", "Архив", null],
];
const URGENCY = { high: "высокая", normal: "обычная", low: "низкая" };
const STATUS = { new: "ждёт ответа", answered: "отвечено", archived: "в архиве" };

const $ = (selector, root = document) => root.querySelector(selector);

/** Создаёт DOM-узел. Строки всегда вставляются как текст, поэтому содержимое писем не исполняется. */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || "GET",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!response.ok) {
    let message = `Ошибка ${response.status}`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") message = data.detail;
      else if (Array.isArray(data.detail)) message = data.detail.map((item) => item.msg).join("; ");
    } catch { /* тело ответа не JSON */ }
    throw new Error(message);
  }
  return response.json();
}

// --- Форматирование ---------------------------------------------------------------------------

const timeFmt = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
const dateFmt = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" });
const numberFmt = new Intl.NumberFormat("ru-RU");

function fmtWhen(iso) {
  const date = new Date(iso);
  const dayStart = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((dayStart(new Date()) - dayStart(date)) / 86400000);
  const time = timeFmt.format(date);
  if (days === 0) return time;
  if (days === 1) return `вчера, ${time}`;
  return `${dateFmt.format(date)}, ${time}`;
}

function fmtDuration(minutes) {
  const total = Math.max(0, Math.round(minutes));
  if (total < 60) return `${total} мин`;
  const hours = Math.floor(total / 60);
  const rest = total % 60;
  if (hours < 24) return rest ? `${hours} ч ${rest} мин` : `${hours} ч`;
  return `${Math.floor(hours / 24)} дн`;
}

function slaInfo(card) {
  if (card.status !== "new" || !card.sla_due_at) return null;
  const left = (new Date(card.sla_due_at) - Date.now()) / 60000;
  if (left < 0) return { text: `просрочено на ${fmtDuration(-left)}`, tone: "bad" };
  return { text: `осталось ${fmtDuration(left)}`, tone: left <= 5 ? "warn" : "ok" };
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = isError ? "toast error" : "toast";
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, isError ? 6000 : 3000);
}

/** Выполняет действие с блокировкой кнопки и показом ошибки. */
async function run(button, action) {
  if (button) button.disabled = true;
  try {
    return await action();
  } catch (error) {
    toast(error.message, true);
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

// --- Загрузка -----------------------------------------------------------------------------------

async function loadConfig() {
  const config = await api("/api/config");
  state.config = config;
  $("#company").textContent = config.company;
  const engine = $("#engine");
  if (config.engine === "claude") {
    engine.textContent = `Движок: ${config.model}`;
    engine.classList.add("live");
    engine.title = "Обращения разбирает Claude. Контакты и суммы дополнительно сверяются с текстом.";
  } else {
    engine.textContent = "Движок: правила, без LLM";
    engine.classList.remove("live");
    engine.title = "Демо-режим без ключа API. Добавьте ANTHROPIC_API_KEY в .env, и обращения будет разбирать Claude.";
  }
  $("#btn-simulate").hidden = !config.demo_mode;
  $("#btn-reset").hidden = !config.demo_mode;

  const banner = $("#banner");
  banner.replaceChildren();
  if (config.engine_error) {
    banner.append(`Claude недоступен (${config.engine_error}). Обращения разбирает резервный движок правил.`);
  } else if (config.demo_stale) {
    banner.append(
      "Демо-данные загружены давно, поэтому сроки ответа выглядят просроченными.",
      h("button", { class: "btn", type: "button", onclick: resetDemo }, "Обновить демо-данные"),
    );
  }
  banner.hidden = !banner.childNodes.length;
}

async function refresh() {
  const params = new URLSearchParams({ view: state.view, q: state.query });
  const [items, stats] = await Promise.all([api(`/api/messages?${params}`), api("/api/stats")]);
  state.items = items;
  state.stats = stats;
  renderStats();
  renderTabs();
  renderList();
}

async function select(id) {
  state.selectedId = id;
  renderList();
  const card = await run(null, () => api(`/api/messages/${id}`));
  if (card && state.selectedId === id) {
    state.card = card;
    renderDetail();
  }
}

/** После действия с карточкой: показать её новое состояние и обновить список с показателями. */
async function applyCard(card) {
  state.card = card;
  state.selectedId = card.id;
  renderDetail();
  await refresh();
}

// --- Показатели и список ------------------------------------------------------------------------

function renderStats() {
  const s = state.stats;
  const accept = s.draft_accept_rate === null ? "—" : `${Math.round(s.draft_accept_rate * 100)}%`;
  const response = s.avg_lead_response_minutes === null ? "—" : fmtDuration(s.avg_lead_response_minutes);
  const tiles = [
    ["Обращений", s.total, `из них спам: ${s.spam_filtered}`],
    ["Ждут ответа", s.waiting, s.overdue ? `просрочен срок: ${s.overdue}` : "все в срок", s.overdue ? "bad" : "ok"],
    ["Горячие лиды", s.hot_waiting, "ждут звонка менеджера"],
    ["Ответ на заявку", response, "в среднем до первого ответа"],
    ["Черновики без правок", accept, "ответы, одобренные как есть"],
    ["Экономия времени", fmtDuration(s.time_saved_minutes), `оценка: ${s.manual_minutes_per_message} мин на обращение`],
  ];
  $("#stats").replaceChildren(
    ...tiles.map(([label, value, note, tone]) =>
      h("div", { class: "tile" },
        h("div", { class: "tile-label" }, label),
        h("div", { class: "tile-value" }, value),
        h("div", { class: `tile-note ${tone || ""}` }, note),
      ),
    ),
  );
}

function renderTabs() {
  $("#tabs").replaceChildren(
    ...VIEWS.map(([view, label, statKey]) =>
      h("button", {
        class: "tab",
        type: "button",
        "aria-pressed": String(state.view === view),
        onclick: () => { state.view = view; refresh().catch((error) => toast(error.message, true)); },
      }, label, statKey ? h("span", { class: "count" }, state.stats[statKey]) : null),
    ),
  );
}

function renderList() {
  const list = $("#items");
  if (!state.items.length) {
    const text = state.query ? "Ничего не найдено" : "Здесь пока пусто";
    list.replaceChildren(h("li", { class: "empty" }, text));
    return;
  }
  list.replaceChildren(...state.items.map((card) => {
    const sla = slaInfo(card);
    const classes = ["item", card.id === state.selectedId ? "selected" : "", card.status !== "new" ? "done" : ""];
    return h("li", {
      class: classes.join(" "),
      tabindex: "0",
      onclick: () => select(card.id),
      onkeydown: (event) => { if (event.key === "Enter") select(card.id); },
    },
      h("div", { class: "item-top" },
        h("span", { class: `badge cat-${card.category}` }, card.category_label),
        card.priority === "hot" && card.status === "new" ? h("span", { class: "flag hot" }, card.category === "lead" ? `горячий · ${card.lead_score}` : "срочно") : null,
        card.needs_review && card.status === "new" ? h("span", { class: "flag review" }, "на проверку") : null,
        h("time", { datetime: card.received_at }, fmtWhen(card.received_at)),
      ),
      h("div", { class: "item-summary" }, card.analysis.extraction.summary),
      h("div", { class: "item-meta" },
        h("span", { class: "who" }, `${card.sender} · ${card.channel_label}`),
        sla ? h("span", { class: `sla ${sla.tone}` }, sla.text)
          : h("span", { class: `flag status ${card.status}` }, STATUS[card.status]),
      ),
    );
  }));
}

// --- Карточка обращения -------------------------------------------------------------------------

function fact(label, value) {
  const empty = value === null || value === undefined || value === "";
  return [h("dt", null, label), h("dd", { class: empty ? "none" : null }, empty ? "не указано" : value)];
}

function renderDetail() {
  const card = state.card;
  const root = $("#detail");
  if (!card) {
    root.replaceChildren(h("div", { class: "placeholder" }, "Выберите обращение в списке слева"));
    return;
  }
  const { extraction, score, routing, meta, checks } = card.analysis;
  const sla = slaInfo(card);

  const categorySelect = h("select", {
    "aria-label": "Категория",
    onchange: (event) => run(event.target, async () => {
      const updated = await api(`/api/messages/${card.id}/category`, { method: "POST", body: { category: event.target.value } });
      toast("Категория исправлена, маршрут пересчитан");
      await applyCard(updated);
    }),
  }, Object.entries(state.config.categories).map(([code, label]) =>
    h("option", { value: code, selected: code === card.category }, label)));

  const confidence = Math.round(extraction.confidence * 100);
  const contact = extraction.contact;
  const engineLine = meta.engine === "claude"
    ? `${meta.model} · ${numberFmt.format(meta.latency_ms)} мс · ${numberFmt.format(meta.input_tokens || 0)} → ${numberFmt.format(meta.output_tokens || 0)} токенов` + (meta.cost_usd ? ` · $${meta.cost_usd.toFixed(4)}` : "")
    : `Разобрано правилами, без LLM · ${meta.latency_ms} мс`;

  const head = h("header", { class: "d-head" },
    h("div", { class: "d-tags" },
      h("span", { class: `badge cat-${card.category}` }, card.category_label),
      card.priority === "hot" ? h("span", { class: "flag hot" }, "высокий приоритет") : null,
      card.needs_review ? h("span", { class: "flag review" }, "на проверку") : null,
      h("span", { class: `flag status ${card.status}` }, STATUS[card.status]),
      sla ? h("span", { class: `sla ${sla.tone}` }, sla.text) : null,
    ),
    h("h2", null, extraction.summary),
    h("p", { class: "d-sub" }, `${card.sender} · ${card.channel_label} · получено ${fmtWhen(card.received_at)}`),
  );

  const message = h("section", { class: "card" },
    h("h3", null, "Обращение"),
    card.subject ? h("p", { class: "msg-subject" }, card.subject) : null,
    h("p", { class: "msg-body" }, card.body),
  );

  const analysis = h("section", { class: "card" },
    h("h3", null, "Разбор"),
    h("dl", { class: "facts" },
      h("dt", null, "Категория"), h("dd", null, categorySelect),
      fact("Срочность", URGENCY[extraction.urgency]),
      fact("Имя", contact.name),
      fact("Телефон", contact.phone),
      fact("E-mail", contact.email),
      fact("Компания", contact.company),
      fact("Потребность", extraction.interest),
      fact("Бюджет", extraction.budget_rub ? `${numberFmt.format(extraction.budget_rub)} ₽` : null),
      fact("Срок", extraction.deadline),
      h("dt", null, "Уверенность"),
      h("dd", { class: "meter" },
        h("span", { class: `meter-bar ${confidence < 60 ? "low" : ""}` }, h("i", { style: `width:${confidence}%` })),
        `${confidence}%`,
      ),
    ),
    checks.length ? h("ul", { class: "checks" }, checks.map((text) => h("li", null, text))) : null,
    meta.note ? h("p", { class: "note" }, meta.note) : null,
    h("p", { class: "engine-line" }, engineLine),
  );

  const route = h("section", { class: "card" },
    h("h3", null, "Маршрут и приоритет"),
    h("div", { class: "route" },
      card.category === "lead"
        ? h("div", { class: `score ${card.priority === "hot" ? "hot" : ""}`, style: `--v:${score.value}`, title: "Оценка лида из 100" }, h("span", null, score.value))
        : null,
      h("div", { class: "route-lines" },
        h("div", { class: "route-who" }, routing.assignee ? `${routing.queue} → ${routing.assignee}` : routing.queue),
        h("div", null, routing.sla_minutes ? `Срок ответа: ${fmtDuration(routing.sla_minutes)}` : "Ответ не требуется"),
        card.corrected_from ? h("div", { class: "note" }, `Категорию исправил оператор (было: ${state.config.categories[card.corrected_from]})`) : null,
      ),
    ),
    score.reasons.length ? h("ul", { class: "reasons" }, score.reasons.map((reason) => h("li", null, reason))) : null,
  );

  const log = h("section", { class: "card" },
    h("h3", null, "Журнал"),
    h("ul", { class: "log" }, card.events.map((event) =>
      h("li", { class: `ev-${event.kind}` },
        h("span", { class: "dot" }),
        h("time", { datetime: event.ts }, timeFmt.format(new Date(event.ts))),
        h("span", null, event.detail),
      ))),
  );

  root.replaceChildren(head, h("div", { class: "d-grid" },
    h("div", { class: "col" }, message, renderReply(card)),
    h("div", { class: "col" }, analysis, route, log),
  ));
  root.scrollTop = 0;
}

function renderReply(card) {
  const draft = card.analysis.extraction.suggested_reply;
  if (card.status === "answered") {
    return h("section", { class: "card reply" },
      h("h3", null, "Ответ клиенту"),
      h("p", { class: "reply-sent" }, card.reply_text),
      h("p", { class: "note" }, `Отправлено ${fmtWhen(card.answered_at)}`),
    );
  }
  const reanalyze = h("button", {
    class: "btn ghost",
    type: "button",
    onclick: (event) => run(event.target, async () => {
      const updated = await api(`/api/messages/${card.id}/reanalyze`, { method: "POST" });
      toast("Обращение разобрано заново");
      await applyCard(updated);
    }),
  }, "Разобрать заново");

  if (card.status === "archived") {
    return h("section", { class: "card reply" },
      h("h3", null, "Ответ клиенту"),
      h("p", { class: "note" }, card.category === "spam"
        ? "Спам отфильтрован автоматически, ответ не требуется. Если это ошибка, исправьте категорию в блоке «Разбор»."
        : "Обращение в архиве."),
      h("div", { class: "reply-actions" }, reanalyze),
    );
  }

  const textarea = h("textarea", { "aria-label": "Черновик ответа" });
  textarea.value = draft;
  const send = h("button", {
    class: "btn primary",
    type: "button",
    onclick: (event) => run(event.target, async () => {
      const updated = await api(`/api/messages/${card.id}/reply`, { method: "POST", body: { text: textarea.value } });
      toast("Ответ отправлен");
      await applyCard(updated);
    }),
  }, "Отправить ответ");
  const archive = h("button", {
    class: "btn ghost",
    type: "button",
    onclick: (event) => run(event.target, async () => {
      const updated = await api(`/api/messages/${card.id}/archive`, { method: "POST" });
      await applyCard(updated);
    }),
  }, "В архив");

  return h("section", { class: "card reply" },
    h("h3", null, "Черновик ответа"),
    textarea,
    h("div", { class: "reply-actions" }, send, archive, reanalyze),
    h("p", { class: "note" }, "Ответ уходит клиенту только после одобрения человеком. В демо отправка имитируется: текст сохраняется в карточке."),
  );
}

// --- Действия в шапке -----------------------------------------------------------------------------

async function showIncoming(card) {
  state.view = card.status === "archived" ? "archived" : "waiting";
  state.query = "";
  $("#search").value = "";
  await applyCard(card);
}

async function resetDemo(event) {
  await run(event ? event.target : null, async () => {
    await api("/api/demo/reset", { method: "POST" });
    state.card = null;
    state.selectedId = null;
    await loadConfig();
    await refresh();
    renderDetail();
    toast("Демо-данные обновлены");
  });
}

function setupActions() {
  $("#btn-simulate").addEventListener("click", (event) => run(event.target, async () => {
    const card = await api("/api/demo/simulate", { method: "POST" });
    toast(`Новое обращение: ${card.category_label.toLowerCase()}`);
    await showIncoming(card);
  }));
  $("#btn-reset").addEventListener("click", resetDemo);

  const dialog = $("#dlg-new");
  const form = $("#form-new");
  const error = $("#new-error");
  $("#btn-new").addEventListener("click", () => { error.hidden = true; dialog.showModal(); });
  $("#new-cancel").addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    const submit = $("#new-submit");
    submit.disabled = true;
    submit.textContent = "Разбираю…";
    try {
      const card = await api("/api/messages", {
        method: "POST",
        body: { channel: data.channel, sender: data.sender.trim(), subject: data.subject.trim() || null, body: data.body.trim() },
      });
      dialog.close();
      form.reset();
      await showIncoming(card);
    } catch (problem) {
      error.textContent = problem.message;
      error.hidden = false;
    } finally {
      submit.disabled = false;
      submit.textContent = "Разобрать";
    }
  });

  let searchTimer;
  $("#search").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = event.target.value;
      refresh().catch((problem) => toast(problem.message, true));
    }, 250);
  });
}

async function start() {
  setupActions();
  renderDetail();
  try {
    await loadConfig();
    await refresh();
    const params = new URLSearchParams(location.search);
    const wanted = Number(params.get("open"));
    const first = state.items.find((card) => card.id === wanted) || state.items[0];
    if (first) await select(first.id);
  } catch (error) {
    toast(`Не удалось загрузить данные: ${error.message}`, true);
  }
  // Сроки ответа «тикают»: раз в полминуты обновляем список и показатели.
  setInterval(() => { refresh().catch(() => {}); }, 30000);
}

start();
