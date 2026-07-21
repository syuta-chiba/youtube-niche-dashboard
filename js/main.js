const PALETTE = ["#0969da", "#cf222e", "#1a7f37", "#bf8700", "#8250df"];
const COL_TICK = "#57606a";
const COL_GRID = "#d0d7de66";
const COL_LEGEND = "#1f2328";
const COL_POS = "#1a7f37";
const COL_POS_BG = "#1a7f3766";
const COL_NEG = "#cf222e";
const COL_NEG_BG = "#cf222e66";

const fmtN = (n) => n.toLocaleString("ja-JP");

// 広告判定バッジ (build_pages_data.py が like数基準線から算出した ad_check を表示)
// base = ch.like_baseline: 非HIT動画への Theil-Sen 回帰 {fan_likes, slope_pct, n}。
//   期待like数 = fan_likes(固定ファン分) + slope×views(一見視聴者のlike率)。
// 実測likeが期待値の50%未満 (または like率0.4%未満) なら suspect = 購入views疑い。
// 仕組みの解説: docs/ad_check_methodology.md
function adBadge(v, base) {
  const expTxt = v.likes_vs_exp_pct != null ? ` (期待比${v.likes_vs_exp_pct}%)` : "";
  const baseTxt = base
    ? `基準線 (非HIT ${base.n}本から回帰): 期待like ≒ ファン分${base.fan_likes}個 + ${base.slope_pct}% × views`
    : "基準線データ不足のため絶対 0.4% のみで判定";
  const low =
    v.likes_vs_exp_pct != null && v.likes_vs_exp_pct < 100 && v.ad_check === "ok";
  const lowMark = low
    ? `<span class="ad-flag ad-below" title="実測 like ${v.likes ?? "?"}個は基準線の期待 ${v.exp_likes}個 の ${v.likes_vs_exp_pct}%。50%以上なので疑いには至らないが、期待値割れは弱いサイン。${baseTxt}">▼期待割れ</span>`
    : "";
  if (v.ad_check === "suspect")
    return `<span class="ad-flag ad-suspect" title="like率 ${v.like_pct}% / 実測 like ${v.likes ?? "?"}個${v.exp_likes != null ? ` (期待 ${v.exp_likes}個)` : ""} — like率0.4%未満 または 期待like数の50%未満は広告/購入views疑い。${baseTxt}">⚠️広告疑 like ${v.like_pct}%${expTxt}</span>`;
  if (v.ad_check === "ok")
    return `<span class="ad-flag ad-ok" title="like率 ${v.like_pct}% / 実測 like ${v.likes ?? "?"}個${v.exp_likes != null ? ` (期待 ${v.exp_likes}個)` : ""} — 健全域。${baseTxt}">like ${v.like_pct}%${expTxt}</span>${lowMark}`;
  if (v.ad_check === "na")
    return `<span class="ad-flag ad-na" title="500再生未満のため広告判定の対象外">like ${v.like_pct}%</span>`;
  if (v.ad_check === "unknown")
    return `<span class="ad-flag ad-na" title="like数非公開チャンネルのため判定不能">like非公開</span>`;
  return "";
}

// video_id → 市場検証結果 (dashboard.json の market フィールド。load() で代入)
let MARKET = {};
// 外部HITコーパス (market_validate / discovery_loop が蓄積した監視枠外の HIT。load() で代入)
let EXTERNAL_HITS = [];

const chIcon = (ch, cls = "ch-icon") =>
  ch.icon ? `<img class="${cls}" src="${ch.icon}" alt="" loading="lazy">` : "";
const JST_MS = 9 * 3600 * 1000;
// ISO タイムスタンプ → JST の YYYY-MM-DD。日付のみの値 (タイムゾーン不明) はそのまま返す。
function jstDate(ts) {
  if (!ts) return "";
  if (ts.length <= 10) return ts;
  return new Date(parseTs(ts).getTime() + JST_MS).toISOString().slice(0, 10);
}
const fmtTs = (ts) => {
  if (!ts || ts.length === 10) return ts;
  const d = new Date(parseTs(ts).getTime() + JST_MS).toISOString();
  return `${d.slice(0, 10)} ${d.slice(11, 16)} JST`;
};

async function load() {
  // Chart.js global defaults
  Chart.defaults.color = COL_LEGEND;
  Chart.defaults.borderColor = COL_GRID;
  Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Kaku Gothic ProN", "Yu Gothic", Meiryo, sans-serif';

  const res = await fetch("data/dashboard.json", { cache: "no-store" });
  const data = await res.json();
  MARKET = data.market || {}; // market_validate.py の全YouTube横断需要検証キャッシュ
  EXTERNAL_HITS = data.external_hits || [];
  document.getElementById("generated-at").textContent =
    "updated: " + fmtTs(data.generated_at);
  const main = document.getElementById("channels");
  data.channels.forEach((ch, idx) => main.appendChild(renderChannel(ch, idx)));
  // 広告混ざり枠 (boosted) は真似元候補ではないので横断急伸ウォッチから除外
  renderRisingWatch(data.channels.filter((c) => !c.boosted));
  renderDiscovery(data.discovery || {});
  buildTabs(data.channels);
}

