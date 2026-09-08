/* mini-ground-station dashboard.
 *
 * Three single-series line charts rather than one chart with three y-axes:
 * volts, degrees, and dBm share no scale, and overlaying them would invent a
 * correlation that is not in the data.
 *
 * Everything the API returns is treated as untrusted text and inserted with
 * textContent, never by building HTML strings.
 */

const API = "/api/v1";
const SVGNS = "http://www.w3.org/2000/svg";

const state = {
  satellite: null,
  range: "pass3",
  auto: true,
  tables: false,
  summary: null,
  series: null,
  passes: [],
  alerts: [],
  cursor: {},
  timer: null,
};

const CHARTS = [
  {
    id: "battery",
    title: "Battery voltage",
    unit: "V",
    digits: 2,
    avg: "battery_avg",
    lo: "battery_min",
    hi: "battery_max",
    limits: (l) => [
      { v: l.battery_min_v, label: "nominal minimum", role: "warning" },
      { v: l.battery_critical_v, label: "critical floor", role: "critical" },
    ],
  },
  {
    id: "temperature",
    title: "Temperature",
    unit: "°C",
    digits: 1,
    avg: "temperature_avg",
    lo: "temperature_min",
    hi: "temperature_max",
    limits: (l) => [
      { v: l.temp_max_c, label: "upper limit", role: "warning" },
      { v: l.temp_min_c, label: "lower limit", role: "warning" },
    ],
  },
  {
    id: "signal",
    title: "Signal strength",
    unit: "dBm",
    digits: 0,
    avg: "signal_avg",
    lo: null,
    hi: null,
    limits: () => [],
  },
];

/* ---------------------------------------------------------------- helpers */

const $ = (sel) => document.querySelector(sel);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svg(tag, attrs) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  return node;
}

async function get(path) {
  const res = await fetch(API + path);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

const clock = (iso) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour12: false }) : "—";

function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function duration(from, to) {
  if (!from || !to) return "—";
  const s = Math.round((new Date(to) - new Date(from)) / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

const num = (v, digits) =>
  v === null || v === undefined ? "—" : v.toFixed(digits);

/** Axis ticks on round numbers, so the reader gets values I did not label. */
function ticks(lo, hi, count) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + step / 1000; t += step) out.push(t);
  return out;
}

/** Split on gaps so a missing bucket breaks the line instead of bridging it. */
function segments(points, key, x, y) {
  const runs = [];
  let run = [];
  points.forEach((p, i) => {
    const v = p[key];
    if (v === null || v === undefined) {
      if (run.length) runs.push(run);
      run = [];
    } else {
      run.push({ x: x(i), y: y(v), v, p, i });
    }
  });
  if (run.length) runs.push(run);
  return runs;
}

/* ------------------------------------------------------------------ fetch */

function windowFor(range, passes) {
  const now = new Date();
  if (range === "all") return {};
  if (range === "1h") return { since: new Date(now - 3600e3) };
  if (range === "24h") return { since: new Date(now - 86400e3) };
  const wanted = range === "pass" ? 1 : 3;
  const recent = passes.slice(0, wanted);
  if (!recent.length) return { since: new Date(now - 3600e3) };
  return { since: new Date(recent[recent.length - 1].aos_at) };
}

async function load() {
  const root = $(".wrap");
  root.classList.add("stale"); // hold the previous render; never a skeleton flash

  try {
    const sat = state.satellite ? `satellite_id=${encodeURIComponent(state.satellite)}` : "";
    const [satellites, summary, passes] = await Promise.all([
      get("/satellites"),
      get(`/summary?${sat}`),
      get(`/passes?${sat}&limit=25`),
    ]);

    if (!state.satellite && satellites.length) {
      state.satellite = satellites[0].satellite_id;
      return load(); // re-fetch now that the scope is known
    }
    renderSatellites(satellites);

    const win = windowFor(state.range, passes);
    const q = new URLSearchParams();
    if (state.satellite) q.set("satellite_id", state.satellite);
    if (win.since) q.set("since", win.since.toISOString());
    q.set("buckets", "300");

    const [series, alerts] = await Promise.all([
      get(`/telemetry/series?${q}`),
      get(`/alerts?${sat}&open_only=true&limit=50`),
    ]);

    state.summary = summary;
    state.passes = passes;
    state.series = series;
    state.alerts = alerts;
    render();
    $("#foot").textContent =
      `${series.frame_count.toLocaleString()} frames in range · ` +
      `${series.points.length} buckets of ${series.bucket_seconds.toFixed(1)}s · ` +
      `updated ${new Date().toLocaleTimeString([], { hour12: false })}`;
  } catch (err) {
    $("#foot").textContent = `Could not reach the API: ${err.message}`;
  } finally {
    root.classList.remove("stale");
  }
}

