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
  buildTabs(data.channels);
}

function buildTabs(channels) {
  const nav = document.getElementById("channel-nav");
  const tabs = [];
  channels.forEach((ch) => {
    const btn = document.createElement("button");
    btn.textContent = ch.title;
    btn.onclick = () => activateTab(ch.id, tabs);
    tabs.push({ id: ch.id, btn });
    nav.appendChild(btn);
  });
  if (tabs.length) activateTab(tabs[0].id, tabs);
}

function activateTab(id, tabs) {
  tabs.forEach((t) => {
    const isActive = t.id === id;
    t.btn.classList.toggle("active", isActive);
    const panel = document.getElementById("ch-" + t.id);
    if (panel) panel.classList.toggle("active", isActive);
  });
  const active = document.getElementById("ch-" + id);
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
  const firstSubs = subs.length ? subs[0].subs : 0;
  const subsDelta = lastSubs - firstSubs;

  // 直近 1 週間の views 増分（snapshot が短いなら最古からの増分）
  const last7 = computeRecentDelta(views, "total_views", 7);

  wrap.innerHTML = `
    <h2>${escapeHtml(ch.title)}</h2>
    <div class="kpi-row">
      <div class="kpi">
        <div class="kpi-label">登録者数</div>
        <div class="kpi-value">${fmtN(lastSubs)}<span class="kpi-delta ${subsDelta >= 0 ? "pos" : "neg"}">${subsDelta >= 0 ? "+" : ""}${fmtN(subsDelta)}</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-label">直近 ${last7.spanLabel} の views</div>
        <div class="kpi-value"><span class="${last7.delta >= 0 ? "pos" : "neg"}" style="color:var(--${last7.delta >= 0 ? "pos" : "hit"})">${last7.delta >= 0 ? "+" : ""}${fmtN(last7.delta)}</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-label">スナップショット</div>
        <div class="kpi-value">${subs.length}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">★直近 HIT (14d/1k+)</div>
        <div class="kpi-value">${(ch.recent_hits || []).length}</div>
      </div>
    </div>

    <div class="charts">
      <div class="chart-box">
        <h3>登録者数 (累計 + 1 日の伸び)</h3>
        <div class="chart-canvas"><canvas id="subs-${ch.id}"></canvas></div>
      </div>
      <div class="chart-box">
        <h3>累計 views (累計 + 1 日の伸び)</h3>
        <div class="chart-canvas"><canvas id="views-${ch.id}"></canvas></div>
      </div>
      <div class="chart-box charts-full">
        <h3>1 日の投稿本数 (Shorts 除外、直近 60 日)</h3>
        <div class="chart-canvas"><canvas id="posts-${ch.id}"></canvas></div>
      </div>
    </div>

    <div class="section-title">★ 直近 HIT 動画</div>
    <div id="hits-${ch.id}"></div>

    <div class="section-title">動画別 views 推移 (左リスト click + ↑↓キーで切替)</div>
    <div class="video-section">
      <div id="vlist-${ch.id}" class="video-list"></div>
      <div>
        <div class="chart-box">
          <div class="video-chart-canvas"><canvas id="vchart-${ch.id}"></canvas></div>
        </div>
        <div class="video-nav">
          <span class="video-nav-hint">↑↓ キーで前後の動画に切替</span>
          <span id="vpos-${ch.id}" class="video-nav-pos"></span>
        </div>
      </div>
    </div>
  `;

  setTimeout(() => {
    drawDualSeries(`subs-${ch.id}`, subs, "subs", "登録者数", "+ 登録者 / 日", PALETTE[idx % PALETTE.length]);
    drawDualSeries(`views-${ch.id}`, views, "total_views", "累計 views", "+ views / 日", PALETTE[(idx + 1) % PALETTE.length]);
    drawDailyPosts(`posts-${ch.id}`, ch.daily_posts || []);
    renderHits(`hits-${ch.id}`, ch.recent_hits || []);
    renderVideoHistory(ch);
  }, 0);

  return wrap;
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
  history.forEach((p) => {
    const d = (p.timestamp || "").slice(0, 10);
    if (!d) return;
    byDate[d] = p[key];
  });
  return Object.keys(byDate).sort().map((d) => ({ date: d, value: byDate[d] }));
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

function renderHits(containerId, hits) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!hits.length) {
    el.innerHTML = `<div class="empty">直近 14 日に 1,000+ views の HIT なし</div>`;
    return;
  }
  el.className = "hits";
  el.innerHTML = hits.map((h) => `
    <a class="hit" href="${h.url}" target="_blank" rel="noopener">
      <div class="hit-title">${escapeHtml(h.title)}</div>
      <div class="hit-meta">
        <span class="views">${fmtN(h.views)} views</span>
        <span>score ${h.score.toFixed(2)}</span>
        <span>${h.age_days}d ago</span>
      </div>
    </a>
  `).join("");
}

