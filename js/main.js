const PALETTE = ["#0969da", "#cf222e", "#1a7f37", "#bf8700", "#8250df"];
const COL_TICK = "#57606a";
const COL_GRID = "#d0d7de66";
const COL_LEGEND = "#1f2328";
const COL_POS = "#1a7f37";
const COL_POS_BG = "#1a7f3766";
const COL_NEG = "#cf222e";
const COL_NEG_BG = "#cf222e66";

const fmtN = (n) => n.toLocaleString("ja-JP");
const fmtTs = (ts) => ts.length === 10 ? ts : ts.replace("T", " ").replace("Z", "");

async function load() {
  // Chart.js global defaults
  Chart.defaults.color = COL_LEGEND;
  Chart.defaults.borderColor = COL_GRID;
  Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Kaku Gothic ProN", "Yu Gothic", Meiryo, sans-serif';

  const res = await fetch("data/dashboard.json", { cache: "no-store" });
  const data = await res.json();
  document.getElementById("generated-at").textContent =
    "updated: " + fmtTs(data.generated_at);
  const main = document.getElementById("channels");
  data.channels.forEach((ch, idx) => main.appendChild(renderChannel(ch, idx)));
  renderRisingWatch(data.channels);
  buildTabs(data.channels);
}

function panelEl(id) {
  if (id === "rising-watch") return document.getElementById("rising-watch-panel");
  return document.getElementById("ch-" + id);
}

function buildTabs(channels) {
  const nav = document.getElementById("channel-nav");
  const tabs = [];

  // 急伸ウォッチ（横断・デフォルト）
  const risingBtn = document.createElement("button");
  risingBtn.textContent = "🔥 急伸ウォッチ";
  risingBtn.onclick = () => activateTab("rising-watch", tabs);
  tabs.push({ id: "rising-watch", btn: risingBtn });
  nav.appendChild(risingBtn);

  channels.forEach((ch) => {
    const btn = document.createElement("button");
    btn.textContent = ch.title;
    btn.onclick = () => activateTab(ch.id, tabs);
    tabs.push({ id: ch.id, btn });
    nav.appendChild(btn);
  });
  activateTab("rising-watch", tabs);
}

function activateTab(id, tabs) {
  tabs.forEach((t) => {
    const isActive = t.id === id;
    t.btn.classList.toggle("active", isActive);
    const p = panelEl(t.id);
    if (p) p.classList.toggle("active", isActive);
  });
  const active = panelEl(id);
  if (active) {
    active.querySelectorAll("canvas").forEach((c) => {
      const chart = Chart.getChart(c);
      if (chart) chart.resize();
    });
  }
}

