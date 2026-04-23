// FORRT Bibliography — single-page app, vanilla JS + Chart.js + vis-network.
// All data is preloaded from /data/*.json; all filtering is client-side.

const PAGE_SIZE = 50;
const CONTRIB_PAGE_SIZE = 48;

const state = {
  works: [],
  contributors: [],
  clusters: [],
  clusterById: new Map(),
  network: null,
  stats: null,
  meta: null,
  filtered: [],
  shown: PAGE_SIZE,
  contribFiltered: [],
  contribShown: CONTRIB_PAGE_SIZE,
};

const el = (id) => document.getElementById(id);

async function loadJSON(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

function formatDate(iso) {
  if (!iso) return "unknown";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined,
    { year: "numeric", month: "short", day: "numeric" });
}

function escapeHTML(s) {
  return (s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- Boot ----

async function boot() {
  const [works, contributors, clusters, network, stats, meta] = await Promise.all([
    loadJSON("data/works.json"),
    loadJSON("data/contributors.json"),
    loadJSON("data/clusters.json"),
    loadJSON("data/network.json"),
    loadJSON("data/stats.json"),
    loadJSON("data/meta.json"),
  ]);
  state.works = works;
  state.contributors = contributors;
  state.clusters = clusters.clusters;
  state.clusters.forEach(c => state.clusterById.set(c.number, c));
  state.network = network;
  state.stats = stats;
  state.meta = meta;

  renderHeroStats();
  renderUpdatedBadge();
  renderYearChart();
  renderClusterBars();
  renderSpotlight();
  populateBrowseFilters();
  applyFilters();
  renderContributors();
  setupNetworkLazyInit();
}

function renderSpotlight() {
  const h = state.stats.highlights;
  if (!h || !h.most_cited?.length) return;

  state.carousel = { items: h.most_cited, index: 0, timer: null };

  const track = el("carousel-track");
  const dots = el("carousel-dots");
  track.innerHTML = h.most_cited.map(renderSpotlightSlide).join("");
  dots.innerHTML = h.most_cited
    .map((_, i) => `<button class="dot" data-index="${i}" aria-label="Go to slide ${i + 1}"></button>`)
    .join("");
  setCarousel(0);

  el("carousel-prev").addEventListener("click", () => stepCarousel(-1));
  el("carousel-next").addEventListener("click", () => stepCarousel(1));
  dots.addEventListener("click", (e) => {
    const b = e.target.closest(".dot");
    if (b) setCarousel(Number(b.dataset.index));
  });
  const wrap = el("carousel-track").parentElement;
  wrap.addEventListener("mouseenter", stopCarouselTimer);
  wrap.addEventListener("mouseleave", startCarouselTimer);
  wrap.addEventListener("focusin", stopCarouselTimer);
  wrap.addEventListener("focusout", startCarouselTimer);
  document.addEventListener("keydown", (e) => {
    if (!wrap.contains(document.activeElement)) return;
    if (e.key === "ArrowLeft")  stepCarousel(-1);
    if (e.key === "ArrowRight") stepCarousel(1);
  });
  startCarouselTimer();
  window.addEventListener("resize", debounce(() => setCarousel(state.carousel.index), 120));

  const venuesMax = Math.max(1, ...h.top_venues.map(v => v.count));
  el("venue-list").innerHTML = h.top_venues.map(v => {
    const pct = Math.max(6, (v.count / venuesMax) * 100);
    return `<li>
      <div class="venue-row">
        <span class="venue-name">${escapeHTML(v.venue)}</span>
        <span class="venue-count">${v.count}</span>
      </div>
      <div class="venue-bar"><div style="width:${pct}%"></div></div>
    </li>`;
  }).join("");
}

function renderSpotlightSlide(w, i) {
  const authors = w.authors.slice(0, 4).join(", ") +
    (w.n_authors > 4 ? ` + ${w.n_authors - 4} more` : "");
  const clusterChips = w.clusters.map((num) => {
    const c = state.clusterById.get(num);
    return c ? `<span class="chip cluster" data-cluster="${num}">${num}. ${escapeHTML(c.name)}</span>` : "";
  }).join(" ");
  const url = w.doi ? `https://doi.org/${w.doi}` : `https://openalex.org/${w.id}`;
  return `<article class="carousel-slide" data-index="${i}" aria-hidden="true">
    <div class="spotlight-label">#${i + 1} most cited · ${w.cited_by_count.toLocaleString()} citations</div>
    <a href="${escapeHTML(url)}" target="_blank" rel="noopener" class="spotlight-title">${escapeHTML(w.title)}</a>
    <div class="spotlight-meta">${w.year ? w.year : ""}${w.venue ? ` · <em>${escapeHTML(w.venue)}</em>` : ""}</div>
    <div class="spotlight-authors">${escapeHTML(authors)}</div>
    <div class="work-chips">${clusterChips}</div>
  </article>`;
}

function setCarousel(i) {
  const n = state.carousel.items.length;
  state.carousel.index = ((i % n) + n) % n;
  const track = el("carousel-track");
  // translateX% is relative to the element's own border-box width, which for
  // a flex track equals ONE slide (not the whole content) — so use pixels.
  const slideWidth = track.parentElement.clientWidth;
  track.style.transform = `translateX(-${state.carousel.index * slideWidth}px)`;
  track.querySelectorAll(".carousel-slide").forEach((slide, idx) => {
    slide.setAttribute("aria-hidden", idx === state.carousel.index ? "false" : "true");
  });
  el("carousel-dots").querySelectorAll(".dot").forEach((d, idx) => {
    d.classList.toggle("active", idx === state.carousel.index);
  });
}

function stepCarousel(delta) {
  setCarousel(state.carousel.index + delta);
  stopCarouselTimer();
  startCarouselTimer();
}

function startCarouselTimer() {
  stopCarouselTimer();
  state.carousel.timer = setInterval(() => setCarousel(state.carousel.index + 1), 7000);
}

function stopCarouselTimer() {
  if (state.carousel?.timer) { clearInterval(state.carousel.timer); state.carousel.timer = null; }
}

// ---- Hero / stats ----

function renderHeroStats() {
  const { totals } = state.meta;
  const { contributors_with_open_work } = state.stats;
  const items = [
    { n: state.stats.open_works.toLocaleString(), lbl: "open-scholarship works" },
    { n: totals.works_in_db.toLocaleString(), lbl: "total works indexed" },
    { n: contributors_with_open_work.toLocaleString(), lbl: "FORRT contributors" },
    { n: state.clusters.length, lbl: "FORRT clusters" },
  ];
  el("hero-stats").innerHTML = items.map(
    (i) => `<div class="stat"><span class="n">${i.n}</span><span class="lbl">${i.lbl}</span></div>`
  ).join("");

  // Cluster strip below the stats — a proportional ribbon of the 11 cluster
  // colours sized by work count. Tiny, decorative, but teases the taxonomy.
  const counts = state.stats.cluster_counts;
  const total = state.clusters.reduce((s, c) => s + (counts[String(c.number)] || 0), 0) || 1;
  el("cluster-strip").innerHTML = state.clusters.map((c) => {
    const n = counts[String(c.number)] || 0;
    const w = Math.max(2, (n / total) * 100);
    return `<span title="Cluster ${c.number}: ${escapeHTML(c.name)} — ${n} works"
               data-cluster="${c.number}" style="flex-basis:${w}%"></span>`;
  }).join("");
}

function renderUpdatedBadge() {
  const when = formatDate(state.meta.generated_at);
  el("updated-badge").textContent = "Last updated: " + when;
  el("updated-inline").textContent = when;
}

// ---- Year chart ----

const YEAR_CHART_MIN = 2000;  // Pre-2000 scraped data is unreliable.

function renderYearChart() {
  // Chart.js is loaded with `defer`; wait for it if the module evaluates first.
  const draw = () => {
    const yrs = Object.keys(state.stats.year_totals)
      .map(Number)
      .filter((y) => y >= YEAR_CHART_MIN)
      .sort((a, b) => a - b);
    const totals = yrs.map((y) => state.stats.year_totals[y] || 0);
    const open = yrs.map((y) => state.stats.year_open_totals[y] || 0);

    const ctx = el("year-chart").getContext("2d");
    new Chart(ctx, {
      type: "bar",
      data: {
        labels: yrs,
        datasets: [
          {
            label: "All works",
            data: totals,
            backgroundColor: "rgba(15,23,42,0.12)",
            borderColor: "rgba(15,23,42,0.8)",
            borderWidth: 0,
            stack: "a",
          },
          {
            label: "Open-scholarship",
            data: open,
            backgroundColor: "rgba(224,122,95,0.88)",
            borderWidth: 0,
            stack: "a",
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: "bottom", labels: { boxWidth: 10, font: { size: 12 } } },
          tooltip: { mode: "index", intersect: false },
        },
        scales: {
          x: { grid: { display: false }, ticks: { autoSkip: true, maxTicksLimit: 10 } },
          y: { beginAtZero: true, grid: { color: "rgba(0,0,0,0.06)" } },
        },
      },
    });
  };
  if (window.Chart) draw();
  else window.addEventListener("load", draw, { once: true });
}

// ---- Cluster bars ----

function renderClusterBars() {
  const counts = state.stats.cluster_counts;
  const max = Math.max(1, ...Object.values(counts).map(Number));
  const html = state.clusters.map((c) => {
    const n = counts[String(c.number)] || 0;
    const pct = Math.max(2, (n / max) * 100);
    const subsText = c.sub_clusters.length
      ? `${c.sub_clusters.length} sub-clusters`
      : "";
    return `<li data-cluster="${c.number}">
      <a href="#browse" data-filter-cluster="${c.number}" title="${escapeHTML(subsText)} — click to filter Browse">
        <div class="cluster-bar-row">
          <span class="num">${c.number}</span>
          <span class="name">${escapeHTML(c.name)}</span>
          <span class="val">${n}</span>
        </div>
        <div class="cluster-bar-track"><div class="cluster-bar-fill" style="width:${pct}%"></div></div>
      </a>
    </li>`;
  }).join("");
  el("cluster-bars").innerHTML = html;

  // Clicking a cluster bar applies that cluster as the Browse filter.
  el("cluster-bars").addEventListener("click", (e) => {
    const a = e.target.closest("a[data-filter-cluster]");
    if (!a) return;
    // Let the anchor navigate to #browse; also set the active chip.
    const num = a.getAttribute("data-filter-cluster");
    document.querySelectorAll("#cluster-chips .cluster-chip").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-cluster") === num);
    });
    applyFilters();
  });
}

