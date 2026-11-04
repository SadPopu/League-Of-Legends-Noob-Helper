const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
const num = (x, d = 1) => (x == null ? "–" : Number(x).toFixed(d));
const icon = (c, cls = "ico") => (c && c.icon ? `<img class="${cls}" src="${c.icon}" alt="${esc(c.name)}" title="${esc(c.name)}">` : `<span class="${cls} blank"></span>`);
const POS_PT = { TOP: "Top", JUNGLE: "Jungle", MIDDLE: "Mid", BOTTOM: "ADC", UTILITY: "Support", "": "—" };
const getJSON = (url, opts) => fetch(url, opts).then((r) => r.json());
const fmtK = (x) => (x == null ? "–" : x >= 1000 ? `${(x / 1000).toFixed(1)}k` : `${Math.round(x)}`);
const fmtN = (x) => (x == null ? "–" : Number(x).toLocaleString("en-GB"));
const mins = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
const ago = (ts) => {
  if (!ts) return "never";
  const h = (Date.now() - ts) / 3600000;
  return h < 1 / 60 ? "just now" : h < 1 ? `${Math.round(h * 60)} min ago` : h < 24 ? `${Math.round(h)} h ago`
    : h < 24 * 30 ? `${Math.round(h / 24)} days ago` : new Date(ts).toLocaleDateString("en-GB");
};
const isActive = (tab) => document.body.dataset.tab === tab;