function renderChannel(ch, idx) {
  const wrap = document.createElement("section");
  wrap.className = "channel";
  wrap.id = "ch-" + ch.id;

  const subs = ch.subs_history || [];
  const views = ch.views_history || [];
  const lastSubs = subs.length ? subs[subs.length - 1].subs : 0;

  // 「その日」= 最新スナップショットの UTC 日付
  const latestTs = subs.length ? (subs[subs.length - 1].timestamp || "") : "";
  const todayDate = latestTs.slice(0, 10);

  const todaySubsDelta = computeTodayDelta(subs, "subs", todayDate);
  const todayViewsDelta = computeTodayDelta(views, "total_views", todayDate);
  const todayPosts = (ch.daily_posts || []).find((d) => d.date === todayDate)?.count || 0;

  // 急伸ウォッチ条件をこのチャンネルだけに適用
  const risingThisCh = collectRising([ch]);
  const hitCount = risingThisCh.filter((r) => r.tier === "hit").length;
  const warmupCount = risingThisCh.filter((r) => r.tier === "warmup").length;

  const fmtDelta = (n) => `${n >= 0 ? "+" : ""}${fmtN(n)}`;
  const deltaCls = (n) => n >= 0 ? "pos" : "neg";

  wrap.innerHTML = `
    <h2>${escapeHtml(ch.title)} <span class="ch-date">(${todayDate || "—"} 時点)</span></h2>
    <div class="kpi-row">
      <div class="kpi">
        <div class="kpi-label">登録者の伸び</div>
        <div class="kpi-value ${deltaCls(todaySubsDelta)}">${fmtDelta(todaySubsDelta)}</div>
        <div class="kpi-sub">累計 ${fmtN(lastSubs)}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">再生数の伸び</div>
        <div class="kpi-value ${deltaCls(todayViewsDelta)}">${fmtDelta(todayViewsDelta)}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">投稿本数</div>
        <div class="kpi-value">${fmtN(todayPosts)}</div>
      </div>
      <div class="kpi" title="3 日合計伸び ≥ 30 または 直近観測 ≥ 20 を満たし、累計 1,000 未満">
        <div class="kpi-label">離陸中</div>
        <div class="kpi-value warmup">${fmtN(warmupCount)}</div>
      </div>
      <div class="kpi" title="3 日合計伸び ≥ 30 または 直近観測 ≥ 20 を満たし、累計 1,000+">
        <div class="kpi-label">HIT</div>
        <div class="kpi-value hit">${fmtN(hitCount)}</div>
      </div>
    </div>

    <div class="charts">
      <div class="chart-box">
        <h3>登録者数 (累計 + 1 日の伸び)</h3>
        <div class="chart-canvas"><canvas id="subs-${ch.id}"></canvas></div>
      </div>
      <div class="chart-box">
        <h3>1 日の再生回数 (チャンネル合計)</h3>
        <div class="chart-canvas"><canvas id="vdelta-${ch.id}"></canvas></div>
      </div>
      <div class="chart-box">
        <h3>1 日の投稿本数 (Shorts 除外、直近 60 日)</h3>
        <div class="chart-canvas"><canvas id="posts-${ch.id}"></canvas></div>
      </div>
    </div>

    <div class="section-title">🔥 急伸動画 (急伸ウォッチと同条件: 3日合計 ≥ 30 または 直近観測 ≥ 20)</div>
    <div id="rising-${ch.id}"></div>

    <div class="section-title">★ 直近 HIT 動画 (age ≤ 14d & 累計 1,000+)</div>
    <div id="hits-${ch.id}"></div>

    <div class="section-title">動画別 views 推移 (↑↓ で動画切替 / ←→ でソート切替)</div>
    <div class="video-sort-tabs" id="vsort-${ch.id}">
      <button class="vsort active" data-key="latest">再生数順</button>
      <button class="vsort" data-key="d1">1 日の伸び</button>
      <button class="vsort" data-key="d2">2 日の伸び</button>
      <button class="vsort" data-key="d3">3 日の伸び</button>
    </div>
    <div class="video-section">
      <div id="vlist-${ch.id}" class="video-list"></div>
      <div>
        <div class="chart-box">
          <div class="video-chart-canvas"><canvas id="vchart-${ch.id}"></canvas></div>
        </div>
        <div class="video-nav">
          <span class="video-nav-hint">↑↓ 動画切替 / ←→ ソート切替</span>
          <span id="vpos-${ch.id}" class="video-nav-pos"></span>
        </div>
      </div>
    </div>
  `;

  setTimeout(() => {
    drawDualSeries(`subs-${ch.id}`, subs, "subs", "登録者数", "+ 登録者 / 日", PALETTE[idx % PALETTE.length]);
    drawDailyDeltaBar(`vdelta-${ch.id}`, views, "total_views", "+ views / 日", PALETTE[(idx + 1) % PALETTE.length]);
    drawDailyPosts(`posts-${ch.id}`, ch.daily_posts || []);
    renderRising(`rising-${ch.id}`, ch);
    renderHits(`hits-${ch.id}`, ch.recent_hits || [], ch);
    renderVideoHistory(ch);
  }, 0);

  return wrap;
}

function computeTodayDelta(history, key, todayDate) {
  // todayDate (YYYY-MM-DD) を基準に、その日始まり (00:00:00Z) 以前で最も新しいスナップショットを base に取って差分を返す。
  // base が存在しなければ、最古スナップショットを使う (=「観測開始以来」になるが当日に複数 snap がある場合は当日内の最古との差で代用)。
  if (!history.length || !todayDate) return 0;
  const sorted = [...history].sort((a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""));
  const dayStartMs = Date.UTC(
    Number(todayDate.slice(0, 4)),
    Number(todayDate.slice(5, 7)) - 1,
    Number(todayDate.slice(8, 10))
  );
  let base = null;
  let firstOfDay = null;
  for (const h of sorted) {
    const t = parseTs(h.timestamp).getTime();
    if (t < dayStartMs) base = h;
    else if (firstOfDay == null) firstOfDay = h;
  }
  const last = sorted[sorted.length - 1];
  if (!base) base = firstOfDay || sorted[0];
  return (last[key] || 0) - (base[key] || 0);
}

function computeRecentDelta(history, key, days) {
  if (!history.length) return { delta: 0, spanLabel: `${days}日` };
  const last = history[history.length - 1];
  const lastDate = parseTs(last.timestamp);
  const cutoff = new Date(lastDate.getTime() - days * 86400000);
  const base = history.find((h) => parseTs(h.timestamp) >= cutoff) || history[0];
  const baseDate = parseTs(base.timestamp);
  const actualDays = Math.max(1, Math.round((lastDate - baseDate) / 86400000));
  const spanLabel = actualDays >= days ? `${days}日` : `${actualDays}日 (短縮)`;
  return { delta: last[key] - base[key], spanLabel };
}

