// Configure this to wherever backend/api.py is actually running.
// Left as relative /api by default assuming the backend is reverse-proxied
// on the same origin — change to e.g. "https://your-server:8000/api" if not.
const API_BASE = "/api";
const REFRESH_MS = 2 * 60 * 1000; // matches backend GMP_REFRESH_SECONDS default

// Backend stores dates as ISO "YYYY-MM-DD" (correct for sorting/storage).
// This converts to DD-MM-YYYY only for on-screen display.
function formatDateDMY(isoDate) {
  if (!isoDate) return "—";
  const parts = isoDate.split("-");
  if (parts.length !== 3) return isoDate; // not ISO -- show as-is rather than mangle it
  const [y, m, d] = parts;
  return `${d}-${m}-${y}`;
}

let currentTab = "open";
let allRecords = [];
let searchTerm = "";

const cardList = document.getElementById("cardList");
const updatedText = document.getElementById("updatedText");
const nextUpdateText = document.getElementById("nextUpdateText");
const updatedRow = document.getElementById("updatedRow");
const searchInput = document.getElementById("searchInput");

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    currentTab = tab.dataset.tab;
    loadTab(currentTab);
  });
});

searchInput.addEventListener("input", (e) => {
  searchTerm = e.target.value.trim().toLowerCase();
  render();
});

function fmtTime(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function minutesAgo(iso) {
  if (!iso) return null;
  return Math.round((Date.now() - new Date(iso).getTime()) / 60000);
}

function gmpClass(pct) {
  if (pct === null || pct === undefined) return "neu";
  if (pct > 0) return "pos";   // any positive GMP reads as a clear gain signal -- green
  if (pct < 0) return "neg";   // any negative GMP -- red
  return "neu";                 // exactly 0% -- flat, neither gain nor loss
}

function directionIndicator(direction) {
  // Computed server-side using a specific rule (see _update_gmp_direction
  // in investorgain_scraper.py): "up"/"down" reflect the most recent
  // REAL change and persist through quiet refresh cycles where GMP
  // hasn't moved -- they don't reset to flat just because nothing changed
  // in the last 2 minutes. "flat" is reserved for ONE specific case: the
  // first check of the day found GMP unchanged from yesterday's closing
  // value. direction is null for a brand-new IPO with no prior value to
  // compare against -- showing nothing in that case is intentional.
  if (direction === "up") return ` <span class="gmp-dir gmp-dir-up" title="GMP has risen — still up since its last real move">▲</span>`;
  if (direction === "down") return ` <span class="gmp-dir gmp-dir-down" title="GMP has fallen — still down since its last real move">▼</span>`;
  if (direction === "flat") return ` <span class="gmp-dir gmp-dir-flat" title="GMP unchanged from yesterday's closing value">—</span>`;
  return ""; // no prior data to compare -- show nothing rather than guess
}

function subClass(v) {
  if (v === null || v === undefined) return "neu";
  if (v >= 5) return "pos";
  if (v >= 1) return "mid";
  return "neg";
}

async function loadTab(tab) {
  cardList.innerHTML = `<div class="empty-state">Loading…</div>`;
  try {
    // Two layers against stale data being served instead of a real fetch:
    // 1. cache: "no-store" tells the browser itself to skip its HTTP
    //    cache entirely for this request, not just the service worker
    //    (which already excludes /api/ calls, but the browser's own
    //    cache is a separate layer the service worker doesn't control).
    // 2. A cache-busting query param defeats any intermediate proxy/CDN
    //    (e.g. on some mobile carriers) that ignores cache headers and
    //    caches by URL alone -- this is why two phones could disagree on
    //    "X mins ago" even though the backend itself was current on both.
    const res = await fetch(`${API_BASE}/ipos/${tab}?_=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Server returned ${res.status}`);
    }
    const data = await res.json();
    allRecords = data.records || [];
    // data_changed_at is the honest "did the numbers actually change"
    // signal; attempted_at proves the refresh loop itself is alive even
    // during a quiet period where nothing changed. Falls back to the
    // older "fetched_at" field for backward compatibility.
    updateFreshness(
      data.data_changed_at || data.fetched_at,
      data.attempted_at || data.fetched_at
    );
    render();
  } catch (err) {
    cardList.innerHTML = `<div class="error-state">⚠ Could not load live data.<br>${escapeHtml(err.message)}<br><br>Showing nothing rather than guessed numbers — pull to retry.</div>`;
    updatedText.textContent = "Update failed";
    updatedRow.classList.add("stale");
  }
}