/* ----------------------------------------------------------------- render */

function render() {
  renderHeader();
  renderTiles();
  renderCharts();
  renderAlerts();
  renderPasses();
}

function renderSatellites(satellites) {
  const sel = $("#satellite");
  if (sel.options.length === satellites.length && sel.value === state.satellite) return;
  sel.textContent = "";
  for (const s of satellites) {
    const opt = el("option", null, `${s.satellite_id} (${s.frame_count.toLocaleString()})`);
    opt.value = s.satellite_id;
    sel.appendChild(opt);
  }
  sel.value = state.satellite || "";
}

function renderHeader() {
  const s = state.summary;
  const pill = $("#link-pill");
  const live = Boolean(s.current_pass);
  pill.classList.toggle("live", live);
  $("#link-text").textContent = live
    ? `in contact · pass ${s.current_pass.id}`
    : `last contact ${ago(s.latest ? s.latest.received_at : null)}`;
  $("#station-note").textContent = s.last_pass
    ? `${s.pass_count} passes tracked · ${s.last_pass.ground_station_id}`
    : "no passes yet";
}

function renderTiles() {
  const s = state.summary;
  const box = $("#tiles");
  box.textContent = "";
  const t = s.latest;

  // One hero figure per view: the number that decides whether the spacecraft
  // survives the next eclipse.
  const hero = el("div", "tile hero");
  hero.appendChild(el("div", "label", "Battery voltage"));
  const heroVal = el("div", "value", t ? num(t.battery_voltage_v, 2) : "—");
  heroVal.appendChild(el("span", "unit", " V"));
  hero.appendChild(heroVal);
  hero.appendChild(
    el("div", "note", t ? `on-board ${clock(t.recorded_at)} · seq ${t.seq}` : "no telemetry"),
  );
  box.appendChild(hero);

  const tile = (label, value, unit, note) => {
    const node = el("div", "tile");
    node.appendChild(el("div", "label", label));
    const v = el("div", "value", value);
    if (unit) v.appendChild(el("span", "unit", " " + unit));
    node.appendChild(v);
    if (note) node.appendChild(el("div", "note", note));
    box.appendChild(node);
    return node;
  };

  tile("Temperature", t ? num(t.temperature_c, 1) : "—", "°C");
  tile("Mode", t ? t.mode : "—", "", t && t.mode !== "NOMINAL" ? "not nominal" : "");
  const a = s.open_alerts;
  const alerts = tile(
    "Open alerts",
    String(a.critical + a.warning + a.info),
    "",
    `${a.critical} critical · ${a.warning} warning`,
  );
  if (a.critical) alerts.querySelector(".value").classList.add("sev-critical");
  tile(
    "Unscreened",
    s.unscreened.toLocaleString(),
    "",
    s.unscreened ? "worker has a backlog" : "worker up to date",
  );
}

/* ------------------------------------------------------------------ chart */

function renderCharts() {
  const host = $("#charts");
  if (!host.dataset.built) {
    for (const cfg of CHARTS) {
      const card = el("div", "card");
      card.id = `card-${cfg.id}`;
      const cap = el("div", "cap");
      cap.appendChild(el("h2", null, cfg.title));
      cap.appendChild(el("span", "spacer"));
      cap.appendChild(el("span", "hint"));
      card.appendChild(cap);
      const plot = el("div", "plot");
      plot.appendChild(el("div", "tip"));
      card.appendChild(plot);
      const tv = el("div", "tableview");
      tv.hidden = true;
      card.appendChild(tv);
      host.appendChild(card);
      new ResizeObserver(() => drawChart(cfg)).observe(plot);
    }
    host.dataset.built = "1";
  }
  for (const cfg of CHARTS) drawChart(cfg);
}