// ---------------------------------------------------------------- abas
function showTab(tab) {
  document.body.dataset.tab = tab;
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${tab}`));
  if (tab === "profile") { loadProfile(); loadHistory(true); }
  if (tab === "mains") loadMainsList();
  if (tab === "data") loadData();
  if (tab !== "cs") $("#sim-panel").hidden = true;
}
$$("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------------------------------------------------------------- websocket
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
  ws.onclose = () => setTimeout(connect, 2000);
}
connect();

function handle(msg) {
  switch (msg.type) {
    case "status": return renderStatus(msg);
    case "champselect": return renderChampSelect(msg);
    case "picks": return renderPicks(msg);
    case "counters": return renderCounters(msg);
    case "build": return renderBuild(msg);
    case "mains": return renderCsMains(msg);
    case "mains_updated":
      if (isActive("mains") && mainsState.selected === msg.champion) openMains(msg.champion, true);
      return;
    case "ingame": return showIngame();
    case "champselect_end": return resetChampSelect();
    case "history_updated":
      if (isActive("profile")) { loadProfile(); loadHistory(true); }
      return;
    case "postgame":
      // jogo acabado e analisado: abre-o no histórico
      hist.openAfterLoad = msg.report?.match_id;
      showTab("profile");
      return;
  }
}

// ---------------------------------------------------------------- estado e recolhas (jobs)
const JOB_UNITS = { sync: "new games", meta: "new games", mastery: "players checked", mains_games: "new games" };
const STEP_TXT = {
  players: "finding top players", games: "downloading games", "new games": "new games", "old game timelines": "adding details to older games",
  masteries: "checking players", "listing games": "looking for new games",
};
const stepText = (s) => STEP_TXT[s] || s.replace(/^main (\d+)\/(\d+) games$/, "player $1 of $2");
const lastJobRunning = {};
let JOBS = {};

function jobView(name) {
  const j = JOBS[name];
  if (!j) return "";
  if (j.running) {
    const parts = Object.entries(j.parts || {});
    const done = parts.reduce((s, [, p]) => s + p.done, 0), total = parts.reduce((s, [, p]) => s + p.total, 0);
    const lines = parts.map(([k, p]) => `${k !== "_" ? `${k}: ` : ""}${stepText(p.step)} ${p.done}/${p.total}`);
    return `<div class="bar ${total ? "" : "indet"}"><span style="width:${total ? Math.round((100 * done) / total) : 0}%"></span></div>
      <small class="muted">${esc(lines.join(" · ") || "Starting…")}</small>`;
  }
  if (j.stopped) return `<small class="muted">Stopped · ${fmtN(j.count || 0)} ${JOB_UNITS[name] || ""} kept</small>`;
  if (j.ok === false) return `<small class="bad">Something went wrong: ${esc((j.result || "").replace(/^Error: /, ""))}</small>`;
  return `<small class="good">✓ Done${j.count != null ? ` · ${fmtN(j.count)} ${JOB_UNITS[name] || ""}` : ""}</small>`;
}
function renderJobs() {
  $$("[data-jobview]").forEach((el) => (el.innerHTML = jobView(el.dataset.jobview)));
  $$("[data-job]").forEach((b) => (b.disabled = !!JOBS[b.dataset.job]?.running));
  const stop = $("#collect-stop");
  if (stop) stop.hidden = !JOBS.meta?.running;
}

function renderStatus(s) {
  const lcu = $("#st-lcu");
  lcu.querySelector("span").textContent = s.lcu ? "Client connected" : "Client offline";
  lcu.className = "pill " + (s.lcu ? "ok" : "off");
  const phase = { ChampSelect: "Champ select", InProgress: "In game", Lobby: "Lobby", Matchmaking: "In queue",
    ReadyCheck: "Match found", EndOfGame: "Post-game", PreEndOfGame: "Post-game", GameStart: "Loading" }[s.phase];
  $("#st-phase").textContent = phase || "";
  if (s.version) DD_VERSION = s.version;
  if (!s.riot_key) $("#st-phase").textContent = "RIOT_API_KEY missing in .env";
  JOBS = s.jobs || {};
  renderJobs();
  for (const name of Object.keys(JOB_UNITS)) {
    const j = JOBS[name];
    // quando uma recolha acaba, atualiza o que estiver aberto
    if (j && !j.running && lastJobRunning[name]) {
      if (isActive("data")) loadData();
      if (isActive("profile")) { loadProfile(); loadHistory(true); if (!$("#prof-champs").hidden) loadStats(); }
      if (isActive("mains")) {
        loadMainsList();
        if (mainsState.selected) openMains(mainsState.selected, true);
      }
    }
    lastJobRunning[name] = !!j?.running;
  }
}

// ---------------------------------------------------------------- champ select
let csWasLocked = false;
let buildChamp = null; // boneco da build no ecrã
let csEnemyRole = {}; // nome do boneco inimigo -> posição provável
let csLast = null;

function slotHtml({ champ, label, name, me, hover }) {
  const bg = champ?.icon ? `style="background-image:url('${champ.icon}')"` : "";
  return `<li class="slot ${champ ? "" : "empty"} ${me ? "me" : ""} ${hover ? "hover" : ""}">
    <div class="portrait" ${bg}></div>
    <div class="who"><small>${esc(label)}</small><strong>${esc(name)}</strong>${hover ? `<em>hovering</em>` : ""}</div>
  </li>`;
}
const banRow = (bans) => Array.from({ length: 5 }, (_, i) => bans[i]
  ? `<span class="ban" style="background-image:url('${bans[i].icon}')" title="${esc(bans[i].name)}"></span>`
  : `<span class="ban none"></span>`).join("");

function renderSides(cs) {
  const allies = cs ? cs.allies : [];
  $("#allies").innerHTML = Array.from({ length: 5 }, (_, i) => {
    const a = allies[i];
    if (!a) return slotHtml({ label: "", name: "" });
    return slotHtml({
      champ: a.champion, me: a.me, hover: a.champion && !a.locked,
      label: `${POS_PT[a.position] ?? a.position}${a.me ? " · You" : ""}`,
      name: a.champion?.name || (a.me ? "Your pick" : "Picking…"),
    });
  }).join("");
  const enemies = cs ? cs.enemies : [];
  $("#enemies").innerHTML = Array.from({ length: 5 }, (_, i) => {
    const c = enemies[i];
    return c ? slotHtml({ champ: c, label: csEnemyRole[c.name] ? `${POS_PT[csEnemyRole[c.name]]} (likely)` : "", name: c.name })
      : slotHtml({ label: "", name: "" });
  }).join("");
  // sessões antigas sem bans por equipa: mostra todos do lado aliado
  const allyBans = cs ? (cs.ally_bans || cs.bans || []) : [];
  $("#ally-bans").innerHTML = banRow(allyBans);
  $("#enemy-bans").innerHTML = banRow(cs ? cs.enemy_bans || [] : []);
}

function renderChampSelect(cs) {
  csLast = cs;
  $("#cs-idle").hidden = true;
  $("#cs-ingame").hidden = true;
  $("#cs-live").hidden = false;
  renderSides(cs);
  const me = cs.allies.find((a) => a.me);
  const hasChamp = !!me?.champion;
  $$('#center-tabs [data-sub="build"], #center-tabs [data-sub="mains"]').forEach((b) => (b.disabled = !hasChamp));
  // trocaste de boneco: mostra "a carregar" até chegar a build/mains do novo
  if (hasChamp && me.champion.id !== buildChamp) {
    buildChamp = me.champion.id;
    $("#build-title").textContent = "";
    $("#build").innerHTML = `<div class="loading">Loading the build for ${esc(me.champion.name)}…</div>`;
    $("#cs-mains").innerHTML = `<div class="loading">Loading ${esc(me.champion.name)} mains…</div>`;
  }
  if (cs.locked && !csWasLocked) setCenter("build");
  csWasLocked = cs.locked;
}

// jogo a começar: atalho para os mains do boneco que escolheste
function showIngame() {
  const champ = csLast?.allies.find((a) => a.me)?.champion;
  const btn = $("#ingame-mains");
  btn.hidden = !champ;
  if (champ) {
    btn.textContent = `★ See ${champ.name} mains`;
    btn.onclick = () => { showTab("mains"); openMains(champ.id); };
  }
  $("#cs-ingame").hidden = false;
}

function resetChampSelect() {
  csLast = null; csEnemyRole = {}; csWasLocked = false; buildChamp = null;
  $("#cs-live").hidden = true; $("#cs-idle").hidden = false; $("#cs-ingame").hidden = true;
  $("#cs-mains").innerHTML = ""; $("#build").innerHTML = "";
  setCenter("mine");
  renderSides(null);
}

const CENTER_TITLES = { mine: "Pick suggestions", counters: "Counters", build: "Build & runes", mains: "One-trick mains" };
function setCenter(sub) {
  $$("#center-tabs button").forEach((x) => x.classList.toggle("active", x.dataset.sub === sub));
  $("#picks").hidden = sub !== "mine";
  $("#counters").hidden = sub !== "counters";
  $("#build-pane").hidden = sub !== "build";
  $("#cs-mains").hidden = sub !== "mains";
  $("#picks-warning").hidden = sub !== "mine" || !$("#picks-warning").textContent;
  $("#center-title").textContent = CENTER_TITLES[sub];
}
$$("#center-tabs button").forEach((b) => b.addEventListener("click", () => setCenter(b.dataset.sub)));

function renderPicks(p) {
  $("#picks-pos").textContent = [
    p.position ? POS_PT[p.position] : "",
    p.lane_opponent ? `vs ${p.lane_opponent.name} (likely)` : "",
  ].filter(Boolean).join(" · ");
  csEnemyRole = Object.fromEntries(Object.entries(p.enemy_roles || {}).map(([pos, n]) => [n, pos]));
  if (csLast) renderSides(csLast);
  $("#picks-warning").textContent = p.warning || "";
  $("#picks-warning").hidden = !p.warning || $("#picks").hidden;
  $("#picks").innerHTML = p.candidates.length
    ? p.candidates.map((c, i) => pickItem(c, i, `${c.games} games · ${pct(c.winrate)} WR${c.kda != null ? ` · KDA ${num(c.kda)}` : ""}`,
        c.comfort === false ? ` <span class="tag few">few games</span>` : "", "fit")).join("")
    : `<li class="muted">No data yet: check for new games in the Profile tab and get high elo games in the Data tab.</li>`;
  if (!p.from_pool && p.candidates.length)
    $("#picks").insertAdjacentHTML("afterbegin", `<p class="muted">You have no games in this role yet: suggestions are based on the high elo meta.</p>`);
}

function pickItem(c, i, sub, tag = "", scoreLabel = "score") {
  return `
    <li>
      <span class="rank-n">${i + 1}</span>
      ${icon({ icon: c.icon, name: c.name }, "ico big")}
      <div>
        <strong>${esc(c.name)}</strong>${tag}
        <span class="sub">${sub}</span>
        <ul class="reasons">${c.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      </div>
      <div class="fit"><b>${pct(c.score)}</b><small>${scoreLabel}</small></div>
    </li>`;
}

function renderCounters(p) {
  $("#counters").innerHTML = p.candidates.length
    ? p.candidates.map((c, i) => pickItem(c, i, `${fmtN(c.games)} high elo games · ${pct(c.winrate)} WR`,
        c.in_pool ? ` <span class="tag">in your pool</span>` : "")).join("")
    : `<li class="muted">Not enough high elo data for this role yet. Get more games in the Data tab.</li>`;
  if (!p.candidates.length) return;
  const note = !p.enemies?.length
    ? "No enemy picks yet: sorted by overall high elo win rate."
    : `Against the enemies picked so far: ${p.enemies.join(", ")}.` + (p.lane_opponent ? "" : " Your lane opponent is not known yet.");
  $("#counters").insertAdjacentHTML("afterbegin", `<p class="muted">${esc(note)}</p>`);
}

// ---------------------------------------------------------------- árvore de runas
let RUNES = null;
getJSON("/api/runes").then((d) => (RUNES = d));

function runeTree(r) {
  if (!RUNES) return "";
  const prim = RUNES.styles.find((s) => s.id === r.primary_id);
  const sec = RUNES.styles.find((s) => s.id === r.secondary_id);
  if (!prim || !sec) return "";
  const chosen = new Set(r.perk_ids);
  const rune = (x, big) => `<img class="rune ${chosen.has(x.id) ? "on" : ""} ${big ? "keystone" : ""}" src="${x.icon}" title="${esc(x.name)}" alt="${esc(x.name)}">`;
  const tree = (style, slots, keystone) => `
    <div class="tree">
      <div class="tree-head"><img src="${style.icon}" alt=""> ${esc(style.name)}</div>
      ${slots.map((row, i) => `<div class="rune-row">${row.map((x) => rune(x, keystone && i === 0)).join("")}</div>`).join("")}
    </div>`;
  // fragmentos: cada linha marca a opção escolhida nessa posição
  const shards = `
    <div class="tree shards">
      <div class="tree-head">Shards</div>
      ${RUNES.shards.map((row, i) => `<div class="rune-row">${row.map((x) =>
        `<img class="rune shard ${r.shard_ids[i] === x.id ? "on" : ""}" src="${x.icon}" title="${esc(x.name)}" alt="${esc(x.name)}">`).join("")}</div>`).join("")}
    </div>`;
  return `<div class="rune-page">${tree(prim, prim.slots, true)}${tree(sec, sec.slots.slice(1), false)}${shards}</div>`;
}

function questItemsHtml(b) {
  if (!b.quest_items?.length) return "";
  return `<h3>Quest item <small>you choose it when the support quest ends</small></h3><div class="items">${
    b.quest_items.map((it) => `
      <div class="item">${icon(it, "ico")}<span>${esc(it.name)}</span>
      <small>${pct(it.pick_rate)} of picks · ${pct(it.winrate)} WR</small></div>`).join("")
  }</div>`;
}

function buildDetailHtml(b) {
  const parts = [];
  if (b.runes) {
    const r = b.runes;
    parts.push(`<h3>Runes <small>${pct(r.winrate)} WR in ${r.games} games</small></h3>
      ${runeTree(r) || `<p><strong>${esc(r.primary)}</strong>: ${r.perks.slice(0, 4).map(esc).join(" · ")}<br>
      <strong>${esc(r.secondary)}</strong>: ${r.perks.slice(4, 6).map(esc).join(" · ")}</p>`}`);
  }
  if (b.spells) parts.push(`<h3>Summoner spells</h3><p class="spells-row">${spellIcons(b.spells)} <small class="muted">${pct(b.spells.winrate)} WR in ${b.spells.games} games</small></p>`);

  const t = b.timeline;
  parts.push(questItemsHtml(b));
  if (t) {
    if (t.skills) {
      const p = t.skills;
      parts.push(`<h3>Skills <small>${pct(p.winrate)} WR in ${p.games} games</small></h3>
        <p class="skills">Start: ${[...p.first3].map((s) => `<span class="key">${s}</span>`).join("")}
        &nbsp; Max order: ${p.priority.split(" > ").map((s) => `<span class="key">${s}</span>`).join(" › ")}</p>`);
    }
    if (t.start_items || t.item_path) {
      parts.push(`<h3>Item order <small>${esc(t.scope)} · ${t.sample} detailed games</small></h3>`);
      if (t.start_items) parts.push(`<div class="build-path"><span class="muted">Start</span>${t.start_items.items.map((it) => icon(it)).join("")}</div>`);
      if (t.item_path) parts.push(`<div class="build-path">${t.item_path.items.map((it) => `
          <div class="step">${icon(it)}<small>${esc(it.name)}<br>~${it.minute} min</small></div>`).join(`<span class="arrow">›</span>`)}
          <small class="muted">${pct(t.item_path.winrate)} WR in ${t.item_path.games} games</small></div>`);
    }
    if (t.jungle) {
      const j = t.jungle;
      // jogos antigos guardaram os campos inimigos como "(inimigo)"
      const d = (arr) => arr.map((x) => `${esc(String(x.name).replace("(inimigo)", "(enemy)"))} <small class="muted">${pct(x.pct)}</small>`).join(" · ") || "–";
      parts.push(`<h3>Jungle pathing <small>${j.sample} games</small></h3>
        ${clearMap(j)}
        ${jungleMap(j) ? `<details class="real-routes"><summary>Real routes from games (first 7 min)</summary>${jungleMap(j)}</details>` : ""}
        <ol class="pathing">
          <li><strong>Starts at:</strong> ${d(j.start)}</li>
          <li><strong>At 2:00:</strong> ${d(j.at2)}</li>
          <li><strong>At 3:00 (end of clear), side:</strong> ${d(j.side3)}</li>
          <li><strong>First gank/kill:</strong> ${d(j.first_gank)}${j.first_gank_minute ? ` <span class="muted">(~${j.first_gank_minute} min)</span>` : ""}</li>
        </ol>`);
    }
  }
  return parts.join("");
}

function renderBuild(b) {
  buildChamp = b.champion.id;
  $$('#center-tabs [data-sub="build"], #center-tabs [data-sub="mains"]').forEach((x) => (x.disabled = false));
  $("#build-title").textContent = `${b.champion.name}${b.locked ? "" : " (hovering)"} · ${b.scope} · based on ${fmtN(b.sample)} high elo games`;
  const parts = [];
  if (b.matchup) {
    const m = b.matchup;
    parts.push(`<div class="matchup">${icon(b.champion, "ico big")}<span>vs</span>${icon(b.opponent, "ico big")}
      <div><strong>${esc(b.champion.name)} vs ${esc(b.opponent.name)}</strong><br>
      High elo: ${m.meta_games ? `${pct(m.meta_winrate)} WR (${m.meta_games} games)` : "no data"} ·
      You: ${m.my_games ? `${pct(m.my_winrate)} WR (${m.my_games} games)` : "never played"}</div></div>`);
  }
  if (!b.sample) parts.push(`<p class="muted">No high elo games with this champion yet. Get more games in the Data tab.</p>`);
  parts.push(buildDetailHtml(b));
  if (b.core_items.length) {
    parts.push(`<h3>Most used items</h3><div class="items">${
      [...(b.boots ? [{ ...b.boots, winrate: null }] : []), ...b.core_items].map((it) => `
        <div class="item">${icon(it, "ico")}<span>${esc(it.name)}</span>
        <small>${pct(it.pick_rate)} of games${it.winrate != null ? ` · ${pct(it.winrate)} WR` : ""}</small></div>`).join("")
    }</div>`);
  }
  if (b.situational.length)
    parts.push(`<h3>Situational (vs this team)</h3><ul class="reasons">${b.situational.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`);
  $("#build").innerHTML = parts.join("");
}

// ---------------------------------------------------------------- simulador
getJSON("/api/champions").then((names) => {
  $("#champ-list").innerHTML = names.map((n) => `<option value="${esc(n)}">`).join("");
});
$("#sim-fab").addEventListener("click", () => ($("#sim-panel").hidden = !$("#sim-panel").hidden));
$("#sim-close").addEventListener("click", () => ($("#sim-panel").hidden = true));
const splitList = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
$("#sim-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const body = {
    position: f.get("position"),
    champion: f.get("champion"),
    locked: f.get("locked") === "on",
    allies: splitList(f.get("allies")).map((a) => {
      const [position, champion = ""] = a.split(":").map((x) => x.trim());
      return { position: position.toUpperCase(), champion };
    }),
    enemies: splitList(f.get("enemies")),
    bans: splitList(f.get("bans")),
    enemy_bans: splitList(f.get("enemy_bans")),
  };
  await fetch("/api/simulate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
});
$("#sim-end").addEventListener("click", () => fetch("/api/simulate/end", { method: "POST" }));

// ---------------------------------------------------------------- perfil
const TIER_NAME = (t) => t.charAt(0) + t.slice(1).toLowerCase();
const APEX = ["MASTER", "GRANDMASTER", "CHALLENGER"];
const emblem = (tier) => `https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-shared-components/global/default/${tier.toLowerCase()}.png`;
const rankName = (r) => `${TIER_NAME(r.tier)}${APEX.includes(r.tier) ? "" : ` ${r.division}`}`;

async function loadProfile() {
  const p = await getJSON("/api/profile").catch(() => ({ ok: false, error: "Could not reach the app." }));
  if (!p.ok) {
    $("#profile-card").innerHTML = `<p class="muted">${esc(p.error)}</p>`;
    return;
  }
  const [name, tag] = p.name.split("#");
  const rankBox = (label, r) => `
    <div class="rank-box">
      ${r ? `<img src="${emblem(r.tier)}" alt="" onerror="this.style.visibility='hidden'">` : ""}
      <div><small>${label}</small>
        ${r ? `<strong>${rankName(r)} · ${r.lp} LP</strong>
          <span class="wl">${r.wins}W ${r.losses}L · <span class="${r.winrate >= 0.5 ? "good" : "bad"}">${pct(r.winrate)}</span></span>`
          : `<strong class="muted">Unranked</strong>`}
      </div>
    </div>`;
  $("#profile-card").innerHTML = `
    <div class="profile-id">
      <div class="avatar">${p.icon ? `<img src="${p.icon}" alt="">` : ""}${p.level ? `<span>${p.level}</span>` : ""}</div>
      <div><h2>${esc(name)} <small class="muted">#${esc(tag || "")}</small></h2></div>
    </div>
    <div class="ranks">${rankBox("Ranked Solo/Duo", p.ranks.solo)}${rankBox("Ranked Flex", p.ranks.flex)}</div>`;
  PROFILE = p;
  loadLp();
}
let PROFILE = null;

// ---------------------------------------------------------------- gráfico de LP
const lp = { queue: "solo", days: 7 };
const ROMAN = ["IV", "III", "II", "I"];
const TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND"];
function lpLabel(v, withLp = true) {
  if (v >= 2800) return withLp ? `Master+ ${Math.round(v - 2800)} LP` : `${Math.round(v - 2800)} LP`;
  const t = Math.max(0, Math.floor(v / 400)), d = Math.floor((v % 400) / 100);
  return `${TIER_NAME(TIERS[t] || "IRON")} ${ROMAN[d]}${withLp ? ` ${Math.round(v % 100)} LP` : ""}`;
}

document.querySelectorAll("#lp-queue button").forEach((b) => b.addEventListener("click", () => {
  lp.queue = b.dataset.q;
  $$("#lp-queue button").forEach((x) => x.classList.toggle("active", x === b));
  loadLp();
}));
document.querySelectorAll("#lp-range button").forEach((b) => b.addEventListener("click", () => {
  lp.days = +b.dataset.d;
  $$("#lp-range button").forEach((x) => x.classList.toggle("active", x === b));
  loadLp();
}));

async function loadLp() {
  const data = await getJSON(`/api/lp?queue=${lp.queue}`);
  const all = data.points || [];
  const el = $("#lp-chart");
  if (!all.length) {
    el.innerHTML = `<div class="empty">${PROFILE?.ranks?.[lp.queue] ? "No rank history yet." : "Unranked in this queue this season."}</div>`;
    $("#lp-note").textContent = "";
    return;
  }
  const now = Date.now();
  const since = lp.days ? now - lp.days * 86400000 : all[0].t;
  // o valor no início do período = último ponto antes dele
  const before = [...all].reverse().find((p) => p.t < since);
  const pts = [...(before ? [{ ...before, t: since, edge: true }] : []), ...all.filter((p) => p.t >= since)];
  pts.push({ ...pts[pts.length - 1], t: now, edge: true });

  const W = 700, H = 200, padL = 78, padR = 10, padT = 10, padB = 22;
  const vs = pts.map((p) => p.v);
  let lo = Math.min(...vs), hi = Math.max(...vs);
  if (hi - lo < 60) { const m = (hi + lo) / 2; lo = m - 30; hi = m + 30; }
  lo -= 10; hi += 10;
  const x = (t) => padL + ((t - since) / Math.max(now - since, 1)) * (W - padL - padR);
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);
  // linhas de divisão (cada 100 LP) com o nome; se forem muitas, só os tiers
  const step = hi - lo > 900 ? 400 : 100;
  const grid = [];
  for (let g = Math.ceil(lo / step) * step; g <= hi; g += step)
    grid.push(`<line x1="${padL}" x2="${W - padR}" y1="${y(g)}" y2="${y(g)}"/><text x="${padL - 6}" y="${y(g) + 3}" text-anchor="end">${lpLabel(g, false)}</text>`);
  const days = lp.days || Math.ceil((now - since) / 86400000);
  for (let i = 0; i <= 4; i++) {
    const t = since + ((now - since) * i) / 4;
    grid.push(`<text x="${x(t)}" y="${H - 4}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${i === 4 ? "now" : new Date(t).toLocaleDateString("en-GB", { day: "numeric", month: "short" })}</text>`);
  }
  // segmentos: tracejado onde o valor é estimado
  const segs = [];
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1], b = pts[i];
    segs.push(`<line class="line ${a.exact && b.exact ? "" : "est"}" x1="${x(a.t)}" y1="${y(a.v)}" x2="${x(b.t)}" y2="${y(b.v)}"/>`);
  }
  const area = `M${x(pts[0].t)},${H - padB} ` + pts.map((p) => `L${x(p.t)},${y(p.v)}`).join(" ") + ` L${x(now)},${H - padB} Z`;
  const dots = pts.filter((p) => !p.edge && (p.win != null || p.exact)).map((p) =>
    `<circle class="pt ${p.win == null ? "x" : p.win ? "w" : "l"}" cx="${x(p.t)}" cy="${y(p.v)}" r="${p.win == null ? 3.5 : 3}">
      <title>${new Date(p.t).toLocaleString("en-GB")} · ${p.win == null ? "rank recorded" : `${p.win ? "Win" : "Loss"} · ${esc(p.champion || "")}`} · ${lpLabel(p.v)}${p.exact ? "" : " (estimated)"}</title></circle>`).join("");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <defs><linearGradient id="lpfill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#f0c96a" stop-opacity=".22"/><stop offset="1" stop-color="#f0c96a" stop-opacity="0"/></linearGradient></defs>
      <g class="grid">${grid.join("")}</g><path class="area" d="${area}"/>${segs.join("")}${dots}
    </svg>`;
  const games = pts.filter((p) => p.win != null && !p.edge);
  const w = games.filter((p) => p.win).length;
  const diff = Math.round(pts[pts.length - 1].v - pts[0].v);
  el.insertAdjacentHTML("beforeend", `<div class="lp-sum">
    <span><b class="${diff >= 0 ? "good" : "bad"}">${diff >= 0 ? "+" : ""}${diff} LP</b> <span class="muted">in the last ${lp.days ? `${days} days` : "season"}</span></span>
    <span><b>${w}W ${games.length - w}L</b> <span class="muted">${games.length ? pct(w / games.length) : ""}</span></span>
    <span class="muted">Now: ${lpLabel(pts[pts.length - 1].v)}</span></div>`);
  const estimated = pts.some((p) => !p.exact && !p.edge);
  $("#lp-note").textContent = estimated
    ? `Riot doesn't share LP history, so dashed parts are estimated from your wins and losses (≈ +${data.gain} / ${data.loss} LP per game). The line becomes exact as the app records your rank after each game.`
    : "";
}