function updateFreshness(dataChangedAt, attemptedAt) {
  // Two different questions, answered honestly instead of conflated:
  //   1. "Is the refresh loop actually alive?" -> use attemptedAt for the
  //      stale/broken warning (if this goes quiet, something's wrong)
  //   2. "When did the numbers on screen last genuinely change?" -> show
  //      dataChangedAt as the human-readable text (this can legitimately
  //      stay the same for a while if GMP just hasn't moved -- that's not
  //      staleness, that's real market quiet)
  const attemptMins = minutesAgo(attemptedAt || dataChangedAt);
  const changedMins = minutesAgo(dataChangedAt);

  if (attemptMins === null) {
    updatedText.textContent = "Never updated";
    updatedRow.classList.add("stale");
    return;
  }

  updatedText.textContent = changedMins !== null && changedMins > 0
    ? `Last Changed: ${fmtTime(dataChangedAt)} (${changedMins} min ago)`
    : `Last Checked: ${fmtTime(attemptedAt || dataChangedAt)}`;

  // Warn only if the refresh loop itself appears to have stopped running
  // (roughly 3x the refresh interval with no attempt at all) -- NOT just
  // because the data hasn't changed, since that can be entirely normal.
  if (attemptMins > 6) {
    updatedText.textContent += ` — ⚠ refresh may be stuck (${attemptMins} min since last check)`;
    updatedRow.classList.add("stale");
  } else {
    updatedRow.classList.remove("stale");
  }
  const next = new Date(new Date(attemptedAt || dataChangedAt).getTime() + REFRESH_MS);
  nextUpdateText.textContent = `Next check: ${fmtTime(next.toISOString())}`;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function render() {
  let records = allRecords;
  if (searchTerm) {
    records = records.filter((r) => r.company_name.toLowerCase().includes(searchTerm));
  }
  // Sort direction depends on the tab: Open/Upcoming want the soonest
  // date first (what's closing soonest, what's opening soonest); Closed
  // wants the OPPOSITE -- most recently closed IPO first, not oldest.
  records = [...records].sort((a, b) => {
    const cmp = (a.close_date || "").localeCompare(b.close_date || "");
    return currentTab === "closed" ? -cmp : cmp;
  });

  if (records.length === 0) {
    cardList.innerHTML = `<div class="empty-state">No ${currentTab} Mainboard IPOs right now.</div>`;
    return;
  }

  cardList.innerHTML = records.map(renderCard).join("");
}

function renderCard(r) {
  const gmpPct = r.gmp_percent === null || r.gmp_percent === undefined ? null : r.gmp_percent;
  const gmpText = gmpPct === null ? "Not Available" : `${gmpPct.toFixed(2)}%`;
  const gmpRupeeText = r.gmp === null || r.gmp === undefined ? "" : `₹${r.gmp}`;
  const sub = r.subscription || {};
  const subText = (v) => (sub.started === false ? "—" : (v === null || v === undefined ? "N/A" : `${v.toFixed(2)}x`));
  const sentiment = gmpClass(gmpPct); // "pos" | "mid" | "neg" | "neu"
  const statusLabel = { open: "Open", upcoming: "Upcoming", closed: "Closed", listed: "Listed" }[r.status] || r.status;

  return `
    <div class="card">
      <div class="name-row">
        <div class="name">${escapeHtml(r.company_name)}</div>
        <span class="status-tag status-${r.status}"><span class="dot"></span>${statusLabel}</span>
      </div>
      <div class="gmp-label">GMP (Indicative)</div>
      <div class="gmp ${gmpClass(gmpPct)}">${gmpText}${directionIndicator(r.gmp_direction)}${gmpRupeeText ? ` <span class="gmp-rupee">(${gmpRupeeText})</span>` : ""}</div>

      ${sub.started === false ? `<div class="meta-row"><span>Subscription: Not Started</span></div>` : `
      <div class="sub-grid">
        <div class="sub-cell"><div class="k">QIB</div><div class="v ${subClass(sub.qib)}">${subText(sub.qib)}</div></div>
        <div class="sub-cell"><div class="k">NII</div><div class="v ${subClass(sub.nii)}">${subText(sub.nii)}</div></div>
        <div class="sub-cell"><div class="k">Retail</div><div class="v ${subClass(sub.retail)}">${subText(sub.retail)}</div></div>
        <div class="sub-cell"><div class="k">Total</div><div class="v ${subClass(sub.total)}">${subText(sub.total)}</div></div>
      </div>`}

      <div class="meta-row issue-size-row">
        <span>Issue Size</span>
        <span>${r.issue_size_cr ? `₹${r.issue_size_cr} Cr` : "Not Available"}</span>
      </div>
      ${r.registrar ? `
      <div class="meta-row">
        <span>Registrar</span>
        <span class="registrar-value">${escapeHtml(r.registrar)}</span>
      </div>` : ""}
      ${r.status === "closed" || r.status === "listed" ? `
      <div class="meta-row">
        <span>Actual Listing Gain</span>
        <span class="${listingGainClass(r.listing_gain_percent)}">${r.listing_gain_percent != null ? `${r.listing_gain_percent > 0 ? "+" : ""}${r.listing_gain_percent}%` : "Not Available"}</span>
      </div>` : ""}
      <div class="dates-row">
        <span><b class="date-label">Open</b> ${formatDateDMY(r.open_date)}</span>
        <span><b class="date-label">Close</b> ${formatDateDMY(r.close_date)}</span>
      </div>
      <div class="dates-row">
        <span><b class="date-label">BOA</b> ${formatDateDMY(r.boa_date)}</span>
        <span><b class="date-label">Listing</b> ${formatDateDMY(r.listing_date)}</span>
      </div>
    </div>
  `;
}

function listingGainClass(pct) {
  if (pct == null) return "";
  return pct >= 0 ? "gain-positive" : "gain-negative";
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  });
}

loadTab(currentTab);
setInterval(() => loadTab(currentTab), REFRESH_MS);