function drawChart(cfg) {
  const card = document.getElementById(`card-${cfg.id}`);
  const plot = card.querySelector(".plot");
  const points = (state.series && state.series.points) || [];
  const width = Math.max(240, plot.clientWidth || 900);
  const narrow = width < 460;
  const padL = narrow ? 42 : 54;
  const padR = narrow ? 46 : 72; // room for the end label so it never overflows
  const padT = 12;
  const plotH = 170;
  const axisH = 26; // sized in, so the axis band is never cut off
  const height = padT + plotH + axisH;

  plot.querySelector("svg")?.remove();
  const root = svg("svg", {
    width,
    height,
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    tabindex: "0",
    "aria-label": `${cfg.title} over the selected range`,
  });
  plot.insertBefore(root, plot.firstChild);

  const values = [];
  for (const p of points) {
    for (const k of [cfg.avg, cfg.lo, cfg.hi]) {
      if (k && p[k] !== null && p[k] !== undefined) values.push(p[k]);
    }
  }
  const limits = state.summary ? cfg.limits(state.summary.limits) : [];

  if (!values.length) {
    card.querySelector(".hint").textContent = "";
    const empty = el("div", "empty", "No telemetry in this range.");
    plot.querySelectorAll(".empty").forEach((n) => n.remove());
    plot.appendChild(empty);
    return;
  }
  plot.querySelectorAll(".empty").forEach((n) => n.remove());

  // Keep every threshold inside the frame, or a limit line silently vanishes
  // exactly when the data is nowhere near it.
  const domain = values.concat(limits.map((l) => l.v));
  let lo = Math.min(...domain);
  let hi = Math.max(...domain);
  const pad = (hi - lo || Math.abs(hi) || 1) * 0.08;
  lo -= pad;
  hi += pad;

  const n = points.length;
  const x = (i) => padL + (n === 1 ? (width - padL - padR) / 2 : ((width - padL - padR) * i) / (n - 1));
  const y = (v) => padT + plotH - ((v - lo) / (hi - lo)) * plotH;

  // Pass windows, behind everything: the shaded stretches are when the station
  // could actually hear the spacecraft.
  const t0 = new Date(points[0].t).getTime();
  const t1 = new Date(points[n - 1].t).getTime();
  const tx = (ms) =>
    t1 === t0 ? x(0) : padL + ((width - padL - padR) * (ms - t0)) / (t1 - t0);
  for (const p of state.passes) {
    const a = new Date(p.aos_at).getTime();
    const b = p.los_at ? new Date(p.los_at).getTime() : t1;
    if (b < t0 || a > t1) continue;
    const xa = Math.max(padL, tx(a));
    const xb = Math.min(width - padR, tx(b));
    if (xb <= xa) continue;
    root.appendChild(
      svg("rect", {
        x: xa, y: padT, width: xb - xa, height: plotH,
        fill: "var(--pass-band)",
      }),
    );
  }

  // Gridlines and y ticks: solid hairlines, one step off the surface.
  for (const t of ticks(lo, hi, 4)) {
    const yy = y(t);
    if (yy < padT - 1 || yy > padT + plotH + 1) continue;
    root.appendChild(
      svg("line", {
        x1: padL, x2: width - padR, y1: yy, y2: yy,
        stroke: "var(--grid)", "stroke-width": 1,
      }),
    );
    const label = svg("text", {
      x: padL - 8, y: yy + 4, "text-anchor": "end",
      fill: "var(--muted)", "font-size": 11,
      style: "font-variant-numeric: tabular-nums",
    });
    label.textContent = num(t, cfg.digits);
    root.appendChild(label);
  }

  root.appendChild(
    svg("line", {
      x1: padL, x2: width - padR, y1: padT + plotH, y2: padT + plotH,
      stroke: "var(--axis)", "stroke-width": 1,
    }),
  );

  // Threshold lines. The line carries the status color; the text stays in ink
  // tokens, keyed by the coloured stroke beside it. Both sit inside the plot at
  // the left, so the right gutter belongs entirely to the end label and the two
  // can never collide.
  for (const lim of limits) {
    const yy = y(lim.v);
    if (yy < padT - 0.5 || yy > padT + plotH + 0.5) continue;
    root.appendChild(
      svg("line", {
        x1: padL, x2: width - padR, y1: yy, y2: yy,
        stroke: `var(--${lim.role})`, "stroke-width": 1,
      }),
    );
    // The caption sits above the line, or below it when the line is near the
    // top of the frame — never dropped, which is what happened when the limit
    // was far outside the data's own range.
    const above = yy - padT > 16;
    const capY = above ? yy - 8 : yy + 12;
    root.appendChild(
      svg("line", {
        x1: padL + 6, x2: padL + 16, y1: capY, y2: capY,
        stroke: `var(--${lim.role})`, "stroke-width": 2,
      }),
    );
    const text = svg("text", {
      x: padL + 21, y: capY + 3.5,
      fill: "var(--muted)", "font-size": 10,
    });
    text.textContent = `${num(lim.v, cfg.digits)} ${lim.label}`;
    root.appendChild(text);
  }

  // The min/max band shows what downsampling hid inside each bucket.
  if (cfg.lo && cfg.hi) {
    for (const run of segments(points, cfg.hi, x, y)) {
      if (run.length < 2) continue;
      const top = run.map((p) => `${p.x},${p.y}`);
      const bottom = run
        .map((p) => [p.x, points[p.i][cfg.lo]])
        .filter(([, v]) => v !== null && v !== undefined)
        .map(([xx, v]) => `${xx},${y(v)}`)
        .reverse();
      if (!bottom.length) continue;
      root.appendChild(
        svg("path", {
          d: `M${top.join("L")}L${bottom.join("L")}Z`,
          fill: "var(--series-wash)",
        }),
      );
    }
  }

  const runs = segments(points, cfg.avg, x, y);
  for (const run of runs) {
    root.appendChild(
      svg("path", {
        d: "M" + run.map((p) => `${p.x},${p.y}`).join("L"),
        fill: "none", stroke: "var(--series)", "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round",
      }),
    );
  }

  // The latest value, direct-labelled once. Every other value lives on the
  // axis, in the tooltip, and in the table view.
  const last = runs.length ? runs[runs.length - 1][runs[runs.length - 1].length - 1] : null;
  if (last) {
    root.appendChild(
      svg("circle", {
        cx: last.x, cy: last.y, r: 4.5,
        fill: "var(--series)", stroke: "var(--surface)", "stroke-width": 2,
      }),
    );
    const label = svg("text", {
      y: last.y + 4, fill: "var(--ink)", "font-size": 12, "font-weight": 600,
    });
    label.textContent = `${num(last.v, cfg.digits)} ${cfg.unit}`;
    root.appendChild(label);
    // Measure, then place: a label that would run past the frame goes to the
    // left of its dot instead of being clipped.
    const lw = label.getBBox().width;
    if (last.x + 10 + lw <= width - 2) {
      label.setAttribute("x", last.x + 10);
    } else {
      label.setAttribute("x", last.x - 9);
      label.setAttribute("text-anchor", "end");
    }
  }

  // X axis: as many clock labels as actually fit, never one per bucket. A
  // fixed count collides as soon as the pane gets narrow.
  const span = width - padL - padR;
  const maxLabels = Math.max(2, Math.floor(span / 78));
  const stride = Math.max(1, Math.ceil(n / maxLabels));
  for (let i = 0; i < n; i += stride) {
    const px = x(i);
    const text = svg("text", {
      x: px, y: padT + plotH + 17,
      "text-anchor": i === 0 ? "start" : "middle",
      fill: "var(--muted)", "font-size": 11,
      style: "font-variant-numeric: tabular-nums",
    });
    text.textContent = clock(points[i].t);
    root.appendChild(text);
  }

  card.querySelector(".hint").textContent =
    `${cfg.unit} · avg with min–max band per ${state.series.bucket_seconds.toFixed(1)}s bucket`;

  attachHover(cfg, root, plot, points, x, y, { padL, padR, padT, plotH, width });
  renderTable(cfg, card, points);
}

