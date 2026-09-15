# TrashTrack｜新北市垃圾車即時動態

以「新莊區／西盛街」作為預設情境，並可搜尋、切換新莊區官方清運路線。後端使用 Flask、requests 與記憶體快取；前端使用原生 HTML、CSS、JavaScript 和 Google Maps JavaScript API，不需要 Node.js、npm、資料庫或前端編譯流程。

## 功能

- 依路線名稱或 `lineid` 即時篩選官方路線；選項資料每個行政區只載入一次。
- 路線選擇支援鍵盤上下鍵、Enter、Escape 與完整 combobox ARIA 語意。
- 選定 `lineid` 後顯示該路線完整站點；網址會保留條件，重新整理後可恢復。
- 首次畫面透過單一 `/api/dashboard` 取得路線、站點與車輛，避免重複處理路線資料。
- 12 小時路線快取會同步建立 `lineid`、摘要與街道搜尋索引；GPS 使用 120 秒快取。
- 清運站依正規化座標與站名去重；同一實體站點會整合各路線、順序與表定時間。
- Google Maps 使用 `AdvancedMarkerElement`、單一 `InfoWindow`，並延遲至地圖接近可視區域才載入。
- 車輛每 30 秒更新位置；既有 Marker 只改座標，離線車輛才移除，不重新建立地圖或調整視野。
- 頁面隱藏時暫停輪詢，回到頁面後立即更新；快速切換路線會中止舊請求。
- 官方 API 暫時失敗時優先顯示舊快取，且 30 秒內不重複撞擊失敗端點。

## Google Maps 設定

未設定 Google Maps 時仍可啟動網站及查看路線、站點與車輛列表；地圖區會顯示設定提示。

### macOS / Linux

```bash
export GOOGLE_MAPS_API_KEY="你的 API 金鑰"
export GOOGLE_MAPS_MAP_ID="你的 Map ID"
```

### Windows cmd

```bat
set GOOGLE_MAPS_API_KEY=你的 API 金鑰
set GOOGLE_MAPS_MAP_ID=你的 Map ID
```

Google Maps JavaScript API 必須啟用 billing，載入地圖可能產生費用。正式環境的 API 金鑰務必：

- 設定 HTTP referrer 網站限制。
- API 限制只允許 Maps JavaScript API。
- localhost 與正式網域最好分開使用不同金鑰管理。
- 不要將真實金鑰寫入程式碼、Git、LOG 或公開設定檔。

Map ID 是 Advanced Marker 的必要設定。本專案不會自動使用 `DEMO_MAP_ID` 冒充正式設定。

## 一鍵啟動

腳本會檢查 Python 3.9+、建立 `.venv`、安裝缺少套件、檢查連接埠，並將啟動與伺服器輸出寫入 `logs/`。缺少 Google Maps 設定時只會警告，不會阻止啟動。

### Windows

直接雙擊 `start.bat`，或在命令提示字元執行：

```bat
start.bat
```

腳本維持 UTF-8 主控台與 Windows CRLF 換行。

### macOS / Linux

```bash
chmod +x start.sh
./start.sh
```

腳本維持 LF 與 executable 權限。預設網址為 <http://localhost:5000>；若該連接埠被占用，會嘗試 5001。

## 手動安裝與啟動

需要 Python 3.9 以上版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Windows 啟用虛擬環境請改用 `.venv\Scripts\activate`。第一次取得新莊區約 4,696 筆站點可能需要幾秒鐘。

## API

- `GET /api/route-options?district=新莊區`
- `GET /api/dashboard?district=新莊區&lineid=242051`
- `GET /api/routes?district=新莊區&lineid=242051`
- `GET /api/trucks?district=新莊區&lineid=242051`
- `GET /api/routes?district=新莊區&street=西盛街`
- `GET /api/trucks?district=新莊區&street=西盛街`
- `GET /api/status`

不存在或不屬於指定行政區的 `lineid` 會回傳 HTTP 200、`routeCount: 0` 與空陣列，方便前端一致處理；不會發生 500。舊有 `district + street` 查詢仍維持相容。

`/api/status` 只會回傳 Google Maps API key／Map ID 是否設定，不會回傳實際值。

## 測試

```bash
python -m unittest discover -s tests -v
```

測試涵蓋路線去重與排序、精確路線、未知路線、舊街道查詢、dashboard 單次索引存取、實體站點去重、舊快取備援與 Google Maps 設定安全性。

## 官方資料與部署提醒

資料來自新北市政府資料開放平臺。GPS 資料只包含目前出勤車輛，非清運時段回傳零筆是正常狀況。程式保留 TLS 主機名稱、有效期與憑證授權單位驗證，僅相容部分 Python／OpenSSL 對政府憑證鏈非關鍵 X.509 延伸欄位的嚴格檢查。

`python app.py` 是 Flask 開發伺服器，適合本機操作；正式部署請改用 WSGI 伺服器、反向代理與 HTTPS。