function renderVideoHistory(ch) {
  const list = document.getElementById("vlist-" + ch.id);
  const canvas = document.getElementById("vchart-" + ch.id);
  if (!list || !canvas) return;

  const entries = Object.entries(ch.video_history || {})
    .map(([vid, v]) => ({ vid, ...v, latest: v.history[v.history.length - 1].views }))
    .sort((a, b) => b.latest - a.latest);

  if (!entries.length) {
    list.innerHTML = `<div class="video-row" style="color:var(--text-dim)">動画データなし</div>`;
    return;
  }

  let chart = null;
  let activeVid = null;
  let activeIdx = 0;

  const posLabel = document.getElementById("vpos-" + ch.id);
  const panel = document.getElementById("ch-" + ch.id);

  const draw = (vid) => {
    if (vid === activeVid) return;
    activeVid = vid;
    activeIdx = entries.findIndex((x) => x.vid === vid);
    const v = entries[activeIdx];
    if (!v) return;
    list.querySelectorAll(".video-row").forEach((row) => {
      const isActive = row.dataset.vid === vid;
      row.classList.toggle("active", isActive);
      if (isActive) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
    if (posLabel) posLabel.textContent = `${activeIdx + 1} / ${entries.length}`;
    const cumData = v.history.map((p) => ({ x: parseTs(p.date), y: p.views }));
    const deltaData = [];
    for (let i = 1; i < v.history.length; i++) {
      deltaData.push({
        x: parseTs(v.history[i].date),
        y: v.history[i].views - v.history[i - 1].views,
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

  list.innerHTML = entries.map((e) => {
    const tier = e.latest >= 1000 ? "hit" : e.latest < 100 ? "cold" : "warm";
    return `
    <div class="video-row tier-${tier}" data-vid="${e.vid}">
      <div class="video-row-title">${escapeHtml(e.title)}</div>
      <div class="video-row-meta">
        <span class="views">${fmtN(e.latest)} views</span>
        <span>${e.published_at}</span>
      </div>
    </div>`;
  }).join("");

  list.querySelectorAll(".video-row").forEach((row) => {
    row.addEventListener("click", () => draw(row.dataset.vid));
  });

  const scrollToChart = () => {
    if (canvas) canvas.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  document.addEventListener("keydown", (e) => {
    if (!panel || !panel.classList.contains("active")) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (e.key === "ArrowDown" || e.key === "ArrowRight" || e.key === "j") {
      if (activeIdx < entries.length - 1) {
        draw(entries[activeIdx + 1].vid);
        scrollToChart();
        e.preventDefault();
      }
    } else if (e.key === "ArrowUp" || e.key === "ArrowLeft" || e.key === "k") {
      if (activeIdx > 0) {
        draw(entries[activeIdx - 1].vid);
        scrollToChart();
        e.preventDefault();
      }
    }
  });

  draw(entries[0].vid);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

load().catch((err) => {
  console.error(err);
  document.body.innerHTML = `<pre style="color:#cf222e;padding:2rem">load error: ${err.message}</pre>`;
});
