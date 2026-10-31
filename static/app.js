const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
const num = (x, d = 1) => (x == null ? "–" : Number(x).toFixed(d));
const icon = (c, cls = "ico") => (c && c.icon ? `<img class="${cls}" src="${c.icon}" alt="${esc(c.name)}" title="${esc(c.name)}">` : `<span class="${cls} blank"></span>`);
const POS_PT = { TOP: "Top", JUNGLE: "Jungle", MIDDLE: "Mid", BOTTOM: "ADC", UTILITY: "Support", "": "—" };

// ---------------------------------------------------------------- tabs
document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("nav button, .tab").forEach((e) => e.classList.remove("active"));
    b.classList.add("active");
    $(`#tab-${b.dataset.tab}`).classList.add("active");
    if (b.dataset.tab === "data") { loadStats(); loadAuto(); }
    if (b.dataset.tab === "pg") loadPostgames();
    if (b.dataset.tab === "hist") loadHistory(true);
    if (b.dataset.tab === "mains") loadMainsList();
  })
);

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
      if ($("#tab-mains").classList.contains("active") && mainsState.selected === msg.champion) openMains(msg.champion, true);
      return;
    case "ingame":
      $("#cs-ingame").hidden = false;
      return;
    case "champselect_end":
      $("#cs-live").hidden = true; $("#cs-idle").hidden = false; $("#build-card").hidden = true; $("#cs-ingame").hidden = true;
      $("#cs-mains").innerHTML = ""; csWasLocked = false; return;
    case "history_updated":
      if ($("#tab-hist").classList.contains("active")) loadHistory(true);
      return;
    case "postgame_pending": $("#pg-msg").textContent = "Game over: waiting for Riot to publish the match data…"; return;
    case "postgame":
      $("#pg-msg").textContent = "";
      document.querySelector('nav button[data-tab="pg"]').click();
      return;
  }
}

const lastJobRunning = {};
function renderStatus(s) {
  const lcu = $("#st-lcu");
  lcu.textContent = s.lcu ? "Client connected" : "Client disconnected";
  lcu.className = "pill " + (s.lcu ? "ok" : "off");
  $("#st-me").textContent = s.me?.name || (s.riot_key ? "Unknown account" : "RIOT_API_KEY missing");
  $("#st-phase").textContent = s.phase && s.phase !== "None" ? s.phase : "";
  if (s.version) DD_VERSION = s.version;
  for (const name of ["sync", "meta", "mastery", "mains_games"]) {
    const j = s.jobs?.[name];
    const label = $(`#job-${name}`);
    if (label) label.textContent = j ? (j.running ? `Running… ${j.progress}` : j.result) : "";
    document.querySelectorAll(`[data-job="${name}"]`).forEach((b) => (b.disabled = !!j?.running));
    // quando um job acaba, atualiza o que estiver aberto
    if (j && !j.running && lastJobRunning[name]) {
      if ($("#tab-data").classList.contains("active")) loadStats();
      if ($("#tab-hist").classList.contains("active")) loadHistory(true);
      if ($("#tab-mains").classList.contains("active")) {
        loadMainsList();
        if (mainsState.selected) openMains(mainsState.selected, true);
      }
    }
    lastJobRunning[name] = !!j?.running;
  }
}

// ---------------------------------------------------------------- champ select
let csWasLocked = false;
function renderChampSelect(cs) {
  $("#cs-idle").hidden = true;
  $("#cs-ingame").hidden = true;
  $("#cs-live").hidden = false;
  $("#allies").innerHTML = cs.allies.map((a) => `
    <li class="${a.me ? "me" : ""}">
      ${icon(a.champion)}
      <span class="pos">${POS_PT[a.position] ?? a.position}</span>
      <span>${esc(a.champion?.name || (a.me ? "You" : "…"))}${a.locked ? "" : a.champion ? " <em>(hover)</em>" : ""}</span>
    </li>`).join("");
  $("#enemies").innerHTML = cs.enemies.length
    ? cs.enemies.map((c) => `<li>${icon(c)}<span>${esc(c.name)}</span></li>`).join("")
    : `<li class="muted">No picks yet</li>`;
  $("#bans").innerHTML = cs.bans.map((c) => icon(c, "ico small")).join("") || `<span class="muted">—</span>`;
  if (cs.locked && !csWasLocked) document.querySelector('#picks-card .subtabs [data-sub="mains"]').click();
  csWasLocked = cs.locked;
}

