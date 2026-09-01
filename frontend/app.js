// Configure this to wherever backend/api.py is actually running.
// Left as relative /api by default assuming the backend is reverse-proxied
// on the same origin — change to e.g. "https://your-server:8000/api" if not.
const API_BASE = "/api";
const REFRESH_MS = 5 * 60 * 1000; // matches backend GMP_REFRESH_SECONDS default

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
  if (pct >= 50) return "pos";
  if (pct >= 15) return "mid";
  if (pct > 0) return "mid";
  return "neg";
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
    const res = await fetch(`${API_BASE}/ipos/${tab}`);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Server returned ${res.status}`);
    }
    const data = await res.json();
    allRecords = data.records || [];
    updateFreshness(data.fetched_at);
    render();
  } catch (err) {
    cardList.innerHTML = `<div class="error-state">⚠ Could not load live data.<br>${escapeHtml(err.message)}<br><br>Showing nothing rather than guessed numbers — pull to retry.</div>`;
    updatedText.textContent = "Update failed";
    updatedRow.classList.add("stale");
  }
}

function updateFreshness(fetchedAt) {
  const mins = minutesAgo(fetchedAt);
  if (mins === null) {
    updatedText.textContent = "Never updated";
    updatedRow.classList.add("stale");
    return;
  }
  updatedText.textContent = `Last Updated: ${fmtTime(fetchedAt)}`;
  if (mins > 15) {
    updatedText.textContent += ` (⚠ ${mins} min ago)`;
    updatedRow.classList.add("stale");
  } else {
    updatedRow.classList.remove("stale");
  }
  const next = new Date(new Date(fetchedAt).getTime() + REFRESH_MS);
  nextUpdateText.textContent = `Next: ${fmtTime(next.toISOString())}`;
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
  // Default sort: closing date / active priority
  records = [...records].sort((a, b) => (a.close_date || "").localeCompare(b.close_date || ""));

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
  const sentiment = gmpClass(gmpPct); // "pos" | "mid" | "neg" | "neu" — also drives the card's accent bar

  return `
    <div class="card accent-${sentiment}">
      <div class="name-row">
        <div class="name">${escapeHtml(r.company_name)}</div>
      </div>
      <div class="gmp-label">GMP (Indicative)</div>
      <div class="gmp ${gmpClass(gmpPct)}">${gmpText}${gmpRupeeText ? ` <span class="gmp-rupee">(${gmpRupeeText})</span>` : ""}</div>

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