// ---- Browse filters ----

function populateBrowseFilters() {
  // Cluster chip filter: horizontal row with counts, single-active-select.
  const counts = state.stats.cluster_counts;
  const chipsHtml = [`<button type="button" class="cluster-chip all active" data-cluster=""><span class="dot"></span>All <span class="count">${state.works.length.toLocaleString()}</span></button>`]
    .concat(state.clusters.map((c) => {
      const n = counts[String(c.number)] || 0;
      return `<button type="button" class="cluster-chip" data-cluster="${c.number}" title="${escapeHTML(c.name)}">
        <span class="dot"></span>${c.number}. ${escapeHTML(c.name)} <span class="count">${n}</span>
      </button>`;
    })).join("");
  el("cluster-chips").innerHTML = chipsHtml;
  el("cluster-chips").addEventListener("click", (e) => {
    const btn = e.target.closest(".cluster-chip");
    if (!btn) return;
    el("cluster-chips").querySelectorAll(".cluster-chip").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    applyFilters();
  });

  const years = [...new Set(state.works.map((w) => w.year).filter(Boolean))]
    .sort((a, b) => b - a);
  const yearSelect = el("year-select");
  years.forEach((y) => {
    const opt = document.createElement("option");
    opt.value = String(y); opt.textContent = y;
    yearSelect.appendChild(opt);
  });

  el("search-input").addEventListener("input", debounce(applyFilters, 180));
  yearSelect.addEventListener("change", applyFilters);
  el("oa-only").addEventListener("change", applyFilters);
  el("sort-select").addEventListener("change", applyFilters);
  el("show-more").addEventListener("click", () => {
    state.shown += PAGE_SIZE; renderResults();
  });
}