// ---------------------------------------------------------------- perfil: sub-abas e estatísticas por boneco
$$("#profile-sub button").forEach((b) => b.addEventListener("click", () => {
  $$("#profile-sub button").forEach((x) => x.classList.toggle("active", x === b));
  $("#prof-games").hidden = b.dataset.view !== "games";
  $("#prof-champs").hidden = b.dataset.view !== "champs";
  if (b.dataset.view === "champs") loadStats();
}));

async function loadStats() {
  const pos = $("#stats-pos").value;
  const data = await getJSON(`/api/stats${pos ? `?position=${pos}` : ""}`);
  $("#stats tbody").innerHTML = data.champions.map((c) => `
    <tr>
      <td>${icon(c, "ico small")} ${esc(c.name)}</td>
      <td>${c.games}</td>
      <td class="${c.winrate >= 0.55 ? "good" : c.winrate < 0.45 ? "bad" : ""}">${pct(c.winrate)}</td>
      <td>${num(c.kda, 2)}</td><td>${num(c.cspm)}</td><td>${num(c.vspm, 2)}</td>
      <td>${pct(c.kp)}</td><td>${pct(c.dmg_share)}</td>
    </tr>`).join("") || `<tr><td colspan="8" class="muted">No ranked games yet. Click "Check for new games".</td></tr>`;
}
$("#stats-pos").addEventListener("change", loadStats);

$("#sync").addEventListener("click", async () => {
  const res = await getJSON("/api/sync", { method: "POST" });
  if (!res.ok && res.error) $('[data-jobview="sync"]').innerHTML = `<small class="bad">${esc(res.error)}</small>`;
});

// ---------------------------------------------------------------- histórico
const hist = { group: "all", offset: 0, openAfterLoad: null, seq: 0 };
const scoreTag = (p) => (p.score == null ? "" :
  `<span class="score-tag ${p.badge ? p.badge.toLowerCase() : ""}">${p.badge ? `${p.badge} ` : ""}${num(p.score)}</span>`);
const itemsRow = (items, cls = "ico small") => items.map((it) => (it ? icon(it, cls) : `<span class="${cls} blank"></span>`)).join("");
const GAMES = {};

$$("#hist-groups button").forEach((b) =>
  b.addEventListener("click", () => {
    $$("#hist-groups button").forEach((x) => x.classList.toggle("active", x === b));
    hist.group = b.dataset.group;
    loadHistory(true);
  })
);
$("#hist-more").addEventListener("click", () => loadHistory(false));

async function loadHistory(reset) {
  if (reset) hist.offset = 0;
  const seq = ++hist.seq;
  const data = await getJSON(`/api/history?group=${hist.group}&offset=${hist.offset}&limit=20`);
  if (seq !== hist.seq) return; // entretanto pediu-se outra página/filtro
  const total = Object.values(data.counts).reduce((a, b) => a + b, 0);
  $$("#hist-groups button").forEach((b) => {
    const n = b.dataset.group === "all" ? total : data.counts[b.dataset.group] || 0;
    b.querySelector("small").textContent = n ? `(${n})` : "";
  });
  if (reset) {
    renderHistSummary(data);
    $("#hist-list").innerHTML = "";
  }
  data.games.forEach((g) => (GAMES[g.match_id] = g));
  $("#hist-list").insertAdjacentHTML("beforeend", data.games.map(gameRow).join("") ||
    (reset ? `<p class="empty">No games here yet. Click "Check for new games".</p>` : ""));
  hist.offset += data.games.length;
  $("#hist-more").hidden = data.games.length < 20;
  if (hist.openAfterLoad && GAMES[hist.openAfterLoad]) {
    const id = hist.openAfterLoad;
    hist.openAfterLoad = null;
    toggleGame(id);
  }
}

