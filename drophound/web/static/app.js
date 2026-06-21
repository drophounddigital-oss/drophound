// DropHound progressive enhancement — app works fully without JS.

function getCsrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content || "";
}

// --------------------------------------------------------------------------
// Copy-to-clipboard (digest share captions)
// --------------------------------------------------------------------------
function flashCopied(btn) {
  const original = btn.dataset.label || btn.textContent;
  btn.dataset.label = original;
  btn.textContent = "Copied ✓";
  btn.classList.add("copied");
  setTimeout(() => {
    btn.textContent = original;
    btn.classList.remove("copied");
  }, 1400);
}

async function copyText(text) {
  // Preferred: async Clipboard API (needs HTTPS / secure context)
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) { /* fall through to legacy path */ }
  }
  // Fallback: hidden textarea + execCommand (works without secure context)
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (_) {
    return false;
  }
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".copy");
  if (!btn) return;
  const text = btn.getAttribute("data-copy") || "";
  const ok = await copyText(text);
  if (ok) flashCopied(btn);
  else {
    btn.textContent = "Press ⌘C";
    setTimeout(() => (btn.textContent = btn.dataset.label || "Copy"), 1600);
  }
});

// --------------------------------------------------------------------------
// Live-refresh public drops feed every 30s (timestamps only, no full reload)
// --------------------------------------------------------------------------
(function pollDrops() {
  if (!location.pathname.startsWith("/drops")) return;
  const tick = async () => {
    try {
      const res = await fetch("/api/drops?limit=50", { headers: { Accept: "application/json" } });
      if (!res.ok) return;
      const data = await res.json();
      const ages = document.querySelectorAll("[data-age-id]");
      const byId = new Map(data.drops.map((d) => [String(d.id), d.age]));
      ages.forEach((el) => {
        const a = byId.get(el.getAttribute("data-age-id"));
        if (a) el.textContent = a;
      });
    } catch (_) { /* offline / transient */ }
  };
  setInterval(tick, 30000);
})();

// --------------------------------------------------------------------------
// Skeleton loaders — show while the catalog API call is in flight
// --------------------------------------------------------------------------
function showCatalogSkeleton(container, count = 12) {
  container.innerHTML = `<div class="catalog-skeleton">` +
    Array.from({ length: count }, () => `
      <div class="skel-item">
        <div class="skeleton skel-card"></div>
        <div class="skeleton skel-line medium" style="margin-top:8px"></div>
        <div class="skeleton skel-line short"></div>
      </div>`).join("") +
    `</div>`;
}

// --------------------------------------------------------------------------
// Optimistic watch-button rendering (session-based — no email param needed)
// --------------------------------------------------------------------------
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".watch-btn");
  if (!btn) return;

  // Not logged in: send to login page
  if (btn.dataset.loggedIn !== "true") {
    location.href = "/login?next=/watch";
    return;
  }

  const pid = btn.dataset.pid;
  const isOn = btn.classList.contains("on");
  const endpoint = isOn ? "/watch/remove" : "/watch/add";

  // --- Optimistic update: flip immediately ---
  btn.classList.add("optimistic");
  btn.classList.toggle("on", !isOn);
  const counterEl = document.querySelector(".watch-count");
  if (counterEl) {
    const n = parseInt(counterEl.textContent, 10) || 0;
    counterEl.textContent = isOn ? Math.max(0, n - 1) : n + 1;
  }

  // --- Send to server (session cookie is sent automatically) ---
  try {
    const body = new URLSearchParams({ product_id: pid, _csrf: getCsrfToken() });
    const res = await fetch(endpoint, { method: "POST", body });
    if (res.status === 401) { location.href = "/login?next=/watch"; return; }
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    btn.classList.toggle("on", data.watched);
    if (counterEl) counterEl.textContent = data.count;
  } catch (err) {
    // Revert on failure
    btn.classList.toggle("on", isOn);
    if (counterEl) {
      const n = parseInt(counterEl.textContent, 10) || 0;
      counterEl.textContent = isOn ? n + 1 : Math.max(0, n - 1);
    }
    console.warn("watch toggle failed:", err);
  } finally {
    btn.classList.remove("optimistic");
  }
});