function aggregateDailyLast(history, key) {
  const byDate = {};
  // 時系列順に処理（snapshots.jsonl のファイル順が乱れていても日内最終値が取れる）
  const sorted = [...history].sort((a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""));
  sorted.forEach((p) => {
    const d = (p.timestamp || "").slice(0, 10);
    if (!d) return;
    byDate[d] = p[key];
  });
  // 累計値は単調増加を保証（API 取得の動画セット入れ替わりで見かけ上減ることがあるノイズを除去）
  const dates = Object.keys(byDate).sort();
  let prev = -Infinity;
  return dates.map((d) => {
    const v = Math.max(byDate[d], prev);
    prev = v;
    return { date: d, value: v };
  });
}

function drawDualSeries(canvasId, history, key, cumLabel, deltaLabel, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !history.length) return;
  const daily = aggregateDailyLast(history, key);
  if (!daily.length) return;
  const labels = daily.map((d) => d.date);
  const cumValues = daily.map((d) => d.value);
  const deltaValues = [null];
  for (let i = 1; i < daily.length; i++) {
    deltaValues.push(daily[i].value - daily[i - 1].value);
  }

  new Chart(canvas, {
    data: {
      labels,
      datasets: [
        {
          type: "line",
          label: cumLabel,
          data: cumValues,
          borderColor: color,
          backgroundColor: color + "22",
          fill: false,
          tension: 0.2,
          pointRadius: 5,
          pointHoverRadius: 9,
          borderWidth: 2.5,
          yAxisID: "y",
          order: 2,
        },
        {
          type: "bar",
          label: deltaLabel,
          data: deltaValues,
          backgroundColor: deltaValues.map((v) => v == null ? "transparent" : (v >= 0 ? COL_POS_BG : COL_NEG_BG)),
          borderColor: deltaValues.map((v) => v == null ? "transparent" : (v >= 0 ? COL_POS : COL_NEG)),
          borderWidth: 1,
          yAxisID: "y1",
          order: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false, axis: "x" },
      plugins: {
        legend: { labels: { color: COL_LEGEND } },
        tooltip: {
          mode: "index",
          intersect: false,
          callbacks: {
            label: (ctx) => {
              const v = ctx.parsed.y;
              if (v == null) return null;
              const sign = ctx.dataset.yAxisID === "y1" && v > 0 ? "+" : "";
              return `${ctx.dataset.label}: ${sign}${fmtN(v)}`;
            },
          },
        },
      },
      scales: {
        x: { ticks: { color: COL_TICK }, grid: { color: COL_GRID } },
        y: {
          type: "linear",
          position: "left",
          ticks: { color: COL_TICK, callback: (v) => fmtN(v) },
          grid: { color: COL_GRID },
          title: { display: true, text: cumLabel, color: COL_TICK },
        },
        y1: {
          type: "linear",
          position: "right",
          beginAtZero: true,
          ticks: { color: COL_TICK, callback: (v) => fmtN(v) },
          grid: { drawOnChartArea: false },
          title: { display: true, text: deltaLabel, color: COL_TICK },
        },
      },
    },
  });
}

function drawDailyDeltaBar(canvasId, history, key, label, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !history.length) return;
  const daily = aggregateDailyLast(history, key);
  if (daily.length < 2) {
    canvas.parentElement.innerHTML = `<div style="color:var(--text-dim);padding:1rem;font-size:0.85rem">スナップショット 2 日分以降で表示</div>`;
    return;
  }
  const labels = daily.slice(1).map((d) => d.date);
  const deltas = [];
  for (let i = 1; i < daily.length; i++) {
    deltas.push(daily[i].value - daily[i - 1].value);
  }
  new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label,
        data: deltas,
        backgroundColor: deltas.map((v) => v >= 0 ? COL_POS_BG : COL_NEG_BG),
        borderColor: deltas.map((v) => v >= 0 ? COL_POS : COL_NEG),
        borderWidth: 1,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false, axis: "x" },
      plugins: {
        legend: { labels: { color: COL_LEGEND } },
        tooltip: {
          mode: "index",
          intersect: false,
          callbacks: {
            label: (ctx) => {
              const v = ctx.parsed.y;
              if (v == null) return null;
              return `${label}: ${v > 0 ? "+" : ""}${fmtN(v)}`;
            },
          },
        },
      },
      scales: {
        x: { ticks: { color: COL_TICK }, grid: { color: COL_GRID } },
        y: {
          beginAtZero: true,
          ticks: { color: COL_TICK, callback: (v) => fmtN(v) },
          grid: { color: COL_GRID },
        },
      },
    },
  });
}

function parseTs(ts) {
  if (ts.length === 10) return new Date(ts + "T00:00:00Z");
  return new Date(ts);
}