function renderHistSummary({ summary: s, top_champions: top }) {
  if (!s.games) { $("#hist-summary").innerHTML = ""; return; }
  const kda = (s.k + s.a) / Math.max(s.d, 1);
  $("#hist-summary").innerHTML = `
    <div class="card hist-summary">
      <div class="big-stat"><strong>${s.games}</strong><small>games</small></div>
      <div class="big-stat"><strong class="${s.wins / s.games >= 0.5 ? "good" : "bad"}">${pct(s.wins / s.games)}</strong><small>${s.wins}W ${s.games - s.wins}L</small></div>
      <div class="big-stat"><strong>${num(kda, 2)}</strong><small>KDA (${num(s.k)}/${num(s.d)}/${num(s.a)})</small></div>
      <div class="big-stat"><strong>${num(s.cspm)}</strong><small>CS/min</small></div>
      <div class="big-stat"><strong>${fmtK(s.dmg)}</strong><small>avg damage</small></div>
      <div class="big-stat"><strong>${pct(s.kp)}</strong><small>kill participation</small></div>
      <div class="big-stat"><strong>${num(s.score)}</strong><small>avg score · ${s.mvp || 0} MVP · ${s.ace || 0} ACE</small></div>
      <div class="top-champs">${top.map((t) => `
        <div title="${esc(t.name)}">${icon(t, "ico")}<small>${t.g}g · <span class="${t.w / t.g >= 0.5 ? "good" : "bad"}">${pct(t.w / t.g)}</span><br>${num((t.k + t.a) / Math.max(t.d, 1), 1)} KDA</small></div>`).join("")}</div>
    </div>`;
}

function gameRow(g) {
  const m = g.me;
  const kda = (m.kills + m.assists) / Math.max(m.deaths, 1);
  return `
  <div class="game ${m.win ? "win" : "loss"}" data-id="${g.match_id}">
    <div class="game-row" onclick="toggleGame('${g.match_id}')">
      <div class="g-meta">
        <strong>${esc(g.queue)}</strong>
        <small>${ago(g.game_creation)}</small>
        <span class="${m.win ? "good" : "bad"}">${m.placement ? `#${m.placement}` : m.win ? "Victory" : "Defeat"}</span>
        <small>${mins(g.duration)}${g.has_report ? ` · <span class="analysed">analysed</span>` : ""}</small>
      </div>
      <div class="g-champ">
        <div class="champ-lvl">${icon(m.champion, "ico big")}<span>${m.level}</span></div>
        <div class="g-spells">${m.spells.map((s) => icon(s, "ico tiny")).join("")}</div>
        <div class="g-spells">${icon(m.keystone, "ico tiny")}${icon(m.sub_style ? { ...m.sub_style, name: "" } : null, "ico tiny")}</div>
      </div>
      <div class="g-kda">
        <strong>${m.kills} / <span class="bad">${m.deaths}</span> / ${m.assists}</strong>
        <small>${num(kda, 2)} KDA</small>
        ${scoreTag(m)}
      </div>
      <div class="g-stats">
        <small>CS ${m.cs} (${num(m.cs / (g.duration / 60))})</small>
        <small>KP ${pct(m.kp)}</small>
        <small>Damage ${fmtK(m.damage)}</small>
        <small>Vision ${m.vision}</small>
      </div>
      <div class="g-items">${itemsRow(m.items)}</div>
      <div class="g-teams">${g.teams.slice(0, 2).map((t) => `<div>${t.map((p) =>
        `<span class="${p.is_me ? "me" : ""}"><img src="${p.icon}" alt="">${esc(p.name)}</span>`).join("")}</div>`).join("")}</div>
      <span class="chev">›</span>
    </div>
    <div class="game-detail" hidden></div>
  </div>`;
}

// abrir um jogo = análise pós-jogo (gerada na hora se ainda não existir) + mapa + tabela
const REPORTS = {};
async function toggleGame(id) {
  const box = document.querySelector(`.game[data-id="${id}"]`);
  const el = box.querySelector(".game-detail");
  if (!el.hidden) { el.hidden = true; box.classList.remove("open"); return; }
  el.hidden = false;
  box.classList.add("open");
  if (el.dataset.loaded) return;
  el.dataset.loaded = "1";
  const g = GAMES[id];
  el.innerHTML = `
    <div class="subtabs gd-tabs">
      ${g.analysable ? `<button data-v="analysis" class="active">Analysis</button><button data-v="map">Map & timeline</button>` : ""}
      <button data-v="board" class="${g.analysable ? "" : "active"}">Scoreboard</button>
    </div>
    <div class="gd-pane" data-v="analysis" ${g.analysable ? "" : "hidden"}><div class="loading">Analysing this game…</div></div>
    <div class="gd-pane" data-v="map" hidden></div>
    <div class="gd-pane" data-v="board" ${g.analysable ? "hidden" : ""}><div class="loading">Loading…</div></div>`;
  el.querySelectorAll(".gd-tabs button").forEach((b) => b.addEventListener("click", () => {
    el.querySelectorAll(".gd-tabs button").forEach((x) => x.classList.toggle("active", x === b));
    el.querySelectorAll(".gd-pane").forEach((p) => (p.hidden = p.dataset.v !== b.dataset.v));
    if (b.dataset.v === "map") openMap(id, el);
  }));
  getJSON(`/api/history/${id}`).then((d) => (el.querySelector('.gd-pane[data-v="board"]').innerHTML = scoreboardHtml(d)));
  if (g.analysable) loadAnalysis(id, el);
}

async function loadAnalysis(id, el, force = false) {
  const pane = el.querySelector('.gd-pane[data-v="analysis"]');
  let r = force ? null : (await getJSON(`/api/postgame/${encodeURIComponent(id)}`)).report;
  if (!r || !r.detail) {
    pane.innerHTML = `<div class="loading">Analysing this game… (downloading the timeline from Riot)</div>`;
    const res = await getJSON(`/api/postgame/analyse?match_id=${encodeURIComponent(id)}`, { method: "POST" });
    if (!res.ok) { pane.innerHTML = `<p class="muted">${esc(res.error || "Could not analyse this game.")}</p>`; return; }
    r = res.report;
    GAMES[id].has_report = true;
  }
  REPORTS[id] = r;
  pane.innerHTML = analysisHtml(r);
  el.querySelector('.gd-pane[data-v="map"]').innerHTML = "";
}

function scoreboardHtml(d) {
  if (!d.ok) return `<p class="muted">No details for this game.</p>`;
  return d.teams.map((t) => `
    <div class="table-wrap"><table class="detail-table ${t.win ? "win" : "loss"}">
      <thead><tr><th>${t.win ? "Victory" : "Defeat"} · ${t.kills} kills · ${fmtK(t.gold)} gold</th>
        <th>Score</th><th>KDA</th><th>Damage</th><th>Gold</th><th>CS</th><th>Vision</th><th>Items</th></tr></thead>
      <tbody>${t.players.map((p) => `
        <tr class="${p.is_me ? "me" : ""}">
          <td class="who-cell">${icon(p.champion, "ico")}<span class="lvl">${p.level}</span>
            <span class="spells">${p.spells.map((s) => icon(s, "ico tiny")).join("")}</span>${icon(p.keystone, "ico tiny")}
            <span class="pname">${esc(p.name.split("#")[0])}<small class="muted">${POS_PT[p.position] ?? ""}</small></span></td>
          <td>${scoreTag(p)}</td>
          <td>${p.kills}/${p.deaths}/${p.assists}<small> (${pct(p.kp)})</small></td>
          <td><div class="dmg"><span style="width:${Math.round((100 * p.damage) / d.max_damage)}%"></span></div>${fmtK(p.damage)}</td>
          <td>${fmtK(p.gold)}</td>
          <td>${p.cs}</td>
          <td>${p.vision}<small> (${p.control_wards})</small></td>
          <td class="items-cell">${itemsRow(p.items, "ico tiny")}</td>
        </tr>`).join("")}</tbody>
    </table></div>`).join("");
}

// ---------------------------------------------------------------- análise pós-jogo
const PG_CATS = {
  farm: "Farming", lane: "Laning", deaths: "Deaths", vision: "Vision", wards: "Control wards",
  fights: "Fights & damage", items: "Items", compare: "Comparisons",
};
const pgCats = (() => {
  const all = Object.fromEntries(Object.keys(PG_CATS).map((k) => [k, true]));
  try { return { ...all, ...JSON.parse(localStorage.getItem("pgCats") || "{}") }; } catch { return all; }
})();

function pgScore(feedback) {
  const good = feedback.filter((f) => f.type === "good").length;
  const bad = feedback.filter((f) => f.type === "bad").length;
  return Math.round((100 * (good + 1)) / (good + bad + 2));
}

function analysisHtml(r) {
  const m = r.metrics;
  const stat = (label, val) => `<div class="stat"><small>${label}</small><strong>${val}</strong></div>`;
  const diff = (v) => (v == null ? "–" : `${v > 0 ? "+" : ""}${v}`);
  const fb = r.feedback.filter((f) => pgCats[f.cat ?? "compare"] !== false);
  return `
    <div class="an-head">
      ${icon(r.champion, "ico big")}
      <div><strong>${esc(r.champion.name)}${r.opponent ? ` vs ${esc(r.opponent.name)}` : ""}</strong><br>
        <span class="muted">${POS_PT[r.position] ?? r.position} · ${r.duration_min} min</span></div>
      <div class="score" title="Strengths vs things to improve (selected categories only)"><b>${pgScore(fb)}</b><small>game rating</small></div>
    </div>
    <div class="stats-grid">
      ${stat("KDA", `${m.kills}/${m.deaths}/${m.assists}`)}
      ${stat("CS/min", num(m.cspm))}
      ${stat("CS at 10 min", m.cs10 ?? "–")}
      ${stat("Gold at 15 vs opponent", diff(m.gold_diff15))}
      ${stat("Vision/min", num(m.vspm, 2))}
      ${stat("Control wards", m.control_wards)}
      ${stat("Kill participation", pct(m.kp))}
      ${stat("Damage share", pct(m.damage_share))}
    </div>
    <div class="pg-cats">${Object.entries(PG_CATS).map(([k, label]) =>
      `<label class="chip ${pgCats[k] ? "on" : ""}"><input type="checkbox" data-cat="${k}" ${pgCats[k] ? "checked" : ""} onchange="toggleCat(this)">${label}</label>`).join("")}</div>
    <ul class="feedback">${fb.map((f) => `<li class="${f.type}">${esc(f.text)}</li>`).join("") || `<li class="info">No category selected.</li>`}</ul>
    <div class="row"><button class="secondary" onclick="reanalyse('${r.match_id}')" title="Run the analysis again">↻ Re-analyse</button></div>`;
}

function toggleCat(input) {
  pgCats[input.dataset.cat] = input.checked;
  try { localStorage.setItem("pgCats", JSON.stringify(pgCats)); } catch {}
  $$(".game.open").forEach((g) => {
    const r = REPORTS[g.dataset.id];
    const pane = g.querySelector('.gd-pane[data-v="analysis"]');
    if (r && pane) pane.innerHTML = analysisHtml(r);
  });
}

function reanalyse(id) {
  const el = document.querySelector(`.game[data-id="${id}"] .game-detail`);
  loadAnalysis(id, el, true);
}

async function openMap(id, el) {
  const pane = el.querySelector('.gd-pane[data-v="map"]');
  if (pane.innerHTML) return;
  if (!REPORTS[id]) { pane.innerHTML = `<div class="loading">Waiting for the analysis…</div>`; setTimeout(() => (pane.innerHTML = "", openMap(id, el)), 1500); return; }
  const r = REPORTS[id];
  if (!r.detail) { pane.innerHTML = `<p class="muted">No timeline for this game. Try "Re-analyse".</p>`; return; }
  pane.innerHTML = `<div class="map-pane">${detailHtml(r)}</div>`;
  drawMap(r, pane.querySelector(".map-pane"));
}

// coordenadas do Summoner's Rift -> píxeis do minimapa (512x512)
const MAP = { minX: -120, minY: -120, maxX: 14870, maxY: 14980, size: 512 };
const mx = (x) => ((x - MAP.minX) / (MAP.maxX - MAP.minX)) * MAP.size;
const my = (y) => MAP.size - ((y - MAP.minY) / (MAP.maxY - MAP.minY)) * MAP.size;
const fmtT = (t) => `${Math.floor(t)}:${String(Math.round((t % 1) * 60)).padStart(2, "0")}`;
const OBJ_PT = {
  FIRE_DRAGON: "Infernal Drake", WATER_DRAGON: "Ocean Drake", EARTH_DRAGON: "Mountain Drake", AIR_DRAGON: "Cloud Drake",
  HEXTECH_DRAGON: "Hextech Drake", CHEMTECH_DRAGON: "Chemtech Drake", ELDER_DRAGON: "Elder Dragon",
  BARON_NASHOR: "Baron", RIFTHERALD: "Rift Herald", HORDE: "Voidgrubs", ATAKHAN: "Atakhan",
};

function detailHtml(r) {
  const d = r.detail;
  const last = d.frames.length - 1;
  return `
    <div class="detail-grid">
      <div>
        <div class="map-wrap"><svg class="minimap" viewBox="0 0 512 512"></svg></div>
        <div class="row map-controls">
          <button class="secondary play">▶</button>
          <input type="range" class="minute" min="0" max="${last}" value="${Math.min(10, last)}">
          <span class="minute-label"></span>
        </div>
        <div class="row layers">
          <label class="inline"><input type="checkbox" data-layer="positions" checked> Positions</label>
          <label class="inline"><input type="checkbox" data-layer="kills" checked> Kills/deaths</label>
          <label class="inline"><input type="checkbox" data-layer="objectives" checked> Objectives</label>
          <label class="inline"><input type="checkbox" data-layer="mypath" checked> My path</label>
          <label class="inline"><input type="checkbox" data-layer="enemyjg" ${d.enemy_jg_pid ? "checked" : "disabled"}> Enemy jungler path</label>
          <label class="inline"><input type="checkbox" data-layer="onlyme"> Only fights I was in</label>
        </div>
        <p class="muted small-note">Riot provides each player's position once per minute, and the exact position of every kill/objective.</p>
      </div>
      <div>
        <h3>Events</h3>
        <ol class="events">${eventRows(r)}</ol>
      </div>
    </div>
    <h3>Build path</h3>${buildPathHtml(d)}
    <h3>Skills</h3>${skillGrid(d.skills)}
    <h3>Gold over time</h3>${goldChart(d)}`;
}

function playerOf(d, pid) { return d.players.find((p) => p.pid === pid); }

function eventRows(r) {
  const d = r.detail;
  const me = d.my_pid;
  return d.events.filter((e) => e.type !== "building").map((e) => {
    let cls = "", txt = "";
    if (e.type === "kill") {
      const k = playerOf(d, e.killer), v = playerOf(d, e.victim);
      cls = e.killer === me ? "good" : e.victim === me ? "bad" : e.assists.includes(me) ? "assist" : "other";
      txt = `${k ? icon({ icon: k.icon, name: k.champion }, "ico tiny") + esc(k.champion) : "Tower/minions"} killed ${v ? icon({ icon: v.icon, name: v.champion }, "ico tiny") + esc(v.champion) : "?"}`
        + (e.assists.length ? ` <small class="muted">+${e.assists.length}</small>` : "");
    } else {
      const ally = e.team === playerOf(d, me).team;
      cls = ally ? "obj-ally" : "obj-enemy";
      txt = `${ally ? "Your team" : "Enemies"}: ${esc(OBJ_PT[e.monster] || e.monster)}`;
    }
    return `<li class="${cls}" data-t="${e.t}" onclick="seekMap(this)"><span class="t">${fmtT(e.t)}</span> ${txt}</li>`;
  }).join("");
}

function buildPathHtml(d) {
  const groups = [];
  for (const p of d.purchases) {
    const g = groups[groups.length - 1];
    if (g && p.t - g.t < 0.75) g.items.push(p); else groups.push({ t: p.t, items: [p] });
  }
  return `<div class="build-timeline">${groups.map((g) => `
    <div class="recall"><small>${fmtT(g.t)}</small><div>${g.items.map((it) =>
      `<img class="ico ${it.completed ? "" : "small component"}" src="${it.icon}" title="${esc(it.name)}" alt="">`).join("")}</div></div>`).join("")}</div>`;
}

function skillGrid(skills) {
  return `<table class="skill-grid"><tbody>${["Q", "W", "E", "R"].map((k) => `
    <tr><th>${k}</th>${Array.from({ length: 18 }, (_, i) => `<td class="${skills[i] === k ? "on" : ""}">${skills[i] === k ? i + 1 : ""}</td>`).join("")}</tr>`).join("")}
  </tbody></table>`;
}

function goldChart(d) {
  const me = String(d.my_pid), opp = d.opp_pid ? String(d.opp_pid) : null;
  const myTeam = playerOf(d, d.my_pid).team;
  const teamOf = (pid) => playerOf(d, +pid).team;
  const series = d.frames.map((f) => {
    let ally = 0, enemy = 0;
    for (const [pid, g] of Object.entries(f.gold)) (teamOf(pid) === myTeam ? (ally += g) : (enemy += g));
    return { m: f.m, team: ally - enemy, lane: opp ? f.gold[me] - f.gold[opp] : null };
  });
  const W = 600, H = 160, maxAbs = Math.max(1000, ...series.flatMap((s) => [Math.abs(s.team), Math.abs(s.lane || 0)]));
  const x = (i) => (i / Math.max(series.length - 1, 1)) * W;
  const y = (v) => H / 2 - (v / maxAbs) * (H / 2 - 8);
  const line = (key) => series.map((s, i) => `${x(i)},${y(s[key])}`).join(" ");
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" class="zero"/>
      <polyline points="${line("team")}" class="team-line"/>
      ${opp ? `<polyline points="${line("lane")}" class="lane-line"/>` : ""}
    </svg>
    <div class="legend"><span class="team-key">Team gold vs enemies</span>${opp ? `<span class="lane-key">You vs lane opponent</span>` : ""}
      <span class="muted">max ±${fmtK(maxAbs)} · above the line = ahead</span></div></div>`;
}

