# TrashTrack｜倒垃圾了嗎？

以住家地址為起點的台灣垃圾清運查詢工具。使用者不必先理解路線編號，只要輸入地址或依序選擇「縣市 → 鄉鎮市區 → 路／街 → 巷／弄」，就能先得到今天是否有清運，再查看附近停靠點與地圖。

## 目前資料能力

- 全台 22 縣市、368 鄉鎮市區：內建內政部國土測繪中心官方資料快照。
- 全台主要道路：按行政區向國土測繪中心官方 API 查詢並存入 SQLite 快取。
- 新北市：官方清運時刻、停靠點座標、即時 GPS、七日實際軌跡與保守的下一站推估。表定虛線只依站序連接，不代表垃圾車實際行駛道路；系統不提供資料無法支持的精確 ETA。
- 交通路況影像：設定 TDX 憑證後，監控模式會顯示車輛或下一站 1.5 公里內最多三支公開交通攝影機；未設定或來源失效時自動降級，不影響清運功能。
- 地圖可切換一般／衛星模式；有可靠座標時可從清運點開啟 100 公尺內的 Google 戶外街景，並保留原生小黃人控制。
- 其他縣市：可完成行政區與道路定位；在尚無穩定且授權明確的清運資料介面前，畫面會導向環境管理署全國查詢，絕不把「沒有資料」顯示成「沒有垃圾車」。

全國清運查詢站目前是 ASP.NET 查詢頁，而非公開穩定的 JSON API。新增縣市清運 provider 前，應先確認自動存取授權或使用地方政府公開 API。

## 本機啟動

需要 Python 3.9 以上版本。

```bash
cp .env.example .env
# 編輯 .env，填入 Google Maps 設定（選填）
./start.sh
```

Windows 10／11 可複製 `.env.example` 為 `.env` 後雙擊 `start.bat`。批次檔會尋找 Python Launcher（`py -3`）或 Python 3.9 以上版本、建立 `.venv`、安裝包含 Windows IANA 時區資料的相依套件，再同時啟動網站與 GPS 蒐集器。沒有 Google Maps 設定時，地址選單、表定時間與停靠點列表仍可使用。

手動啟動網站與常駐 GPS 蒐集器：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python service_runner.py
```

## 參數設定

設定集中於 `config.py`，本機值放在不納入版控的 `.env`。優先順序為：

1. 作業系統或部署平台環境變數
2. 本機 `.env`
3. `config.py` 的安全預設值

主要參數：

```dotenv
GOOGLE_MAPS_API_KEY=
GOOGLE_MAPS_MAP_ID=
GOOGLE_PLACES_ENABLED=true
GOOGLE_MAPS_LANGUAGE=zh-TW
GOOGLE_MAPS_REGION=TW

TDX_CLIENT_ID=
TDX_CLIENT_SECRET=

DEFAULT_COUNTY=新北市
DEFAULT_DISTRICT=新莊區
DEFAULT_STREET=西盛街
DEFAULT_SEARCH_RADIUS_METERS=600
MAX_SEARCH_RADIUS_METERS=1500
NEARBY_RESULT_LIMIT=5
TRACK_RETENTION_DAYS=7
TRACK_COLLECTION_SECONDS=120
LIVE_POLL_SECONDS=30
CCTV_CACHE_SECONDS=21600
CCTV_SEARCH_RADIUS_METERS=1500
DATABASE_PATH=data/trashtrack.sqlite3
```

完整參數請參考 `.env.example`。SQLite 保存官方公開資料快取及最近七日車輛位置，不保存使用者輸入地址。完整車牌只在寫入當下用資料庫內隨機 HMAC 金鑰建立匿名 ID；軌跡歷史不保存完整車牌。

## Google Maps 與 Places

同一組瀏覽器金鑰可供地圖與地址自動完成功能使用，Google Cloud 專案需啟用：

- Maps JavaScript API
- Places API（New）
- Billing

正式環境務必設定 HTTP referrer 網站限制，並將 API 限制縮到上述兩項服務。localhost 與正式網域建議使用不同金鑰。Map ID 是 Advanced Marker 必要設定；專案不會拿 `DEMO_MAP_ID` 冒充正式值。

瀏覽器金鑰必須傳給 Google Maps JavaScript API，因此會出現在前端；安全性依賴 referrer 與 API 限制。伺服器端祕密不得放入這組公開設定。`/api/status` 只回傳是否已設定，不回傳 Key 或 Map ID 內容。

## TDX 交通路況影像

至交通部 TDX 申請 Client ID／Client Secret 後填入 `.env`。這兩個值只由後端用來交換短效存取權杖，不會送至 HTML、JavaScript、狀態 API 或紀錄檔。系統快取新北攝影機目錄六小時，並在本機依直線距離排序。

影像網址不經 TrashTrack 代理或轉存。HTTPS 快照優先直接顯示；瀏覽器不能播放、跨來源受限、只有 HTTP 或來源失效時，介面會提供官方影像連結。這些是交通路況 CCTV，不是警方治安監視器，也不保證鏡頭剛好拍到垃圾車。

## 正式部署

正式環境應由 systemd、Supervisor、Docker Compose 或平台程序管理器分別監督下列兩個命令：

```bash
.venv/bin/python app.py
.venv/bin/python collector.py
```

`collector.py` 每 120 秒取得全市出勤車輛；SQLite 租約確保誤啟多份時只有一個程序呼叫官方 API。官方回傳空陣列代表非清運時段，視為正常成功。若以 Gunicorn 等 WSGI 伺服器取代 `app.py`，仍須保留獨立 collector 程序。

## API

新介面：

- `GET /api/regions`
- `GET /api/location-options?county=新北市&district=新莊區`
- `GET /api/location-options?county=新北市&district=新莊區&road=西盛街`
- `POST /api/nearby`
- `GET /api/live-route?district=新莊區&lineid=242051&date=YYYY-MM-DD`
- `GET /api/cameras/nearby?latitude=25.04&longitude=121.45&radiusMeters=1500&limit=3`

`POST /api/nearby` 範例：

```json
{
  "county": "新北市",
  "district": "新莊區",
  "road": "西盛街",
  "lane": "33巷",
  "latitude": 25.0275,
  "longitude": 121.4255,
  "limit": 5
}
```

地址使用 POST JSON，避免完整查詢內容出現在網址。回應包含 `answer`、`capabilities`、`matchQuality`、`stops`、`updatedAt` 與 `stale`。

相容介面仍保留：

- `GET /api/route-options?district=新莊區`
- `GET /api/dashboard?district=新莊區&lineid=242051`
- `GET /api/routes?district=新莊區&street=西盛街`
- `GET /api/trucks?district=新莊區&lineid=242051`
- `GET /api/status`

## 測試

```bash
.venv/bin/python -m unittest discover -s tests -v
```

測試涵蓋行政區數量、地址正規化、巷弄索引、附近查詢、GPS 去重與七日清除、匿名 ID、蒐集租約、下一站降級、TDX 權杖與目錄快取、危險網址過濾、資料能力降級、舊 API 相容、設定覆寫及金鑰不外洩。