function renderPicks(p) {
  $("#picks-pos").textContent = [
    p.position ? POS_PT[p.position] : "",
    p.lane_opponent ? `vs ${p.lane_opponent.name} (likely)` : "",
  ].filter(Boolean).join(" · ");
  const roles = Object.entries(p.enemy_roles || {}).map(([pos, n]) => `${POS_PT[pos]}: ${n}`).join(", ");
  $("#enemy-roles").textContent = roles ? `(${roles})` : "";
  $("#picks-warning").hidden = !p.warning;
  $("#picks-warning").textContent = p.warning || "";
  $("#picks").innerHTML = p.candidates.length
    ? p.candidates.map((c) => pickItem(c, `${c.games} games · ${pct(c.winrate)} WR${c.kda != null ? ` · KDA ${num(c.kda)}` : ""}`)).join("")
    : `<li class="muted">No data yet: sync your games and collect high elo data in the "My data" tab.</li>`;
  if (!p.from_pool && p.candidates.length)
    $("#picks").insertAdjacentHTML("afterbegin", `<p class="muted">You have no games in this role yet: suggestions are based on the high elo meta.</p>`);
}

function pickItem(c, sub, tag = "") {
  return `
    <li>
      ${icon({ icon: c.icon, name: c.name }, "ico big")}
      <div>
        <strong>${esc(c.name)}</strong>${tag}
        <span class="muted">${sub}</span>
        <ul class="reasons">${c.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      </div>
    </li>`;
}

function renderCounters(p) {
  $("#counters").innerHTML = p.candidates.length
    ? p.candidates.map((c) => pickItem(c, `score ${pct(c.score)}`, c.in_pool ? ` <span class="tag">in your pool</span>` : "")).join("")
    : `<li class="muted">Not enough high elo data for this role. Collect more games in "My data".</li>`;
  if (!p.lane_opponent && p.candidates.length)
    $("#counters").insertAdjacentHTML("afterbegin", `<p class="muted">Your lane opponent is not known yet: sorted by overall win rate and against the enemies already picked.</p>`);
}

document.querySelectorAll("#picks-card .subtabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#picks-card .subtabs button").forEach((x) => x.classList.toggle("active", x === b));
    $("#picks").hidden = b.dataset.sub !== "mine";
    $("#counters").hidden = b.dataset.sub !== "counters";
    $("#cs-mains").hidden = b.dataset.sub !== "mains";
  })
);

// ---------------------------------------------------------------- árvore de runas
let RUNES = null;
fetch("/api/runes").then((r) => r.json()).then((d) => (RUNES = d));

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
  if (t) {
    if (t.skills) {
      const p = t.skills;
      parts.push(`<h3>Skills <small>${pct(p.winrate)} WR in ${p.games} games</small></h3>
        <p class="skills">Start: ${[...p.first3].map((s) => `<span class="key">${s}</span>`).join("")}
        &nbsp; Max order: ${p.priority.split(" > ").map((s) => `<span class="key">${s}</span>`).join(" › ")}</p>`);
    }
    if (t.start_items || t.item_path) {
      parts.push(`<h3>Item order <small>${esc(t.scope)} · ${t.sample} games with timeline</small></h3>`);
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
  $("#build-card").hidden = false;
  $("#build-title").textContent = `${b.champion.name}${b.locked ? "" : " (hover)"} · ${b.scope} · ${b.sample} high elo games`;
  const parts = [];
  if (b.matchup) {
    const m = b.matchup;
    parts.push(`<div class="matchup">${icon(b.champion, "ico big")}<span>vs</span>${icon(b.opponent, "ico big")}
      <div><strong>${esc(b.champion.name)} vs ${esc(b.opponent.name)}</strong><br>
      High elo: ${m.meta_games ? `${pct(m.meta_winrate)} WR (${m.meta_games} games)` : "no data"} ·
      You: ${m.my_games ? `${pct(m.my_winrate)} WR (${m.my_games} games)` : "never played"}</div></div>`);
  }
  if (!b.sample) {
    parts.push(`<p class="muted">No high elo games with this champion in the database yet. Collect high elo data in the "My data" tab.</p>`);
  }
  parts.push(buildDetailHtml(b));
  if (b.core_items.length) {
    parts.push(`<h3>Most used items</h3><div class="items">${
      [...(b.boots ? [{ ...b.boots, winrate: null }] : []), ...b.core_items].map((it) => `
        <div class="item">${icon(it, "ico")}<span>${esc(it.name)}</span>
        <small>${pct(it.pick_rate)} of games${it.winrate != null ? ` · ${pct(it.winrate)} WR` : ""}</small></div>`).join("")
    }</div>`);
  }
  if (b.situational.length) {
    parts.push(`<h3>Situational (vs this team)</h3><ul class="reasons">${b.situational.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`);
  }
  $("#build").innerHTML = parts.join("");
}

// ---------------------------------------------------------------- simulador
fetch("/api/champions").then((r) => r.json()).then((names) => {
  $("#champ-list").innerHTML = names.map((n) => `<option value="${esc(n)}">`).join("");
});
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
  };
  await fetch("/api/simulate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
});
$("#sim-end").addEventListener("click", () => fetch("/api/simulate/end", { method: "POST" }));