function drawDailyPosts(canvasId, posts) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  if (!posts.length) {
    canvas.parentElement.innerHTML = `<div style="color:var(--text-dim);padding:1rem;font-size:0.85rem">投稿データなし</div>`;
    return;
  }
  const start = new Date(posts[0].date + "T00:00:00Z");
  const end = new Date(posts[posts.length - 1].date + "T00:00:00Z");
  const map = Object.fromEntries(posts.map((p) => [p.date, p.count]));
  const labels = [];
  const values = [];
  for (let d = new Date(start); d <= end; d.setUTCDate(d.getUTCDate() + 1)) {
    const key = d.toISOString().slice(0, 10);
    labels.push(key);
    values.push(map[key] || 0);
  }
  new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "投稿本数 / 日",
        data: values,
        backgroundColor: PALETTE[0] + "66",
        borderColor: PALETTE[0],
        borderWidth: 1,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false, axis: "x" },
      plugins: {
        legend: { labels: { color: COL_LEGEND } },
        tooltip: { mode: "index", intersect: false },
      },
      scales: {
        x: { ticks: { color: COL_TICK, maxRotation: 60, minRotation: 45 }, grid: { color: COL_GRID } },
        y: {
          beginAtZero: true,
          ticks: { color: COL_TICK, precision: 0 },
          grid: { color: COL_GRID },
        },
      },
    },
  });
}

