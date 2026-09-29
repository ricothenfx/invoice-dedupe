/* invoice-dedupe review app (Phase 3).
 *
 * React SPA served by the FastAPI service from vendored UMD builds — no build
 * step, no CDN, fully offline. Tabs:
 *   Dashboard    threshold slider + live precision/exposure + score histogram
 *   Review       pair comparisons with per-field score breakdown + triage
 *   Invoices     paginated invoice list
 *   Upload       PDF upload with job progress
 */
(function () {
  "use strict";

  const h = React.createElement;
  const { useState, useEffect, useMemo, useCallback, useRef } = React;

  // Theme persistence key — must match the inline bootstrap in index.html.
  const THEME_KEY = "dedupe-theme";

  // Display-only copy of scoring.FieldWeights (engine constants, DESIGN §3).
  const WEIGHTS = { invoice_no: 0.35, vendor: 0.25, amount: 0.3, date: 0.1 };

  // ---------------------------------------------------------------- utilities

  async function request(path, opts) {
    let resp;
    try {
      resp = await fetch(path, opts);
    } catch (e) {
      throw new Error("network error — is the service running?");
    }
    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const body = await resp.json();
        if (body && body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch (e) { /* keep statusText */ }
      throw new Error(`HTTP ${resp.status}: ${detail}`);
    }
    return resp.json();
  }

  const getJSON = (path) => request(path);
  const postJSON = (path, body) =>
    request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

  function fmtMoney(n) {
    if (n == null) return "—";
    if (n >= 1e9) return "$" + (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return "$" + (n / 1e6).toFixed(2) + "M";
    if (n >= 1e4) return "$" + (n / 1e3).toFixed(1) + "K";
    return "$" + Math.round(n).toLocaleString("en-US");
  }

  function fmtMoneyFull(n) {
    return n == null ? "—" : "$" + Math.round(n).toLocaleString("en-US");
  }

  function fmtPct(x, digits) {
    return x == null ? "—" : (100 * x).toFixed(digits == null ? 2 : digits) + "%";
  }

  function fmtConf(c) {
    return c == null || c === "" ? "—" : Math.round(100 * c) + "%";
  }

  function normText(s) {
    return (s == null ? "" : String(s)).toLowerCase().replace(/[^a-z0-9]+/g, "");
  }

  function errorBanner(message) {
    return h("div", { className: "panel", key: "err" },
      h("span", { className: "err", style: { color: "var(--red)" } }, message),
      h("span", { className: "muted", style: { marginLeft: "10px" } }, "Retry by reloading the page."));
  }

  // ------------------------------------------------------------- tiny widgets

  function Meter({ value, color }) {
    return h("div", { className: "meter" },
      h("i", { style: { width: Math.round(100 * Math.max(0, Math.min(1, value))) + "%", background: color || "var(--accent)" } }));
  }

  function Badge(kind, text) {
    return h("span", { className: "badge " + kind }, text);
  }

  // ---------------------------------------------------------------- dashboard

  function computeLive(data, t) {
    const rows = data.distribution || [];
    const flaggedRows = rows.filter((r) => r.score >= t);
    const flagged = flaggedRows.length;
    const valueAtRisk = flaggedRows.reduce((s, r) => s + r.value, 0);
    const tp = flaggedRows.filter((r) => r.gt).length;
    const decidedRows = flaggedRows.filter((r) => r.decision);
    const decidedDup = decidedRows.filter((r) => r.decision === "duplicate").length;

    const gtTotal = data.n_gt_pairs || 0;
    const gtPrecision = flagged > 0 ? tp / flagged : null;
    const gtRecall = gtTotal > 0 ? tp / gtTotal : null;
    const estPrecision = decidedRows.length > 0 ? decidedDup / decidedRows.length : null;

    // curves at 0.01 granularity for the chart
    const curve = [];
    for (let x = 0.5; x <= 1.0001; x += 0.01) {
      const fr = rows.filter((r) => r.score >= x);
      const tpX = fr.filter((r) => r.gt).length;
      const decX = fr.filter((r) => r.decision);
      const decDupX = decX.filter((r) => r.decision === "duplicate").length;
      curve.push({
        t: x,
        flagged: fr.length,
        precision: data.n_gt_pairs ? (fr.length ? tpX / fr.length : null) : null,
        estimated: decX.length ? decDupX / decX.length : null,
      });
    }

    // score histogram, buckets of 0.05
    const buckets = [];
    for (let i = 0; i < 20; i++) buckets.push({ gt: 0, nongt: 0 });
    rows.forEach((r) => {
      const idx = Math.min(19, Math.max(0, Math.floor(r.score * 20)));
      if (r.gt) buckets[idx].gt += 1; else buckets[idx].nongt += 1;
    });

    return {
      flagged, valueAtRisk, tp,
      gtPrecision, gtRecall, estPrecision,
      decidedCount: decidedRows.length,
      curve, buckets,
    };
  }

  function LineChart({ series, color, yMax, yLabel }) {
    const W = 300, H = 110, PAD = 24;
    const n = series.length;
    if (n < 2) return null;
    const max = yMax != null ? yMax : Math.max(1, ...series.map((p) => p.y));
    const x = (i) => PAD + (i / (n - 1)) * (W - PAD - 6);
    const y = (v) => H - 18 - (v / max) * (H - 30);
    const path = series.map((p, i) => (i ? "L" : "M") + x(i).toFixed(1) + "," + y(p.y).toFixed(1)).join(" ");
    return h("svg", { className: "chart", width: "100%", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" },
      [0, 0.5, 1].map((f) => h("line", { key: f, className: "gridline", x1: PAD, x2: W - 6, y1: y(max * f), y2: y(max * f) })),
      h("text", { x: 2, y: y(max) + 4 }, String(yLabel ? yLabel(max) : max)),
      h("text", { x: 2, y: y(0) + 4 }, String(yLabel ? yLabel(0) : 0)),
      h("text", { x: PAD, y: H - 4 }, series[0].x.toFixed(2)),
      h("text", { x: W - 20, y: H - 4 }, series[n - 1].x.toFixed(2)),
      h("path", { className: color, d: path }));
  }

  function DashboardTab() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [threshold, setThreshold] = useState(0.9);

    useEffect(() => {
      getJSON("/metrics").then((d) => {
        setData(d);
        if (d.tuning) setThreshold(Math.min(1, Math.max(0.5, d.tuning.flag_threshold)));
      }).catch((e) => setError(e.message));
    }, []);

    const live = useMemo(() => (data ? computeLive(data, threshold) : null), [data, threshold]);

    if (error) return errorBanner(error);
    if (!data) return h("div", { className: "empty" }, "Loading metrics…");

    const labels = data.label_counts || {};
    const decisions = data.decisions || {};
    const decidedTotal = (decisions.duplicate || 0) + (decisions.not_duplicate || 0);

    const precisionLine = live.gtPrecision != null
      ? h("div", { className: "precision-note" },
          h("strong", null, "Measured precision @ " + threshold.toFixed(3) + ": " + fmtPct(live.gtPrecision)),
          " — true positives " + live.tp + " of " + live.flagged + " flagged pairs, recall " + fmtPct(live.gtRecall) +
          " (against " + data.n_gt_pairs + " seeded ground-truth duplicates).")
      : live.estPrecision != null
        ? h("div", { className: "precision-note" },
            h("strong", null, "Estimated precision @ " + threshold.toFixed(3) + ": " + fmtPct(live.estPrecision)),
            " — " + live.decidedCount + " reviewed pair(s) at or above this threshold; " +
            (live.decidedCount - (live.estPrecision * live.decidedCount)) + " marked not a duplicate. Review more pairs to sharpen this estimate.")
        : h("div", { className: "precision-note" },
            "No ground truth loaded and no review decisions at this threshold yet. Triage pairs in the ",
            h("a", { href: "#", onClick: (e) => { e.preventDefault(); window.dispatchEvent(new CustomEvent("nav", { detail: "queue" })); } }, "review queue"),
            " — precision is then estimated from your decisions.");

    const cards = [
      { label: "Invoices", value: (data.n_invoices || 0).toLocaleString("en-US") },
      { label: data.tuning ? "Flagged (tuned)" : "Flagged @ 0.90", value: (labels.flag || 0).toLocaleString("en-US"), sub: "auto-flagged duplicates" },
      { label: "Review queue", value: (labels.review || 0).toLocaleString("en-US"), sub: "needs human triage" },
      { label: "Decisions", value: decidedTotal.toLocaleString("en-US"), sub: (decisions.duplicate || 0) + " confirmed duplicates" },
      { label: "Value at risk @ " + threshold.toFixed(2), value: fmtMoney(live ? live.valueAtRisk : 0), sub: "double-payment exposure" },
    ];

    const precisionSeries = data.n_gt_pairs
      ? live.curve.filter((p) => p.precision != null).map((p) => ({ x: p.t, y: p.precision }))
      : live.curve.filter((p) => p.estimated != null).map((p) => ({ x: p.t, y: p.estimated }));
    const flaggedSeries = live.curve.map((p) => ({ x: p.t, y: p.flagged }));

    const maxBucket = Math.max(1, ...live.buckets.map((b) => b.gt + b.nongt));

    return h("div", null,
      h("h1", null, "Dashboard"),
      h("div", { className: "cards" },
        cards.map((c) => h("div", { className: "card", key: c.label },
          h("div", { className: "label" }, c.label),
          h("div", { className: "value" }, c.value),
          c.sub ? h("div", { className: "sub" }, c.sub) : null))),

      h("div", { className: "panel" },
        h("h2", { style: { marginTop: 0 } }, "Flag threshold"),
        data.tuning ? h("div", { className: "precision-note", style: { marginBottom: "10px" } },
          h("strong", null, "Tuned model active (" + data.tuning.model_id + ")"),
          " — learned from " + data.tuning.n_decisions + " review decisions; flag ≥ " +
          data.tuning.flag_threshold + ", review ≥ " + data.tuning.review_threshold +
          " (pair scores are the tuned model's). Run invoice-dedupe tune --reset to revert.")
        : null,
        h("div", { className: "slider-row" },
          h("input", {
            type: "range", min: "0.5", max: "1", step: "0.005", value: threshold,
            onChange: (e) => setThreshold(parseFloat(e.target.value)),
          }),
          h("div", { className: "slider-value" }, threshold.toFixed(3))),
        h("div", { className: "cards", style: { marginBottom: 0 } },
          h("div", { className: "card", key: "f" },
            h("div", { className: "label" }, "Flagged at threshold"),
            h("div", { className: "value" }, live.flagged.toLocaleString("en-US"))),
          h("div", { className: "card", key: "p" },
            h("div", { className: "label" }, "Precision"),
            h("div", { className: "value" }, fmtPct(live.gtPrecision != null ? live.gtPrecision : live.estPrecision)),
            h("div", { className: "sub" }, data.n_gt_pairs ? "measured vs seeded ground truth" : "estimated from review decisions")),
          h("div", { className: "card", key: "r" },
            h("div", { className: "label" }, "Recall"),
            h("div", { className: "value" }, fmtPct(live.gtRecall)),
            h("div", { className: "sub" }, data.n_gt_pairs ? "measured vs seeded ground truth" : "needs ground truth")),
          h("div", { className: "card", key: "v" },
            h("div", { className: "label" }, "Value at risk"),
            h("div", { className: "value" }, fmtMoney(live.valueAtRisk)),
            h("div", { className: "sub" }, fmtMoneyFull(live.valueAtRisk) + " potential double payments"))),
        precisionLine),

      h("div", { className: "panel" },
        h("h2", { style: { marginTop: 0 } }, "Threshold curves"),
        h("div", { style: { display: "flex", gap: "24px", flexWrap: "wrap" } },
          precisionSeries.length > 1
            ? h("div", { style: { flex: 1, minWidth: "280px" }, key: "pc" },
                h("div", { className: "muted", style: { fontSize: "12px", marginBottom: "4px" } }, data.n_gt_pairs ? "Precision vs threshold (measured)" : "Estimated precision vs threshold"),
                h(LineChart, { series: precisionSeries, color: "line-precision", yMax: 1 }))
            : h("div", { className: "muted", key: "pc-empty" }, "Precision curve appears once ground truth or review decisions exist."),
          h("div", { style: { flex: 1, minWidth: "280px" }, key: "fc" },
            h("div", { className: "muted", style: { fontSize: "12px", marginBottom: "4px" } }, "Flagged pairs vs threshold"),
            h(LineChart, { series: flaggedSeries, color: "line-flagged" })))),

      h("div", { className: "panel" },
        h("h2", { style: { marginTop: 0 } }, "Pair score distribution"),
        h("div", { className: "histogram" },
          live.buckets.map((b, i) =>
            h("div", { className: "bar", key: i, title: (0.05 * i).toFixed(2) + "–" + (0.05 * (i + 1)).toFixed(2) + ": " + (b.gt + b.nongt) + " pairs (" + b.gt + " true duplicates)" },
              h("div", { className: "seg nongt", style: { height: ((b.nongt / maxBucket) * 100).toFixed(1) + "%" } }),
              h("div", { className: "seg gt", style: { height: ((b.gt / maxBucket) * 100).toFixed(1) + "%" } }))),
          h("div", { className: "tick", style: { position: "absolute", left: 0 } }, "")),
        h("div", { style: { display: "flex", justifyContent: "space-between", fontFamily: "var(--mono)", fontSize: "10px", color: "var(--muted)" } },
          h("span", null, "0.00"), h("span", null, "0.50"), h("span", null, "1.00")),
        h("div", { className: "legend" },
          h("span", null, h("i", { className: "swatch", style: { background: "var(--red)" } }), "true duplicates (seeded GT)"),
          h("span", null, h("i", { className: "swatch", style: { background: "var(--hist-nongt)" } }), "other candidate pairs"))));
  }

  // ------------------------------------------------------------- review queue

  const FIELD_DEFS = [
    { key: "invoice_no", label: "Invoice no", weight: WEIGHTS.invoice_no },
    { key: "vendor", label: "Vendor", weight: WEIGHTS.vendor },
    { key: "amount", label: "Amount", weight: WEIGHTS.amount },
    { key: "date", label: "Date", weight: WEIGHTS.date },
  ];

  function InvoiceSide({ tag, inv, other }) {
    const rows = [
      { k: "Vendor", v: inv.vendor_name, diff: normText(inv.vendor_name) !== normText(other.vendor_name) },
      { k: "Number", v: inv.invoice_no, diff: normText(inv.invoice_no) !== normText(other.invoice_no) },
      { k: "Amount", v: fmtMoneyFull(inv.amount), diff: inv.amount !== other.amount },
      { k: "Date", v: inv.invoice_date || "—", diff: inv.invoice_date !== other.invoice_date },
      { k: "Tax ID", v: inv.tax_id || "—", diff: inv.tax_id !== other.tax_id },
      { k: "Source", v: (inv.source_type || "manual") + (inv.source_name ? " · " + inv.source_name : ""), diff: false },
      { k: "Extraction", v: fmtConf(inv.extraction_confidence), diff: false },
    ];
    return h("div", { className: "inv-side" },
      h("div", { className: "side-tag" }, tag + " · " + inv.id),
      h("div", { className: "vendor", style: { fontWeight: 600, marginBottom: "6px" } }, inv.vendor_name || "(no vendor)"),
      rows.map((r) =>
        h("div", { className: "field-row" + (r.diff ? " diff" : ""), key: r.k },
          h("span", { className: "k" }, r.k),
          h("span", { className: "v", title: r.diff ? "fields differ" : null }, r.v))));
  }

  function PairCard({ pair, selected, onDecide }) {
    const b = pair.breakdown || {};
    const taxChip = b.tax_id === "mismatch" ? Badge("flag", "tax ID mismatch")
      : b.tax_id === "match" ? Badge("dup", "tax ID match") : null;
    const ruleChip = b.rule ? Badge("rule", "rule: " + b.rule) : null;
    const decidedBadge = pair.decision === "duplicate" ? Badge("dup", "confirmed duplicate")
      : pair.decision === "not_duplicate" ? Badge("pass", "marked not a duplicate") : null;

    return h("div", { className: "pair-card" + (selected ? " selected" : "") + (pair.decision ? " decided" : "") },
      h("div", { className: "pair-head" },
        h("span", { className: "score" }, pair.score.toFixed(4)),
        Badge(pair.label, pair.label),
        ruleChip, taxChip, decidedBadge,
        h("span", { className: "spacer" }),
        h("span", { className: "ids" }, pair.id_a + "  ↔  " + pair.id_b)),
      h("div", { className: "compare" },
        h(InvoiceSide, { tag: "A", inv: pair.a, other: pair.b }),
        h(InvoiceSide, { tag: "B", inv: pair.b, other: pair.a })),
      h("div", { className: "breakdown" },
        FIELD_DEFS.map((f) => {
          const sim = typeof b[f.key] === "number" ? b[f.key] : 0;
          const color = sim >= 0.99 ? "var(--green)" : sim >= 0.85 ? "var(--amber)" : "var(--red)";
          return [
            h("span", { className: "fname", key: f.key + "-n" }, f.label + " (w " + f.weight.toFixed(2) + ")"),
            h(Meter, { key: f.key + "-m", value: sim, color }),
            h("span", { className: "fval", key: f.key + "-v" }, sim.toFixed(4) + " → " + (sim * f.weight).toFixed(4)),
          ];
        }).flat()),
      h("div", { className: "actions" },
        h("button", {
          className: "btn duplicate", disabled: !!pair.decision,
          onClick: () => onDecide(pair, "duplicate"),
        }, "Duplicate"),
        h("button", {
          className: "btn not-duplicate", disabled: !!pair.decision,
          onClick: () => onDecide(pair, "not_duplicate"),
        }, "Not a duplicate"),
        pair.decision ? h("span", { className: "decided-as muted" }, "decision saved") : null,
        h("span", { className: "kbd-hint" },
          h("kbd", null, "j"), "/", h("kbd", null, "k"), " navigate  ",
          h("kbd", null, "d"), " duplicate  ", h("kbd", null, "n"), " not a duplicate")));
  }

  function QueueTab() {
    const [label, setLabel] = useState("flag");
    const [undecidedOnly, setUndecidedOnly] = useState(false);
    const [pairs, setPairs] = useState(null);
    const [error, setError] = useState(null);
    const [sel, setSel] = useState(0);
    const [saving, setSaving] = useState(false);
    const pairsRef = useRef(null);
    pairsRef.current = pairs;
    const selRef = useRef(0);
    selRef.current = sel;

    const load = useCallback(() => {
      const params = new URLSearchParams({ label, limit: "1000" });
      if (undecidedOnly) params.set("undecided", "true");
      setPairs(null);
      getJSON("/pairs?" + params.toString())
        .then((rows) => { setPairs(rows); setSel(0); })
        .catch((e) => setError(e.message));
    }, [label, undecidedOnly]);

    useEffect(load, [load]);

    const decide = useCallback((pair, decision) => {
      if (pair.decision || saving) return;
      setSaving(true);
      postJSON(`/pairs/${pair.id_a}/${pair.id_b}/decision`, { decision, reviewer: "web-reviewer" })
        .then((saved) => {
          setSaving(false);
          const next = pairsRef.current.map((p) =>
            p.id_a === pair.id_a && p.id_b === pair.id_b ? { ...p, decision: saved.decision } : p);
          setPairs(next);
          // advance to the next undecided pair
          const cur = selRef.current;
          let j = -1;
          for (let i = cur + 1; i < next.length; i++) if (!next[i].decision) { j = i; break; }
          if (j < 0) for (let i = 0; i < next.length; i++) if (!next[i].decision) { j = i; break; }
          if (j >= 0) setSel(j);
        })
        .catch((e) => { setSaving(false); setError(e.message); });
    }, [saving]);

    useEffect(() => {
      function onKey(e) {
        const t = e.target;
        if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) return;
        const rows = pairsRef.current;
        if (!rows || !rows.length) return;
        if (e.key === "j" || e.key === "ArrowDown") {
          setSel((s) => Math.min(rows.length - 1, s + 1));
          e.preventDefault();
        } else if (e.key === "k" || e.key === "ArrowUp") {
          setSel((s) => Math.max(0, s - 1));
          e.preventDefault();
        } else if (e.key === "d") {
          const p = rows[selRef.current];
          if (p && !p.decision) decide(p, "duplicate");
        } else if (e.key === "n") {
          const p = rows[selRef.current];
          if (p && !p.decision) decide(p, "not_duplicate");
        }
      }
      document.addEventListener("keydown", onKey);
      return () => document.removeEventListener("keydown", onKey);
    }, [decide]);

    if (error) return errorBanner(error);
    if (!pairs) return h("div", { className: "empty" }, "Loading pairs…");

    const decidedCount = pairs.filter((p) => p.decision).length;

    return h("div", null,
      h("h1", null, "Review queue"),
      h("div", { className: "toolbar" },
        h("label", null, "Label ",
          h("select", { value: label, onChange: (e) => setLabel(e.target.value) },
            h("option", { value: "flag" }, "Flag (auto-flagged)"),
            h("option", { value: "review" }, "Review zone"),
            h("option", { value: "pass" }, "Pass"),
            h("option", { value: "" }, "All"))),
        h("label", null,
          h("input", { type: "checkbox", checked: undecidedOnly, onChange: (e) => setUndecidedOnly(e.target.checked) }),
          "Hide decided"),
        h("span", { className: "progress" },
          `showing ${pairs.length} pairs · ${decidedCount} decided in this view`)),
      pairs.length === 0
        ? h("div", { className: "empty" }, "Nothing here — every pair in this view has been triaged.")
        : pairs.map((p, i) => h(PairCard, {
            key: p.id_a + "|" + p.id_b, pair: p, selected: i === sel, onDecide: decide,
          })));
  }

  // ----------------------------------------------------------------- invoices

  function InvoicesTab() {
    const [page, setPage] = useState(0);
    const [rows, setRows] = useState(null);
    const [error, setError] = useState(null);
    const pageSize = 50;

    useEffect(() => {
      setRows(null);
      getJSON(`/invoices?limit=${pageSize}&offset=${page * pageSize}`)
        .then(setRows)
        .catch((e) => setError(e.message));
    }, [page]);

    if (error) return errorBanner(error);

    return h("div", null,
      h("h1", null, "Invoices"),
      h("div", { className: "panel" },
        !rows
          ? h("div", { className: "empty" }, "Loading…")
          : rows.length === 0
            ? h("div", { className: "empty" }, "No invoices yet — upload a PDF or run ", h("code", { className: "mono" }, "invoice-dedupe seed-demo"), ".")
            : h("table", null,
                h("thead", null, h("tr", null,
                  ["ID", "Vendor", "Invoice no", "Amount", "Date", "Tax ID", "Source", "Extraction", ""].map((x) => h("th", { key: x, className: x === "Amount" ? "num" : "" }, x)))),
                h("tbody", null, rows.map((inv) =>
                  h("tr", { key: inv.id },
                    h("td", { className: "mono" }, inv.id),
                    h("td", null, inv.vendor_name),
                    h("td", { className: "mono" }, inv.invoice_no),
                    h("td", { className: "num" }, fmtMoneyFull(inv.amount)),
                    h("td", { className: "mono" }, inv.invoice_date || "—"),
                    h("td", { className: "mono" }, inv.tax_id || "—"),
                    h("td", null, inv.source_type + (inv.source_name ? " · " + inv.source_name : "")),
                    h("td", null, fmtConf(inv.extraction_confidence)),
                    h("td", null, inv.variant ? Badge("review", inv.variant) : null))))),
        h("div", { className: "pager" },
          h("button", { className: "btn", disabled: page === 0, onClick: () => setPage(page - 1) }, "Previous"),
          h("button", { className: "btn", disabled: !rows || rows.length < pageSize, onClick: () => setPage(page + 1) }, "Next"),
          h("span", { className: "info" }, "page " + (page + 1)))));
  }

  // ------------------------------------------------------------------- upload

  function UploadTab() {
    const [drag, setDrag] = useState(false);
    const [busy, setBusy] = useState(false);
    const [log, setLog] = useState([]);
    const inputRef = useRef(null);

    const push = (line) => setLog((l) => [...l, line]);

    const pollJob = async (jobId, tries) => {
      for (let i = 0; i < (tries || 120); i++) {
        const job = await getJSON("/jobs/" + jobId);
        if (job.status === "done") return job;
        if (job.status === "failed") throw new Error("job failed: " + (job.error || "unknown error"));
        await new Promise((r) => setTimeout(r, 1000));
      }
      throw new Error("timed out waiting for job " + jobId);
    };

    const upload = async (file) => {
      if (!file) return;
      if (!file.name.toLowerCase().endsWith(".pdf")) { push({ err: true, text: "Please choose a PDF file." }); return; }
      setBusy(true);
      setLog([]);
      try {
        push({ text: "Uploading " + file.name + " (" + Math.ceil(file.size / 1024) + " KiB)…" });
        const fd = new FormData();
        fd.append("file", file, file.name);
        const resp = await request("/invoices/pdf", { method: "POST", body: fd });
        push({ text: "Extraction job " + resp.job_id + " queued." });
        const extractJob = await pollJob(resp.job_id);
        push({ text: "Extracted: confidence " + fmtConf(extractJob.result.confidence) +
          (extractJob.result.missing && extractJob.result.missing.length ? ", missing fields: " + extractJob.result.missing.join(", ") : ", all core fields found") + "." });
        push({ text: "Detection job " + extractJob.result.detect_job_id + " queued." });
        const detectJob = await pollJob(extractJob.result.detect_job_id);
        push({ text: "Detection done: " + detectJob.result.n_invoices + " invoices, " + detectJob.result.n_pairs + " candidate pairs (" + detectJob.result.flagged + " flagged, " + detectJob.result.review + " review)." });
        push({ text: "Invoice added — see the Invoices tab; check the Review queue for new pairs." });
      } catch (e) {
        push({ err: true, text: e.message });
      } finally {
        setBusy(false);
      }
    };

    return h("div", null,
      h("h1", null, "Upload invoice PDF"),
      h("div", { className: "panel" },
        h("div", {
          className: "dropzone" + (drag ? " drag" : ""),
          onClick: () => inputRef.current && inputRef.current.click(),
          onDragOver: (e) => { e.preventDefault(); setDrag(true); },
          onDragLeave: () => setDrag(false),
          onDrop: (e) => { e.preventDefault(); setDrag(false); if (!busy) upload(e.dataTransfer.files[0]); },
        },
          busy ? "Working…" : "Drop a text-layer PDF here or click to choose a file",
          h("div", { className: "muted", style: { marginTop: "8px", fontSize: "12px" } },
            "Extraction and detection run asynchronously in the worker; max 10 MiB.")),
        h("input", {
          ref: inputRef, type: "file", accept: ".pdf,application/pdf", style: { display: "none" },
          onChange: (e) => { if (!busy && e.target.files[0]) upload(e.target.files[0]); e.target.value = ""; },
        }),
        log.length
          ? h("div", { className: "job-result" },
              log.map((l, i) => h("div", { key: i, className: l.err ? "err" : "" }, l.text)))
          : null));
  }

  // --------------------------------------------------------------------- app

  const TABS = [
    { id: "dashboard", title: "Dashboard" },
    { id: "queue", title: "Review queue" },
    { id: "invoices", title: "Invoices" },
    { id: "upload", title: "Upload" },
  ];

  function App() {
    // tabs are deep-linkable via location.hash (e.g. /#queue)
    const [tab, setTabState] = useState(() => {
      const hash = (window.location.hash || "").replace("#", "");
      return TABS.some((t) => t.id === hash) ? hash : "dashboard";
    });
    const setTab = useCallback((id) => {
      setTabState(id);
      history.replaceState(null, "", "#" + id);
    }, []);
    const [dbStatus, setDbStatus] = useState(null);
    // Theme is applied to <html data-theme> by the inline bootstrap; React only mirrors it.
    const [theme, setTheme] = useState(() => document.documentElement.dataset.theme === "light" ? "light" : "dark");
    const toggleTheme = useCallback(() => {
      setTheme((t) => {
        const next = t === "dark" ? "light" : "dark";
        document.documentElement.dataset.theme = next;
        try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* storage unavailable */ }
        return next;
      });
    }, []);

    useEffect(() => {
      const onNav = (e) => setTab(e.detail);
      const onHash = () => {
        const hash = (window.location.hash || "").replace("#", "");
        if (TABS.some((t) => t.id === hash)) setTabState(hash);
      };
      window.addEventListener("nav", onNav);
      window.addEventListener("hashchange", onHash);
      return () => { window.removeEventListener("nav", onNav); window.removeEventListener("hashchange", onHash); };
    }, [setTab]);

    useEffect(() => {
      getJSON("/health").then((r) => setDbStatus(r.status === "ok")).catch(() => setDbStatus(false));
    }, []);

    let view = null;
    if (tab === "dashboard") view = h(DashboardTab, { key: "dashboard" });
    else if (tab === "queue") view = h(QueueTab, { key: "queue" });
    else if (tab === "invoices") view = h(InvoicesTab, { key: "invoices" });
    else view = h(UploadTab, { key: "upload" });

    return h("div", null,
      h("header", null,
        h("div", { className: "brand" }, "invoice", h("span", null, "-"), "dedupe"),
        h("nav", null, TABS.map((t) =>
          h("button", {
            key: t.id, className: tab === t.id ? "active" : "",
            onClick: () => setTab(t.id),
          }, t.title))),
        h("div", { className: "status" },
          dbStatus == null ? "checking database…" :
            dbStatus ? h("span", { className: "ok" }, "● database connected") :
              h("span", { className: "down" }, "● database unavailable")),
        h("button", {
          className: "theme-toggle",
          onClick: toggleTheme,
          title: "Switch between dark and light theme",
          "aria-label": "Switch theme",
        }, theme === "dark" ? "Light mode" : "Dark mode")),
      h("main", null, view));
  }

  ReactDOM.createRoot(document.getElementById("root")).render(h(App));
})();
