// Small progressive-enhancement helpers. The app works fully without JS.

// Copy-to-clipboard for digest social captions.
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".copy");
  if (!btn) return;
  const text = btn.getAttribute("data-copy") || "";
  navigator.clipboard?.writeText(text).then(() => {
    const original = btn.textContent;
    btn.textContent = "Copied ✓";
    setTimeout(() => (btn.textContent = original), 1400);
  });
});

// Live-refresh the public drops feed JSON every 30s on the /drops page,
// updating the "x ago" timestamps without a full reload.
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
    } catch (_) {
      /* offline / transient — ignore */
    }
  };
  setInterval(tick, 30000);
})();