function renderRising(containerId, ch) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const rising = collectRising([ch]);
  if (!rising.length) {
    el.innerHTML = `<div class="empty">急伸条件を満たす動画なし</div>`;
    return;
  }
  el.className = "hits";
  el.innerHTML = rising.map((r) => {
    const tierBadge = r.tier === "hit"
      ? '<span class="rw-tier rw-tier-hit">HIT</span>'
      : '<span class="rw-tier rw-tier-warmup">離陸中</span>';
    const recent = r.recent || [];
    const dayDeltas = [
      recent[recent.length - 1]?.delta,
      recent[recent.length - 2]?.delta,
      recent[recent.length - 3]?.delta,
    ];
    const fmtCell = (v) => {
      if (v == null) return `<div class="card-metric-value muted">—</div>`;
      const cls = v > 0 ? "pos" : v < 0 ? "neg" : "muted";
      return `<div class="card-metric-value ${cls}">${v > 0 ? "+" : ""}${fmtN(v)}</div>`;
    };
    return `
    <a class="hit ${r.tier === "warmup" ? "rising" : ""}" href="${r.url}" data-vid="${escapeHtml(r.vid)}" target="_blank" rel="noopener" title="クリック: 下のグラフを表示 / Ctrl+クリック: YouTube">
      <div class="hit-title">${escapeHtml(r.title)}</div>
      <div class="hit-meta">${tierBadge}</div>
      <div class="card-metrics">
        <div class="card-metric">
          <div class="card-metric-label">総再生数</div>
          <div class="card-metric-value">${fmtN(r.latest)}</div>
        </div>
        <div class="card-metric">
          <div class="card-metric-label">1日</div>
          ${fmtCell(dayDeltas[0])}
        </div>
        <div class="card-metric">
          <div class="card-metric-label">2日</div>
          ${fmtCell(dayDeltas[1])}
        </div>
        <div class="card-metric">
          <div class="card-metric-label">3日</div>
          ${fmtCell(dayDeltas[2])}
        </div>
      </div>
      <div class="hit-meta" style="margin-top:0.3rem">
        <span>投稿: ${r.published_at}</span>
        <span class="${r.trend.cls}">${r.trend.icon} ${r.trend.label}</span>
      </div>
    </a>
  `;
  }).join("");

  // カードクリック → 下のグラフリストで該当動画を選択 + スクロール
  // 修飾キー (Ctrl/⌘/Shift) や 中クリックは YouTube 遷移を許可
  el.querySelectorAll(".hit").forEach((card) => {
    card.addEventListener("click", (e) => {
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) return;
      e.preventDefault();
      const vid = card.dataset.vid;
      if (!vid) return;
      const list = document.getElementById("vlist-" + ch.id);
      const chart = document.getElementById("vchart-" + ch.id);
      if (!list) return;
      const target = list.querySelector(`.video-row[data-vid="${CSS.escape(vid)}"]`);
      if (target) {
        target.click();
        target.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
      if (chart) {
        chart.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
  });
}

function renderHits(containerId, hits, ch) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!hits.length) {
    el.innerHTML = `<div class="empty">直近 14 日に 1,000+ views の HIT なし</div>`;
    return;
  }
  el.className = "hits";
  el.innerHTML = hits.map((h) => `
    <a class="hit" href="${h.url}" data-vid="${escapeHtml(h.video_id)}" target="_blank" rel="noopener" title="クリック: 下のグラフを表示 / Ctrl+クリック: YouTube">
      <div class="hit-title">${escapeHtml(h.title)}</div>
      <div class="hit-meta">
        <span class="views">${fmtN(h.views)} views</span>
        <span>score ${h.score.toFixed(2)}</span>
        <span>${h.age_days}d ago</span>
      </div>
    </a>
  `).join("");

  if (!ch) return;
  el.querySelectorAll(".hit").forEach((card) => {
    card.addEventListener("click", (e) => {
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) return;
      e.preventDefault();
      const vid = card.dataset.vid;
      if (!vid) return;
      const list = document.getElementById("vlist-" + ch.id);
      const chart = document.getElementById("vchart-" + ch.id);
      if (!list) return;
      const target = list.querySelector(`.video-row[data-vid="${CSS.escape(vid)}"]`);
      if (target) {
        target.click();
        target.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
      if (chart) {
        chart.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
  });
}

function renderVideoHistory(ch) {
  const list = document.getElementById("vlist-" + ch.id);
  const canvas = document.getElementById("vchart-" + ch.id);
  const tabs = document.getElementById("vsort-" + ch.id);
  if (!list || !canvas) return;

  // 「N 日の伸び」= 直近 N 観測ポイント合計の伸び（観測 1 個 ≒ 現状 1 日）
  // accelerating = 直近の伸びが前回の伸びより大きい（緑の丸つける条件）
  const entries = Object.entries(ch.video_history || {}).map(([vid, v]) => {
    const sorted = [...v.history].sort((a, b) => a.date.localeCompare(b.date));
    const latest = sorted[sorted.length - 1].views;
    const at = (n) => sorted.length > n ? sorted[sorted.length - 1 - n].views : 0;
    const lastDelta = latest - at(1);
    const prevDelta = at(1) - at(2);
    // 30+ views の伸びがあって、かつ前回より加速しているものだけマーク
    const accelerating = sorted.length >= 3 && lastDelta > prevDelta && lastDelta >= 30;
    return {
      vid, ...v, latest,
      d1: lastDelta,
      d2: latest - at(2),
      d3: latest - at(3),
      accelerating,
    };
  });

  if (!entries.length) {
    list.innerHTML = `<div class="video-row" style="color:var(--text-dim)">動画データなし</div>`;
    return;
  }

  let sortKey = "latest";
  let entriesView = [...entries].sort((a, b) => (b[sortKey] || 0) - (a[sortKey] || 0));

  let chart = null;
  let activeVid = null;
  let activeIdx = 0;

  const posLabel = document.getElementById("vpos-" + ch.id);
  const panel = document.getElementById("ch-" + ch.id);

  const renderRow = (e) => {
    const tier = e.latest >= 1000 ? "hit" : e.latest < 100 ? "cold" : "warm";
    let metric;
    if (sortKey === "latest") {
      metric = `<span class="views">${fmtN(e.latest)} views</span>`;
    } else {
      const days = sortKey.slice(1);
      const delta = e[sortKey] || 0;
      metric = `<span class="vrow-label">直近${days}日の伸び</span><span class="vrow-delta ${delta >= 0 ? "pos" : "neg"}">${delta >= 0 ? "+" : ""}${fmtN(delta)}</span><span class="vrow-cum">累計 ${fmtN(e.latest)}</span>`;
    }
    const accel = e.accelerating
      ? '<span class="vrow-accel" title="直近の伸びが前回より加速">●</span>'
      : '';
    return `
      <div class="video-row tier-${tier}" data-vid="${e.vid}">
        <div class="video-row-title">${escapeHtml(e.title)}</div>
        <div class="video-row-meta">
          ${metric}
          <span>${e.published_at}</span>
          ${accel}
        </div>
      </div>`;
  };

  const renderList = () => {
    list.innerHTML = entriesView.map(renderRow).join("");
    list.querySelectorAll(".video-row").forEach((row) => {
      row.addEventListener("click", () => draw(row.dataset.vid));
    });
    // 選択動画の状態を維持
    if (activeVid) {
      const i = entriesView.findIndex((x) => x.vid === activeVid);
      activeIdx = i >= 0 ? i : 0;
      list.querySelectorAll(".video-row").forEach((row) => {
        row.classList.toggle("active", row.dataset.vid === activeVid);
      });
      if (posLabel) posLabel.textContent = `${activeIdx + 1} / ${entriesView.length}`;
    }
  };

  const setSort = (key) => {
    if (!tabs) return;
    const btns = [...tabs.querySelectorAll(".vsort")];
    const btn = btns.find((b) => b.dataset.key === key);
    if (!btn) return;
    btns.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    sortKey = key;
    entriesView = [...entries].sort((a, b) => (b[sortKey] || 0) - (a[sortKey] || 0));
    renderList();
  };
  if (tabs) {
    tabs.querySelectorAll(".vsort").forEach((btn) => {
      btn.addEventListener("click", () => setSort(btn.dataset.key));
    });
  }

  const draw = (vid) => {
    if (vid === activeVid) return;
    activeVid = vid;
    activeIdx = entriesView.findIndex((x) => x.vid === vid);
    const v = entriesView[activeIdx];
    if (!v) return;
    list.querySelectorAll(".video-row").forEach((row) => {
      const isActive = row.dataset.vid === vid;
      row.classList.toggle("active", isActive);
      if (isActive) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
    if (posLabel) posLabel.textContent = `${activeIdx + 1} / ${entriesView.length}`;
    // 日付順 + cumulative max で単調増加を保証
    const sortedH = [...v.history].sort((a, b) => a.date.localeCompare(b.date));
    let prevV = 0;
    const monotone = sortedH.map((p) => {
      const y = Math.max(p.views, prevV);
      prevV = y;
      return { date: p.date, views: y };
    });
    const cumData = monotone.map((p) => ({ x: parseTs(p.date), y: p.views }));
    const deltaData = [];
    for (let i = 1; i < monotone.length; i++) {
      deltaData.push({
        x: parseTs(monotone[i].date),
        y: monotone[i].views - monotone[i - 1].views,
      });
    }
    if (chart) chart.destroy();
    chart = new Chart(canvas, {
      data: {
        datasets: [
          {
            type: "line",
            label: "累計 views",
            data: cumData,
            borderColor: PALETTE[0],
            backgroundColor: PALETTE[0] + "22",
            fill: false,
            tension: 0.2,
            pointRadius: 5,
            pointHoverRadius: 9,
            borderWidth: 2.5,
            yAxisID: "y",
            order: 2,
          },
          {
            type: "bar",
            label: "1 日の伸び",
            data: deltaData,
            backgroundColor: deltaData.map((d) => d.y >= 0 ? COL_POS_BG : COL_NEG_BG),
            borderColor: deltaData.map((d) => d.y >= 0 ? COL_POS : COL_NEG),
            borderWidth: 1,
            yAxisID: "y1",
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", intersect: false, axis: "x" },
        plugins: {
          legend: { labels: { color: COL_LEGEND } },
          tooltip: {
            mode: "index",
            intersect: false,
            callbacks: {
              afterTitle: () => `${escapeHtml(v.title)}\npublished: ${v.published_at}`,
              label: (ctx) => {
                const val = ctx.parsed.y;
                if (val == null) return null;
                const sign = ctx.dataset.yAxisID === "y1" && val > 0 ? "+" : "";
                return `${ctx.dataset.label}: ${sign}${fmtN(val)}`;
              },
            },
          },
        },
        scales: {
          x: {
            type: "time",
            time: { unit: "day", tooltipFormat: "yyyy-MM-dd" },
            ticks: { color: COL_TICK },
            grid: { color: COL_GRID },
          },
          y: {
            type: "linear",
            position: "left",
            beginAtZero: true,
            suggestedMax: 1250,
            ticks: { color: COL_TICK, callback: (v) => fmtN(v) },
            grid: { color: COL_GRID },
            title: { display: true, text: "累計 views (1,000 で HIT)", color: COL_TICK },
          },
          y1: {
            type: "linear",
            position: "right",
            beginAtZero: true,
            suggestedMax: 1250,
            ticks: { color: COL_TICK, callback: (v) => fmtN(v) },
            grid: { drawOnChartArea: false },
            title: { display: true, text: "1 日の伸び (1,000 で HIT)", color: COL_TICK },
          },
        },
      },
    });
  };

  renderList();

  const scrollToChart = () => {
    if (canvas) canvas.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  const SORT_KEYS = ["latest", "d1", "d2", "d3"];
  document.addEventListener("keydown", (e) => {
    if (!panel || !panel.classList.contains("active")) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (e.key === "ArrowDown" || e.key === "j") {
      if (activeIdx < entriesView.length - 1) {
        draw(entriesView[activeIdx + 1].vid);
        scrollToChart();
        e.preventDefault();
      }
    } else if (e.key === "ArrowUp" || e.key === "k") {
      if (activeIdx > 0) {
        draw(entriesView[activeIdx - 1].vid);
        scrollToChart();
        e.preventDefault();
      }
    } else if (e.key === "ArrowRight" || e.key === "l") {
      const i = SORT_KEYS.indexOf(sortKey);
      if (i >= 0 && i < SORT_KEYS.length - 1) {
        setSort(SORT_KEYS[i + 1]);
        e.preventDefault();
      }
    } else if (e.key === "ArrowLeft" || e.key === "h") {
      const i = SORT_KEYS.indexOf(sortKey);
      if (i > 0) {
        setSort(SORT_KEYS[i - 1]);
        e.preventDefault();
      }
    }
  });

  draw(entriesView[0].vid);
}

// === 急伸ウォッチ（横断ビュー） ===
//
// 各動画について「日次デルタ」を計算 (history は date,views のペア)。
// その後直近 3 日（最終観測日とその前 2 日）のデルタを抜き出し、
//   - 3 日合計 >= 30 view、または
//   - 直近 1 日 >= 20 view
// なら "rising" として拾う。1,000 未満の離陸中動画も対象。

const RISING_DEFAULT = {
  windowDays: 3,         // 3 日窓で合計伸び閾値を判定
  totalDeltaMin: 30,
  todayDeltaMin: 20,
  recentObservations: 6, // テーブルに表示する直近観測ポイント数
};

function obsDeltas(history) {
  // monotone clamp してから観測ポイント間の差分
  const sorted = [...history].sort((a, b) => a.date.localeCompare(b.date));
  let prev = 0;
  const mono = sorted.map((p) => {
    const v = Math.max(p.views, prev);
    prev = v;
    return { ts: p.date, views: v };
  });
  const deltas = [];
  for (let i = 1; i < mono.length; i++) {
    deltas.push({ ts: mono[i].ts, delta: mono[i].views - mono[i - 1].views });
  }
  return { mono, deltas };
}

function trendIcon(deltas) {
  // 直近 3 デルタの加速度ざっくり判定
  const tail = deltas.slice(-3).map((d) => d.delta);
  if (tail.length < 2) return { icon: "—", label: "観測不足", cls: "trend-flat" };
  const last = tail[tail.length - 1];
  const prev = tail[tail.length - 2];
  if (last > prev * 1.3 && last > 0) return { icon: "▲", label: "加速", cls: "trend-accel" };
  if (last < prev * 0.5 && prev > 0) return { icon: "▽", label: "減速", cls: "trend-decel" };
  if (last <= 0) return { icon: "■", label: "停止", cls: "trend-flat" };
  return { icon: "→", label: "横ばい", cls: "trend-flat" };
}

function collectRising(channels, cfg = RISING_DEFAULT) {
  const out = [];
  const cutoffMs = Date.now() - cfg.windowDays * 86400000;
  channels.forEach((ch) => {
    const vh = ch.video_history || {};
    for (const [vid, v] of Object.entries(vh)) {
      if (!v.history || v.history.length < 2) continue;
      const { mono, deltas } = obsDeltas(v.history);
      const latest = mono[mono.length - 1].views;
      // 3 日窓内のデルタ合計（観測点数が変動しても期間ベース）
      const windowDeltas = deltas.filter((d) => parseTs(d.ts).getTime() >= cutoffMs);
      const totalDelta = windowDeltas.reduce((s, d) => s + Math.max(0, d.delta), 0);
      const todayDelta = windowDeltas.length ? windowDeltas[windowDeltas.length - 1].delta : 0;
      if (totalDelta < cfg.totalDeltaMin && todayDelta < cfg.todayDeltaMin) continue;
      // 表示用には直近 N 観測ポイント
      const recent = deltas.slice(-cfg.recentObservations);
      // age 30d 超の古い動画はノイズなので除外
      const pub = (v.published_at || "").slice(0, 10);
      let ageDays = 999;
      if (pub) {
        const ageMs = Date.now() - new Date(pub + "T00:00:00Z").getTime();
        ageDays = Math.floor(ageMs / 86400000);
      }
      if (ageDays > 30 && latest < 500) continue;
      out.push({
        vid,
        title: v.title,
        url: v.url || `https://www.youtube.com/watch?v=${vid}`,
        published_at: v.published_at || "",
        ageDays,
        channelTitle: ch.title,
        channelId: ch.id,
        latest,
        totalDelta,
        todayDelta,
        recent,
        trend: trendIcon(deltas),
        tier: latest >= 1000 ? "hit" : "warmup",
      });
    }
  });
  // 3 日合計伸び降順
  out.sort((a, b) => b.totalDelta - a.totalDelta);
  return out;
}

function renderRisingWatch(channels) {
  const panel = document.getElementById("rising-watch-panel");
  if (!panel) return;
  const rows = collectRising(channels);

  // 全動画の観測ポイント (ts) を集約してソート → 直近 N 個を列ヘッダに採用
  const allTs = new Set();
  rows.forEach((r) => r.recent.forEach((d) => allTs.add(d.ts)));
  const recentTs = [...allTs].sort().slice(-RISING_DEFAULT.recentObservations);

  // チャンネル毎件数（ボタン用ラベル）
  const byChannel = {};
  rows.forEach((r) => {
    byChannel[r.channelId] = byChannel[r.channelId] || { title: r.channelTitle, count: 0 };
    byChannel[r.channelId].count += 1;
  });

  panel.innerHTML = `
    <div class="rw-header">
      <h2>🔥 急伸ウォッチ — 直近 3 日に伸びている動画</h2>
      <p class="rw-help">既に HIT (累計 1,000+) と 離陸中 (1,000 未満) を含む。「3日合計伸び」降順。<strong>▲</strong> = 直近で加速、<strong>▽</strong> = 減速、<strong>→</strong> = 横ばい、<strong>■</strong> = 停止。</p>

      <div class="rw-filter-group">
        <span class="rw-filter-label">チャンネル</span>
        <div class="rw-filters">
          <button class="rw-filter rw-filter-ch active" data-ch="all">横断 (${rows.length})</button>
          ${Object.entries(byChannel).map(([cid, c]) => `
            <button class="rw-filter rw-filter-ch" data-ch="${escapeHtml(cid)}">${escapeHtml(c.title)} (${c.count})</button>
          `).join("")}
        </div>
      </div>

      <div class="rw-filter-group">
        <span class="rw-filter-label">tier</span>
        <div class="rw-filters">
          <button class="rw-filter rw-filter-tier active" data-tier="all">全部</button>
          <button class="rw-filter rw-filter-tier" data-tier="hit">既に HIT (${rows.filter((r) => r.tier === "hit").length})</button>
          <button class="rw-filter rw-filter-tier" data-tier="warmup">離陸中 (${rows.filter((r) => r.tier === "warmup").length})</button>
        </div>
      </div>
    </div>
    <div class="rw-table-wrap">
      <table class="rw-table">
        <thead>
          <tr>
            <th class="rw-col-title">動画 (チャンネル)</th>
            <th class="rw-col-now">累計</th>
            <th class="rw-col-age">age</th>
            ${recentTs.map((t) => `<th class="rw-col-day">${fmtObsLabel(t)}</th>`).join("")}
            <th class="rw-col-total">3日合計</th>
            <th class="rw-col-trend">勢い</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((r) => renderRisingRow(r, recentTs)).join("")}
        </tbody>
      </table>
      <div class="rw-empty empty" style="display:${rows.length === 0 ? "block" : "none"};margin-top:1rem">該当動画なし。</div>
    </div>
  `;

  // フィルタ状態
  let curTier = "all";
  let curCh = "all";
  const applyFilters = () => {
    let visible = 0;
    panel.querySelectorAll(".rw-row").forEach((row) => {
      const tierOk = curTier === "all" || row.dataset.tier === curTier;
      const chOk = curCh === "all" || row.dataset.ch === curCh;
      const show = tierOk && chOk;
      row.style.display = show ? "" : "none";
      if (show) visible += 1;
    });
    const empty = panel.querySelector(".rw-empty");
    if (empty) {
      empty.style.display = visible === 0 ? "block" : "none";
      empty.textContent = visible === 0 ? "該当動画なし（フィルタ条件で 0 件）。" : "";
    }
  };

  panel.querySelectorAll(".rw-filter-tier").forEach((btn) => {
    btn.addEventListener("click", () => {
      panel.querySelectorAll(".rw-filter-tier").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      curTier = btn.dataset.tier;
      applyFilters();
    });
  });
  panel.querySelectorAll(".rw-filter-ch").forEach((btn) => {
    btn.addEventListener("click", () => {
      panel.querySelectorAll(".rw-filter-ch").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      curCh = btn.dataset.ch;
      applyFilters();
    });
  });
}

function fmtObsLabel(ts) {
  // "2026-05-13T03:00:23Z" → "05-13 03:00"、"2026-05-12" → "05-12"
  if (!ts) return "";
  if (ts.length <= 10) return ts.slice(5);
  return `${ts.slice(5, 10)}<br>${ts.slice(11, 16)}`;
}

function renderRisingRow(r, recentTs) {
  // 観測点デルタを ts でマップ
  const byTs = Object.fromEntries(r.recent.map((d) => [d.ts, d.delta]));
  const dayCells = recentTs.map((t) => {
    const v = byTs[t];
    if (v == null) return `<td class="rw-day rw-day-empty">–</td>`;
    const cls = v <= 0 ? "rw-day-zero" : v >= 80 ? "rw-day-strong" : v >= 30 ? "rw-day-mid" : "rw-day-low";
    return `<td class="rw-day ${cls}">${v > 0 ? "+" : ""}${fmtN(v)}</td>`;
  }).join("");
  const tierLabel = r.tier === "hit" ? '<span class="rw-tier rw-tier-hit">HIT</span>' : '<span class="rw-tier rw-tier-warmup">離陸中</span>';
  const ageLabel = r.ageDays === 0 ? "今日" : `${r.ageDays}d`;
  return `
    <tr class="rw-row" data-tier="${r.tier}" data-ch="${escapeHtml(r.channelId)}">
      <td class="rw-col-title">
        <a href="${r.url}" target="_blank" rel="noopener" class="rw-title">${escapeHtml(r.title)}</a>
        <div class="rw-sub">
          ${tierLabel}
          <span class="rw-channel">${escapeHtml(r.channelTitle)}</span>
        </div>
      </td>
      <td class="rw-num">${fmtN(r.latest)}</td>
      <td class="rw-age">${ageLabel}</td>
      ${dayCells}
      <td class="rw-total">+${fmtN(r.totalDelta)}</td>
      <td class="rw-trend ${r.trend.cls}" title="${r.trend.label}">${r.trend.icon}</td>
    </tr>
  `;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

load().catch((err) => {
  console.error(err);
  document.body.innerHTML = `<pre style="color:#cf222e;padding:2rem">load error: ${err.message}</pre>`;
});