// --------------------------------------------------------------------------
// Catalog browse on /watch — skeleton → real cards via /api/catalog
// --------------------------------------------------------------------------
(function initCatalog() {
  const grid = document.getElementById("catalog-grid");
  if (!grid) return;

  const loggedIn = grid.dataset.loggedIn === "true";
  let currentPage = 1;
  let totalPages = 1;

  const searchEl  = document.getElementById("catalog-search");
  const chipEls   = document.querySelectorAll(".chip[data-char]");
  const stockEl   = document.getElementById("filter-stock");
  const prevBtn   = document.getElementById("page-prev");
  const nextBtn   = document.getElementById("page-next");
  const pageInfo  = document.getElementById("page-info");
  const totalEl   = document.getElementById("catalog-total");

  let activeChar = "";

  function buildCard(p) {
    const watchLabel = p.watched ? "Watching" : "Watch";
    const watchClass = p.watched ? "watch-btn on" : "watch-btn";
    const statusDot  = p.status === "in_stock"
      ? `<span class="dot dot-green" data-tip="In stock"></span>`
      : p.status === "sold_out"
      ? `<span class="dot dot-red" data-tip="Sold out"></span>`
      : `<span class="dot dot-amber" data-tip="Unknown stock"></span>`;
    const watchBtn = loggedIn
      ? `<button class="${watchClass}" data-pid="${p.id}" data-logged-in="true"
           data-tip="${p.watched ? 'Stop watching' : 'Get alerts for this drop'}">${watchLabel}</button>`
      : `<button class="watch-btn" data-pid="${p.id}" data-logged-in="false"
           data-tip="Sign up for alerts">Watch</button>`;
    return `
      <div class="cat-card">
        <div class="cat-thumb" style="background:${p.color}">
          <span class="cat-initials">${p.initials}</span>
        </div>
        <div class="cat-info">
          <div class="cat-name-row">${statusDot}<span class="cat-name">${esc(p.name)}</span></div>
          <div class="cat-meta">${esc(p.brand)} · ${esc(p.retailer)}</div>
          ${p.resale_low && p.resale_high
            ? `<div class="cat-price" data-tip="eBay price range">$${p.resale_low.toFixed(0)} – $${p.resale_high.toFixed(0)}</div>`
            : p.price ? `<div class="cat-price">$${p.price.toFixed(2)}</div>` : ""}
          ${p.resale_median ? `<div class="cat-resale" data-tip="eBay median">↗ $${p.resale_median.toFixed(2)}</div>` : ""}
        </div>
        <div class="cat-actions">
          ${watchBtn}
          <a class="btn btn-ghost btn-sm" href="/go/${p.id}?to=site" target="_blank"
             data-tip="Buy this drop">Buy</a>
        </div>
      </div>`;
  }

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g,"&amp;").replace(/</g,"&lt;")
      .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
  }

  async function load(page = 1) {
    showCatalogSkeleton(grid);
    const q   = searchEl ? searchEl.value.trim() : "";
    const ins = stockEl  ? (stockEl.checked ? "1" : "0") : "0";
    const params = new URLSearchParams({ q, character: activeChar, in_stock: ins, page });
    try {
      const res  = await fetch(`/api/catalog?${params}`);
      const data = await res.json();
      currentPage = data.page;
      totalPages  = data.pages;
      if (data.products.length === 0) {
        grid.innerHTML = `<p class="muted center" style="padding:40px 0">No products match your filters.</p>`;
      } else {
        grid.innerHTML = `<div class="catalog-grid">${data.products.map(buildCard).join("")}</div>`;
      }
      if (totalEl)  totalEl.textContent  = data.total.toLocaleString();
      if (pageInfo) pageInfo.textContent  = `Page ${currentPage} of ${totalPages}`;
      if (prevBtn)  prevBtn.disabled      = currentPage <= 1;
      if (nextBtn)  nextBtn.disabled      = currentPage >= totalPages;
      const wc = document.querySelector(".watch-count");
      if (wc && data.watch_count != null) wc.textContent = data.watch_count;
    } catch (err) {
      grid.innerHTML = `<p class="muted center" style="padding:40px 0">
        Failed to load catalog — <a href="">reload</a>.</p>`;
      console.error("catalog load failed:", err);
    }
  }

  // Debounced search
  let debounce;
  if (searchEl) searchEl.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => load(1), 280);
  });

  // Character chips
  chipEls.forEach(chip => chip.addEventListener("click", () => {
    const char = chip.dataset.char;
    activeChar = activeChar === char ? "" : char;
    chipEls.forEach(c => c.classList.toggle("active", c.dataset.char === activeChar));
    load(1);
  }));

  if (stockEl) stockEl.addEventListener("change", () => load(1));
  if (prevBtn) prevBtn.addEventListener("click", () => load(currentPage - 1));
  if (nextBtn) nextBtn.addEventListener("click", () => load(currentPage + 1));

  grid.addEventListener("dh:reload", () => load(1));

  load(1);
})();
