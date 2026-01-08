# yoyakuban importer — Unified Input Mode 対応

この改修版では、**施設ごとのフォルダを1つにまとめた構成**でのインポートに対応しました。
Selenium を使わず (`--skip-selenium` あるいは config.setting.*.selenium=false) で、
トップレベルの `csv_*` ディレクトリに保存された CSV を**ファイル名から facility / login_slug を判別**して、
**正しいDBにインポート**します。

## ディレクトリ構成（推奨）
```
yoyakuban/
  config.yaml
  config_facility.yaml
  csv_reservations_history/        <- 旧: <facility>/csv_reservations_history/
    sankoh_yumenoi_yoyakuban_history_20250926.csv
    a_and_c_roka_yoyakuban_history_20250926.csv
    ...
  csv_reservations_onhand/         <- 旧: <facility>/csv_reservations_onhand/
    sankoh_yumenoi_yoyakuban_onhand_20250926.csv
    ...
  csv_customer/                    <- 旧: <facility>/csv_customer/
    sankoh_yumenoi_member_20250926.csv
    ...
  processed-csv/                   <- 取り込み後に自動移動（統合モード時）
  yoyakuban.py
```

> 旧来の施設別フォルダ（`sankoh/`, `hachinobo/` など）は**残っていても構いません**。
> トップレベルの `csv_*` ディレクトリが存在する場合は**自動で「統合モード」**になります。

## ファイル名ルール（厳守）
```
<facility>_<login-slug>_yoyakuban_history_YYYYMMDD.csv
<facility>_<login-slug>_yoyakuban_onhand_YYYYMMDD.csv
<facility>_<login-slug>_member_YYYYMMDD.csv
```
例: `sunshine_tsuganoki_yoyakuban_onhand_20250926.csv`

- `<facility>` は `config_facility.yaml` のキー名（小文字）に一致させてください。
- `<login-slug>` は `config.yaml / config_facility.yaml` の `yoyakuban.id / pw / facility_number` のキーと一致。

## 実行例
Selenium を使わずにインポートのみ行う場合:
```
python yoyakuban.py --base-dir . --skip-selenium
```
またはタスクスケジューラ等で exe 化／同等の引数で実行してください。

## 動作仕様（統合モード）
- `csv_reservations_history/` と `csv_reservations_onhand/` をスキャンし、
  ファイル名から facility / slug を解析してインポートします。
- **onhand** は施設ごとに `facility_code` で **DELETE** してから挿入します（従来仕様踏襲）。
- `csv_customer/` は施設ごとの DB に接続し、
  - ログインスラッグが **複数**ある施設は、対象 `facility_code` の **DELETE** を実行
  - **1つ**だけの施設は `TRUNCATE TABLE yoyakuban_customer;` を実行（従来仕様踏襲）
- 取り込みに成功した CSV は `processed-csv/`（トップレベル）へ自動移動します。

## 互換性
- トップレベルの `csv_*` ディレクトリが**存在しない**場合は、**旧来の施設別ディレクトリ**を用いた処理に自動でフォールバックします。
- 既存の `config.yaml` / `config_facility.yaml` は**変更不要**です。

## よくある質問
- **facility_code の判定はどこから？**  
  CSV のファイル名から `<facility>` と `<login-slug>` を取り出し、`resolve_yoyakuban_for_slug()` で `facility_number` を取得して設定します。

- **ファイル名が規則と違う場合は？**  
  無視されます（ログに WARN を出します）。

- **処理順序や接続は？**  
  施設単位で DB 接続を開き、施設内で並んだ CSV を順次投入します。

