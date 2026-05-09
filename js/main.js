const PALETTE = ["#58a6ff", "#f85149", "#3fb950", "#d29922", "#a371f7"];
const fmtN = (n) => n.toLocaleString("ja-JP");
const fmtTs = (ts) => ts.length === 10 ? ts : ts.replace("T", " ").replace("Z", "");

async function load() {
  const res = await fetch("data/dashboard.json", { cache: "no-store" });
  const data = await res.json();
  document.getElementById("generated-at").textContent =
    "updated: " + fmtTs(data.generated_at);
  buildNav(data.channels);
  const main = document.getElementById("channels");
  data.channels.forEach((ch, idx) => main.appendChild(renderChannel(ch, idx)));
}

function buildNav(channels) {
  const nav = document.getElementById("channel-nav");
  channels.forEach((ch) => {
    const btn = document.createElement("button");
    btn.textContent = ch.title;
    btn.onclick = () => {
      document.getElementById("ch-" + ch.id).scrollIntoView({ behavior: "smooth", block: "start" });
    };
    nav.appendChild(btn);
  });
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
        <h3>登録者数の推移</h3>
        <div class="chart-canvas"><canvas id="subs-${ch.id}"></canvas></div>
      </div>
      <div class="chart-box">
        <h3>累計 views の推移</h3>
        <div class="chart-canvas"><canvas id="views-${ch.id}"></canvas></div>
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
    drawTimeSeries(`subs-${ch.id}`, subs, "subs", "登録者", PALETTE[idx % PALETTE.length]);
    drawTimeSeries(`views-${ch.id}`, views, "total_views", "累計 views", PALETTE[(idx + 1) % PALETTE.length]);
    renderHits(`hits-${ch.id}`, ch.recent_hits || []);
    renderVideoHistory(ch);
  }, 0);

  return wrap;
}

function drawTimeSeries(canvasId, points, key, label, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !points.length) return;
  const data = points.map((p) => ({ x: parseTs(p.timestamp), y: p[key] }));
  new Chart(canvas, {
    type: "line",
    data: {
      datasets: [{
        label,
        data,
        borderColor: color,
        backgroundColor: color + "33",
        fill: true,
        tension: 0.2,
        pointRadius: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: "#c9d1d9" } } },
      scales: {
        x: {
          type: "time",
          time: { unit: "day", tooltipFormat: "yyyy-MM-dd HH:mm" },
          ticks: { color: "#8b949e" },
          grid: { color: "#30363d44" },
        },
        y: {
          beginAtZero: false,
          ticks: { color: "#8b949e", callback: (v) => fmtN(v) },
          grid: { color: "#30363d44" },
        },
      },
    },
  });
}

function parseTs(ts) {
  if (ts.length === 10) return new Date(ts + "T00:00:00Z");
  return new Date(ts);
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
    const data = v.history.map((p) => ({ x: parseTs(p.date), y: p.views }));
    if (chart) chart.destroy();
    chart = new Chart(canvas, {
      type: "line",
      data: {
        datasets: [{
          label: v.title.slice(0, 50),
          data,
          borderColor: PALETTE[2],
          backgroundColor: PALETTE[2] + "33",
          fill: true,
          tension: 0.2,
          pointRadius: 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: "#c9d1d9" } },
          tooltip: {
            callbacks: {
              afterTitle: () => `published: ${v.published_at}`,
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
            beginAtZero: false,
            ticks: { color: "#8b949e", callback: (v) => fmtN(v) },
            grid: { color: "#30363d44" },
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