function panelEl(id) {
  if (id === "rising-watch") return document.getElementById("rising-watch-panel");
  if (id === "discovery") return document.getElementById("discovery-panel");
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

  // 既存の注目チャンネル群 (priority 観測対象)
  channels.forEach((ch) => {
    const btn = document.createElement("button");
    btn.innerHTML = `${chIcon(ch)}${ch.boosted ? "⚠️ " : ""}${escapeHtml(ch.title)}`;
    btn.onclick = () => activateTab(ch.id, tabs);
    tabs.push({ id: ch.id, btn });
    nav.appendChild(btn);
  });

  // 新規発見 (discovery loop) — 既存チャンネル群と区切り線で分ける
  const sep = document.createElement("span");
  sep.className = "nav-sep";
  nav.appendChild(sep);
  const dvBtn = document.createElement("button");
  dvBtn.className = "nav-discovery";
  dvBtn.textContent = "🧭 新規発見";
  dvBtn.onclick = () => activateTab("discovery", tabs);
  tabs.push({ id: "discovery", btn: dvBtn });
  nav.appendChild(dvBtn);

  activateTab("rising-watch", tabs);
}

// === 🧭 新規発見タブ (discovery loop の検索キーワード + 評価済み ch 台帳) ===

const DV_DECISION = {
  promoted: { label: "✅ AUTO追加済み", cls: "dv-ok" },
  "queued(cap)": { label: "✅ 合格・確認待ち", cls: "dv-ok" },
  queued: { label: "🔎 queue (見送り)", cls: "dv-queue" },
  rejected: { label: "✗ 不採用", cls: "dv-ng" },
  skip: { label: "− 取得不可", cls: "dv-na" },
};