function getActiveCluster() {
  const active = document.querySelector("#cluster-chips .cluster-chip.active");
  return active ? active.getAttribute("data-cluster") : "";
}

function debounce(fn, ms) {
  let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function applyFilters() {
  const q = el("search-input").value.trim().toLowerCase();
  const cluster = getActiveCluster();
  const year = el("year-select").value;
  const oaOnly = el("oa-only").checked;
  const sort = el("sort-select").value;

  let out = state.works;
  if (cluster) {
    const num = parseInt(cluster, 10);
    out = out.filter((w) => w.clusters.includes(num));
  }
  if (year) out = out.filter((w) => String(w.year) === year);
  if (oaOnly) out = out.filter((w) => w.is_oa);
  if (q) {
    // Token-OR match on title, venue, authors.
    const tokens = q.split(/\s+/).filter(Boolean);
    out = out.filter((w) => {
      const hay = (w.title + " " + (w.venue || "") + " " +
                   w.authors.map((a) => a.name).join(" ")).toLowerCase();
      return tokens.every((t) => hay.includes(t));
    });
  }
  if (sort === "year-desc") {
    out = [...out].sort((a, b) => (b.year || 0) - (a.year || 0) ||
                                  (b.cited_by_count - a.cited_by_count));
  } else if (sort === "year-asc") {
    out = [...out].sort((a, b) => (a.year || 9999) - (b.year || 9999));
  } else if (sort === "cited-desc") {
    out = [...out].sort((a, b) => b.cited_by_count - a.cited_by_count);
  }

  state.filtered = out;
  state.shown = PAGE_SIZE;
  renderResults();
}

function renderResults() {
  const total = state.filtered.length;
  el("result-count").textContent =
    `${total.toLocaleString()} work${total === 1 ? "" : "s"} match`;

  if (total === 0) {
    el("results").innerHTML =
      '<li class="empty">No works match these filters. Try clearing them or broadening your search.</li>';
    const more = el("show-more");
    more.disabled = true;
    more.textContent = "Nothing to show";
    return;
  }

  const slice = state.filtered.slice(0, state.shown);
  el("results").innerHTML = slice.map(renderWork).join("");

  const more = el("show-more");
  if (state.shown >= total) { more.disabled = true; more.textContent = "End of list"; }
  else { more.disabled = false; more.textContent = `Show more (${total - state.shown} remaining)`; }
}

function renderWork(w) {
  const authors = w.authors.slice(0, 12).map((a) => {
    const name = escapeHTML(a.name);
    if (a.forrt) {
      const href = a.orcid
        ? `https://orcid.org/${a.orcid}`
        : (a.openalex_id ? `https://openalex.org/${a.openalex_id}` : null);
      return href
        ? `<a class="forrt" href="${href}" target="_blank" rel="noopener">${name}</a>`
        : `<span class="forrt">${name}</span>`;
    }
    return name;
  }).join(", ");
  const more = w.authors.length > 12 ? `, +${w.authors.length - 12} more` : "";

  const clusters = w.clusters.map((num) => {
    const c = state.clusterById.get(num);
    return c ? `<span class="chip cluster" data-cluster="${num}" title="Cluster ${num}: ${escapeHTML(c.name)}">${num}. ${escapeHTML(c.name)}</span>` : "";
  }).join("");

  const oa = w.is_oa
    ? `<span class="chip oa" title="Open access: ${escapeHTML(w.oa_status || "")}">OA</span>` : "";
  const year = w.year ? `<span class="chip year">${w.year}</span>` : "";

  const url = w.doi ? `https://doi.org/${w.doi}` : (w.oa_url || `https://openalex.org/${w.id}`);
  const venue = w.venue ? ` · <em>${escapeHTML(w.venue)}</em>` : "";
  const type = w.type && w.type !== "article" ? ` · ${escapeHTML(w.type)}` : "";

  return `<li class="work">
    <div>
      <a class="work-title" href="${escapeHTML(url)}" target="_blank" rel="noopener">${escapeHTML(w.title || "(untitled)")}</a>
      <div class="work-meta">${w.year ? w.year : "n.d."}${venue}${type}</div>
      <div class="work-authors">${authors}${more}</div>
      <div class="work-chips">${year}${oa}${clusters}</div>
    </div>
    <div class="work-stats">
      <span class="big">${(w.cited_by_count || 0).toLocaleString()}</span>
      <span class="lbl">citations</span>
      ${w.doi ? `<a class="doi" href="https://doi.org/${escapeHTML(w.doi)}" target="_blank" rel="noopener">doi.org/${escapeHTML(w.doi)}</a>` : ""}
    </div>
  </li>`;
}

// ---- Contributors ----

function renderContributors() {
  const input = el("contrib-search");
  input.addEventListener("input", debounce(() => {
    const q = input.value.trim().toLowerCase();
    state.contribFiltered = q
      ? state.contributors.filter((c) => c.name.toLowerCase().includes(q))
      : state.contributors;
    state.contribShown = CONTRIB_PAGE_SIZE;
    drawContribs();
  }, 180));

  el("contrib-more").addEventListener("click", () => {
    state.contribShown += CONTRIB_PAGE_SIZE; drawContribs();
  });

  state.contribFiltered = state.contributors;
  drawContribs();
}

function drawContribs() {
  const total = state.contribFiltered.length;
  el("contrib-count").textContent = `${total.toLocaleString()} contributor${total === 1 ? "" : "s"}`;
  const slice = state.contribFiltered.slice(0, state.contribShown);
  el("contrib-grid").innerHTML = slice.map((c) => {
    const orcid = c.orcid
      ? `<a href="https://orcid.org/${c.orcid}" target="_blank" rel="noopener">ORCID</a>`
      : "";
    const oa = c.openalex_id
      ? `<a href="https://openalex.org/${c.openalex_id}" target="_blank" rel="noopener">OpenAlex</a>`
      : "";
    const dc = c.dominant_cluster
      ? ` data-cluster="${c.dominant_cluster}"` : "";
    return `<li class="contrib-card"${dc}>
      <div class="name">${escapeHTML(c.name)}</div>
      <div class="counts"><strong>${c.open_works}</strong> open · <strong>${c.total_works}</strong> total</div>
      <div class="links">${orcid}${orcid && oa ? " · " : ""}${oa}</div>
    </li>`;
  }).join("");

  const more = el("contrib-more");
  if (state.contribShown >= total) { more.disabled = true; more.textContent = "End of list"; }
  else { more.disabled = false; more.textContent = `Show more (${total - state.contribShown} remaining)`; }
}

// ---- Network (lazy-load when scrolled into view) ----

function setupNetworkLazyInit() {
  const wrap = el("network-wrap");
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) { io.disconnect(); initNetwork(); return; }
    }
  }, { rootMargin: "200px" });
  io.observe(wrap);
}