function seekMap(li) {
  const el = li.closest(".map-pane");
  const slider = el.querySelector(".minute");
  slider.value = Math.min(+slider.max, Math.ceil(+li.dataset.t));
  slider.dispatchEvent(new Event("input"));
}

function drawMap(r, el) {
  const d = r.detail;
  const svg = el.querySelector(".minimap");
  const slider = el.querySelector(".minute");
  const layer = (k) => el.querySelector(`[data-layer="${k}"]`)?.checked;
  const me = d.my_pid;
  const myTeam = playerOf(d, me).team;

  const render = () => {
    const t = +slider.value;
    el.querySelector(".minute-label").textContent = `${t}:00`;
    const parts = [`<image href="${r.map_icon}" width="512" height="512" opacity=".85"/>`];
    const path = (pid, cls) => {
      const pts = d.frames.slice(0, t + 1).map((f) => f.pos[pid]).filter(Boolean).map(([x, y]) => `${mx(x)},${my(y)}`).join(" ");
      return `<polyline points="${pts}" class="${cls}"/>`;
    };
    if (layer("mypath")) parts.push(path(String(me), "path-me"));
    if (layer("enemyjg") && d.enemy_jg_pid) parts.push(path(String(d.enemy_jg_pid), "path-enemy"));
    const evs = d.events.filter((e) => e.t <= t + 1 && e.x != null);
    if (layer("objectives")) for (const e of evs.filter((e) => e.type === "objective")) {
      const ally = e.team === myTeam;
      parts.push(`<rect x="${mx(e.x) - 7}" y="${my(e.y) - 7}" width="14" height="14" transform="rotate(45 ${mx(e.x)} ${my(e.y)})" class="obj ${ally ? "ally" : "enemy"}"><title>${fmtT(e.t)} ${OBJ_PT[e.monster] || e.monster}</title></rect>`);
    }
    if (layer("kills")) for (const e of evs.filter((e) => e.type === "kill")) {
      const mine = e.killer === me || e.victim === me || e.assists.includes(me);
      if (layer("onlyme") && !mine) continue;
      const cls = e.killer === me ? "k-me" : e.victim === me ? "k-dead" : e.assists.includes(me) ? "k-assist" : "k-other";
      const recent = t - e.t < 1.01 ? "recent" : "";
      const v = playerOf(d, e.victim), k = playerOf(d, e.killer);
      parts.push(`<g class="kill ${cls} ${recent}" transform="translate(${mx(e.x)} ${my(e.y)})"><circle r="8"/><path d="M-4,-4 L4,4 M4,-4 L-4,4"/>
        <title>${fmtT(e.t)} ${k ? k.champion : "?"} killed ${v ? v.champion : "?"}</title></g>`);
    }
    if (layer("positions")) {
      const f = d.frames[t];
      for (const p of d.players) {
        const pos = f.pos[String(p.pid)];
        if (!pos) continue;
        const cx = mx(pos[0]), cy = my(pos[1]);
        parts.push(`<clipPath id="c${r.match_id}${p.pid}"><circle cx="${cx}" cy="${cy}" r="13"/></clipPath>
          <image href="${p.icon}" x="${cx - 13}" y="${cy - 13}" width="26" height="26" clip-path="url(#c${r.match_id}${p.pid})"/>
          <circle cx="${cx}" cy="${cy}" r="13" class="ring ${p.team === myTeam ? "ally" : "enemy"} ${p.is_me ? "me" : ""}"><title>${esc(p.champion)} (${esc(p.name)})</title></circle>`);
      }
    }
    svg.innerHTML = parts.join("");
    el.querySelectorAll(".events li").forEach((li) => li.classList.toggle("past", +li.dataset.t > t + 1));
  };
  slider.oninput = render;
  el.querySelectorAll(".layers input").forEach((c) => (c.onchange = render));
  const play = el.querySelector(".play");
  let timer = null;
  play.onclick = () => {
    if (timer) { clearInterval(timer); timer = null; play.textContent = "▶"; return; }
    if (+slider.value >= +slider.max) slider.value = 0;
    play.textContent = "❚❚";
    timer = setInterval(() => {
      if (+slider.value >= +slider.max) { clearInterval(timer); timer = null; play.textContent = "▶"; return; }
      slider.value = +slider.value + 1;
      render();
    }, 700);
  };
  render();
}