function renderDiscovery(dv) {
  const panel = document.getElementById("discovery-panel");
  if (!panel) return;
  const kws = dv.keywords || [];
  const evaluated = dv.evaluated || [];

  const kwRows = kws.map((k) => `
    <tr>
      <td class="dv-kw">${escapeHtml(k.kw)}</td>
      <td class="dv-note">${escapeHtml(k.note || "")}</td>
    </tr>`).join("");

  const evRows = evaluated.map((e) => {
    const d = DV_DECISION[e.decision] || { label: escapeHtml(e.decision || "?"), cls: "dv-na" };
    return `
    <tr>
      <td class="dv-date">${escapeHtml(e.date || "")}</td>
      <td class="dv-ch"><a href="https://www.youtube.com/channel/${encodeURIComponent(e.channel_id)}" target="_blank" rel="noopener">${escapeHtml(e.title)}</a></td>
      <td><span class="dv-badge ${d.cls}">${d.label}</span></td>
      <td class="dv-note">${escapeHtml(e.reason || "")}</td>
    </tr>`;
  }).join("");

  panel.innerHTML = `
    <h2>🧭 新規発見 — discovery loop (毎朝 JST 4:23)</h2>
    <p class="dv-desc">ニッチ技術キーワードで YouTube を検索し、「普段の再生数が低いのに特定企画だけ跳ねる」同層チャンネル (subs 50-2000) を発掘する自動ループ。
    HIT はチャンネル相対 (普段中央値の5倍・下限500v・広告偽HIT除外後)。合格候補もここと Slack に出るだけで、priority への追加は人間の確認制。</p>

    <h3>検索キーワード (${kws.length}語)</h3>
    <p class="dv-sub">${escapeHtml(dv.header || "")} — 1語 = search.list 100u/日。ニッチさ自体が「同規模ch」フィルタとして機能する。検知した HIT タイトルから新KWを還流して鮮度を保つ。</p>
    <div class="dv-table-wrap">
      <table class="dv-table">
        <thead><tr><th>キーワード</th><th>根拠 (実証HIT)</th></tr></thead>
        <tbody>${kwRows || '<tr><td colspan="2">（データなし）</td></tr>'}</tbody>
      </table>
    </div>

    <h3>評価済みチャンネル (${evaluated.length}件)</h3>
    <p class="dv-sub">discovery が深掘り評価した候補の台帳 (新しい順)。「✅ 合格・確認待ち」を見て、良ければ priority_channels.txt へ手動追加する。</p>
    <div class="dv-table-wrap">
      <table class="dv-table">
        <thead><tr><th>評価日</th><th>チャンネル</th><th>判定</th><th>理由</th></tr></thead>
        <tbody>${evRows || '<tr><td colspan="4">（まだ評価履歴なし）</td></tr>'}</tbody>
      </table>
    </div>`;
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

  // 「その日」= 最新スナップショットの JST 日付
  const latestTs = subs.length ? (subs[subs.length - 1].timestamp || "") : "";
  const todayDate = jstDate(latestTs);

  const todaySubsDelta = computeTodayDelta(subs, "subs", todayDate);
  const todayViewsDelta = computeTodayDelta(views, "total_views", todayDate);
  const todayPosts = (ch.daily_posts || []).find((d) => d.date === todayDate)?.count || 0;

  // KPI 期間切替 (今日 / 3日 / 7日 / 28日)
  const dateMinus = (dstr, days) => new Date(Date.UTC(
    Number(dstr.slice(0, 4)), Number(dstr.slice(5, 7)) - 1, Number(dstr.slice(8, 10))
  ) - days * 86400000).toISOString().slice(0, 10);
  const sumPosts = (days) => !todayDate ? 0 : (ch.daily_posts || [])
    .filter((p) => p.date > dateMinus(todayDate, days) && p.date <= todayDate)
    .reduce((s, p) => s + p.count, 0);
  const kpiPeriods = [
    { key: "today", label: "今日" },
    { key: "3", label: "3日", days: 3 },
    { key: "7", label: "7日", days: 7 },
    { key: "28", label: "28日", days: 28 },
  ];
  const kpiStats = {};
  kpiPeriods.forEach((p) => {
    if (p.key === "today") {
      kpiStats[p.key] = { subs: todaySubsDelta, views: todayViewsDelta, posts: todayPosts, span: "" };
    } else {
      const s = computeRecentDelta(subs, "subs", p.days);
      const v = computeRecentDelta(views, "total_views", p.days);
      kpiStats[p.key] = {
        subs: s.delta, views: v.delta, posts: sumPosts(p.days),
        span: v.spanLabel.includes("短縮") ? `実データ ${v.spanLabel}` : "",
      };
    }
  });

  // 急伸ウォッチ条件をこのチャンネルだけに適用
  const risingThisCh = collectRising([ch]);
  const hitCount = risingThisCh.filter((r) => r.tier === "hit").length;
  const semiCount = risingThisCh.filter((r) => r.tier === "semi").length;
  const warmupCount = risingThisCh.filter((r) => r.tier === "warmup").length;

  const fmtDelta = (n) => `${n >= 0 ? "+" : ""}${fmtN(n)}`;
  const deltaCls = (n) => n >= 0 ? "pos" : "neg";

  wrap.innerHTML = `
    <h2>${chIcon(ch, "ch-icon ch-icon-lg")}<a class="ch-link" href="https://www.youtube.com/channel/${encodeURIComponent(ch.id)}" target="_blank" rel="noopener" title="YouTube でチャンネルを開く">${escapeHtml(ch.title)}</a> <span class="ch-date">(${todayDate || "—"} JST 時点)</span>${ch.boosted ? ' <span class="boosted-badge">⚠️ 広告混ざり枠 — 過去にブースト形跡あり。views/score は割引で読み、like率を併読</span>' : ""}</h2>
    <div class="kpi-period-tabs" id="kpip-${ch.id}">
      ${kpiPeriods.map((p, i) => `<button class="kpip${i === 0 ? " active" : ""}" data-p="${p.key}">${p.label}</button>`).join("")}
      <span class="kpip-span" id="kpipspan-${ch.id}"></span>
    </div>
    <div class="kpi-row">
      <div class="kpi">
        <div class="kpi-label">登録者の伸び</div>
        <div class="kpi-value ${deltaCls(todaySubsDelta)}" id="kpiv-subs-${ch.id}">${fmtDelta(todaySubsDelta)}</div>
        <div class="kpi-sub">累計 ${fmtN(lastSubs)}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">再生数の伸び</div>
        <div class="kpi-value ${deltaCls(todayViewsDelta)}" id="kpiv-views-${ch.id}">${fmtDelta(todayViewsDelta)}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">投稿本数</div>
        <div class="kpi-value" id="kpiv-posts-${ch.id}">${fmtN(todayPosts)}</div>
      </div>
      <div class="kpi" title="急伸中だが、まだチャンネルの普段の再生数（中央値）の範囲内">
        <div class="kpi-label">離陸中</div>
        <div class="kpi-value warmup">${fmtN(warmupCount)}</div>
      </div>
      <div class="kpi" title="チャンネル中央値の ${HIT.mult}〜${HIT.strongMult} 倍の再生 = 軽い突出（伸びかけ）">
        <div class="kpi-label">🚀 準HIT</div>
        <div class="kpi-value semi">${fmtN(semiCount)}</div>
      </div>
      <div class="kpi" title="チャンネル中央値の ${HIT.strongMult} 倍以上の再生 = 本物の突出（ブレイク）">
        <div class="kpi-label">🎯 HIT</div>
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

    <div class="section-title">★ 直近 HIT / 準HIT 動画 (age ≤ 14d・ch相対判定: 普段の${HIT.mult}倍で🚀 / ${HIT.strongMult}倍で🎯)</div>
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
          <span class="video-nav-hint">↑↓ 動画切替 / ←→ ソート切替 (チャートにカーソルを載せている間は ←→ で前日/翌日へ)</span>
          <span id="vpos-${ch.id}" class="video-nav-pos"></span>
        </div>
      </div>
    </div>
  `;

  setTimeout(() => {
    const kpTabs = wrap.querySelectorAll(`.kpi-period-tabs .kpip`);
    kpTabs.forEach((btn) => btn.addEventListener("click", () => {
      kpTabs.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const st = kpiStats[btn.dataset.p];
      const subsEl = document.getElementById(`kpiv-subs-${ch.id}`);
      subsEl.textContent = fmtDelta(st.subs);
      subsEl.className = `kpi-value ${deltaCls(st.subs)}`;
      const viewsEl = document.getElementById(`kpiv-views-${ch.id}`);
      viewsEl.textContent = fmtDelta(st.views);
      viewsEl.className = `kpi-value ${deltaCls(st.views)}`;
      document.getElementById(`kpiv-posts-${ch.id}`).textContent = fmtN(st.posts);
      document.getElementById(`kpipspan-${ch.id}`).textContent = st.span;
    }));
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
  // todayDate (JST の YYYY-MM-DD) を基準に、その日の JST 0時以前で最も新しいスナップショットを base に取って差分を返す。
  // base が存在しなければ、最古スナップショットを使う (=「観測開始以来」になるが当日に複数 snap がある場合は当日内の最古との差で代用)。
  if (!history.length || !todayDate) return 0;
  const sorted = [...history].sort((a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""));
  const dayStartMs = Date.UTC(
    Number(todayDate.slice(0, 4)),
    Number(todayDate.slice(5, 7)) - 1,
    Number(todayDate.slice(8, 10))
  ) - JST_MS;
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
    const d = jstDate(p.timestamp || "");
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
    const badge = tierBadge(r.tier);
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
      <div class="hit-meta">${badge}</div>
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
    el.innerHTML = `<div class="empty">直近 14 日に HIT / 準HIT なし（ch相対しきい値未達）</div>`;
    return;
  }
  el.className = "hits";
  // tier はバックエンド算出値 (recent_hits[].tier) を優先。無い古い JSON は views から判定。
  const tierOf = (h) => h.tier || (ch ? hitTier(h.views, channelHitThreshold(ch), channelSemiThreshold(ch)) : "hit");
  el.innerHTML = hits.map((h) => `
    <a class="hit" href="${h.url}" data-vid="${escapeHtml(h.video_id)}" target="_blank" rel="noopener" title="クリック: 下のグラフを表示 / Ctrl+クリック: YouTube">
      <div class="hit-title">${escapeHtml(h.title)}</div>
      <div class="hit-meta">
        ${tierBadge(tierOf(h))}
        <span class="views">${fmtN(h.views)} views</span>
        ${h.baseline_mult != null ? `<span title="チャンネルの普段（中央値）の何倍か">普段の${h.baseline_mult}倍</span>` : ""}
        <span>score ${h.score.toFixed(2)}</span>
        <span>${h.age_days}d ago</span>
        ${adBadge(h, ch ? ch.like_baseline : null)}
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
  let curDaily = [];
  let activeVid = null;
  let activeIdx = 0;

  const posLabel = document.getElementById("vpos-" + ch.id);
  const panel = document.getElementById("ch-" + ch.id);

  const renderRow = (e) => {
    const hitTh = channelHitThreshold(ch);
    const tier = e.latest >= hitTh ? "hit" : e.latest < hitTh * 0.1 ? "cold" : "warm";
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
          ${adBadge(e, ch.like_baseline)}
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
    // 1日4回の観測点をそのまま出すと点が多すぎるので、JST日ごとに平均して1点に集約
    const byDay = new Map();
    monotone.forEach((p) => {
      const d = jstDate(p.date);
      if (!byDay.has(d)) byDay.set(d, []);
      byDay.get(d).push(p.views);
    });
    const daily = [...byDay.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([d, vals]) => ({
        date: d,
        views: Math.round(vals.reduce((s, x) => s + x, 0) / vals.length),
      }));
    curDaily = daily;
    const cumData = daily.map((p) => ({ x: parseTs(p.date), y: p.views }));
    const deltaData = [];
    for (let i = 1; i < daily.length; i++) {
      deltaData.push({
        x: parseTs(daily[i].date),
        y: daily[i].views - daily[i - 1].views,
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
            title: { display: true, text: `累計 views (${fmtN(channelHitThreshold(ch))} で🎯HIT)`, color: COL_TICK },
          },
          y1: {
            type: "linear",
            position: "right",
            beginAtZero: true,
            suggestedMax: 1250,
            ticks: { color: COL_TICK, callback: (v) => fmtN(v) },
            grid: { drawOnChartArea: false },
            title: { display: true, text: "1 日の伸び", color: COL_TICK },
          },
        },
      },
    });
  };

  renderList();

  const scrollToChart = () => {
    if (canvas) canvas.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  // チャートにカーソルを載せている間は ←→ でツールチップを前日/翌日に移動
  let chartHover = false;
  if (canvas) {
    canvas.addEventListener("mouseenter", () => { chartHover = true; });
    canvas.addEventListener("mouseleave", () => { chartHover = false; });
  }
  const moveTooltip = (dir) => {
    if (!chart || !curDaily.length) return false;
    const act = chart.getActiveElements();
    let i = act.length ? act[0].index : (dir > 0 ? -1 : curDaily.length);
    i = Math.min(curDaily.length - 1, Math.max(0, i + dir));
    const els = [{ datasetIndex: 0, index: i }];
    if (i >= 1) els.push({ datasetIndex: 1, index: i - 1 });
    chart.setActiveElements(els);
    const pt = chart.getDatasetMeta(0).data[i];
    chart.tooltip.setActiveElements(els, { x: pt.x, y: pt.y });
    chart.update();
    return true;
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
      if (chartHover && e.key === "ArrowRight") {
        if (moveTooltip(1)) e.preventDefault();
        return;
      }
      const i = SORT_KEYS.indexOf(sortKey);
      if (i >= 0 && i < SORT_KEYS.length - 1) {
        setSort(SORT_KEYS[i + 1]);
        e.preventDefault();
      }
    } else if (e.key === "ArrowLeft" || e.key === "h") {
      if (chartHover && e.key === "ArrowLeft") {
        if (moveTooltip(-1)) e.preventDefault();
        return;
      }
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
  recentDays: 7,         // テーブルに表示する直近日数（1 日刻み）
};

// === HIT 定義（チャンネル相対・二段構え）===
// 絶対 1,000 再生ではなく「そのチャンネルの普段（中央値）に対して突出しているか」で判定する。
// 例: 普段 48 再生の ch が 1,000 再生 = 20倍 → 明確なブレイク。逆に普段数千の ch が 3,000 再生でも日常なので HIT ではない。
//   🎯 HIT  = 普段の strongMult 倍以上（本物の突出）
//   🚀 準HIT = 普段の mult 倍以上（軽い突出・伸びかけ）
//   離陸中   = それ未満（急伸条件は満たすが、まだ普段の範囲）
const HIT = {
  strongMult: 5,     // 🎯 HIT のしきい（中央値の何倍）
  mult: 3,           // 🚀 準HIT のしきい（中央値の何倍）
  relFloor: 500,     // 相対しきいの絶対下限（小チャンネルのノイズ除け）
  absFallback: 1000, // 本数が少なく中央値が不安定な ch はこの絶対値で判定
  minSample: 5,      // 中央値を信頼するのに必要な最低動画本数
};

const TIER_META = {
  hit:    { label: "🎯 HIT",   cls: "rw-tier-hit" },
  semi:   { label: "🚀 準HIT",  cls: "rw-tier-semi" },
  warmup: { label: "離陸中",     cls: "rw-tier-warmup" },
};
const tierBadge = (tier) => {
  const m = TIER_META[tier] || TIER_META.warmup;
  return `<span class="rw-tier ${m.cls}">${m.label}</span>`;
};

// チャンネルの「普段の再生数」。バックエンド (build_pages_data.py) が最新 CSV 全動画から
// 算出した baseline_median を優先する。video_history からの再計算は「追跡対象（≒HITと直近30日）
// だけ」の偏ったサンプルで中央値が過大に出るため、古い JSON 向けのフォールバック扱い。
function channelBaseline(ch) {
  if (ch.baseline_median != null) return ch.baseline_median;
  const views = [];
  for (const v of Object.values(ch.video_history || {})) {
    if (!v.history || !v.history.length) continue;
    views.push(v.history[v.history.length - 1].views);
  }
  if (views.length < HIT.minSample) return null;
  views.sort((a, b) => a - b);
  const mid = Math.floor(views.length / 2);
  return views.length % 2 ? views[mid] : (views[mid - 1] + views[mid]) / 2;
}

// 中央値の mult 倍しきい（バックエンド算出値優先・baseline 不安定なら絶対値フォールバック）
function channelThreshold(ch, mult) {
  if (mult === HIT.strongMult && ch.hit_threshold != null) return ch.hit_threshold;
  if (mult === HIT.mult && ch.semi_threshold != null) return ch.semi_threshold;
  const base = channelBaseline(ch);
  if (base == null) return HIT.absFallback;
  return Math.max(HIT.relFloor, Math.round(mult * base));
}
const channelHitThreshold = (ch) => channelThreshold(ch, HIT.strongMult);  // 🎯 HIT
const channelSemiThreshold = (ch) => channelThreshold(ch, HIT.mult);       // 🚀 準HIT

// 再生数 → tier ("hit" | "semi" | "warmup")
function hitTier(latest, hitTh, semiTh) {
  if (latest >= hitTh) return "hit";
  if (latest >= semiTh) return "semi";
  return "warmup";
}

function dailyDeltas(history) {
  // 観測ポイント (1 日複数回) を日単位に集約してから日次デルタを出す。
  // 各日はその日の最終観測値を採用し、cumulative max で単調増加を保証。
  const sorted = [...history].sort((a, b) => a.date.localeCompare(b.date));
  const byDay = {};
  sorted.forEach((p) => { byDay[jstDate(p.date || "")] = p.views; });
  const days = Object.keys(byDay).filter(Boolean).sort();
  let prev = 0;
  const mono = days.map((d) => {
    const v = Math.max(byDay[d], prev);
    prev = v;
    return { ts: d, views: v };
  });
  const deltas = [];
  for (let i = 1; i < mono.length; i++) {
    deltas.push({ ts: mono[i].ts, delta: mono[i].views - mono[i - 1].views });
  }
  return { mono, deltas };
}

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
    const hitThreshold = channelHitThreshold(ch);   // 🎯 HIT しきい（5x）
    const semiThreshold = channelSemiThreshold(ch); // 🚀 準HIT しきい（3x）
    const base = channelBaseline(ch);
    for (const [vid, v] of Object.entries(vh)) {
      if (!v.history || v.history.length < 2) continue;
      const { mono, deltas } = obsDeltas(v.history);
      const latest = mono[mono.length - 1].views;
      // 3 日窓内のデルタ合計（観測点数が変動しても期間ベース）
      const windowDeltas = deltas.filter((d) => parseTs(d.ts).getTime() >= cutoffMs);
      const totalDelta = windowDeltas.reduce((s, d) => s + Math.max(0, d.delta), 0);
      const todayDelta = windowDeltas.length ? windowDeltas[windowDeltas.length - 1].delta : 0;
      if (totalDelta < cfg.totalDeltaMin && todayDelta < cfg.todayDeltaMin) continue;
      // 表示用には直近 N 日分（1 日刻みに集約）
      const recent = dailyDeltas(v.history).deltas.slice(-cfg.recentDays);
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
        tier: hitTier(latest, hitThreshold, semiThreshold),
        hitThreshold,
        semiThreshold,
        baselineMult: base ? +(latest / base).toFixed(1) : null,
        ad_check: v.ad_check,
        like_pct: v.like_pct,
        likes: v.likes,
        exp_likes: v.exp_likes,
        likes_vs_exp_pct: v.likes_vs_exp_pct,
        likeBaseline: ch.like_baseline,
      });
    }
  });
  // 3 日合計伸び降順
  out.sort((a, b) => b.totalDelta - a.totalDelta);
  return out;
}

// === チャンネル横断分析 ===
//
// Slack 急伸通知に同梱している「類似HIT＋参入タイミング分析」
// (priority_channels_check.py の extract_keywords / find_similar_hits / timing_label)
// の JS 移植。急伸ウォッチの各候補に対して自動で横断分析をかける。
// ロジックを変える時は Python 側と両方直すこと。

const KW_RE = /[A-Za-z][A-Za-z0-9.+#_-]{2,}|[ァ-ヶー]{3,}|[一-龥]{2,}/g;
const KW_STOP = new Set(["claude", "claudecode", "code", "with", "your", "the", "for", "and", "ai"]);

function extractKeywords(title) {
  const out = new Set();
  for (const m of (title || "").match(KW_RE) || []) {
    const t = m.toLowerCase();
    if (!KW_STOP.has(t) && t.length >= 3) out.add(t);
  }
  return out;
}

// 全 priority ch の HIT 動画 (1,000+ views) + 外部HITコーパスを類似HIT検索コーパスにする。
// boosted 枠は views が実需要を反映しないためコーパス外 (channel_strategy §5 と同方針)。
// externalHits = 監視枠外の HIT (market_validate / discovery_loop 蓄積。external:true で 🌍 表示)。
function buildHitCorpus(channels, externalHits) {
  const corpus = [];
  const watched = new Set(channels.map((c) => c.id));
  channels.filter((c) => !c.boosted).forEach((ch) => {
    const hitTh = channelHitThreshold(ch); // 相対 HIT しきい値でコーパスを絞る
    for (const [vid, v] of Object.entries(ch.video_history || {})) {
      if (!v.history || !v.history.length) continue;
      const hist = [...v.history].sort((a, b) => a.date.localeCompare(b.date));
      const latest = hist[hist.length - 1].views;
      if (latest < hitTh) continue;
      corpus.push({
        vid,
        title: v.title,
        url: v.url || `https://www.youtube.com/watch?v=${vid}`,
        published_at: (v.published_at || "").slice(0, 10),
        views: latest,
        channelId: ch.id,
        channelTitle: ch.title,
        kw: extractKeywords(v.title),
      });
    }
  });
  (externalHits || []).forEach((e) => {
    if (!e.video_id || watched.has(e.channel_id)) return; // 監視枠内は上で照合済み
    corpus.push({
      vid: e.video_id,
      title: e.title,
      url: e.url || `https://www.youtube.com/watch?v=${e.video_id}`,
      published_at: (e.published_at || "").slice(0, 10),
      views: e.views || 0,
      channelId: e.channel_id || "",
      channelTitle: e.channel || "?",
      external: true,
      kw: extractKeywords(e.title),
    });
  });
  return corpus;
}