function attachHover(cfg, root, plot, points, x, y, box) {
  const tip = plot.querySelector(".tip");
  const cross = svg("line", {
    y1: box.padT, y2: box.padT + box.plotH,
    stroke: "var(--axis)", "stroke-width": 1, opacity: 0,
  });
  root.appendChild(cross);
  const dot = svg("circle", {
    r: 4.5, fill: "var(--series)", stroke: "var(--surface)",
    "stroke-width": 2, opacity: 0,
  });
  root.appendChild(dot);

  const show = (i) => {
    const p = points[i];
    if (!p) return;
    state.cursor[cfg.id] = i;
    const px = x(i);
    cross.setAttribute("x1", px);
    cross.setAttribute("x2", px);
    cross.setAttribute("opacity", 1);

    tip.textContent = "";
    tip.appendChild(el("div", "when", `${clock(p.t)} · ${p.n} frame${p.n === 1 ? "" : "s"}`));
    const rows = [["average", p[cfg.avg]]];
    if (cfg.lo) rows.push(["minimum", p[cfg.lo]], ["maximum", p[cfg.hi]]);
    for (const [name, value] of rows) {
      const row = el("div", "row");
      const left = el("span");
      left.appendChild(el("span", "key"));
      left.appendChild(el("span", "name", " " + name));
      row.appendChild(left);
      row.appendChild(el("span", "num", `${num(value, cfg.digits)} ${cfg.unit}`));
      tip.appendChild(row);
    }

    const v = p[cfg.avg];
    if (v !== null && v !== undefined) {
      dot.setAttribute("cx", px);
      dot.setAttribute("cy", y(v));
      dot.setAttribute("opacity", 1);
    } else {
      dot.setAttribute("opacity", 0);
    }

    tip.style.opacity = 1;
    const w = tip.offsetWidth || 150;
    tip.style.left = `${Math.max(4, Math.min(px + 14, box.width - w - 4))}px`;
    tip.style.top = `${box.padT + 6}px`;
  };

  const hide = () => {
    tip.style.opacity = 0;
    cross.setAttribute("opacity", 0);
    dot.setAttribute("opacity", 0);
  };

  // Aim at a time, not at a 2px line: snap to the nearest bucket.
  const nearest = (clientX) => {
    const rect = root.getBoundingClientRect();
    const px = clientX - rect.left;
    const span = box.width - box.padL - box.padR;
    const i = Math.round(((px - box.padL) / span) * (points.length - 1));
    return Math.max(0, Math.min(points.length - 1, i));
  };

  root.addEventListener("pointermove", (e) => show(nearest(e.clientX)));
  root.addEventListener("pointerleave", hide);
  root.addEventListener("focus", () => show(state.cursor[cfg.id] ?? points.length - 1));
  root.addEventListener("blur", hide);
  root.addEventListener("keydown", (e) => {
    const cur = state.cursor[cfg.id] ?? points.length - 1;
    const step = e.shiftKey ? 10 : 1;
    if (e.key === "ArrowRight") show(Math.min(points.length - 1, cur + step));
    else if (e.key === "ArrowLeft") show(Math.max(0, cur - step));
    else if (e.key === "Home") show(0);
    else if (e.key === "End") show(points.length - 1);
    else if (e.key === "Escape") hide();
    else return;
    e.preventDefault();
  });
}

