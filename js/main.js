const PALETTE = ["#58a6ff", "#f85149", "#3fb950", "#d29922", "#a371f7"];
const fmtN = (n) => n.toLocaleString("ja-JP");
const fmtTs = (ts) => ts.length === 10 ? ts : ts.replace("T", " ").replace("Z", "");

async function load() {
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
  channels.forEach((ch, idx) => {
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
  const lastViews = views.length ? views[views.length - 1].total_views : 0;
  const firstViews = views.length ? views[0].total_views : 0;
  const viewsDelta = lastViews - firstViews;

  wrap.innerHTML = `
    <h2>${escapeHtml(ch.title)}</h2>
    <div class="kpi-row">
      <div class="kpi">
        <div class="kpi-label">登録者数</div>
        <div class="kpi-value">${fmtN(lastSubs)}<span class="kpi-delta ${subsDelta >= 0 ? "pos" : "neg"}">${subsDelta >= 0 ? "+" : ""}${fmtN(subsDelta)}</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-label">累計 views (取得 window)</div>
        <div class="kpi-value">${fmtN(lastViews)}<span class="kpi-delta ${viewsDelta >= 0 ? "pos" : "neg"}">${viewsDelta >= 0 ? "+" : ""}${fmtN(viewsDelta)}</span></div>
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

    <div class="section-title">動画別 views 推移</div>
    <div class="video-picker">
      <select id="vsel-${ch.id}"></select>
    </div>
    <div class="chart-box">
      <div class="video-chart-canvas"><canvas id="vchart-${ch.id}"></canvas></div>
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
          backgroundColor: color + "33",
          fill: false,
          tension: 0.2,
          pointRadius: 5,
          pointHoverRadius: 8,
          borderWidth: 2.5,
          yAxisID: "y",
          order: 2,
        },
        {
          type: "bar",
          label: deltaLabel,
          data: deltaValues,
          backgroundColor: deltaValues.map((v) => v == null ? "transparent" : (v >= 0 ? "#3fb95099" : "#f8514999")),
          borderColor: deltaValues.map((v) => v == null ? "transparent" : (v >= 0 ? "#3fb950" : "#f85149")),
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
        legend: { labels: { color: "#c9d1d9" } },
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
        x: { ticks: { color: "#8b949e" }, grid: { color: "#30363d44" } },
        y: {
          type: "linear",
          position: "left",
          ticks: { color: "#8b949e", callback: (v) => fmtN(v) },
          grid: { color: "#30363d44" },
          title: { display: true, text: cumLabel, color: "#8b949e" },
        },
        y1: {
          type: "linear",
          position: "right",
          beginAtZero: true,
          ticks: { color: "#8b949e", callback: (v) => fmtN(v) },
          grid: { drawOnChartArea: false },
          title: { display: true, text: deltaLabel, color: "#8b949e" },
        },
      },
    },
  });
}

function parseTs(ts) {
  if (ts.length === 10) return new Date(ts + "T00:00:00Z");
  return new Date(ts);
}

function aggregateDailyLast(history, key) {
  // 同じ日付内の複数スナップショットは「最後の値」を採用
  const byDate = {};
  history.forEach((p) => {
    const d = (p.timestamp || "").slice(0, 10);
    if (!d) return;
    byDate[d] = p[key];
  });
  return Object.keys(byDate).sort().map((d) => ({ date: d, value: byDate[d] }));
}

function drawDailyPosts(canvasId, posts) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  if (!posts.length) {
    canvas.parentElement.innerHTML = `<div style="color:var(--text-dim);padding:1rem;font-size:0.85rem">投稿データなし</div>`;
    return;
  }
  // 投稿のない日も含めて日付範囲を埋める
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
        backgroundColor: "#58a6ff99",
        borderColor: "#58a6ff",
        borderWidth: 1,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false, axis: "x" },
      plugins: {
        legend: { labels: { color: "#c9d1d9" } },
        tooltip: { mode: "index", intersect: false },
      },
      scales: {
        x: { ticks: { color: "#8b949e", maxRotation: 60, minRotation: 45 }, grid: { color: "#30363d44" } },
        y: {
          beginAtZero: true,
          ticks: { color: "#8b949e", precision: 0 },
          grid: { color: "#30363d44" },
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
  const sel = document.getElementById("vsel-" + ch.id);
  const canvas = document.getElementById("vchart-" + ch.id);
  if (!sel || !canvas) return;

  const entries = Object.entries(ch.video_history || {})
    .map(([vid, v]) => ({ vid, ...v, latest: v.history[v.history.length - 1].views }))
    .sort((a, b) => b.latest - a.latest);

  if (!entries.length) {
    sel.innerHTML = `<option>動画データなし</option>`;
    return;
  }

  sel.innerHTML = entries.map((e) =>
    `<option value="${e.vid}">${e.latest.toLocaleString()} views | ${e.published_at} | ${escapeHtml(e.title.slice(0, 60))}</option>`
  ).join("");

  let chart = null;
  const draw = () => {
    const v = entries.find((x) => x.vid === sel.value);
    if (!v) return;
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
            backgroundColor: PALETTE[0] + "33",
            fill: false,
            tension: 0.2,
            pointRadius: 4,
            yAxisID: "y",
          },
          {
            type: "bar",
            label: "1 日の伸び",
            data: deltaData,
            backgroundColor: deltaData.map((d) => d.y >= 0 ? "#3fb95099" : "#f8514999"),
            borderColor: deltaData.map((d) => d.y >= 0 ? "#3fb950" : "#f85149"),
            borderWidth: 1,
            yAxisID: "y1",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", intersect: false, axis: "x" },
        plugins: {
          legend: { labels: { color: "#c9d1d9" } },
          tooltip: {
            mode: "index",
            intersect: false,
            callbacks: {
              afterTitle: () => `published: ${v.published_at}`,
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
            ticks: { color: "#8b949e" },
            grid: { color: "#30363d44" },
          },
          y: {
            type: "linear",
            position: "left",
            beginAtZero: false,
            ticks: { color: "#8b949e", callback: (v) => fmtN(v) },
            grid: { color: "#30363d44" },
            title: { display: true, text: "累計 views", color: "#8b949e" },
          },
          y1: {
            type: "linear",
            position: "right",
            beginAtZero: true,
            ticks: { color: "#8b949e", callback: (v) => fmtN(v) },
            grid: { drawOnChartArea: false },
            title: { display: true, text: "1 日の伸び", color: "#8b949e" },
          },
        },
      },
    });
  };

  sel.value = entries[0].vid;
  sel.onchange = draw;
  draw();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

load().catch((err) => {
  console.error(err);
  document.body.innerHTML = `<pre style="color:#f85149;padding:2rem">load error: ${err.message}</pre>`;
});
