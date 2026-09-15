(() => {
  "use strict";

  const defaults = window.TRASH_TRACK_DEFAULTS;
  const elements = {
    form: document.querySelector("#search-form"),
    district: document.querySelector("#district"),
    street: document.querySelector("#street"),
    routeCount: document.querySelector("#route-count"),
    truckCount: document.querySelector("#truck-count"),
    updatedAt: document.querySelector("#updated-at"),
    notice: document.querySelector("#notice"),
    routeList: document.querySelector("#route-list"),
    truckList: document.querySelector("#truck-list"),
  };

  const state = {
    map: null,
    stopLayer: null,
    truckLayer: null,
    routes: [],
    query: { ...defaults },
    refreshTimer: null,
    requestSerial: 0,
  };

  function escapeHtml(value) {
    const node = document.createElement("div");
    node.textContent = value ?? "";
    return node.innerHTML;
  }

  function initMap() {
    if (state.map || typeof L === "undefined") return;
    state.map = L.map("map", { zoomControl: false }).setView([25.0275, 121.4255], 14);
    L.control.zoom({ position: "bottomright" }).addTo(state.map);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(state.map);
    state.stopLayer = L.layerGroup().addTo(state.map);
    state.truckLayer = L.layerGroup().addTo(state.map);
  }

  function searchParams() {
    return new URLSearchParams(state.query).toString();
  }

  async function fetchJson(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  function setNotice(kind, title, detail) {
    const icons = { loading: "◌", ok: "✓", warn: "!", error: "×" };
    elements.notice.className = `notice ${kind}`;
    elements.notice.innerHTML = `<span class="notice-icon" aria-hidden="true">${icons[kind]}</span><div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></div>`;
  }

  function formatDateTime(value) {
    if (!value) return "未提供";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat("zh-TW", {
      timeZone: "Asia/Taipei", month: "numeric", day: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(date);
  }

  function formatDistance(meters) {
    if (meters == null) return "無法計算";
    return meters < 1000 ? `約 ${Math.round(meters)} 公尺` : `約 ${(meters / 1000).toFixed(1)} 公里`;
  }

  function renderRoutes(data) {
    state.routes = data.routes;
    elements.routeCount.textContent = data.routeCount;
    elements.routeList.innerHTML = data.routes.length ? data.routes.map((route, index) => {
      const services = [
        ["garbage", "一般垃圾"], ["recycling", "資源回收"], ["foodScraps", "廚餘"],
      ].map(([key, label]) => `<span class="chip ${route.today[key] ? "on" : ""}">今日${label}：${route.today[key] ? "有" : "無"}</span>`).join("");
      const stops = route.stops.map(stop => `<li><span class="stop-rank">${stop.rank}</span><span>${escapeHtml(stop.name)}</span><time class="stop-time">${escapeHtml(stop.scheduledTime || "—")}</time></li>`).join("");
      return `<details class="route-item" ${index === 0 ? "open" : ""}><summary class="route-summary"><div><h3>${escapeHtml(route.linename || "未命名路線")}</h3><span class="route-id">${escapeHtml(route.lineid)} · 全線 ${route.totalRouteStops} 站</span><div class="service-chips">${services}</div></div></summary><ol class="stop-list">${stops}</ol></details>`;
    }).join("") : `<div class="empty-state"><strong>找不到相關路線</strong><span>請確認道路名稱，或改用不含「新北市」的道路名稱搜尋。</span></div>`;
    renderStopMarkers(data.routes);
  }

  function renderStopMarkers(routes) {
    if (!state.map) return;
    state.stopLayer.clearLayers();
    const bounds = [];
    routes.forEach(route => route.stops.forEach(stop => {
      if (stop.latitude == null || stop.longitude == null) return;
      bounds.push([stop.latitude, stop.longitude]);
      const icon = L.divIcon({ className: "stop-marker", html: String(stop.rank) });
      L.marker([stop.latitude, stop.longitude], { icon })
        .bindPopup(`<div class="popup-title">🚮 ${escapeHtml(stop.name)}</div><div class="popup-meta">路線：${escapeHtml(route.linename)}<br>順序：${stop.rank}<br>表定時間：${escapeHtml(stop.scheduledTime || "未提供")}</div>`)
        .addTo(state.stopLayer);
    }));
    if (bounds.length) state.map.fitBounds(bounds, { padding: [34, 34], maxZoom: 16 });
  }

  function renderTrucks(data) {
    elements.truckCount.textContent = data.truckCount;
    elements.updatedAt.textContent = formatDateTime(data.updatedAt);
    elements.truckList.innerHTML = data.trucks.length ? data.trucks.map(truck => {
      const next = truck.estimatedNextStop;
      return `<article class="truck-card"><h3>🚛 ${escapeHtml(truck.linename || truck.lineid)}</h3><dl><dt>車牌</dt><dd>${escapeHtml(truck.car || "未提供")}</dd><dt>目前位置</dt><dd>${escapeHtml(truck.location || "未提供")}</dd><dt>GPS 更新</dt><dd>${escapeHtml(formatDateTime(truck.gpsTime))}</dd><dt>推估下一站</dt><dd>${next ? `${escapeHtml(next.name)}（${escapeHtml(next.scheduledTime || "時間未提供")}）` : "已接近路線末站或無法推估"}</dd><dt>最近清運點</dt><dd>${formatDistance(truck.nearestStopDistanceMeters)}</dd></dl>${truck.gpsDelayed ? '<span class="delay-tag">⚠ GPS 資料可能延遲</span>' : ""}</article>`;
    }).join("") : `<div class="empty-state"><strong>目前沒有相關垃圾車出勤</strong><span>可能尚未到清運時段，請參考右側表定時間。</span></div>`;

    if (data.stale) {
      setNotice("warn", "目前顯示最近一次可用資料", "官方 API 暫時無法更新，系統會持續嘗試。");
    } else if (data.truckCount > 0) {
      setNotice("ok", `目前有 ${data.truckCount} 台垃圾車正在相關路線執勤`, "位置會每 30 秒向本站更新一次。");
    } else {
      setNotice("ok", `目前沒有${state.query.street}相關垃圾車出勤`, "可能尚未到清運時段，請參考下方表定時間。");
    }
    renderTruckMarkers(data.trucks);
  }

  function renderTruckMarkers(trucks) {
    if (!state.map) return;
    state.truckLayer.clearLayers();
    trucks.forEach(truck => {
      const icon = L.divIcon({ className: "truck-marker", html: "🚛" });
      L.marker([truck.latitude, truck.longitude], { icon, zIndexOffset: 1000 })
        .bindPopup(`<div class="popup-title">🚛 垃圾車</div><div class="popup-meta">路線：${escapeHtml(truck.linename)}<br>車牌：${escapeHtml(truck.car || "未提供")}<br><br>GPS 時間：${escapeHtml(formatDateTime(truck.gpsTime))}<br>位置：${escapeHtml(truck.location || "未提供")}</div>`)
        .addTo(state.truckLayer);
    });
  }

  async function loadAll() {
    const serial = ++state.requestSerial;
    setNotice("loading", "正在取得清運資訊", "第一次載入路線資料可能需要幾秒鐘。");
    try {
      const [routes, trucks] = await Promise.all([
        fetchJson(`/api/routes?${searchParams()}`),
        fetchJson(`/api/trucks?${searchParams()}`),
      ]);
      if (serial !== state.requestSerial) return;
      renderRoutes(routes);
      renderTrucks(trucks);
      const url = new URL(window.location.href);
      url.search = searchParams();
      history.replaceState(null, "", url);
    } catch (error) {
      if (serial !== state.requestSerial) return;
      setNotice("error", "目前無法取得清運資料", error.message);
      elements.updatedAt.textContent = "更新失敗";
    }
  }

  async function refreshTrucks() {
    try {
      const trucks = await fetchJson(`/api/trucks?${searchParams()}`);
      renderTrucks(trucks);
    } catch (error) {
      setNotice("error", "目前無法取得最新垃圾車資料", "請保留此頁，系統稍後會自動重試。");
    }
  }

  elements.form.addEventListener("submit", event => {
    event.preventDefault();
    state.query = { district: elements.district.value.trim(), street: elements.street.value.trim() };
    loadAll();
  });

  const initial = new URLSearchParams(window.location.search);
  state.query = {
    district: initial.get("district")?.trim() || defaults.district,
    street: initial.get("street")?.trim() || defaults.street,
  };
  elements.district.value = state.query.district;
  elements.street.value = state.query.street;
  initMap();
  loadAll();
  state.refreshTimer = window.setInterval(refreshTrucks, 30_000);
})();