/** The table view: every charted value, reachable without a pointer. */
function renderTable(cfg, card, points) {
  const host = card.querySelector(".tableview");
  host.hidden = !state.tables;
  if (!state.tables) return;

  host.textContent = "";
  const table = el("table");
  const head = el("tr");
  const cols = cfg.lo
    ? ["Time", "Frames", `Avg (${cfg.unit})`, `Min (${cfg.unit})`, `Max (${cfg.unit})`]
    : ["Time", "Frames", `Avg (${cfg.unit})`];
  for (const c of cols) head.appendChild(el("th", null, c));
  table.appendChild(head);
  for (const p of points) {
    const row = el("tr");
    row.appendChild(el("td", "text", clock(p.t)));
    row.appendChild(el("td", null, String(p.n)));
    row.appendChild(el("td", null, num(p[cfg.avg], cfg.digits)));
    if (cfg.lo) {
      row.appendChild(el("td", null, num(p[cfg.lo], cfg.digits)));
      row.appendChild(el("td", null, num(p[cfg.hi], cfg.digits)));
    }
    table.appendChild(row);
  }
  host.appendChild(table);
}

/* ----------------------------------------------------------------- alerts */

const SEVERITY_ICON = { critical: "▲", warning: "●", info: "■" };

function renderAlerts() {
  const host = $("#alerts");
  host.textContent = "";
  $("#alert-hint").textContent = state.alerts.length
    ? `${state.alerts.length} open`
    : "nothing open";

  if (!state.alerts.length) {
    host.appendChild(el("div", "empty", "No open alerts. Every screened frame is within limits."));
    return;
  }

  for (const a of state.alerts) {
    const row = el("div", "alert");
    // Icon and word, never colour alone.
    const icon = el("span", `icon sev-${a.severity}`, SEVERITY_ICON[a.severity] || "●");
    row.appendChild(icon);

    const body = el("div", "body");
    const head = el("div", "head");
    head.appendChild(el("span", "rule", a.rule));
    head.appendChild(el("span", `sev sev-${a.severity}`, a.severity));
    body.appendChild(head);
    body.appendChild(el("div", "msg", a.message));
    body.appendChild(
      el("div", "when", `${clock(a.detected_at)} · ${ago(a.detected_at)}` +
        (a.acknowledged_at ? " · acknowledged" : "")),
    );
    row.appendChild(body);

    const ack = el("button", null, a.acknowledged_at ? "Resolve" : "Ack");
    ack.addEventListener("click", async () => {
      ack.disabled = true;
      await fetch(`${API}/alerts/${a.id}/ack`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ resolve: Boolean(a.acknowledged_at) }),
      });
      load();
    });
    row.appendChild(ack);
    host.appendChild(row);
  }
}