// Keep in sync with --cN-ink in style.css.
const CLUSTER_COLORS = {
  1: "#b13d28", 2: "#2b6185", 3: "#3b6d3d", 4: "#a6661e",
  5: "#1f6b67", 6: "#6d4e95", 7: "#a84370", 8: "#3b3e8a",
  9: "#516a2c", 10: "#8a5a44", 11: "#8a6d1c",
};
const CLUSTER_BGS = {
  1: "#fbe2da", 2: "#dceaf3", 3: "#dbe9dc", 4: "#fbe6d1",
  5: "#d4ebea", 6: "#e4dbf0", 7: "#f6dce6", 8: "#dbdcf1",
  9: "#e1e8d0", 10: "#efdfd4", 11: "#f0e5c6",
};

function initNetwork() {
  const draw = () => {
    if (!window.vis) { setTimeout(draw, 80); return; }
    const { nodes, edges } = state.network;
    if (!nodes.length) {
      el("network-wrap").innerHTML =
        '<p style="padding: 2rem; color: var(--ink-faint);">No co-authorship edges yet.</p>';
      return;
    }

    // Prune isolated edges under weight threshold to cut visual noise.
    // (Network layout has 157 nodes and up to 1500 edges — many are single
    // co-publications that add clutter without helping readers see structure.)
    const visibleEdges = edges.filter(e => e.weight >= 1);

    const maxW = Math.max(1, ...nodes.map((n) => n.open_works));
    const visNodes = new vis.DataSet(nodes.map((n) => {
      const ink = CLUSTER_COLORS[n.cluster] || "#4b5163";
      const bg  = CLUSTER_BGS[n.cluster]    || "#ece6d6";
      const cname = n.cluster && state.clusterById.get(n.cluster)?.name;
      return {
        id: n.id,
        label: n.label,
        value: n.open_works,
        title: `${n.label} · ${n.open_works} open works${cname ? ` · mostly ${cname}` : ""}`,
        color: { background: bg, border: ink, highlight: { background: bg, border: ink } },
        font: { size: 12 + Math.min(12, n.open_works / maxW * 12), color: "#0f172a",
                face: "ui-sans-serif, system-ui" },
      };
    }));
    const visEdges = new vis.DataSet(visibleEdges.map((e) => ({
      from: e.source, to: e.target, value: e.weight,
      color: { color: "rgba(15,23,42,0.14)", highlight: "#da5d45" },
      smooth: false,
    })));

    // Show a spinner overlay while physics stabilises. vis-network fires
    // stabilizationProgress / stabilizationIterationsDone events; we
    // freeze physics at done so the graph stops drifting.
    const wrap = el("network-wrap");
    const overlay = document.createElement("div");
    overlay.className = "network-loading";
    overlay.innerHTML = '<div>Laying out the graph… <span class="pct">0%</span></div>';
    wrap.appendChild(overlay);

    state.networkNodes = visNodes;
    state.networkNodeSpecs = nodes;
    const network = new vis.Network(wrap, { nodes: visNodes, edges: visEdges }, {
      autoResize: true,
      nodes: { shape: "dot", scaling: { min: 6, max: 34 }, borderWidth: 1.5 },
      edges: { scaling: { min: 0.5, max: 5 } },
      physics: {
        solver: "forceAtlas2Based",
        forceAtlas2Based: {
          gravitationalConstant: -80,
          centralGravity: 0.012,
          springLength: 90,
          springConstant: 0.05,
          damping: 0.5,
          avoidOverlap: 0.7,
        },
        maxVelocity: 40,
        minVelocity: 0.5,  // stops when avg node velocity drops below this
        timestep: 0.4,
        stabilization: {
          enabled: true,
          iterations: 1200,
          updateInterval: 25,
          fit: true,
          onlyDynamicEdges: false,
        },
      },
      interaction: { hover: true, tooltipDelay: 100, navigationButtons: false,
                     zoomView: true, dragView: true },
    });

    network.on("stabilizationProgress", (p) => {
      const pct = Math.round((p.iterations / p.total) * 100);
      overlay.querySelector(".pct").textContent = pct + "%";
    });
    network.on("stabilizationIterationsDone", () => {
      overlay.remove();
      // Freeze the layout once stabilised — physics running indefinitely is
      // what caused the "erratic bubbles" feeling.
      network.setOptions({ physics: { enabled: false } });
      network.fit({ animation: { duration: 300 } });
    });
    state.networkInstance = network;

    // Safety: if stabilisation event doesn't fire within 15s, disable anyway.
    setTimeout(() => {
      if (overlay.parentNode) {
        overlay.remove();
        network.setOptions({ physics: { enabled: false } });
      }
    }, 15000);

    // Render the legend once.
    const legend = el("network-legend");
    if (legend && !legend.childElementCount) {
      legend.innerHTML =
        '<span class="muted">Node colour = contributor\'s dominant cluster ·</span> ' +
        state.clusters.map((c) =>
          `<span class="legend-item"><span class="dot" style="background:${CLUSTER_COLORS[c.number]}"></span>${c.number}</span>`
        ).join(" ");
    }

    // Wire up the search box. Matches on substring of label, highlights hits
    // (accent colour + solid opacity) and dims the rest. Focus + select the
    // first match to pull it toward centre.
    const search = el("network-search");
    const countEl = el("network-search-count");
    if (search) {
      search.addEventListener("input", debounce(() => {
        const q = search.value.trim().toLowerCase();
        if (!q) {
          resetNetworkHighlight();
          countEl.textContent = "";
          return;
        }
        const hits = nodes.filter((n) => (n.label || "").toLowerCase().includes(q));
        highlightNetworkNodes(hits.map((n) => n.id));
        countEl.textContent = hits.length === 0
          ? "no match"
          : `${hits.length} match${hits.length === 1 ? "" : "es"}`;
        if (hits.length) {
          network.focus(hits[0].id, { scale: 1.1, animation: { duration: 500 } });
          network.selectNodes(hits.map((n) => n.id));
        } else {
          network.unselectAll();
        }
      }, 180));
    }
  };

  function resetNetworkHighlight() {
    if (!state.networkNodes) return;
    const updates = state.networkNodeSpecs.map((n) => ({
      id: n.id,
      color: {
        background: CLUSTER_BGS[n.cluster] || "#ece6d6",
        border: CLUSTER_COLORS[n.cluster] || "#4b5163",
      },
      borderWidth: 1.5,
      opacity: 1,
    }));
    state.networkNodes.update(updates);
    state.networkInstance?.unselectAll();
    state.networkInstance?.fit({ animation: { duration: 400 } });
  }

  function highlightNetworkNodes(ids) {
    if (!state.networkNodes) return;
    const hitSet = new Set(ids);
    const updates = state.networkNodeSpecs.map((n) => {
      const hit = hitSet.has(n.id);
      return {
        id: n.id,
        color: hit
          ? { background: "#fef3c7", border: "#da5d45" }
          : { background: "#f0ede6", border: "#c9c3b5" },
        borderWidth: hit ? 3 : 1,
        opacity: hit ? 1 : 0.35,
      };
    });
    state.networkNodes.update(updates);
  }

  draw();
}

boot().catch((e) => {
  console.error(e);
  document.body.insertAdjacentHTML("afterbegin",
    `<div style="background:#fee2e2;color:#991b1b;padding:.6rem 1rem">Failed to load bibliography data: ${escapeHTML(e.message)}</div>`);
});