// ---------------------------------------------------------------- mains
const mainsState = { champs: [], mine: [], mineError: "", filter: "mine", selected: null, sort: "points", view: "all" };
const REGION_NAMES = { euw1: "EUW", kr: "KR", eun1: "EUNE", na1: "NA" };

function mainsTable(rep, compact) {
  if (!rep.mains.length)
    return `<p class="muted">No known mains for ${esc(rep.champion.name)} yet. They appear once the top players list is updated (Mains tab).</p>`;
  return `<div class="table-wrap"><table class="mains-table">
    <thead><tr><th>#</th><th>Player</th><th>Region</th><th>Rank</th><th>Mastery</th>${compact ? "" : "<th>Role</th>"}<th>Games*</th><th>WR</th><th>KDA</th><th>Rune</th><th>Items</th></tr></thead>
    <tbody>${rep.mains.map((m, i) => `
      <tr>
        <td>${i + 1}</td>
        <td><strong>${esc(m.name || "…")}</strong></td>
        <td><span class="tag">${m.region}</span></td>
        <td>${m.tier === "CHALLENGER" ? "Challenger" : "Grandmaster"} <small class="muted">${m.lp} LP</small></td>
        <td>${fmtK(m.points)}</td>
        ${compact ? "" : `<td>${m.position ? POS_PT[m.position] ?? m.position : "–"}</td>`}
        <td>${m.games || "–"}</td>
        <td>${m.winrate == null ? "–" : `<span class="${m.winrate >= 0.5 ? "good" : "bad"}">${pct(m.winrate)}</span>`}</td>
        <td>${m.kda == null ? "–" : num(m.kda, 2)}</td>
        <td>${m.keystone?.icon ? icon(m.keystone, "ico small") : "–"}</td>
        <td class="items-cell">${m.items.length ? m.items.map((it) => icon(it, "ico small")).join("") : "–"}</td>
      </tr>`).join("")}</tbody>
  </table></div>
  <p class="muted small-note">* recent games of these players on this champion that the app has downloaded.</p>`;
}

function renderCsMains(rep) {
  $("#cs-mains").innerHTML = `
    <p class="muted">Top 5 <strong>${esc(rep.champion.name)}</strong> mains (most mastery points, Challenger/GM EUW + KR)</p>
    ${mainsTable(rep, true)}
    ${rep.sample ? `<details><summary>Mains' runes and build (${rep.sample} games)</summary>${buildDetailHtml(rep)}</details>` : ""}`;
}

async function loadMainsList() {
  const [data, mine] = await Promise.all([
    getJSON("/api/mains"),
    getJSON("/api/my-mastery").catch(() => ({ ok: false, champions: [] })),
  ]);
  mainsState.champs = data.champions;
  mainsState.mine = mine.champions || [];
  mainsState.mineError = mine.ok ? "" : mine.error || "";
  const regions = (data.regions || []).map((r) => REGION_NAMES[r] || r).join(" & ");
  $("#mains-count").textContent = data.players
    ? `${fmtN(data.players)} Challenger & Grandmaster players${regions ? ` from ${regions}` : ""}`
    : "No top players yet: click “Update now” to find each champion's best one-trick players";
  $("#mains-updated").textContent = data.players
    ? `List updated ${ago(data.updated ? data.updated * 1000 : null)} · it refreshes automatically once a day`
    : "Takes about 20 minutes the first time.";
  renderMainsGrid();
}

function renderMainsGrid() {
  const q = $("#mains-search").value.trim().toLowerCase();
  const mineRank = new Map(mainsState.mine.map((c, i) => [c.id, i + 1]));
  const byId = new Map(mainsState.champs.map((c) => [c.id, c]));
  const showMine = mainsState.filter === "mine";
  // "My top 10": por ordem de maestria; "All": todos, com os teus 10 marcados com ★
  const list = showMine ? mainsState.mine.map((m) => ({ ...(byId.get(m.id) || m), points: m.points })) : mainsState.champs;
  const tiles = list
    .filter((c) => !q || c.name.toLowerCase().includes(q))
    .map((c) => {
      const rank = mineRank.get(c.id);
      return `<button class="champ-tile ${c.id === mainsState.selected ? "active" : ""} ${c.mains ? "" : "empty-tile"} ${rank ? "mine" : ""}"
        onclick="openMains(${c.id})" title="${esc(c.name)} · ${c.mains ?? 0} known mains${rank ? ` · your #${rank} by mastery` : ""}">
        ${rank ? `<span class="mine-badge">★${rank}</span>` : ""}
        <img src="${c.icon}" alt=""><span>${esc(c.name)}</span>
        ${showMine ? `<small class="pts">${fmtK(c.points)} pts</small>` : ""}</button>`;
    }).join("");
  $("#mains-grid").classList.toggle("mine-grid", showMine);
  $("#mains-grid").innerHTML = tiles || (showMine
    ? `<p class="muted">${esc(mainsState.mineError || "Could not load your masteries.")}</p>`
    : `<p class="muted">No champion found.</p>`);
}

$$("#mains-filter button").forEach((b) =>
  b.addEventListener("click", () => {
    mainsState.filter = b.dataset.filter;
    $$("#mains-filter button").forEach((x) => x.classList.toggle("active", x === b));
    renderMainsGrid();
  })
);

async function openMains(id, quiet = false) {
  // quiet = recarregar o mesmo boneco (nomes/jogos novos chegaram) sem limpar o ecrã nem a vista escolhida
  if (mainsState.selected !== id) mainsState.view = "all";
  mainsState.selected = id;
  renderMainsGrid();
  if (!quiet) $("#mains-panel").innerHTML = `<div class="loading">Loading…</div>`;
  const rep = await getJSON(`/api/mains/${id}?sort=${mainsState.sort}`);
  const last = rep.last_refresh ? +rep.last_refresh * 1000 : null;
  $("#mains-panel").innerHTML = `
    <div class="mains-head">${icon(rep.champion, "ico big")}<h2>${esc(rep.champion.name)}</h2>
      <label>Sort by <select id="mains-sort">
        <option value="points" ${mainsState.sort === "points" ? "selected" : ""}>Mastery points</option>
        <option value="lp" ${mainsState.sort === "lp" ? "selected" : ""}>LP</option></select></label>
      <div class="mains-games">
        <div class="job-view" data-jobview="mains_games"></div>
        <small>${JOBS.mains_games?.running ? "" : `Recent games ${last ? `updated ${ago(last)}` : "not downloaded yet"}`}</small>
        <button id="mains-refresh" data-job="mains_games" class="secondary" title="Download these players' latest games">↻ Refresh games</button>
      </div>
    </div>
    ${rep.names_pending ? `<p class="muted small-note">Looking up player names…</p>` : ""}
    ${mainsTable(rep, false)}
    ${rep.sample ? `
      <div class="subtabs main-pick">
        <button data-main="all" class="active">All (${rep.sample} games)</button>
        ${rep.mains.map((m, i) => `<button data-main="${i}" ${m.games ? "" : "disabled"}>${i + 1}. ${esc((m.name || "?").split("#")[0])} (${m.games})</button>`).join("")}
        <button data-main="compare">Compare</button>
      </div>
      <div id="main-build"></div>`
      : `<p class="muted">${rep.auto_refresh ? "Downloading these players' recent games… this page updates itself when done." : "No games of these mains downloaded yet."}</p>`}`;
  renderJobs();
  if (rep.sample) {
    const show = (key) => {
      mainsState.view = key;
      $$(".main-pick button").forEach((b) => b.classList.toggle("active", b.dataset.main === key));
      $("#main-build").innerHTML = key === "all" ? buildDetailHtml(rep)
        : key === "compare" ? compareMains(rep) : buildDetailHtml(rep.mains[+key].build);
    };
    $$(".main-pick button").forEach((b) => b.addEventListener("click", () => show(b.dataset.main)));
    show(rep.mains[+mainsState.view]?.games || ["all", "compare"].includes(mainsState.view) ? mainsState.view : "all");
  }
  $("#mains-sort").addEventListener("change", (e) => { mainsState.sort = e.target.value; openMains(id); });
  $("#mains-refresh").addEventListener("click", async () => {
    const res = await getJSON(`/api/mains/${id}/refresh`, { method: "POST" });
    if (!res.ok && res.error) $('[data-jobview="mains_games"]').innerHTML = `<small class="bad">${esc(res.error)}</small>`;
  });
}

$("#mains-search").addEventListener("input", renderMainsGrid);
$("#mastery-crawl").addEventListener("click", () =>
  fetch("/api/mains/crawl", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ regions: [...$$(".mastery-region:checked")].map((c) => c.value) }),
  })
);

// ---------------------------------------------------------------- ícones de feitiços, rotas de jungle, comparar mains
let DD_VERSION = "";
const mapUrl = () => `https://ddragon.leagueoflegends.com/cdn/${DD_VERSION}/img/map/map11.png`;
const spellIcons = (sp) => (sp.icons || sp.names.map((n) => ({ name: n }))).map((x) =>
  x.icon ? `<img class="ico spell" src="${x.icon}" alt="${esc(x.name)}" title="${esc(x.name)}">` : esc(x.name)).join("");

// rota mais comum: em cada minuto vai para a zona do mapa onde está a maioria dos junglers
// que fizeram o mesmo caminho até aí (zonas de ~2000 unidades); devolve a posição média e a % de jogos
function commonRoute(paths) {
  const zone = ([x, y]) => `${Math.floor(x / 2000)},${Math.floor(y / 2000)}`;
  let group = paths.filter((p) => p.length > 1);
  const total = group.length;
  const route = [];
  const len = Math.max(0, ...group.map((p) => p.length));
  for (let m = 1; m < len && group.length; m++) {
    const withM = group.filter((p) => p[m]);
    if (!withM.length) break;
    const counts = {};
    for (const p of withM) counts[zone(p[m])] = (counts[zone(p[m])] || 0) + 1;
    const best = Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0];
    const next = withM.filter((p) => zone(p[m]) === best);
    if (next.length < Math.max(2, total * 0.1)) break; // a partir daqui já não é uma rota "comum"
    group = next;
    const x = group.reduce((s, p) => s + p[m][0], 0) / group.length;
    const y = group.reduce((s, p) => s + p[m][1], 0) / group.length;
    route.push({ m, x, y, pct: group.length / total });
  }
  return route;
}

const JG_MAPS = {};
let jgSeq = 0;
function jungleMap(j) {
  const sides = Object.entries(j.paths || {}).filter(([, ps]) => ps.length);
  if (!sides.length) return "";
  const id = `jg${++jgSeq}`;
  JG_MAPS[id] = j.paths;
  const start = sides[0][0];
  return `<div class="jg-map" id="${id}">
    <div class="subtabs">${sides.map(([team, ps]) => `<button data-team="${team}" class="${team === start ? "active" : ""}"
      onclick="drawJungle('${id}', '${team}')">${team === "100" ? "Blue side" : "Red side"} (${ps.length})</button>`).join("")}</div>
    <svg viewBox="0 0 512 512" class="minimap jg">${jungleSvg(j.paths[start])}</svg>
    <p class="muted small-note">Thin lines: real routes over the first 7 min. Gold line: the most common route (numbers = minute; hover to see the % of games).</p>
  </div>`;
}

function jungleSvg(paths) {
  const line = (p, cls) => `<polyline points="${p.map(([x, y]) => `${mx(x)},${my(y)}`).join(" ")}" class="${cls}"/>`;
  const route = commonRoute(paths);
  return `<image href="${mapUrl()}" width="512" height="512" opacity=".8"/>
    ${paths.map((p) => line(p, "jg-faint")).join("")}
    ${route.length ? line(route.map((r) => [r.x, r.y]), "jg-main") + route.map((r) => `<g transform="translate(${mx(r.x)} ${my(r.y)})">
      <circle r="9" class="jg-dot"/><text y="4">${r.m}</text><title>${r.m}:00 · ${pct(r.pct)} of games follow this route up to here</title></g>`).join("") : ""}`;
}

function drawJungle(id, team) {
  const el = document.getElementById(id);
  el.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.team === team));
  el.querySelector("svg").innerHTML = jungleSvg(JG_MAPS[id][team]);
}