function renderPasses() {
  const table = $("#passes");
  table.textContent = "";
  $("#pass-hint").textContent = state.passes.length ? `${state.passes.length} shown` : "";

  const head = el("tr");
  for (const c of ["Pass", "AOS", "Duration", "Max elev.", "Frames", "Status"]) {
    head.appendChild(el("th", null, c));
  }
  table.appendChild(head);

  if (!state.passes.length) {
    const row = el("tr");
    const cell = el("td", "text", "No passes recorded yet.");
    cell.colSpan = 6;
    row.appendChild(cell);
    table.appendChild(row);
    return;
  }

  for (const p of state.passes) {
    const row = el("tr");
    row.appendChild(el("td", null, String(p.id)));
    row.appendChild(el("td", "text", clock(p.aos_at)));
    row.appendChild(el("td", null, p.los_at ? duration(p.aos_at, p.los_at) : "open"));
    row.appendChild(
      el("td", null, p.max_elevation_deg === null ? "—" : `${p.max_elevation_deg.toFixed(1)}°`),
    );
    row.appendChild(el("td", null, p.frame_count.toLocaleString()));
    row.appendChild(el("td", "text", p.status));
    table.appendChild(row);
  }
}

/* --------------------------------------------------------------- controls */

function schedule() {
  clearInterval(state.timer);
  if (state.auto) state.timer = setInterval(load, 5000);
}

$("#satellite").addEventListener("change", (e) => {
  state.satellite = e.target.value;
  load();
});
$("#range").addEventListener("change", (e) => {
  state.range = e.target.value;
  load();
});
$("#refresh").addEventListener("click", load);
$("#auto").addEventListener("click", (e) => {
  state.auto = !state.auto;
  e.target.setAttribute("aria-pressed", String(state.auto));
  e.target.textContent = state.auto ? "Auto every 5s" : "Auto off";
  schedule();
});
$("#tables").addEventListener("click", (e) => {
  state.tables = !state.tables;
  e.target.setAttribute("aria-pressed", String(state.tables));
  renderCharts();
});
$("#theme").addEventListener("click", () => {
  const now = document.documentElement.getAttribute("data-theme");
  const next = now === "dark" ? "light" : now === "light" ? null : "dark";
  if (next) document.documentElement.setAttribute("data-theme", next);
  else document.documentElement.removeAttribute("data-theme");
  try {
    next ? localStorage.setItem("mgs-theme", next) : localStorage.removeItem("mgs-theme");
  } catch {
    /* private browsing: the choice just does not persist */
  }
  renderCharts();
});

try {
  const saved = localStorage.getItem("mgs-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
} catch {
  /* ignore */
}

load();
schedule();