// ---------------------------------------------------------------- pós-jogo
async function loadPostgames() {
  const list = await (await fetch("/api/postgame")).json();
  PG_REPORTS = list;
  renderPgCats();
  $("#pg-list").innerHTML = list.length ? list.map(renderReport).join("") : `<p class="muted">No analysed games yet.</p>`;
}

$("#pg-analyse").addEventListener("click", async () => {
  $("#pg-msg").textContent = "Analysing…";
  const res = await (await fetch("/api/postgame/analyse", { method: "POST" })).json();
  $("#pg-msg").textContent = res.ok ? "" : res.error || "Could not analyse the game.";
  loadPostgames();
});

// ---------------------------------------------------------------- dados
async function loadStats() {
  const pos = $("#stats-pos").value;
  const data = await (await fetch(`/api/stats${pos ? `?position=${pos}` : ""}`)).json();
  const regions = Object.entries(data.meta_regions || {}).map(([r, n]) => `${r} ${n}`).join(", ");
  $("#meta-count").textContent = `· ${data.meta_games} high elo games in the database${regions ? ` (${regions})` : ""}, ${data.meta_timelines} with timeline`;
  $("#stats tbody").innerHTML = data.champions.map((c) => `
    <tr>
      <td>${icon(c, "ico small")} ${esc(c.name)}</td>
      <td>${c.games}</td>
      <td class="${c.winrate >= 0.55 ? "good" : c.winrate < 0.45 ? "bad" : ""}">${pct(c.winrate)}</td>
      <td>${num(c.kda, 2)}</td><td>${num(c.cspm)}</td><td>${num(c.vspm, 2)}</td>
      <td>${pct(c.kp)}</td><td>${pct(c.dmg_share)}</td>
    </tr>`).join("") || `<tr><td colspan="8" class="muted">No games yet. Click "Sync season".</td></tr>`;
}
$("#stats-pos").addEventListener("change", loadStats);

$("#sync").addEventListener("click", async () => {
  const res = await (await fetch("/api/sync", { method: "POST" })).json();
  if (!res.ok && res.error) $("#job-sync").textContent = res.error;
});
$("#crawl").addEventListener("click", () =>
  fetch("/api/meta/crawl", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      regions: [...document.querySelectorAll(".crawl-region:checked")].map((c) => c.value),
      players: +$("#crawl-players").value,
      per_player: +$("#crawl-per").value,
      include_master: $("#crawl-master").checked,
      timelines: $("#crawl-tl").checked,
      backfill: +$("#crawl-backfill").value,
    }),
  })
);

// ---------------------------------------------------------------- histórico
const hist = { group: "all", offset: 0 };
const fmtK = (x) => (x == null ? "–" : x >= 1000 ? `${(x / 1000).toFixed(1)}k` : `${Math.round(x)}`);
const mins = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
const ago = (ts) => {
  const h = (Date.now() - ts) / 3600000;
  return h < 1 ? "just now" : h < 24 ? `${Math.round(h)} h ago` : h < 24 * 30 ? `${Math.round(h / 24)} days ago` : new Date(ts).toLocaleDateString("en-GB");
};
const scoreTag = (p) => (p.score == null ? "" :
  `<span class="score-tag ${p.badge ? p.badge.toLowerCase() : ""}">${p.badge ? `${p.badge} ` : ""}${num(p.score)}</span>`);
const itemsRow = (items, cls = "ico small") => items.map((it) => (it ? icon(it, cls) : `<span class="${cls} blank"></span>`)).join("");

document.querySelectorAll("#hist-groups button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#hist-groups button").forEach((x) => x.classList.toggle("active", x === b));
    hist.group = b.dataset.group;
    loadHistory(true);
  })
);
$("#hist-more").addEventListener("click", () => loadHistory(false));

