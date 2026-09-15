# TrashTrack｜新北市垃圾車即時動態

以「新莊區／西盛街」為預設條件，動態搜尋新北市垃圾車路線、表定清運站與目前出勤車輛。後端使用 Flask 與記憶體快取，前端使用原生 HTML、CSS、JavaScript、Leaflet 與 OpenStreetMap，不需要 Node.js、資料庫或編譯流程。

## 功能

- 依行政區與道路名稱動態找出所有相關 `lineid`，不硬編碼路線。
- 以 `lineid` 串接表定路線與即時 GPS。
- 地圖顯示相關清運站及出勤垃圾車。
- 依最近站點與清運順序推估下一站，並顯示距離。
- 依台灣時間顯示今日一般垃圾、資源回收與廚餘清運狀態。
- 瀏覽器每 30 秒更新；路線快取 12 小時、GPS 快取 120 秒。
- 官方 API 暫時失敗時，優先顯示舊快取並標示資料可能過期。
- 取得失敗後 30 秒內不重複撞擊官方 API，避免服務異常時放大流量。
- 非清運時段查無出勤車輛時，正常回傳 HTTP 200 與空陣列。

## 一鍵啟動（建議）

腳本會自動檢查 Python 3.9+、建立 `.venv`、安裝缺少的套件、檢查 5000 埠，並將啟動及伺服器輸出寫入 `logs/`。

### Windows

直接雙擊 `start.bat`，或在命令提示字元執行：

```bat
start.bat
```

腳本使用 UTF-8 主控台與 Windows CRLF 換行，支援中文訊息及包含空白的專案路徑。

### macOS / Linux

首次執行先賦予權限，之後直接執行：

```bash
chmod +x start.sh
./start.sh
```

啟動成功後依腳本顯示的網址開啟，預設為 <http://localhost:5000>。若 5000 已被占用（macOS 的 AirPlay 接收器很常見），腳本會自動改用 <http://localhost:5001>。

按 `Ctrl+C` 可停止服務。每次執行的 LOG 位於 `logs/server-年月日-時間.log`。

## 手動安裝

需要 Python 3.9 以上版本。

### Windows

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### macOS / Linux

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 手動啟動

```bash
python app.py
```

開啟：<http://localhost:5000>

第一次取得垃圾車路線資料可能稍慢。垃圾車 GPS 約每數分鐘更新；非清運時間沒有垃圾車 GPS 資料屬於正常狀況。

## 測試

```bash
python -m unittest discover -s tests -v
```

## API

- `GET /api/routes?district=新莊區&street=西盛街`
- `GET /api/trucks?district=新莊區&street=西盛街`
- `GET /api/status`

查詢參數會去除前後空白，行政區最長 20 字、道路名稱最長 50 字。

## 官方 API 實測紀錄

2026-09-15 實測新北市政府資料開放平臺：

- 兩個端點皆回傳 HTTP 200 與 JSON 陣列。
- `page` 從 0 起算，`size` 會限制每頁筆數。
- 路線 API 的 `$filter=city eq 新莊區` 可用；程式仍保留篩選失效時的全量分頁與 Python 端過濾備援。
- 經緯度、`rank` 均以字串回傳，程式會安全轉型；空值或無效座標會被略過。
- GPS 時間格式為 `YYYY/MM/DD HH:MM:SS`，表定時間格式為 `HH:MM`。
- 星期清運欄位實際值為 `Y` 或空字串；程式亦相容 `1/0`、`是/否` 等常見值。
- GPS 資料只包含目前出勤車輛，零筆是正常結果。
- Python 3.14／OpenSSL 嚴格模式會因官方憑證鏈缺少 Subject Key Identifier 而拒絕連線；程式僅停用該項嚴格延伸欄位檢查，仍保留主機名稱、有效期與憑證授權單位驗證，未使用 `verify=False`。

## 部署提醒

`python app.py` 使用 Flask 開發伺服器，適合本機操作。正式上線時請改用支援 WSGI 的正式伺服器，並依部署環境設定反向代理與 HTTPS。

## 後續可擴充

到站倒數、PWA、歷史軌跡、收藏地址與 LINE 通知可在核心資料穩定後再加入。
