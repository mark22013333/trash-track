(() => {
  "use strict";

  const config = window.TRASH_TRACK_CONFIG;
  const elements = {
    form: document.querySelector("#search-form"), district: document.querySelector("#district"),
    routeSearch: document.querySelector("#route-search"), routeToggle: document.querySelector("#route-toggle"),
    routeOptions: document.querySelector("#route-options"), routeHelp: document.querySelector("#route-help"),
    showDefault: document.querySelector("#show-default"), routeCount: document.querySelector("#route-count"),
    stopCount: document.querySelector("#stop-count"), truckCount: document.querySelector("#truck-count"),
    updatedAt: document.querySelector("#updated-at"), notice: document.querySelector("#notice"),
    routeList: document.querySelector("#route-list"), routeListTitle: document.querySelector("#route-list-title"),
    truckList: document.querySelector("#truck-list"), pageTitle: document.querySelector("#page-title"),
    pageIntro: document.querySelector("#page-intro"), map: document.querySelector("#map"),
    mapMessage: document.querySelector("#map-message"),
  };

  const initial = new URLSearchParams(window.location.search);
  const state = {
    query: {
      district: initial.get("district")?.trim() || config.defaultDistrict,
      lineid: initial.get("lineid")?.trim() || null,
      street: initial.get("lineid") ? null : (initial.get("street")?.trim() || config.defaultStreet),
    },
    routeOptionsCache: new Map(), options: [], filteredOptions: [], activeOption: -1,
    optionsLoading: false, optionsRequestSerial: 0,
    dashboardController: null, truckController: null, requestSerial: 0, refreshTimer: null,
    map: null, mapInitPromise: null, mapVisible: false, mapDataReady: false,
    latestStops: [], latestTrucks: [], stopMarkers: [], truckMarkers: new Map(), infoWindow: null,
    AdvancedMarkerElement: null,
  };

  function escapeHtml(value) {
    const node = document.createElement("div");
    node.textContent = value ?? "";
    return node.innerHTML;
  }

  async function fetchJson(url, signal) {
    const response = await fetch(url, { signal, headers: { Accept: "application/json" } });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  function setNotice(kind, title, detail) {
    const icons = { loading: "◌", ok: "✓", warn: "!", error: "×" };
    elements.notice.className = `notice ${kind}`;
    elements.notice.innerHTML = `<span class="notice-icon" aria-hidden="true">${icons[kind]}</span><div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></div>`;
  }

  function showMapMessage(title, detail, kind = "") {
    elements.mapMessage.hidden = false;
    elements.mapMessage.className = `map-message ${kind}`;
    elements.mapMessage.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span>`;
  }

  function formatDateTime(value) {
    if (!value) return "未提供";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-TW", {
      timeZone: "Asia/Taipei", month: "numeric", day: "numeric", hour: "2-digit",
      minute: "2-digit", second: "2-digit", hour12: false,
    }).format(date);
  }

  function formatDistance(meters) {
    if (meters == null) return "無法計算";
    return meters < 1000 ? `約 ${Math.round(meters)} 公尺` : `約 ${(meters / 1000).toFixed(1)} 公里`;
  }

  function queryString(includeStreet = true) {
    const params = new URLSearchParams({ district: state.query.district });
    if (state.query.lineid) params.set("lineid", state.query.lineid);
    else if (includeStreet && state.query.street) params.set("street", state.query.street);
    return params.toString();
  }

  function updateUrl() {
    const url = new URL(window.location.href);
    url.search = queryString();
    history.replaceState(null, "", url);
  }

  function optionText(route) {
    const time = route.firstTime && route.lastTime ? `${route.firstTime}–${route.lastTime}` : "時間未完整提供";
    return `${route.linename || "未命名路線"}｜${route.lineid}｜${route.stopCount} 站｜${time}`;
  }

  function setComboboxOpen(open) {
    elements.routeOptions.hidden = !open;
    elements.routeSearch.setAttribute("aria-expanded", String(open));
    if (!open) {
      state.activeOption = -1;
      elements.routeSearch.removeAttribute("aria-activedescendant");
    }
  }

  function renderRouteOptions() {
    if (state.optionsLoading) {
      elements.routeOptions.innerHTML = '<div class="option-message">路線載入中…</div>';
      elements.routeHelp.textContent = `正在載入${state.query.district}路線…`;
      return;
    }
    const rawQuery = elements.routeSearch.value.trim();
    const selected = state.options.find(route => route.lineid === state.query.lineid);
    const query = (selected && rawQuery === optionText(selected) ? "" : rawQuery).toLocaleLowerCase("zh-TW");
    state.filteredOptions = state.options.filter(route =>
      !query || route.linename.toLocaleLowerCase("zh-TW").includes(query) || route.lineid.toLocaleLowerCase("zh-TW").includes(query)
    );
    state.activeOption = -1;
    elements.routeOptions.innerHTML = state.filteredOptions.length
      ? state.filteredOptions.map((route, index) => {
          const time = route.firstTime && route.lastTime ? `${route.firstTime}–${route.lastTime}` : "表定時間未完整提供";
          return `<div id="route-option-${index}" class="route-option" role="option" aria-selected="false" data-index="${index}"><strong>${escapeHtml(route.linename || "未命名路線")}</strong><span>${escapeHtml(route.lineid)} · ${route.stopCount} 站 · ${escapeHtml(time)}</span></div>`;
        }).join("")
      : '<div class="option-message">沒有符合的路線，換個名稱或編號試試看。</div>';
    elements.routeHelp.textContent = state.filteredOptions.length ? `共 ${state.filteredOptions.length} 條符合路線` : "沒有搜尋結果";
  }

  function activateOption(index) {
    const options = elements.routeOptions.querySelectorAll('[role="option"]');
    if (!options.length) return;
    state.activeOption = Math.max(0, Math.min(index, options.length - 1));
    options.forEach((option, current) => option.setAttribute("aria-selected", String(current === state.activeOption)));
    const active = options[state.activeOption];
    elements.routeSearch.setAttribute("aria-activedescendant", active.id);
    active.scrollIntoView({ block: "nearest" });
  }

  function chooseRoute(route) {
    state.query = { district: elements.district.value.trim(), lineid: route.lineid, street: null };
    elements.routeSearch.value = optionText(route);
    elements.routeHelp.textContent = `已選擇 ${route.lineid}`;
    setComboboxOpen(false);
    loadDashboard();
  }

  async function loadRouteOptions(district) {
    const serial = ++state.optionsRequestSerial;
    state.optionsLoading = true;
    elements.routeHelp.textContent = `正在載入${district}路線…`;
    setComboboxOpen(false);
    try {
      let promise = state.routeOptionsCache.get(district);
      if (!promise) {
        promise = fetchJson(`/api/route-options?${new URLSearchParams({ district })}`);
        state.routeOptionsCache.set(district, promise);
      }
      const data = await promise;
      if (serial !== state.optionsRequestSerial || district !== elements.district.value.trim()) return;
      state.optionsLoading = false;
      state.options = data.routes;
      renderRouteOptions();
      const selected = state.query.lineid && state.options.find(route => route.lineid === state.query.lineid);
      if (selected) elements.routeSearch.value = optionText(selected);
      elements.routeHelp.textContent = data.stale
        ? `已載入 ${data.routeCount} 條路線（最近一次可用資料）`
        : `已載入 ${data.routeCount} 條路線，可輸入名稱或編號搜尋`;
    } catch (error) {
      if (serial !== state.optionsRequestSerial) return;
      state.optionsLoading = false;
      state.routeOptionsCache.delete(district);
      state.options = [];
      elements.routeHelp.textContent = `路線選項載入失敗：${error.message}`;
      elements.routeOptions.innerHTML = '<div class="option-message error">路線載入失敗，請稍後再試。</div>';
    }
  }

  function renderRoutes(data) {
    elements.routeCount.textContent = data.routeCount;
    elements.stopCount.textContent = data.physicalStops.length;
    elements.routeList.innerHTML = data.routes.length ? data.routes.map((route, index) => {
      const services = [["garbage", "一般垃圾"], ["recycling", "資源回收"], ["foodScraps", "廚餘"]]
        .map(([key, label]) => `<span class="chip ${route.today[key] ? "on" : ""}">今日${label}：${route.today[key] ? "有" : "無"}</span>`).join("");
      const stops = route.stops.map(stop => `<li><span class="stop-rank">${stop.rank}</span><span>${escapeHtml(stop.name)}</span><time class="stop-time">${escapeHtml(stop.scheduledTime || "—")}</time></li>`).join("");
      return `<details class="route-item" ${index === 0 ? "open" : ""}><summary class="route-summary"><div><h3>${escapeHtml(route.linename || "未命名路線")}</h3><span class="route-id">${escapeHtml(route.lineid)} · 全線 ${route.totalRouteStops} 站</span><div class="service-chips">${services}</div></div></summary><ol class="stop-list">${stops}</ol></details>`;
    }).join("") : '<div class="empty-state"><strong>找不到這條路線</strong><span>這個路線編號不屬於目前行政區，請從上方選單重新選擇。</span></div>';

    const selected = data.selectedRoute;
    if (selected) {
      elements.pageTitle.innerHTML = `${escapeHtml(data.district)}<br><em>${escapeHtml(selected.linename || selected.lineid)}</em>`;
      elements.pageIntro.textContent = `顯示路線 ${selected.lineid} 的完整 ${selected.totalRouteStops} 個表定站次與即時出勤資訊。`;
      elements.routeListTitle.textContent = `${selected.linename || selected.lineid}完整站點`;
      document.title = `${selected.linename || selected.lineid}｜新北清運觀測站`;
    } else {
      elements.pageTitle.innerHTML = `${escapeHtml(data.district)}<br><em>${escapeHtml(data.street || "清運路線")}</em>`;
      elements.pageIntro.textContent = data.street ? `目前顯示「${data.street}」相關路線；也可以從右側搜尋並切換成任一完整路線。` : "請從右側搜尋並選擇清運路線。";
      elements.routeListTitle.textContent = data.street ? `${data.street}相關路線` : "路線與站點";
      document.title = `${data.district}${data.street || "垃圾車即時動態"}｜新北清運觀測站`;
    }
    state.latestStops = data.physicalStops;
    state.mapDataReady = true;
    maybeInitMap();
    if (state.map) renderStopMarkers();
  }

  function renderTrucks(data) {
    elements.truckCount.textContent = data.truckCount;
    elements.updatedAt.textContent = formatDateTime(data.updatedAt);
    elements.truckList.innerHTML = data.trucks.length ? data.trucks.map(truck => {
      const next = truck.estimatedNextStop;
      return `<article class="truck-card"><h3>🚛 ${escapeHtml(truck.linename || truck.lineid)}</h3><dl><dt>車牌</dt><dd>${escapeHtml(truck.car || "未提供")}</dd><dt>目前位置</dt><dd>${escapeHtml(truck.location || "未提供")}</dd><dt>GPS 更新</dt><dd>${escapeHtml(formatDateTime(truck.gpsTime))}</dd><dt>推估下一站</dt><dd>${next ? `${escapeHtml(next.name)}（${escapeHtml(next.scheduledTime || "時間未提供")}）` : "已接近路線末站或無法推估"}</dd><dt>最近清運點</dt><dd>${formatDistance(truck.nearestStopDistanceMeters)}</dd></dl>${truck.gpsDelayed ? '<span class="delay-tag">⚠ GPS 資料可能延遲</span>' : ""}</article>`;
    }).join("") : '<div class="empty-state"><strong>目前沒有相關垃圾車出勤</strong><span>可能尚未到清運時段，請參考表定時間。</span></div>';
    state.latestTrucks = data.trucks;
    if (data.stale) setNotice("warn", "目前顯示最近一次可用資料", "官方 API 暫時無法更新，系統會持續嘗試。");
    else if (data.truckCount) setNotice("ok", `目前有 ${data.truckCount} 台垃圾車執勤`, "位置會每 30 秒向本站更新一次。");
    else setNotice("ok", "目前沒有相關垃圾車出勤", "可能尚未到清運時段，請參考下方表定時間。");
    if (state.map) updateTruckMarkers();
  }

  function clearTimer() {
    if (state.refreshTimer) window.clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }

  function startTimer() {
    clearTimer();
    if (!document.hidden) state.refreshTimer = window.setInterval(refreshTrucks, 30_000);
  }

  async function loadDashboard() {
    clearTimer();
    state.dashboardController?.abort();
    state.truckController?.abort();
    const controller = new AbortController();
    state.dashboardController = controller;
    const serial = ++state.requestSerial;
    setNotice("loading", "正在取得清運資訊", "第一次載入路線資料可能需要幾秒鐘。");
    try {
      const data = await fetchJson(`/api/dashboard?${queryString()}`, controller.signal);
      if (serial !== state.requestSerial) return;
      renderRoutes(data);
      renderTrucks(data);
      updateUrl();
      startTimer();
    } catch (error) {
      if (error.name === "AbortError" || serial !== state.requestSerial) return;
      setNotice("error", "目前無法取得清運資料", error.message);
      elements.updatedAt.textContent = "更新失敗";
    }
  }

  async function refreshTrucks() {
    state.truckController?.abort();
    const controller = new AbortController();
    state.truckController = controller;
    const serial = state.requestSerial;
    try {
      const data = await fetchJson(`/api/trucks?${queryString()}`, controller.signal);
      if (serial === state.requestSerial) renderTrucks(data);
    } catch (error) {
      if (error.name !== "AbortError" && serial === state.requestSerial) {
        setNotice("error", "目前無法取得最新垃圾車資料", "請保留此頁，系統稍後會自動重試。");
      }
    }
  }

  function markerNode(kind, label) {
    const node = document.createElement("div");
    node.className = `${kind}-marker`;
    node.textContent = label;
    return node;
  }

  async function initMap() {
    if (state.map) return state.map;
    if (!config.mapsApiKeyConfigured || !config.mapsMapIdConfigured) {
      showMapMessage("尚未設定 Google Maps", "請設定 GOOGLE_MAPS_API_KEY 與 GOOGLE_MAPS_MAP_ID；路線與站點列表仍可正常使用。", "warn");
      return null;
    }
    if (!window.google?.maps?.importLibrary) {
      showMapMessage("Google Maps 載入失敗", "請檢查網路、API 金鑰、Maps JavaScript API 與 billing 設定。", "error");
      return null;
    }
    try {
      const [{ Map }, { AdvancedMarkerElement }] = await Promise.all([
        google.maps.importLibrary("maps"), google.maps.importLibrary("marker"),
      ]);
      if (state.map) return state.map;
      state.AdvancedMarkerElement = AdvancedMarkerElement;
      state.map = new Map(elements.map, {
        center: { lat: 25.0275, lng: 121.4255 }, zoom: 14, mapId: config.mapsMapId,
        streetViewControl: false, mapTypeControl: false, fullscreenControl: true,
      });
      state.infoWindow = new google.maps.InfoWindow();
      elements.mapMessage.hidden = true;
      renderStopMarkers();
      updateTruckMarkers();
      return state.map;
    } catch (error) {
      showMapMessage("Google Maps 無法顯示", "請確認 API 金鑰限制、billing 與 Map ID 設定。", "error");
      return null;
    }
  }

  function maybeInitMap() {
    if (state.mapDataReady && state.mapVisible && !state.mapInitPromise) state.mapInitPromise = initMap();
  }

  function renderStopMarkers() {
    if (!state.map) return;
    state.stopMarkers.forEach(marker => { marker.map = null; });
    state.stopMarkers = [];
    const bounds = new google.maps.LatLngBounds();
    state.latestStops.forEach((stop, index) => {
      const marker = new state.AdvancedMarkerElement({
        map: state.map, position: { lat: stop.latitude, lng: stop.longitude },
        title: stop.name, content: markerNode("stop", String(index + 1)),
      });
      marker.addListener("click", () => {
        const appearances = stop.appearances.map(item => `<li><strong>${escapeHtml(item.linename || item.lineid)}</strong><span>第 ${item.rank} 站 · ${escapeHtml(item.scheduledTime || "時間未提供")}</span></li>`).join("");
        state.infoWindow.setContent(`<div class="popup-title">${escapeHtml(stop.name)}</div><ul class="popup-routes">${appearances}</ul>`);
        state.infoWindow.open({ map: state.map, anchor: marker });
      });
      state.stopMarkers.push(marker);
      bounds.extend(marker.position);
    });
    if (!bounds.isEmpty()) state.map.fitBounds(bounds, 48);
  }

  function updateTruckMarkers() {
    if (!state.map) return;
    const activeKeys = new Set();
    state.latestTrucks.forEach(truck => {
      const key = `${truck.lineid}|${truck.car}`;
      activeKeys.add(key);
      let marker = state.truckMarkers.get(key);
      if (marker) {
        marker.position = { lat: truck.latitude, lng: truck.longitude };
        marker.trashTrackData = truck;
      } else {
        marker = new state.AdvancedMarkerElement({
          map: state.map, position: { lat: truck.latitude, lng: truck.longitude },
          title: `垃圾車 ${truck.car || truck.lineid}`, content: markerNode("truck", "🚛"), zIndex: 1000,
        });
        marker.trashTrackData = truck;
        marker.addListener("click", () => {
          const current = marker.trashTrackData;
          state.infoWindow.setContent(`<div class="popup-title">垃圾車 ${escapeHtml(current.car || "未提供")}</div><div class="popup-meta">路線：${escapeHtml(current.linename)}<br>GPS：${escapeHtml(formatDateTime(current.gpsTime))}<br>位置：${escapeHtml(current.location || "未提供")}</div>`);
          state.infoWindow.open({ map: state.map, anchor: marker });
        });
        state.truckMarkers.set(key, marker);
      }
    });
    state.truckMarkers.forEach((marker, key) => {
      if (!activeKeys.has(key)) { marker.map = null; state.truckMarkers.delete(key); }
    });
  }

  window.gm_authFailure = () => showMapMessage(
    "Google Maps 認證失敗", "API 金鑰可能無效、來源網域未授權，或尚未啟用 billing。路線列表不受影響。", "error"
  );

  elements.routeSearch.addEventListener("focus", () => { renderRouteOptions(); setComboboxOpen(true); });
  elements.routeSearch.addEventListener("input", () => { renderRouteOptions(); setComboboxOpen(true); });
  elements.routeSearch.addEventListener("keydown", event => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault(); setComboboxOpen(true);
      const nextIndex = state.activeOption < 0 && event.key === "ArrowUp"
        ? state.filteredOptions.length - 1
        : state.activeOption + (event.key === "ArrowDown" ? 1 : -1);
      activateOption(nextIndex);
    } else if (event.key === "Enter" && state.activeOption >= 0) {
      event.preventDefault(); chooseRoute(state.filteredOptions[state.activeOption]);
    } else if (event.key === "Escape") {
      event.preventDefault(); setComboboxOpen(false);
    }
  });
  elements.routeOptions.addEventListener("mousedown", event => {
    const option = event.target.closest('[role="option"]');
    if (option) { event.preventDefault(); chooseRoute(state.filteredOptions[Number(option.dataset.index)]); }
  });
  elements.routeToggle.addEventListener("click", () => {
    renderRouteOptions(); setComboboxOpen(elements.routeOptions.hidden); elements.routeSearch.focus();
  });
  elements.district.addEventListener("change", () => {
    const district = elements.district.value.trim();
    if (!district) return;
    state.query = { district, lineid: null, street: config.defaultStreet };
    elements.routeSearch.value = "";
    loadRouteOptions(district); loadDashboard();
  });
  elements.showDefault.addEventListener("click", () => {
    state.query = { district: elements.district.value.trim() || config.defaultDistrict, lineid: null, street: config.defaultStreet };
    elements.routeSearch.value = "";
    loadDashboard();
  });
  elements.form.addEventListener("submit", event => event.preventDefault());
  document.addEventListener("click", event => {
    if (!elements.form.contains(event.target)) setComboboxOpen(false);
  });
  document.addEventListener("visibilitychange", () => {
    clearTimer();
    if (!document.hidden) { refreshTrucks(); startTimer(); }
  });

  const observer = new IntersectionObserver(entries => {
    state.mapVisible = entries.some(entry => entry.isIntersecting);
    if (state.mapVisible) { maybeInitMap(); observer.disconnect(); }
  }, { rootMargin: "240px" });
  observer.observe(elements.map);

  elements.district.value = state.query.district;
  loadRouteOptions(state.query.district);
  loadDashboard();
})();