async function loadHistory(reset) {
  if (reset) hist.offset = 0;
  const data = await (await fetch(`/api/history?group=${hist.group}&offset=${hist.offset}&limit=20`)).json();
  const total = Object.values(data.counts).reduce((a, b) => a + b, 0);
  document.querySelectorAll("#hist-groups button").forEach((b) => {
    const n = b.dataset.group === "all" ? total : data.counts[b.dataset.group] || 0;
    b.querySelector("small").textContent = n ? `(${n})` : "";
  });
  if (reset) {
    renderHistSummary(data);
    $("#hist-list").innerHTML = "";
  }
  $("#hist-list").insertAdjacentHTML("beforeend", data.games.map(gameRow).join("") ||
    (reset ? `<p class="empty">No games here. Click "Sync season" in the "My data" tab.</p>` : ""));
  hist.offset += data.games.length;
  $("#hist-more").hidden = data.games.length < 20;
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
      <div class="big-stat"><strong>${fmtK(s.gold)}</strong><small>avg gold</small></div>
      <div class="big-stat"><strong>${pct(s.kp)}</strong><small>KP</small></div>
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
        <small>${mins(g.duration)}</small>
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
        <small>Gold ${fmtK(m.gold)}</small>
        <small>Vision ${m.vision}</small>
      </div>
      <div class="g-items">${itemsRow(m.items)}</div>
      <div class="g-teams">${g.teams.slice(0, 2).map((t) => `<div>${t.map((p) =>
        `<span class="${p.is_me ? "me" : ""}"><img src="${p.icon}" alt="">${esc(p.name)}</span>`).join("")}</div>`).join("")}</div>
    </div>
    <div class="game-detail" hidden></div>
  </div>`;
}

async function toggleGame(id) {
  const el = document.querySelector(`.game[data-id="${id}"] .game-detail`);
  if (!el.hidden) { el.hidden = true; return; }
  if (!el.innerHTML) {
    const d = await (await fetch(`/api/history/${id}`)).json();
    el.innerHTML = d.teams.map((t) => `
      <div class="table-wrap"><table class="detail-table ${t.win ? "win" : "loss"}">
        <thead><tr><th>${t.win ? "Victory" : "Defeat"} · ${t.kills} kills · ${fmtK(t.gold)} gold</th>
          <th>Score</th><th>KDA</th><th>Damage</th><th>Gold</th><th>CS</th><th>Vision</th><th>Items</th></tr></thead>
        <tbody>${t.players.map((p) => `
          <tr class="${p.is_me ? "me" : ""}">
            <td class="who">${icon(p.champion, "ico")}<span class="lvl">${p.level}</span>
              <span class="spells">${p.spells.map((s) => icon(s, "ico tiny")).join("")}</span>${icon(p.keystone, "ico tiny")}
              <span class="pname">${esc(p.name.split("#")[0])}<small>${POS_PT[p.position] ?? ""}</small></span></td>
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
  el.hidden = false;
}

// ---------------------------------------------------------------- mains
const mainsState = { champs: [], mine: [], mineError: "", filter: "mine", selected: null, sort: "points", view: "all" };

function mainsTable(rep, compact) {
  if (!rep.mains.length)
    return `<p class="muted">No known mains for ${esc(rep.champion.name)} yet. Collect masteries in the "Mains" tab.</p>`;
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
  <p class="muted small-note">* games of these players on this champion that are already in the database.</p>`;
}

function renderCsMains(rep) {
  $("#cs-mains").innerHTML = `
    <p class="muted">Top 5 <strong>${esc(rep.champion.name)}</strong> mains (most mastery points, Challenger/GM EUW + KR)</p>
    ${mainsTable(rep, true)}
    ${rep.sample ? `<details><summary>Mains' runes and build (${rep.sample} games)</summary>${buildDetailHtml(rep)}</details>` : ""}`;
}

async function loadMainsList() {
  const [data, mine] = await Promise.all([
    fetch("/api/mains").then((r) => r.json()),
    fetch("/api/my-mastery").then((r) => r.json()).catch(() => ({ ok: false, champions: [] })),
  ]);
  mainsState.champs = data.champions;
  mainsState.mine = mine.champions || [];
  mainsState.mineError = mine.ok ? "" : mine.error || "";
  $("#mains-count").textContent = data.players ? `${data.players} Challenger/GM players with collected masteries` : "No masteries collected yet";
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

document.querySelectorAll("#mains-filter button").forEach((b) =>
  b.addEventListener("click", () => {
    mainsState.filter = b.dataset.filter;
    document.querySelectorAll("#mains-filter button").forEach((x) => x.classList.toggle("active", x === b));
    renderMainsGrid();
  })
);

async function openMains(id, quiet = false) {
  // quiet = recarregar o mesmo boneco (nomes/jogos novos chegaram) sem limpar o ecrã nem a vista escolhida
  if (mainsState.selected !== id) mainsState.view = "all";
  mainsState.selected = id;
  renderMainsGrid();
  if (!quiet) $("#mains-panel").innerHTML = `<p class="muted">Loading…</p>`;
  const rep = await (await fetch(`/api/mains/${id}?sort=${mainsState.sort}`)).json();
  $("#mains-panel").innerHTML = `
    <div class="mains-head">${icon(rep.champion, "ico big")}<h2>${esc(rep.champion.name)}</h2>
      <label>Sort by <select id="mains-sort">
        <option value="points" ${mainsState.sort === "points" ? "selected" : ""}>Mastery points</option>
        <option value="lp" ${mainsState.sort === "lp" ? "selected" : ""}>LP</option></select></label>
      <button id="mains-refresh" data-job="mains_games" class="secondary">Update these mains' games</button>
      <span id="job-mains_games" class="muted"></span>
    </div>
    ${rep.auto_refresh ? `<p class="muted small-note">Updating these mains' recent games in the background… the page refreshes itself when done.</p>` : ""}
    ${rep.names_pending ? `<p class="muted small-note">Looking up player names…</p>` : ""}
    ${mainsTable(rep, false)}
    ${rep.sample ? `
      <div class="subtabs main-pick">
        <button data-main="all" class="active">All (${rep.sample} games)</button>
        ${rep.mains.map((m, i) => `<button data-main="${i}" ${m.games ? "" : "disabled"}>${i + 1}. ${esc((m.name || "?").split("#")[0])} (${m.games})</button>`).join("")}
        <button data-main="compare">Compare</button>
      </div>
      <div id="main-build"></div>`
      : `<p class="muted">No games of these mains in the database yet.</p>`}`;
  if (rep.sample) {
    const show = (key) => {
      mainsState.view = key;
      document.querySelectorAll(".main-pick button").forEach((b) => b.classList.toggle("active", b.dataset.main === key));
      $("#main-build").innerHTML = key === "all" ? buildDetailHtml(rep)
        : key === "compare" ? compareMains(rep) : buildDetailHtml(rep.mains[+key].build);
    };
    document.querySelectorAll(".main-pick button").forEach((b) => b.addEventListener("click", () => show(b.dataset.main)));
    show(rep.mains[+mainsState.view]?.games || ["all", "compare"].includes(mainsState.view) ? mainsState.view : "all");
  }
  $("#mains-sort").addEventListener("change", (e) => { mainsState.sort = e.target.value; openMains(id); });
  $("#mains-refresh").addEventListener("click", async () => {
    const res = await (await fetch(`/api/mains/${id}/refresh`, { method: "POST" })).json();
    if (!res.ok && res.error) $("#job-mains_games").textContent = res.error;
  });
}

$("#mains-search").addEventListener("input", renderMainsGrid);
$("#mastery-crawl").addEventListener("click", () =>
  fetch("/api/mains/crawl", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ regions: [...document.querySelectorAll(".mastery-region:checked")].map((c) => c.value) }),
  })
);

// ---------------------------------------------------------------- pós-jogo: detalhe
const PG_CATS = {
  farm: "Farming", lane: "Laning phase", deaths: "Deaths", vision: "Vision", wards: "Control wards",
  fights: "Fights / damage", items: "Items", compare: "Comparisons",
};
const pgCats = (() => {
  try { return { ...Object.fromEntries(Object.keys(PG_CATS).map((k) => [k, true])), ...JSON.parse(localStorage.getItem("pgCats") || "{}") }; }
  catch { return Object.fromEntries(Object.keys(PG_CATS).map((k) => [k, true])); }
})();
let PG_REPORTS = [];

function renderPgCats() {
  $("#pg-cats").innerHTML = `<span class="muted">Analyse:</span>` + Object.entries(PG_CATS).map(([k, label]) =>
    `<label class="inline"><input type="checkbox" data-cat="${k}" ${pgCats[k] ? "checked" : ""}> ${label}</label>`).join("");
  $("#pg-cats").querySelectorAll("input").forEach((c) => c.addEventListener("change", () => {
    pgCats[c.dataset.cat] = c.checked;
    try { localStorage.setItem("pgCats", JSON.stringify(pgCats)); } catch {}
    $("#pg-list").innerHTML = PG_REPORTS.map(renderReport).join("");
  }));
}

function pgScore(feedback) {
  const good = feedback.filter((f) => f.type === "good").length;
  const bad = feedback.filter((f) => f.type === "bad").length;
  return Math.round((100 * (good + 1)) / (good + bad + 2));
}

function renderReport(r) {
  const m = r.metrics;
  const date = new Date(r.date).toLocaleString("en-GB");
  const stat = (label, val) => `<div class="stat"><small>${label}</small><strong>${val}</strong></div>`;
  const diff = (v) => (v == null ? "–" : `${v > 0 ? "+" : ""}${v}`);
  const fb = r.feedback.filter((f) => pgCats[f.cat ?? "compare"] !== false);
  return `
  <article class="card report ${r.win ? "win" : "loss"}" data-id="${r.match_id}">
    <header>
      ${icon(r.champion, "ico big")}
      <div>
        <h2>${r.win ? "Victory" : "Defeat"} · ${esc(r.champion.name)} ${r.opponent ? `vs ${esc(r.opponent.name)}` : ""}</h2>
        <span class="muted">${POS_PT[r.position] ?? r.position} · ${r.duration_min} min · ${date}</span>
      </div>
      <div class="score" title="Score based on strengths vs things to improve (selected categories only)">${pgScore(fb)}</div>
    </header>
    <div class="stats-grid">
      ${stat("KDA", `${m.kills}/${m.deaths}/${m.assists}`)}
      ${stat("CS/min", num(m.cspm))}
      ${stat("CS @10", m.cs10 ?? "–")}
      ${stat("Gold @15 vs opp.", diff(m.gold_diff15))}
      ${stat("Vision/min", num(m.vspm, 2))}
      ${stat("Control wards", m.control_wards)}
      ${stat("KP", pct(m.kp))}
      ${stat("Damage %", pct(m.damage_share))}
    </div>
    <ul class="feedback">${fb.map((f) => `<li class="${f.type}">${esc(f.text)}</li>`).join("") || `<li class="info">No category selected.</li>`}</ul>
    <div class="row">
      ${r.detail ? `<button class="secondary" onclick="toggleDetail('${r.match_id}')">Show game details (map, kills, build)</button>`
        : `<button class="secondary" onclick="reanalyse('${r.match_id}')">Generate game details</button>`}
      <button class="secondary" onclick="reanalyse('${r.match_id}')" title="Run the analysis again (e.g. games analysed with an older version)">↻ Re-analyse</button>
    </div>
    <div class="pg-detail" hidden></div>
  </article>`;
}

async function reanalyse(id) {
  $("#pg-msg").textContent = "Analysing…";
  const res = await (await fetch(`/api/postgame/analyse?match_id=${encodeURIComponent(id)}`, { method: "POST" })).json();
  $("#pg-msg").textContent = res.ok ? "" : res.error || "Could not analyse the game.";
  await loadPostgames();
  if (res.ok) toggleDetail(id);
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

function toggleDetail(id) {
  const card = document.querySelector(`.report[data-id="${id}"]`);
  const el = card.querySelector(".pg-detail");
  if (!el.hidden) { el.hidden = true; return; }
  const r = PG_REPORTS.find((x) => x.match_id === id);
  if (!el.innerHTML) el.innerHTML = detailHtml(r);
  el.hidden = false;
  drawMap(r, el);
}

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
          <label class="inline"><input type="checkbox" data-layer="onlyme"> Only events I was in</label>
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
  const el = li.closest(".pg-detail");
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
      ${row("Most used items", (b, m) => m.items.map((it) => icon(it, "ico small")).join("") || "–")}
    </tbody></table></div>`;
}

// ---------------------------------------------------------------- tarefas automáticas
async function loadAuto() {
  const a = await (await fetch("/api/auto")).json();
  $("#auto-enabled").checked = a.enabled;
  const when = (t) => (t ? new Date(t * 1000).toLocaleString("en-GB") : "never");
  $("#auto-last").textContent = `Last run: high elo games ${when(a.last.meta)} · masteries/top 5 ${when(a.last.mastery)} · your games ${when(a.last.sync)}`;
}
$("#auto-enabled").addEventListener("change", async (e) => {
  await fetch(`/api/auto?enabled=${e.target.checked}`, { method: "POST" });
  loadAuto();
});

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