function compareMains(rep) {
  const ms = rep.mains.filter((m) => m.games);
  const runes = (b) => b.runes ? `${b.runes.perk_ids.slice(0, 6).map((pid) => {
      const r = RUNES?.styles.flatMap((s) => s.slots.flat()).find((x) => x.id === pid);
      return r ? `<img class="ico small" src="${r.icon}" title="${esc(r.name)}" alt="">` : "";
    }).join("")}<br><small class="muted">${pct(b.runes.winrate)} WR · ${b.runes.games}g</small>` : "–";
  const row = (label, fn) => `<tr><th>${label}</th>${ms.map((m) => `<td>${fn(m.build, m)}</td>`).join("")}</tr>`;
  return `<div class="table-wrap"><table class="compare">
    <thead><tr><th></th>${ms.map((m) => `<th>${esc((m.name || "?").split("#")[0])}<br><small class="muted">${m.region} · ${m.games} games · ${pct(m.winrate)}</small></th>`).join("")}</tr></thead>
    <tbody>
      ${row("Runes", runes)}
      ${row("Spells", (b) => b.spells ? spellIcons(b.spells) : "–")}
      ${row("Skills", (b) => b.timeline?.skills ? `${b.timeline.skills.first3} · ${b.timeline.skills.priority}` : "–")}
      ${row("Start items", (b) => b.timeline?.start_items ? b.timeline.start_items.items.map((it) => icon(it, "ico small")).join("") : "–")}
      ${row("1st → 3rd item", (b) => b.timeline?.item_path ? b.timeline.item_path.items.map((it) => icon(it, "ico small")).join("›") : "–")}
      ${ms.some((m) => m.build.quest_items?.length) ? row("Quest item", (b) => b.quest_items?.map((it) => icon(it, "ico small")).join("") || "–") : ""}
      ${row("Most used items", (b, m) => m.items.map((it) => icon(it, "ico small")).join("") || "–")}
    </tbody></table></div>`;
}

// ---------------------------------------------------------------- rotas fixas de full clear
// Posição dos campos no mapa (a mesma tabela do backend). 100 = lado azul, 200 = lado vermelho.
const CAMP_XY = {
  100: { "Blue buff": [3821, 8101], Gromp: [2288, 8448], Wolves: [3783, 6495], Raptors: [7061, 5325], "Red buff": [7762, 4011], Krugs: [8394, 2641] },
  200: { "Blue buff": [11032, 6990], Gromp: [12703, 6443], Wolves: [11008, 8387], Raptors: [7852, 9471], "Red buff": [7101, 10903], Krugs: [6317, 12146] },
};
const SCUTTLES = [[4400, 9600], [10500, 5000]];
const CAMP_PT = { "Blue buff": "Blue", "Red buff": "Red", Raptors: "Raptors", Krugs: "Krugs", Wolves: "Wolves", Gromp: "Gromp", Scuttle: "Scuttle" };
// Full clear padrão conforme o campo inicial
const CLEARS = {
  Raptors: ["Raptors", "Krugs", "Red buff", "Wolves", "Gromp", "Blue buff", "Scuttle"],
  "Blue buff": ["Blue buff", "Gromp", "Wolves", "Raptors", "Red buff", "Krugs", "Scuttle"],
  "Red buff": ["Red buff", "Krugs", "Raptors", "Wolves", "Blue buff", "Gromp"],
  Gromp: ["Gromp", "Blue buff", "Wolves", "Raptors", "Red buff", "Krugs", "Scuttle"],
  Krugs: ["Krugs", "Red buff", "Raptors", "Wolves", "Blue buff", "Gromp", "Scuttle"],
  Wolves: ["Wolves", "Blue buff", "Gromp", "Raptors", "Red buff", "Krugs", "Scuttle"],
};
const CLEAR_STATE = {};
let clearSeq = 0;

function clearMap(j) {
  // inícios conhecidos, pela ordem de popularidade (ignora inícios no jungle inimigo)
  const starts = (j.start || []).filter((s) => CLEARS[s.name]);
  if (!starts.length) return "";
  const id = `clr${++clearSeq}`;
  CLEAR_STATE[id] = { starts, start: starts[0].name, team: "100" };
  return `<div class="jg-map clear-map" id="${id}">${clearInner(id)}</div>`;
}

function clearInner(id) {
  const st = CLEAR_STATE[id];
  const route = CLEARS[st.start];
  const camps = CAMP_XY[st.team];
  const pts = [];
  for (const c of route) {
    if (c === "Scuttle") {
      // scuttle do rio mais perto do último campo
      const [lx, ly] = pts[pts.length - 1];
      pts.push(SCUTTLES.reduce((a, b) => (Math.hypot(a[0] - lx, a[1] - ly) < Math.hypot(b[0] - lx, b[1] - ly) ? a : b)));
    } else pts.push(camps[c]);
  }
  const line = pts.map(([x, y]) => `${mx(x)},${my(y)}`).join(" ");
  return `
    <div class="subtabs">${st.starts.map((s) => `<button class="${s.name === st.start ? "active" : ""}"
      onclick="setClear('${id}', 'start', '${s.name}')">Start ${CAMP_PT[s.name]} <small>${pct(s.pct)}</small></button>`).join("")}</div>
    <div class="subtabs">${[["100", "Blue side"], ["200", "Red side"]].map(([t, l]) =>
      `<button class="${t === st.team ? "active" : ""}" onclick="setClear('${id}', 'team', '${t}')">${l}</button>`).join("")}</div>
    <svg viewBox="0 0 512 512" class="minimap jg">
      <defs><marker id="arrow-${id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" class="arrow-head"/></marker></defs>
      <image href="${mapUrl()}" width="512" height="512" opacity=".8"/>
      ${pts.slice(1).map(([x, y], i) => {
        // seta de cada campo para o seguinte, encurtada para não tapar os números
        const [x0, y0] = [mx(pts[i][0]), my(pts[i][1])], [x1, y1] = [mx(x), my(y)];
        const d = Math.hypot(x1 - x0, y1 - y0) || 1, k = 13 / d;
        return `<line x1="${x0 + (x1 - x0) * k}" y1="${y0 + (y1 - y0) * k}" x2="${x1 - (x1 - x0) * k}" y2="${y1 - (y1 - y0) * k}" class="clear-line" marker-end="url(#arrow-${id})"/>`;
      }).join("")}
      ${pts.map(([x, y], i) => `<g transform="translate(${mx(x)} ${my(y)})"><circle r="11" class="jg-dot"/><text y="4">${i + 1}</text>
        <text y="24" class="camp-label">${CAMP_PT[route[i]]}</text></g>`).join("")}
    </svg>
    <p class="clear-steps">${route.map((c, i) => `<span><b>${i + 1}</b> ${CAMP_PT[c]}</span>`).join(" → ")}</p>`;
}

function setClear(id, key, value) {
  CLEAR_STATE[id][key] = value;
  document.getElementById(id).innerHTML = clearInner(id);
}

// ---------------------------------------------------------------- dados
const QUEUE_LABEL = { solo: "Solo/Duo", flex: "Flex", normal: "Normals", aram: "ARAM", other: "Other" };
let OVERVIEW = null;
const collect = { target: 1000, regions: ["euw1", "kr"], timelines: true, days: 30 };