function timingVerdict(ageDays, velocity) {
  if (ageDays <= 3) return { icon: "🟢", label: "初速ゾーン（参入の最良タイミング）", cls: "xa-timing-best" };
  if (velocity != null && velocity >= 80) return { icon: "🟡", label: "まだ伸びている（参入間に合う）", cls: "xa-timing-ok" };
  if (velocity != null && velocity < 20) return { icon: "🔴", label: "ピークアウト気味（真似るなら急ぐ）", cls: "xa-timing-late" };
  return { icon: "🟡", label: "観測中", cls: "xa-timing-ok" };
}

// 急伸候補 1 件に横断分析をかける。
// 戻り値: {sims, cross, isNew, velocity, timing}
function crossAnalysis(r, corpus) {
  const kw = extractKeywords(r.title);
  const scored = [];
  for (const c of corpus) {
    if (c.vid === r.vid) continue;
    const shared = [...kw].filter((k) => c.kw.has(k));
    if (!shared.length) continue;
    scored.push({ nShared: shared.length, sharedKw: shared, ...c });
  }
  scored.sort((a, b) => b.nShared - a.nShared || b.views - a.views);
  const sims = scored.slice(0, 3);
  const cross = sims.some((s) => s.channelId !== r.channelId);
  const crossExternal = sims.some((s) => s.external);
  // views/日 ≒ 直近 1 日の伸び (Python 版は直近2観測点から算出、閾値 80/20 は共通)
  const velocity = r.recent && r.recent.length ? r.recent[r.recent.length - 1].delta : null;
  return {
    sims,
    cross,
    crossExternal,
    isNew: sims.length === 0,
    velocity,
    timing: timingVerdict(r.ageDays, velocity),
  };
}

