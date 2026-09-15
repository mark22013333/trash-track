(() => {
  "use strict";

  const config = window.TRASH_TRACK_CONFIG;
  const $ = selector => document.querySelector(selector);
  const elements = {
    addressForm: $("#address-form"), addressInput: $("#address-input"), placesField: $("#places-field"),
    addressHelp: $("#address-help"), manualPanel: $("#manual-panel"), county: $("#county"),
    district: $("#district"), road: $("#road"), lane: $("#lane"), roadOptions: $("#road-options"),
    laneOptions: $("#lane-options"), optionStatus: $("#option-status"), manualSearch: $("#manual-search"),
    remember: $("#remember-location"), answerSection: $("#answer-section"), answerCard: $("#answer-card"),
    answerKicker: $("#answer-kicker"), answerTitle: $("#answer-title"), answerDetail: $("#answer-detail"),
    nextCollection: $("#next-collection"), nextTime: $("#next-time"), nextPlace: $("#next-place"),
    resultMeta: $("#result-meta"), queryLocation: $("#query-location"), dataCapability: $("#data-capability"),
    updatedAt: $("#updated-at"), sourceHandoff: $("#source-handoff"), officialLink: $("#official-link"),
    nearbySection: $("#nearby-section"), stopList: $("#stop-list"), matchNote: $("#match-note"),
    map: $("#map"), mapMessage: $("#map-message"), routeForm: $("#route-form"),
    routeDistrict: $("#route-district"), routeQuery: $("#route-query"), advancedResult: $("#advanced-result"),
    mapModeButtons: [...document.querySelectorAll("[data-map-type]")],
    streetViewButton: $("#street-view-button"), streetViewStatus: $("#street-view-status"),
    livePanel: $("#live-panel"), liveLegend: $("#live-legend"), liveStatus: $("#live-status"),
    liveClose: $("#live-close"), liveVehicle: $("#live-vehicle"), liveDate: $("#live-date"),
    liveAge: $("#live-age"), liveNextStop: $("#live-next-stop"), liveNextDetail: $("#live-next-detail"),
    cameraPanel: $("#camera-panel"), cameraStatus: $("#camera-status"), cameraList: $("#camera-list"),
  };

  const state = {
    regions: [], selected: null, requestController: null, optionSerial: 0,
    map: null, AdvancedMarkerElement: null, infoWindow: null, markers: [],
    homeMarker: null, latestStops: [], latestCoordinates: null, selectedStopIndex: 0,
    streetViewService: null, streetViewRequestSerial: 0,
    placeAutocomplete: null,
    live: {
      district: null, lineid: null, date: null, data: null, selectedVehicleId: null,
      timer: null, controller: null, retryIndex: 0, requestSerial: 0,
      routeLine: null, trailLine: null, truckMarkers: [], nextMarker: null,
      cameraTarget: null, cameraFetchedAt: 0,
    },
  };

  function escapeHtml(value) {
    const node = document.createElement("div");
    node.textContent = value ?? "";
    return node.innerHTML;
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  function formatDateTime(value) {
    if (!value) return "未提供";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-TW", {
      timeZone: "Asia/Taipei", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(date);
  }

  function formatDistance(value) {
    if (value == null) return null;
    return value < 1000 ? `約 ${Math.round(value)} 公尺` : `約 ${(value / 1000).toFixed(1)} 公里`;
  }

  function distanceMeters(a, b) {
    const radius = 6371000;
    const toRadians = value => value * Math.PI / 180;
    const dLat = toRadians(b.latitude - a.latitude);
    const dLon = toRadians(b.longitude - a.longitude);
    const value = Math.sin(dLat / 2) ** 2 + Math.cos(toRadians(a.latitude)) * Math.cos(toRadians(b.latitude)) * Math.sin(dLon / 2) ** 2;
    return radius * 2 * Math.atan2(Math.sqrt(value), Math.sqrt(1 - value));
  }

  function normalized(value) {
    return String(value || "").trim().replaceAll("台", "臺").replace(/\s+/g, "").replace(/[－—]/g, "-");
  }

  function parseAddress(rawValue) {
    const value = normalized(rawValue);
    const county = state.regions.find(item => value.includes(item.name));
    const district = county?.districts.find(item => value.includes(item.name));
    let streetPart = value;
    if (county) streetPart = streetPart.split(county.name, 2).at(-1);
    if (district) streetPart = streetPart.split(district.name, 2).at(-1);
    streetPart = streetPart.replace(
      /^[\u3400-\u9fffA-Za-z0-9]+?[里村](?=[\u3400-\u9fffA-Za-z0-9]+(?:大道|路|街))/, "",
    );
    const roads = streetPart.match(/[\u3400-\u9fffA-Za-z0-9]+?(?:大道|路|街)(?:[一二三四五六七八九十百千0-9]+段)?/g);
    const lane = streetPart.match(/[一二三四五六七八九十百千0-9-]+巷(?:[一二三四五六七八九十百千0-9-]+弄)?/);
    return {
      county: county?.name || elements.county.value,
      district: district?.name || (county?.name === elements.county.value ? elements.district.value : ""),
      road: roads?.at(-1) || "",
      lane: lane?.[0] || "",
    };
  }

  function setAnswer(kind, title, detail, kicker = "TRASHTRACK") {
    elements.answerCard.className = `answer-card ${kind}`;
    elements.answerKicker.textContent = kicker;
    elements.answerTitle.textContent = title;
    elements.answerDetail.textContent = detail;
  }

  function showFormError(message) {
    setAnswer("error", "地址還差一點點", message, "CHECK THE ADDRESS");
    elements.nextCollection.hidden = true;
    elements.resultMeta.hidden = true;
    elements.nearbySection.hidden = true;
    elements.sourceHandoff.hidden = true;
    elements.answerSection.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function populateCounties() {
    elements.county.innerHTML = state.regions.map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join("");
  }

  function populateDistricts(preferred) {
    const county = state.regions.find(item => item.name === elements.county.value);
    elements.district.innerHTML = county?.districts.map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join("") || "";
    const wanted = preferred || config.defaultDistrict;
    if ([...elements.district.options].some(option => option.value === wanted)) elements.district.value = wanted;
  }

  function setDatalist(element, values) {
    element.innerHTML = values.map(value => `<option value="${escapeHtml(value)}"></option>`).join("");
  }

  async function loadRoads(preferredRoad = "") {
    const serial = ++state.optionSerial;
    const county = elements.county.value;
    const district = elements.district.value;
    elements.road.disabled = true;
    elements.lane.disabled = true;
    elements.optionStatus.textContent = `正在載入${county}${district}道路…`;
    try {
      const query = new URLSearchParams({ county, district });
      const data = await fetchJson(`/api/location-options?${query}`);
      if (serial !== state.optionSerial) return;
      setDatalist(elements.roadOptions, data.options);
      elements.road.disabled = false;
      elements.lane.disabled = false;
      if (preferredRoad) elements.road.value = preferredRoad;
      elements.optionStatus.textContent = data.options.length
        ? `已載入 ${data.options.length} 條官方道路，可直接輸入篩選。`
        : "官方道路清單目前沒有結果，仍可直接輸入道路名稱。";
      if (elements.road.value) await loadLanes();
    } catch (error) {
      if (serial !== state.optionSerial) return;
      elements.road.disabled = false;
      elements.lane.disabled = false;
      setDatalist(elements.roadOptions, []);
      elements.optionStatus.textContent = `${error.message} 仍可直接輸入道路名稱。`;
    }
  }

  async function loadLanes(preferredLane = "") {
    const road = normalized(elements.road.value);
    if (!road) {
      setDatalist(elements.laneOptions, []);
      elements.optionStatus.textContent = "選擇或輸入道路後，會列出有清運紀錄的巷弄。";
      return;
    }
    const serial = ++state.optionSerial;
    try {
      const query = new URLSearchParams({ county: elements.county.value, district: elements.district.value, road });
      const data = await fetchJson(`/api/location-options?${query}`);
      if (serial !== state.optionSerial) return;
      setDatalist(elements.laneOptions, data.options);
      if (preferredLane) elements.lane.value = preferredLane;
      elements.optionStatus.textContent = data.laneSupported
        ? `找到 ${data.options.length} 個有清運紀錄的巷弄；留白代表查整條路。`
        : (data.message || "此地區可直接輸入巷弄，查詢時會標示官方資料能力。 ");
    } catch (error) {
      if (serial === state.optionSerial) elements.optionStatus.textContent = `${error.message} 可留白查整條路。`;
    }
  }

  function syncManualFields(location) {
    const county = state.regions.find(item => item.name === normalized(location.county));
    if (!county) return false;
    elements.county.value = county.name;
    populateDistricts(location.district);
    if (![...elements.district.options].some(option => option.value === normalized(location.district))) return false;
    elements.district.value = normalized(location.district);
    elements.road.value = normalized(location.road);
    elements.lane.value = normalized(location.lane);
    return true;
  }

  async function performSearch(location, coordinates = null) {
    if (!syncManualFields(location) || !normalized(location.road)) {
      elements.manualPanel.open = true;
      showFormError("請至少確認縣市、行政區和道路；也可以展開手動選擇。 ");
      return;
    }
    closeLiveTracking();
    state.requestController?.abort();
    const controller = new AbortController();
    state.requestController = controller;
    state.latestCoordinates = coordinates;
    setAnswer("loading", "正在比對今晚的清運資料", "先找同巷弄，再找同一路段與附近停靠點。", "CHECKING OFFICIAL DATA");
    elements.nextCollection.hidden = true;
    elements.resultMeta.hidden = true;
    elements.sourceHandoff.hidden = true;
    elements.nearbySection.hidden = true;
    elements.answerSection.scrollIntoView({ behavior: "smooth", block: "center" });
    const body = {
      county: elements.county.value,
      district: elements.district.value,
      road: normalized(elements.road.value),
      lane: normalized(elements.lane.value) || undefined,
      latitude: coordinates?.latitude,
      longitude: coordinates?.longitude,
      limit: config.resultLimit,
    };
    try {
      const data = await fetchJson("/api/nearby", { method: "POST", body: JSON.stringify(body), signal: controller.signal });
      renderResult(data);
      if (elements.remember.checked) {
        localStorage.setItem("trashTrackLocation", JSON.stringify({
          county: body.county, district: body.district, road: body.road, lane: body.lane || "",
        }));
      } else {
        localStorage.removeItem("trashTrackLocation");
      }
    } catch (error) {
      if (error.name !== "AbortError") showFormError(error.message);
    }
  }

  function renderResult(data) {
    const statusKinds = { today: "today", notToday: "not-today", noMatch: "no-match", sourceUnavailable: "unavailable" };
    const kickers = { today: "YES · TODAY", notToday: "NOT SCHEDULED TODAY", noMatch: "NO RELIABLE MATCH", sourceUnavailable: "SOURCE HANDOFF" };
    setAnswer(statusKinds[data.answer.status] || "idle", data.answer.title, data.answer.detail, kickers[data.answer.status] || "RESULT");
    const next = data.answer.next;
    elements.nextCollection.hidden = !next;
    if (next) {
      elements.nextTime.textContent = `${next.weekday} ${next.scheduledTime}`;
      elements.nextPlace.textContent = next.stopName;
    }
    elements.resultMeta.hidden = false;
    const query = data.query;
    elements.queryLocation.textContent = `${query.county}${query.district}${query.road}${query.lane || ""}`;
    elements.dataCapability.textContent = data.capabilities.liveGps ? "表定時刻＋即時 GPS" : data.capabilities.schedule ? "官方表定時刻" : "地址定位完成／時刻待介接";
    elements.updatedAt.textContent = `${formatDateTime(data.updatedAt)}${data.stale ? "（最近可用資料）" : ""}`;
    elements.sourceHandoff.hidden = data.answer.status !== "sourceUnavailable" && data.answer.status !== "noMatch";
    elements.officialLink.href = data.officialUrl;
    renderStops(data.stops);
    state.latestStops = data.stops;
    renderMap();
  }

  function renderStops(stops) {
    elements.nearbySection.hidden = !stops.length;
    elements.matchNote.textContent = stops.some(stop => stop.distanceMeters != null)
      ? `先找 ${config.defaultRadius} 公尺，無結果再擴大到 ${config.maxRadius} 公尺`
      : "此結果依同巷弄與同一路段排序，未顯示推測距離";
    elements.stopList.innerHTML = stops.map((stop, index) => {
      const distance = formatDistance(stop.distanceMeters);
      const status = stop.today ? (stop.timePassed ? "今日表定時間可能已過" : "今天會來") : `清運日：${stop.serviceDays.join("、") || "未提供"}`;
      return `<article class="stop-card" data-stop-index="${index}" tabindex="0">
        <div class="stop-number">${String(index + 1).padStart(2, "0")}</div>
        <div class="stop-main"><p>${escapeHtml(stop.matchQuality === "exactLane" ? "同巷弄" : "同一路段")}${distance ? ` · ${escapeHtml(distance)}` : ""}</p><h3>${escapeHtml(stop.name)}</h3><span>${escapeHtml(stop.linename || stop.lineid)}</span></div>
        <div class="stop-side"><div class="stop-time"><strong>${escapeHtml(stop.scheduledTime || "—")}</strong><span>${escapeHtml(status)}</span></div>${stop.latitude != null ? '<div class="stop-actions"><button class="street-stop-action" type="button" data-action="street-view">查看街景</button><button class="track-stop-action" type="button" data-action="live-track">追蹤這條路線</button></div>' : ""}</div>
      </article>`;
    }).join("");
  }

  function showMapMessage(title, detail, kind = "") {
    elements.mapMessage.hidden = false;
    elements.mapMessage.className = `map-message ${kind}`;
    elements.mapMessage.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span>`;
  }

  function markerNode(kind, label) {
    const node = document.createElement("div");
    node.className = `${kind}-marker`;
    node.textContent = label;
    return node;
  }

  async function ensureMap() {
    if (state.map) return state.map;
    if (!config.mapsApiKeyConfigured || !config.mapsMapIdConfigured) {
      showMapMessage("地圖尚未設定", "請在 .env 設定 GOOGLE_MAPS_API_KEY 與 GOOGLE_MAPS_MAP_ID；地址選單和時刻查詢仍可使用。", "warn");
      return null;
    }
    if (!window.google?.maps?.importLibrary) {
      showMapMessage("Google Maps 載入失敗", "請檢查金鑰限制、Maps JavaScript API 與 billing。", "error");
      return null;
    }
    try {
      const [{ Map }, { AdvancedMarkerElement }] = await Promise.all([
        google.maps.importLibrary("maps"), google.maps.importLibrary("marker"),
      ]);
      state.AdvancedMarkerElement = AdvancedMarkerElement;
      state.map = new Map(elements.map, {
        center: { lat: 23.7, lng: 121 }, zoom: 7, mapId: config.mapsMapId,
        mapTypeId: localStorage.getItem("trashTrackMapType") || "roadmap",
        streetViewControl: true, mapTypeControl: false, fullscreenControl: true,
      });
      state.infoWindow = new google.maps.InfoWindow();
      state.streetViewService = new google.maps.StreetViewService();
      state.map.getStreetView().addListener("visible_changed", () => {
        const visible = state.map.getStreetView().getVisible();
        elements.streetViewButton.setAttribute("aria-pressed", String(visible));
        elements.streetViewButton.textContent = visible ? "離開街景" : "街景";
        if (!visible) elements.streetViewStatus.textContent = "";
      });
      syncMapModeButtons(state.map.getMapTypeId());
      return state.map;
    } catch (error) {
      showMapMessage("Google Maps 無法顯示", "請確認 API 金鑰、Map ID、來源網域與 billing 設定。", "error");
      return null;
    }
  }

  async function renderMap() {
    const positionedStops = state.latestStops.filter(stop => stop.latitude != null && stop.longitude != null);
    elements.streetViewButton.disabled = !positionedStops.length && !state.latestCoordinates;
    if (!positionedStops.length && !state.latestCoordinates) {
      showMapMessage("這批官方資料沒有可靠座標", "我們保留站名與表定時間，但不會拿猜測的位置畫地圖。", "warn");
      return;
    }
    const map = await ensureMap();
    if (!map) return;
    elements.mapMessage.hidden = true;
    state.markers.forEach(marker => { marker.map = null; });
    state.markers = [];
    if (state.homeMarker) state.homeMarker.map = null;
    const bounds = new google.maps.LatLngBounds();
    if (state.latestCoordinates) {
      const position = { lat: state.latestCoordinates.latitude, lng: state.latestCoordinates.longitude };
      state.homeMarker = new state.AdvancedMarkerElement({ map, position, title: "你的查詢位置", content: markerNode("home", "家") });
      bounds.extend(position);
    }
    positionedStops.forEach((stop, index) => {
      const position = { lat: stop.latitude, lng: stop.longitude };
      const marker = new state.AdvancedMarkerElement({ map, position, title: stop.name, content: markerNode("stop", String(index + 1)) });
      marker.addListener("click", () => {
        state.infoWindow.setContent(`<div class="popup"><strong>${escapeHtml(stop.name)}</strong><span>${escapeHtml(stop.linename)} · ${escapeHtml(stop.scheduledTime || "時間未提供")}</span></div>`);
        state.infoWindow.open({ map, anchor: marker });
      });
      state.markers.push(marker);
      bounds.extend(position);
    });
    if (!bounds.isEmpty()) map.fitBounds(bounds, 72);
  }

  function syncMapModeButtons(activeType) {
    elements.mapModeButtons.forEach(button => button.setAttribute("aria-pressed", String(button.dataset.mapType === activeType)));
  }

  async function setMapType(type) {
    const map = await ensureMap();
    if (!map) return;
    map.setMapTypeId(type);
    localStorage.setItem("trashTrackMapType", type);
    syncMapModeButtons(type);
  }

  async function openStreetView(stop = null, { toggle = false } = {}) {
    const map = await ensureMap();
    if (!map) return;
    const panorama = map.getStreetView();
    if (toggle && panorama.getVisible()) {
      state.streetViewRequestSerial += 1;
      panorama.setVisible(false);
      return;
    }
    const target = stop?.latitude != null
      ? { lat: stop.latitude, lng: stop.longitude }
      : state.latestCoordinates
        ? { lat: state.latestCoordinates.latitude, lng: state.latestCoordinates.longitude }
        : null;
    if (!target) {
      elements.streetViewStatus.textContent = "目前沒有可用來尋找街景的可靠座標。";
      return;
    }
    const requestSerial = ++state.streetViewRequestSerial;
    elements.streetViewButton.disabled = true;
    elements.streetViewStatus.textContent = "正在尋找距離這個清運點最近的街景…";
    state.streetViewService.getPanorama(
      { location: target, radius: 100, source: google.maps.StreetViewSource.OUTDOOR },
      (data, status) => {
        if (requestSerial !== state.streetViewRequestSerial) return;
        elements.streetViewButton.disabled = false;
        if (status !== google.maps.StreetViewStatus.OK || !data?.location?.latLng) {
          elements.streetViewStatus.textContent = "這個清運點 100 公尺內沒有可用的 Google 街景。";
          return;
        }
        panorama.setPosition(data.location.latLng);
        panorama.setPov({ heading: 0, pitch: 0 });
        panorama.setVisible(true);
        elements.streetViewStatus.textContent = stop ? `正在顯示「${stop.name}」附近街景。` : "正在顯示查詢位置附近街景。";
        elements.map.scrollIntoView({ behavior: "smooth", block: "center" });
      },
    );
  }

  function clearLiveLayers() {
    if (state.live.routeLine) state.live.routeLine.setMap(null);
    if (state.live.trailLine) state.live.trailLine.setMap(null);
    state.live.truckMarkers.forEach(marker => { marker.map = null; });
    if (state.live.nextMarker) state.live.nextMarker.map = null;
    state.live.routeLine = null;
    state.live.trailLine = null;
    state.live.truckMarkers = [];
    state.live.nextMarker = null;
  }

  function closeLiveTracking() {
    window.clearTimeout(state.live.timer);
    state.live.controller?.abort();
    state.live.requestSerial += 1;
    clearLiveLayers();
    Object.assign(state.live, {
      district: null, lineid: null, date: null, data: null, selectedVehicleId: null,
      timer: null, controller: null, retryIndex: 0, cameraTarget: null, cameraFetchedAt: 0,
    });
    if (elements.livePanel) elements.livePanel.hidden = true;
    if (elements.liveLegend) elements.liveLegend.hidden = true;
    if (elements.cameraPanel) elements.cameraPanel.hidden = true;
  }

  function selectedLiveVehicle() {
    return state.live.data?.vehicles.find(vehicle => vehicle.vehicleId === state.live.selectedVehicleId) || null;
  }

  function chooseVehicle(vehicles) {
    const retained = vehicles.find(vehicle => vehicle.vehicleId === state.live.selectedVehicleId);
    if (retained) return retained;
    const origin = state.latestCoordinates;
    const viewingToday = state.live.data?.date === state.live.data?.availableDates?.[0];
    const ranked = [...vehicles].sort((a, b) => {
      if (!viewingToday && Boolean(a.trail?.length) !== Boolean(b.trail?.length)) return a.trail?.length ? -1 : 1;
      const aFresh = a.current && !a.current.gpsDelayed ? 0 : 1;
      const bFresh = b.current && !b.current.gpsDelayed ? 0 : 1;
      if (aFresh !== bFresh) return aFresh - bFresh;
      if (!origin) return (a.current?.ageSeconds ?? Infinity) - (b.current?.ageSeconds ?? Infinity);
      const aDistance = a.current ? distanceMeters(origin, a.current) : Infinity;
      const bDistance = b.current ? distanceMeters(origin, b.current) : Infinity;
      return aDistance - bDistance;
    });
    return ranked[0] || null;
  }

  async function startLiveTracking(stop) {
    if (!stop.lineid) return;
    window.clearTimeout(state.live.timer);
    state.live.controller?.abort();
    clearLiveLayers();
    Object.assign(state.live, {
      district: elements.district.value, lineid: stop.lineid, date: null, data: null,
      selectedVehicleId: null, retryIndex: 0, cameraTarget: null, cameraFetchedAt: 0,
    });
    elements.livePanel.hidden = false;
    elements.liveLegend.hidden = false;
    elements.cameraPanel.hidden = false;
    elements.liveStatus.textContent = `正在取得「${stop.linename || stop.lineid}」的官方 GPS…`;
    elements.liveVehicle.innerHTML = '<option>正在載入…</option>';
    elements.liveDate.innerHTML = '<option>今天</option>';
    elements.livePanel.scrollIntoView({ behavior: "smooth", block: "center" });
    await loadLiveRoute({ fitRoute: true });
  }

  function scheduleLiveRefresh(seconds) {
    window.clearTimeout(state.live.timer);
    if (!state.live.lineid || document.hidden || state.live.date !== state.live.data?.availableDates?.[0]) return;
    state.live.timer = window.setTimeout(() => loadLiveRoute(), seconds * 1000);
  }

  async function loadLiveRoute({ fitRoute = false } = {}) {
    if (!state.live.lineid || document.hidden) return;
    state.live.controller?.abort();
    const controller = new AbortController();
    const serial = ++state.live.requestSerial;
    state.live.controller = controller;
    const params = new URLSearchParams({ district: state.live.district, lineid: state.live.lineid });
    if (state.live.date) params.set("date", state.live.date);
    try {
      const data = await fetchJson(`/api/live-route?${params}`, { signal: controller.signal });
      if (serial !== state.live.requestSerial) return;
      state.live.data = data;
      state.live.date = data.date;
      const selected = chooseVehicle(data.vehicles);
      state.live.selectedVehicleId = selected?.vehicleId || null;
      state.live.retryIndex = 0;
      renderLivePanel();
      await renderLiveLayers(fitRoute);
      await refreshCameras();
      scheduleLiveRefresh(config.livePollSeconds || 30);
    } catch (error) {
      if (error.name === "AbortError" || serial !== state.live.requestSerial) return;
      const delays = [30, 60, 120, 300];
      const delay = delays[Math.min(state.live.retryIndex, delays.length - 1)];
      state.live.retryIndex += 1;
      elements.liveStatus.textContent = `更新失敗：${error.message}；保留最後位置，${delay} 秒後重試。`;
      scheduleLiveRefresh(delay);
    }
  }

  function renderLivePanel() {
    const data = state.live.data;
    if (!data) return;
    elements.liveDate.innerHTML = data.availableDates.map(value => `<option value="${escapeHtml(value)}"${value === data.date ? " selected" : ""}>${value === data.availableDates[0] ? `今天 · ${value}` : value}</option>`).join("");
    const selected = selectedLiveVehicle();
    elements.liveVehicle.innerHTML = data.vehicles.length
      ? data.vehicles.map(vehicle => `<option value="${escapeHtml(vehicle.vehicleId)}"${vehicle.vehicleId === state.live.selectedVehicleId ? " selected" : ""}>${escapeHtml(vehicle.displayName)}${vehicle.current ? " · 出勤中" : " · 歷史軌跡"}</option>`).join("")
      : '<option value="">目前沒有出勤或歷史軌跡</option>';
    const staleNote = data.status === "stale" ? "（最近有效資料）" : "";
    elements.liveStatus.textContent = data.message || `${data.route.linename || data.route.lineid} · ${data.activeVehicleCount} 台出勤中 ${staleNote}`;
    elements.liveAge.textContent = selected?.current
      ? `${selected.current.gpsDelayed ? "訊號延遲 · " : "GPS · "}${Math.max(0, Math.round(selected.current.ageSeconds / 60))} 分鐘前`
      : "目前未出勤";
    const prediction = selected?.prediction;
    if (prediction?.nextStop) {
      elements.liveNextStop.textContent = prediction.nextStop.name;
      const confidence = { high: "高", medium: "中", low: "低" }[prediction.confidence] || "低";
      const distance = formatDistance(prediction.nearestStopDistanceMeters);
      elements.liveNextDetail.textContent = `${prediction.nextStop.scheduledTime || "未提供表定時間"} · 可信度 ${confidence}${distance ? ` · 距最近站點${distance.replace("約 ", "")}` : ""}；${prediction.reason}`;
    } else {
      elements.liveNextStop.textContent = "暫時無法可靠推估";
      elements.liveNextDetail.textContent = prediction?.reason || "目前沒有足夠 GPS 資料，不提供猜測 ETA。";
    }
  }

  async function renderLiveLayers(fitRoute = false) {
    const map = await ensureMap();
    if (!map || !state.live.data) return;
    clearLiveLayers();
    const routePath = state.live.data.route.stops
      .filter(stop => stop.latitude != null && stop.longitude != null)
      .map(stop => ({ lat: stop.latitude, lng: stop.longitude }));
    state.live.routeLine = new google.maps.Polyline({
      map, path: routePath, strokeOpacity: 0, strokeColor: "#56756d",
      icons: [{ icon: { path: "M 0,-1 0,1", strokeOpacity: 1, scale: 3 }, offset: "0", repeat: "18px" }],
      zIndex: 2,
    });
    const selected = selectedLiveVehicle();
    const trailPath = (selected?.trail || []).map(point => ({ lat: point.latitude, lng: point.longitude }));
    state.live.trailLine = new google.maps.Polyline({ map, path: trailPath, strokeColor: "#f06d3d", strokeOpacity: .95, strokeWeight: 5, zIndex: 4 });
    for (const vehicle of state.live.data.vehicles.filter(item => item.current)) {
      const current = vehicle.current;
      const content = markerNode("truck", "♻");
      if (current.gpsDelayed) content.classList.add("delayed");
      const marker = new state.AdvancedMarkerElement({
        map, position: { lat: current.latitude, lng: current.longitude }, title: `${vehicle.displayName} · ${formatDateTime(current.gpsTime)}`, content,
      });
      marker.addListener("click", () => {
        state.live.selectedVehicleId = vehicle.vehicleId;
        renderLivePanel();
        renderLiveLayers();
        refreshCameras(true);
      });
      state.live.truckMarkers.push(marker);
    }
    if (selected?.prediction?.nextStop) {
      const stop = selected.prediction.nextStop;
      state.live.nextMarker = new state.AdvancedMarkerElement({ map, position: { lat: stop.latitude, lng: stop.longitude }, title: `推估下一站：${stop.name}`, content: markerNode("next", "NEXT") });
    }
    if (fitRoute && routePath.length) {
      const bounds = new google.maps.LatLngBounds();
      routePath.forEach(point => bounds.extend(point));
      map.fitBounds(bounds, 58);
    }
  }

  function validWebUrl(value) {
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) ? url.href : null;
    } catch { return null; }
  }

  function renderCameras(data) {
    elements.cameraStatus.textContent = data.message || (data.cameras.length ? `距車輛或下一站 ${config.cameraRadiusMeters} 公尺內，依距離顯示。` : "附近目前沒有可用的公開交通攝影機。 ");
    elements.cameraList.replaceChildren();
    for (const camera of data.cameras) {
      const card = document.createElement("article");
      card.className = "camera-card";
      const media = document.createElement("div");
      media.className = "camera-media";
      const mediaUrl = validWebUrl(camera.mediaUrl);
      const officialUrl = validWebUrl(camera.officialUrl);
      const fallback = () => {
        media.replaceChildren();
        const link = document.createElement("a");
        link.textContent = "影像無法直接載入，前往官方影像 ↗";
        link.href = officialUrl || "https://atis.ntpc.gov.tw/";
        link.target = "_blank";
        link.rel = "noopener";
        media.append(link);
      };
      if (camera.mediaType === "image" && mediaUrl) {
        const image = document.createElement("img");
        image.src = mediaUrl;
        image.alt = `${camera.roadName}交通路況影像`;
        image.loading = "lazy";
        image.addEventListener("error", fallback, { once: true });
        media.append(image);
      } else if (camera.mediaType === "video" && mediaUrl) {
        const video = document.createElement("video");
        video.src = mediaUrl;
        video.controls = true;
        video.muted = true;
        video.playsInline = true;
        video.preload = "metadata";
        video.addEventListener("error", fallback, { once: true });
        media.append(video);
      } else fallback();
      const body = document.createElement("div");
      body.className = "camera-card-body";
      const title = document.createElement("strong");
      title.textContent = camera.roadName || "未提供道路名稱";
      const detail = document.createElement("span");
      detail.textContent = `${formatDistance(camera.distanceMeters) || "距離未提供"} · ${camera.id}`;
      body.append(title, detail);
      card.append(media, body);
      elements.cameraList.append(card);
    }
  }

  async function refreshCameras(force = false) {
    const selected = selectedLiveVehicle();
    const target = selected?.current || selected?.prediction?.nextStop;
    if (!target) {
      renderCameras({ message: "目前沒有車輛或下一站座標可供尋找攝影機。", cameras: [] });
      return;
    }
    const nextTarget = { latitude: target.latitude, longitude: target.longitude };
    const elapsed = Date.now() - state.live.cameraFetchedAt;
    if (!force && state.live.cameraTarget && distanceMeters(state.live.cameraTarget, nextTarget) < 250 && elapsed < 120000) return;
    state.live.cameraTarget = nextTarget;
    state.live.cameraFetchedAt = Date.now();
    elements.cameraStatus.textContent = "正在尋找附近公開交通路況攝影機…";
    try {
      const params = new URLSearchParams({ latitude: nextTarget.latitude, longitude: nextTarget.longitude, radiusMeters: config.cameraRadiusMeters, limit: 3 });
      renderCameras(await fetchJson(`/api/cameras/nearby?${params}`));
    } catch (error) {
      renderCameras({ message: `交通影像暫時無法取得：${error.message}`, cameras: [] });
    }
  }

  async function initPlaces() {
    if (!config.placesEnabled || !config.mapsApiKeyConfigured || !window.google?.maps?.importLibrary) {
      elements.addressHelp.textContent = "Google 地址建議未啟用；可直接輸入完整地址，或使用手動選單。";
      return;
    }
    try {
      const { PlaceAutocompleteElement } = await google.maps.importLibrary("places");
      const autocomplete = new PlaceAutocompleteElement({ includedRegionCodes: ["tw"] });
      autocomplete.id = "place-autocomplete";
      autocomplete.setAttribute("aria-label", "搜尋台灣地址或地標");
      autocomplete.placeholder = "輸入台灣地址或附近地標";
      elements.placesField.append(autocomplete);
      state.placeAutocomplete = autocomplete;
      if (elements.addressInput.value) autocomplete.value = elements.addressInput.value;
      elements.addressInput.classList.add("fallback-hidden");
      autocomplete.addEventListener("gmp-select", async event => {
        const place = event.placePrediction.toPlace();
        await place.fetchFields({ fields: ["formattedAddress", "location", "addressComponents"] });
        const parsed = parseAddress(place.formattedAddress || "");
        for (const component of place.addressComponents || []) {
          const types = component.types || [];
          if (types.includes("administrative_area_level_1")) parsed.county = normalized(component.longText);
          if (types.includes("administrative_area_level_3") || types.includes("administrative_area_level_2")) {
            if (state.regions.some(item => item.districts.some(district => district.name === normalized(component.longText)))) parsed.district = normalized(component.longText);
          }
          if (types.includes("route")) {
            const routeParsed = parseAddress(component.longText);
            parsed.road = routeParsed.road || normalized(component.longText);
            parsed.lane = routeParsed.lane || parsed.lane;
          }
        }
        elements.addressInput.value = place.formattedAddress || autocomplete.value || "";
        const coordinates = place.location ? { latitude: place.location.lat(), longitude: place.location.lng() } : null;
        await performSearch(parsed, coordinates);
      });
      elements.addressHelp.textContent = "選取地址建議可取得精確距離；也可輸入道路後直接按查詢。";
    } catch (error) {
      elements.addressHelp.textContent = "地址建議載入失敗；仍可直接輸入完整地址或使用手動選單。";
    }
  }

  async function initRegions() {
    try {
      const data = await fetchJson("/api/regions");
      state.regions = data.counties;
      populateCounties();
      const saved = JSON.parse(localStorage.getItem("trashTrackLocation") || "null");
      const initial = saved || { county: config.defaultCounty, district: config.defaultDistrict, road: config.defaultStreet, lane: "" };
      elements.remember.checked = Boolean(saved);
      elements.county.value = initial.county;
      populateDistricts(initial.district);
      elements.road.value = initial.road;
      elements.lane.value = initial.lane || "";
      await loadRoads(initial.road);
      if (saved) {
        const savedAddress = `${saved.county}${saved.district}${saved.road}${saved.lane || ""}`;
        elements.addressInput.value = savedAddress;
        if (state.placeAutocomplete) state.placeAutocomplete.value = savedAddress;
        elements.addressHelp.textContent = "已載入這台裝置記住的地點；按查詢即可更新。";
      }
    } catch (error) {
      elements.optionStatus.textContent = `行政區載入失敗：${error.message}`;
      showFormError("目前無法準備全台行政區，請稍後重新整理。 ");
    }
  }

  async function renderAdvanced(event) {
    event.preventDefault();
    const district = elements.routeDistrict.value.trim();
    const query = elements.routeQuery.value.trim();
    if (!district || !query) {
      elements.advancedResult.textContent = "請輸入行政區與路線編號或道路。";
      return;
    }
    elements.advancedResult.innerHTML = '<span class="mini-loader"></span> 正在載入路線資料…';
    const isStreet = /[路街大道]/.test(query);
    try {
      const params = new URLSearchParams({ district, [isStreet ? "street" : "lineid"]: query });
      const data = await fetchJson(`/api/dashboard?${params}`);
      elements.advancedResult.innerHTML = data.routes.length
        ? data.routes.map(route => `<article><strong>${escapeHtml(route.linename || route.lineid)}</strong><span>${route.totalRouteStops} 站 · 路線 ${escapeHtml(route.lineid)}</span><p>${route.stops.slice(0, 5).map(stop => `${escapeHtml(stop.scheduledTime || "—")} ${escapeHtml(stop.name)}`).join("<br>")}</p></article>`).join("")
        : "找不到符合的路線，請確認行政區與輸入內容。";
    } catch (error) {
      elements.advancedResult.textContent = error.message;
    }
  }

  elements.addressForm.addEventListener("submit", event => {
    event.preventDefault();
    const visibleValue = state.placeAutocomplete
      ? String(state.placeAutocomplete.value || "")
      : elements.addressInput.value;
    elements.addressInput.value = visibleValue;
    performSearch(parseAddress(visibleValue));
  });
  elements.manualSearch.addEventListener("click", () => performSearch({
    county: elements.county.value, district: elements.district.value, road: elements.road.value, lane: elements.lane.value,
  }));
  elements.county.addEventListener("change", () => { populateDistricts(); elements.road.value = ""; elements.lane.value = ""; loadRoads(); });
  elements.district.addEventListener("change", () => { elements.road.value = ""; elements.lane.value = ""; loadRoads(); });
  elements.road.addEventListener("change", () => { elements.lane.value = ""; loadLanes(); });
  elements.routeForm.addEventListener("submit", renderAdvanced);
  elements.stopList.addEventListener("click", event => {
    const card = event.target.closest("[data-stop-index]");
    const stop = card && state.latestStops[Number(card.dataset.stopIndex)];
    if (!stop) return;
    state.selectedStopIndex = Number(card.dataset.stopIndex);
    if (event.target.closest('[data-action="live-track"]')) {
      startLiveTracking(stop);
      return;
    }
    if (event.target.closest('[data-action="street-view"]')) {
      openStreetView(stop);
      return;
    }
    if (stop?.latitude != null && state.map) {
      state.map.panTo({ lat: stop.latitude, lng: stop.longitude });
      state.map.setZoom(17);
      elements.map.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  });
  elements.stopList.addEventListener("keydown", event => {
    if (!event.target.closest("button") && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      event.target.closest("[data-stop-index]")?.click();
    }
  });
  elements.mapModeButtons.forEach(button => button.addEventListener("click", () => setMapType(button.dataset.mapType)));
  elements.streetViewButton.addEventListener("click", () => openStreetView(state.latestStops[state.selectedStopIndex], { toggle: true }));
  elements.liveClose.addEventListener("click", closeLiveTracking);
  elements.liveVehicle.addEventListener("change", () => {
    state.live.selectedVehicleId = elements.liveVehicle.value || null;
    renderLivePanel();
    renderLiveLayers();
    refreshCameras(true);
  });
  elements.liveDate.addEventListener("change", () => {
    state.live.date = elements.liveDate.value;
    state.live.selectedVehicleId = null;
    state.live.cameraTarget = null;
    loadLiveRoute();
  });
  document.addEventListener("visibilitychange", () => {
    if (!state.live.lineid) return;
    window.clearTimeout(state.live.timer);
    if (!document.hidden) loadLiveRoute();
  });
  window.gm_authFailure = () => showMapMessage("Google Maps 認證失敗", "請檢查 .env 金鑰、來源網域、API 限制與 billing。查詢功能仍可使用。", "error");

  initRegions();
  initPlaces();
})();