async function loadData() {
  const o = await getJSON("/api/data/overview");
  OVERVIEW = o;
  const m = o.mine, h = o.meta;
  $("#src-mine").innerHTML = `
    <div class="src-head"><span class="src-icon">◉</span><h3>Your games</h3></div>
    <div class="src-big">${fmtN(m.games)} <small>games this season</small></div>
    <div class="chips">${Object.entries(m.by_queue).sort((a, b) => b[1] - a[1]).map(([q, n]) => `<span>${QUEUE_LABEL[q] || q} <b>${fmtN(n)}</b></span>`).join("")}</div>
    <p class="muted small-note">Last game ${ago(m.last_game)} · checked ${ago(m.updated ? m.updated * 1000 : null)}</p>
    <div class="src-actions">
      <button data-job="sync" onclick="$('#sync').click()">↻ Check for new games</button>
      <div class="job-view" data-jobview="sync"></div>
    </div>`;
  const regionChips = h.regions.map((r) => `<span>${REGION_NAMES[r.region] || r.region.toUpperCase()} <b>${fmtN(r.n)}</b></span>`).join("");
  $("#src-meta").innerHTML = `
    <div class="src-head"><span class="src-icon">♛</span><h3>High elo games</h3></div>
    <div class="src-big">${fmtN(h.games)} <small>Challenger & Grandmaster games</small></div>
    <div class="chips">${regionChips}</div>
    <p class="muted small-note">${fmtN(h.with_details)} with full details (item order, skills, jungle paths) ·
      from ${h.first ? new Date(h.first).toLocaleDateString("en-GB", { day: "numeric", month: "short" }) : "–"} to ${h.last ? ago(h.last) : "–"} ·
      updated ${ago(h.updated ? h.updated * 1000 : null)}</p>
    <div class="collect">
      <span class="muted">Get new games</span>
      <div class="seg" id="collect-n">${[500, 1000, 5000].map((n) =>
        `<button data-n="${n}" class="${collect.target === n ? "active" : ""}">+${fmtN(n)}</button>`).join("")}</div>
      <input type="number" id="collect-custom" min="1" max="50000" step="100" placeholder="other" value="${[500, 1000, 5000].includes(collect.target) ? "" : collect.target}">
    </div>
    <p class="muted small-note" id="collect-est"></p>
    <div class="src-actions">
      <button data-job="meta" id="collect-go"></button>
      <button class="secondary" id="collect-stop" hidden>■ Stop</button>
      <div class="job-view" data-jobview="meta"></div>
    </div>
    <details class="advanced">
      <summary>Advanced options</summary>
      <div class="row"><span class="muted">Regions</span>${o.regions.map((r) =>
        `<label class="inline"><input type="checkbox" class="crawl-region" value="${r}" ${collect.regions.includes(r) ? "checked" : ""}> ${REGION_NAMES[r] || r}</label>`).join("")}</div>
      <div class="row">
        <label class="inline"><input type="checkbox" id="collect-tl" ${collect.timelines ? "checked" : ""}> Full details (item order, skills, jungle paths: about 2× slower)</label>
      </div>
      <div class="row">
        <label class="inline">Only games from the last <input type="number" id="collect-days" value="${collect.days}" min="1" max="120"> days</label>
      </div>
      ${h.games - h.with_details > 0 ? `<div class="row">
        <button class="secondary" data-job="meta" id="backfill-go">Add details to ${fmtN(Math.min(1000, h.games - h.with_details))} older games</button>
        <span class="muted small-note">${fmtN(h.games - h.with_details)} games still without details</span></div>` : ""}
      <p class="muted small-note">Games already in the database are never downloaded again. The app goes through Challenger, then Grandmaster, then Master players, further back in each one's history, until it reaches the number you asked for.</p>
    </details>`;
  const syncCollect = () => {
    collect.regions = [...$$(".crawl-region:checked")].map((c) => c.value);
    collect.timelines = $("#collect-tl").checked;
    collect.days = +$("#collect-days").value || 30;
    const regions = Math.max(1, collect.regions.length);
    const perMin = collect.timelines ? 22 : 40; // jogos/minuto por região (limite de pedidos da chave)
    const m = Math.ceil(collect.target / regions / perMin);
    $("#collect-go").textContent = `＋ Get ${fmtN(collect.target)} new games`;
    $("#collect-est").textContent = `About ${m < 60 ? `${m} min` : `${(m / 60).toFixed(1)} h`} with ${regions} region${regions > 1 ? "s" : ""} in parallel. The app keeps working meanwhile, and you can stop at any time without losing what was downloaded.`;
  };
  $$("#collect-n button").forEach((b) => b.addEventListener("click", () => {
    collect.target = +b.dataset.n;
    $$("#collect-n button").forEach((x) => x.classList.toggle("active", x === b));
    $("#collect-custom").value = "";
    syncCollect();
  }));
  $("#collect-custom").addEventListener("input", (e) => {
    if (+e.target.value > 0) {
      collect.target = Math.min(50000, +e.target.value);
      $$("#collect-n button").forEach((x) => x.classList.remove("active"));
      syncCollect();
    }
  });
  $$(".crawl-region, #collect-tl, #collect-days").forEach((x) => x.addEventListener("change", syncCollect));
  syncCollect();
  $("#collect-go").addEventListener("click", async () => {
    syncCollect();
    const res = await getJSON("/api/meta/collect", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: collect.target, regions: collect.regions, days: collect.days, timelines: collect.timelines }),
    });
    if (!res.ok && res.error) $('[data-jobview="meta"]').innerHTML = `<small class="bad">${esc(res.error)}</small>`;
  });
  $("#collect-stop").addEventListener("click", () => fetch("/api/jobs/meta/stop", { method: "POST" }));
  $("#backfill-go")?.addEventListener("click", () => fetch("/api/meta/crawl", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ regions: collect.regions, players: 0, timelines: true, backfill: 1000 }),
  }));
  $("#auto-enabled").checked = o.auto;
  const a = await getJSON("/api/auto");
  const when = (t) => ago(t ? t * 1000 : null);
  $("#auto-last").textContent = `Last automatic run: high elo games ${when(a.last.meta)} · top players ${when(a.last.mastery)} · your games ${when(a.last.sync)}`;
  // filtros do explorador
  const keep = (sel, opts) => { const v = $(sel).value; $(sel).innerHTML = opts; $(sel).value = v; };
  keep("#ex-region", `<option value="">All regions</option>` + h.regions.map((r) => `<option value="${r.region}">${REGION_NAMES[r.region] || r.region.toUpperCase()}</option>`).join(""));
  keep("#ex-patch", `<option value="">All patches</option>` + h.patches.map((p) => `<option value="${p.patch}">Patch ${p.patch}</option>`).join(""));
  renderJobs();
  loadExplorer();
}
$("#auto-enabled").addEventListener("change", (e) => fetch(`/api/auto?enabled=${e.target.checked}`, { method: "POST" }).then(loadData));

// ---------------------------------------------------------------- explorador de bonecos
const ex = { source: "meta", role: "", sort: "games", dir: -1, rows: [], total: 0, open: null };
const segBind = (sel, key, attr) => $$(`${sel} button`).forEach((b) => b.addEventListener("click", () => {
  $$(`${sel} button`).forEach((x) => x.classList.toggle("active", x === b));
  ex[key] = b.dataset[attr];
  loadExplorer();
}));
segBind("#ex-source", "source", "src");
segBind("#ex-role", "role", "role");
["#ex-region", "#ex-patch"].forEach((s) => $(s).addEventListener("change", loadExplorer));
$("#ex-search").addEventListener("input", renderExplorer);
$("#ex-rare").addEventListener("change", renderExplorer);
$$("#ex-table th[data-sort]").forEach((th) => th.addEventListener("click", () => {
  const k = th.dataset.sort;
  ex.dir = ex.sort === k ? -ex.dir : k === "name" ? 1 : -1;
  ex.sort = k;
  renderExplorer();
}));

const exQuery = () => new URLSearchParams({ source: ex.source, role: ex.role, region: $("#ex-region").value, patch: $("#ex-patch").value });
async function loadExplorer() {
  const d = await getJSON(`/api/data/champions?${exQuery()}`);
  ex.rows = d.champions;
  ex.total = d.total_games;
  ex.open = null;
  renderExplorer();
}

function renderExplorer() {
  const q = $("#ex-search").value.trim().toLowerCase();
  const minGames = $("#ex-rare").checked ? (ex.source === "me" ? 3 : 20) : 1;
  const rows = ex.rows.filter((r) => r.games >= minGames && (!q || r.name.toLowerCase().includes(q)))
    .sort((a, b) => (ex.sort === "name" ? a.name.localeCompare(b.name) : (a[ex.sort] ?? 0) - (b[ex.sort] ?? 0)) * ex.dir);
  $$("#ex-table th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === ex.sort);
    th.classList.toggle("asc", ex.dir === 1);
  });
  const what = ex.source === "me" ? "of your ranked games" : "high elo games";
  $("#ex-summary").textContent = `${fmtN(ex.total)} ${what} match these filters · ${rows.length} champions · click a champion to see its matchups`;
  $("#ex-table tbody").innerHTML = rows.map((r) => `
    <tr class="ex-row ${ex.open === r.champion_id ? "open" : ""}" data-id="${r.champion_id}">
      <td>${icon(r, "ico small")} ${esc(r.name)}</td>
      <td>${fmtN(r.games)}</td>
      <td>${r.pickrate < 0.1 ? `${(r.pickrate * 100).toFixed(1)}%` : pct(r.pickrate)}</td>
      <td class="${r.winrate >= 0.53 ? "good" : r.winrate <= 0.47 ? "bad" : ""}">${pct(r.winrate)}<span class="wr-bar"><i style="width:${Math.round(r.winrate * 100)}%"></i></span></td>
      <td>${num(r.kda, 2)}</td><td>${num(r.cspm)}</td><td>${pct(r.dmg)}</td>
    </tr>${ex.open === r.champion_id ? `<tr class="ex-detail"><td colspan="7"><div class="loading">Loading…</div></td></tr>` : ""}`).join("")
    || `<tr><td colspan="7" class="muted">No games match these filters.</td></tr>`;
  $$("#ex-table .ex-row").forEach((tr) => tr.addEventListener("click", () => {
    ex.open = ex.open === +tr.dataset.id ? null : +tr.dataset.id;
    renderExplorer();
  }));
  if (ex.open) loadExDetail(ex.open);
}

async function loadExDetail(cid) {
  const d = await getJSON(`/api/data/champion/${cid}?${exQuery()}`);
  const cell = $("#ex-table .ex-detail td");
  if (!cell || ex.open !== cid) return;
  const mu = (list, empty) => list.length ? `<ul class="mu-list">${list.map((u) => `
    <li>${icon(u, "ico small")} ${esc(u.name)} <small>${u.g} games</small>
      <span class="wr ${u.winrate >= 0.5 ? "good" : "bad"}">${pct(u.winrate)}</span></li>`).join("")}</ul>` : `<p class="muted small-note">${empty}</p>`;
  cell.innerHTML = `<div class="ex-cols">
    <div><h3>Played as</h3><ul class="mu-list">${d.roles.map((r) => `<li>${POS_PT[r.position] ?? r.position} <small>${fmtN(r.games)} games</small>
      <span class="wr ${r.winrate >= 0.5 ? "good" : "bad"}">${pct(r.winrate)}</span></li>`).join("") || `<li class="muted">–</li>`}</ul></div>
    <div><h3>Best against</h3>${mu(d.best, "No winning matchups with 5+ games.")}</div>
    <div><h3>Worst against</h3>${mu(d.worst, "No losing matchups with 5+ games.")}</div>
    <div><h3>Most common opponents</h3>${mu(d.common, "–")}</div>
  </div>`;
}

// arranque
renderSides(null);