function xaBadge(a) {
  if (a.crossExternal) return '<span class="xa-badge xa-badge-cross" title="監視枠外のチャンネルでも同テーマがHIT＝世の中の需要実証（最強シグナル）">🌍 外部実証</span>';
  if (a.cross) return '<span class="xa-badge xa-badge-cross" title="同テーマが他チャンネルでもHIT＝横展開の実証あり（強シグナル）">🌐 横展開実証</span>';
  if (a.isNew) return '<span class="xa-badge xa-badge-new" title="過去履歴・外部コーパスにキーワードが重なるHITなし">🆕 新規テーマ</span>';
  return '<span class="xa-badge xa-badge-same" title="類似HITは自チャンネル内のみ">🔁 自ch実証</span>';
}

function renderRisingWatch(channels) {
  const panel = document.getElementById("rising-watch-panel");
  if (!panel) return;
  const rows = collectRising(channels);
  const corpus = buildHitCorpus(channels, EXTERNAL_HITS);
  rows.forEach((r) => { r.xa = crossAnalysis(r, corpus); });

  // 全動画の観測ポイント (ts) を集約してソート → 直近 N 個を列ヘッダに採用
  const allTs = new Set();
  rows.forEach((r) => r.recent.forEach((d) => allTs.add(d.ts)));
  const recentTs = [...allTs].sort().slice(-RISING_DEFAULT.recentDays);

  // チャンネル毎件数（ボタン用ラベル）
  const byChannel = {};
  rows.forEach((r) => {
    byChannel[r.channelId] = byChannel[r.channelId] || { title: r.channelTitle, count: 0 };
    byChannel[r.channelId].count += 1;
  });

  panel.innerHTML = `
    <div class="rw-header">
      <h2>🔥 急伸ウォッチ — 直近 3 日に伸びている動画</h2>
      <p class="rw-help">HIT はチャンネル相対で判定（普段=中央値の何倍か）。<span class="rw-tier rw-tier-hit">🎯 HIT</span> = ${HIT.strongMult}倍以上（本物の突出） / <span class="rw-tier rw-tier-semi">🚀 準HIT</span> = ${HIT.mult}〜${HIT.strongMult}倍（伸びかけ） / <span class="rw-tier rw-tier-warmup">離陸中</span> = それ未満。「3日合計伸び」降順。<strong>▲</strong> = 直近で加速、<strong>▽</strong> = 減速、<strong>→</strong> = 横ばい、<strong>■</strong> = 停止。</p>
      <p class="rw-help"><strong>行クリックで横断分析を展開</strong>（類似HIT・参入タイミング。Slack 急伸通知と同ロジック）。<span class="xa-badge xa-badge-cross">🌐 横展開実証</span> = 同テーマが他チャンネルでもHIT（真似る価値の強シグナル） / <span class="xa-badge xa-badge-same">🔁 自ch実証</span> = 類似HITが自チャンネル内のみ / <span class="xa-badge xa-badge-new">🆕 新規テーマ</span> = 過去履歴に類似HITなし。⏱ 🟢初速ゾーン 🟡伸び継続 🔴ピークアウト気味。</p>

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
          <button class="rw-filter rw-filter-tier" data-tier="hit">🎯 HIT (${rows.filter((r) => r.tier === "hit").length})</button>
          <button class="rw-filter rw-filter-tier" data-tier="semi">🚀 準HIT (${rows.filter((r) => r.tier === "semi").length})</button>
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
    // フィルタ変更時は展開中の横断分析を全部畳む（親行と表示がズレるのを防ぐ）
    panel.querySelectorAll(".rw-xa").forEach((row) => { row.style.display = "none"; });
    const empty = panel.querySelector(".rw-empty");
    if (empty) {
      empty.style.display = visible === 0 ? "block" : "none";
      empty.textContent = visible === 0 ? "該当動画なし（フィルタ条件で 0 件）。" : "";
    }
  };

  // 行クリック → 横断分析の展開/折りたたみ（リンククリックは除外）
  panel.querySelectorAll(".rw-row").forEach((row) => {
    row.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;
      const vid = row.dataset.vid;
      if (!vid) return;
      const xa = panel.querySelector(`.rw-xa[data-for="${CSS.escape(vid)}"]`);
      if (xa) xa.style.display = xa.style.display === "none" ? "" : "none";
    });
  });

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
  const tierLabel = tierBadge(r.tier);
  const ageLabel = r.ageDays === 0 ? "今日" : `${r.ageDays}d`;
  const xa = r.xa;
  const mainRow = `
    <tr class="rw-row" data-vid="${escapeHtml(r.vid)}" data-tier="${r.tier}" data-ch="${escapeHtml(r.channelId)}" title="クリックで横断分析を展開">
      <td class="rw-col-title">
        <a href="${r.url}" target="_blank" rel="noopener" class="rw-title">${escapeHtml(r.title)}</a>
        <div class="rw-sub">
          ${tierLabel}
          <span class="rw-channel">${escapeHtml(r.channelTitle)}</span>
          ${xa ? `<span class="xa-timing-icon" title="${xa.timing.label}">${xa.timing.icon}</span>` : ""}
          ${xa ? xaBadge(xa) : ""}
          ${MARKET[r.vid] ? '<span class="xa-badge xa-badge-market" title="全YouTube横断の市場検証データあり — クリックで展開">🌍 市場検証</span>' : ""}
          ${adBadge(r, r.likeBaseline)}
        </div>
      </td>
      <td class="rw-num">${fmtN(r.latest)}</td>
      <td class="rw-age">${ageLabel}</td>
      ${dayCells}
      <td class="rw-total">+${fmtN(r.totalDelta)}</td>
      <td class="rw-trend ${r.trend.cls}" title="${r.trend.label}">${r.trend.icon}</td>
    </tr>
  `;
  if (!xa) return mainRow;

  const velTxt = xa.velocity != null ? `~${fmtN(xa.velocity)} views/日` : "速度データ不足";
  let simsHtml;
  if (xa.sims.length) {
    simsHtml = `<div class="xa-sims-title">🔁 類似HIT ${xa.sims.length}件:</div>` + xa.sims.map((s) => `
      <div class="xa-sim">
        ${s.external ? '<span class="xa-sim-cross" title="監視枠外チャンネルの HIT (外部コーパス)">🌍</span>' : s.channelId !== r.channelId ? '<span class="xa-sim-cross" title="別チャンネルの HIT">🌐</span>' : "・"}
        <span class="xa-sim-ch">[${escapeHtml(s.channelTitle)}]</span>
        <a href="${s.url}" target="_blank" rel="noopener">${escapeHtml(s.title)}</a>
        — ${fmtN(s.views)}v${s.published_at ? ` / ${s.published_at}公開` : ""}
        <span class="xa-sim-kw">共有KW: ${s.sharedKw.slice(0, 4).map(escapeHtml).join(", ")}</span>
      </div>`).join("");
    if (xa.crossExternal) {
      simsHtml += `<div class="xa-verdict xa-verdict-cross">→ 監視枠外のチャンネルでも同テーマがHIT＝世の中の需要実証（最強シグナル）</div>`;
    } else if (xa.cross) {
      simsHtml += `<div class="xa-verdict xa-verdict-cross">→ 同テーマが他チャンネルでもHIT＝横展開の実証あり（強シグナル）</div>`;
    } else {
      simsHtml += `<div class="xa-verdict">→ 類似HITは同一チャンネル内のみ（横展開は未実証）</div>`;
    }
  } else {
    simsHtml = `<div class="xa-verdict">🔁 類似HIT: 過去履歴・外部コーパスに該当なし（新規テーマの可能性 — 当たれば先行者、外れれば需要なし）</div>`;
  }
  // 🌍 市場検証 (market_validate.py が YouTube 全体を検索した結果。急伸検知された動画のみ存在)
  const m = MARKET[r.vid];
  let marketHtml = "";
  if (m && m.n != null) {
    if (m.n === 0) {
      marketHtml = `<div class="xa-market"><div class="xa-market-title">🌍 市場検証「${escapeHtml(m.query)}」: 外部動画が見つからず（新規テーマ濃厚）</div></div>`;
    } else {
      const ex = (m.examples || []).slice(0, 3).map((e) => `
        <div class="xa-sim">・<a href="${e.url}" target="_blank" rel="noopener">${escapeHtml(e.title)}</a>
          <span class="xa-sim-ch">[${escapeHtml(e.channel)}]</span>
          — ${fmtN(e.views)}v / 登録${fmtN(e.subs)} / score ${e.score} / ${e.age_days}d前</div>`).join("");
      marketHtml = `
        <div class="xa-market">
          <div class="xa-market-title">🌍 市場検証「${escapeHtml(m.query)}」: ${escapeHtml(m.verdict || "")}</div>
          <div class="xa-market-stats">監視枠外 ${m.n}本中 HIT率(score≥2.0) ${Math.round((m.hit_rate || 0) * 100)}% ・ 直近90日 ${m.recent90_hits}/${m.recent90_n}本HIT ・ 外部中央値 ${fmtN(m.median_views || 0)}v <span class="xa-market-date">(検証 ${(m.checked_at || "").slice(0, 10)})</span></div>
          ${ex}
        </div>`;
    }
  }
  const analysisRow = `
    <tr class="rw-xa" data-for="${escapeHtml(r.vid)}" data-tier="${r.tier}" data-ch="${escapeHtml(r.channelId)}" style="display:none">
      <td colspan="${5 + recentTs.length}">
        <div class="xa-detail">
          <div class="xa-timing ${xa.timing.cls}">⏱ 参入タイミング: ${xa.timing.icon} ${xa.timing.label}（公開${r.ageDays}日前 / ${velTxt}）</div>
          ${simsHtml}
          ${marketHtml}
        </div>
      </td>
    </tr>
  `;
  return mainRow + analysisRow;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

load().catch((err) => {
  console.error(err);
  document.body.innerHTML = `<pre style="color:#cf222e;padding:2rem">load error: ${err.message}</pre>`;
});
