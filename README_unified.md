# yoyakuban importer — Unified Input Mode 対応

この改修版では、**施設ごとのフォルダを1つにまとめた構成**でのインポートに対応しています。
また、CSV カラムの **mapping 設定は施設ごとに `config/[施設名].yaml` へ分離**し、`config_facility.yaml` から使用する YAML を指定する構成に変更しました。

Selenium を使わず (`--skip-selenium` あるいは config.setting.*.selenium=false) で、
トップレベルの `csv_*` ディレクトリに保存された CSV を**ファイル名から facility / login_slug を判別**して、
**正しいDBにインポート**します。

## ディレクトリ構成（推奨）
```
yoyakuban/
  config.yaml                         <- 共通設定（CSVパス、Selenium、import設定など）
  config_facility.yaml                <- 施設設定 + 施設別mapping YAMLの指定
  config/
    sankoh.yaml                       <- sankoh 用 mapping
    a_and_c.yaml                      <- a_and_c 用 mapping
    [施設名].yaml                     <- 施設ごとに追加
  csv_reservations_history/           <- 旧: <facility>/csv_reservations_history/
    sankoh_yumenoi_yoyakuban_history_20250926.csv
    a_and_c_roka_yoyakuban_history_20250926.csv
    ...
  csv_reservations_onhand/            <- 旧: <facility>/csv_reservations_onhand/
    sankoh_yumenoi_yoyakuban_onhand_20250926.csv
    ...
  csv_customer/                       <- 旧: <facility>/csv_customer/
    sankoh_yumenoi_member_20250926.csv
    ...
  processed-csv/                      <- 取り込み後に自動移動（統合モード時）
  yoyakuban_importer.py
```

## 施設別 mapping 設定

### `config_facility.yaml` で使用する YAML を指定
各施設の直下に `config_file` を追加し、施設ごとの mapping YAML を指定します。

```yaml
sankoh:
  name: sankoh
  config_file: sankoh
  enabled: true
  db:
    host: xxxxx
    port: 5432
    user: user_sankoh_insert
    password: xxxxx
    database: reservation_db_sankoh
```

`config_file` は `config/` フォルダ内の YAML ファイル名を、拡張子なしで指定します。例: `config_file: sankoh` は `config/sankoh.yaml` を読み込みます。

### `config/[施設名].yaml` に mapping を保持
CSV の列名が施設ごとに異なる場合は、対象施設の YAML だけを変更してください。

```yaml
mappings:
  reservation:
    string:
      reservation_status: 状態
      reservation_number: 予約番号
      reservation_method: 予約方法
    integer:
      age: 年齢
    date:
      start_date: 宿泊日（チェックイン）
      birthdate: 生年月日

  customer:
    string:
      guest_name: 氏名
      email: メールアドレス
    integer:
      age: 年齢
      number_of_use: 施設利用回数
    date:
      registration_date: 会員登録日
      birthdate: 生年月日
```

> 後方互換として、`config_file` が未指定の場合は従来どおり `config.yaml` 内の `mappings` を参照します。
> ただし、現在の推奨構成では `config.yaml` から `mappings` を外し、施設別 YAML に分離します。

## ファイル名ルール（厳守）
```
<facility>_<login-slug>_yoyakuban_history_YYYYMMDD.csv
<facility>_<login-slug>_yoyakuban_onhand_YYYYMMDD.csv
<facility>_<login-slug>_member_YYYYMMDD.csv
```
例: `sunshine_tsuganoki_yoyakuban_onhand_20250926.csv`

- `<facility>` は `config_facility.yaml` のキー名（小文字）に一致させてください。
- `<login-slug>` は `config_facility.yaml` の `yoyakuban.id / pw / facility_number` のキーと一致。

## 実行例
Selenium を使わずにインポートのみ行う場合:
```
python yoyakuban_importer.py --base-dir . --skip-selenium
```
またはタスクスケジューラ等で exe 化／同等の引数で実行してください。

## 動作仕様（統合モード）
- 起動時に `config.yaml` と `config_facility.yaml` を読み込みます。
- `config_facility.yaml` の `config_file` で指定された名前から `config/[config_file].yaml` を施設ごとに読み込みます。
- 予約・顧客 CSV の取り込み時は、処理中の施設に対応する mapping を使用します。
- `csv_reservations_history/` と `csv_reservations_onhand/` をスキャンし、
  ファイル名から facility / slug を解析してインポートします。
- **onhand** は施設ごとに `facility_code` で **DELETE** してから挿入します（従来仕様踏襲）。
- `csv_customer/` は施設ごとの DB に接続し、
  - ログインスラッグが **複数**ある施設は、対象 `facility_code` の **DELETE** を実行
  - **1つ**だけの施設は `TRUNCATE TABLE yoyakuban_customer;` を実行（従来仕様踏襲）
- 取り込みに成功した history CSV は `processed-csv/`（トップレベル）へ自動移動します。
- 取り込みに成功した onhand / customer CSV は削除します。

## 互換性
- `config_facility.yaml` に `config_file` がある施設は、その YAML の `mappings` を使用します。
- `config_file` がない施設は、従来どおり `config.yaml` の `mappings` を使用できます。
- 既存施設に新しい CSV レイアウトが必要になった場合は、`config/[施設名].yaml` のみ変更してください。

## よくある質問
- **facility_code の判定はどこから？**
  CSV のファイル名から `<facility>` と `<login-slug>` を取り出し、`resolve_yoyakuban_for_slug()` で `facility_number` を取得して設定します。

- **施設ごとに列名が違う場合は？**
  `config/[施設名].yaml` の `mappings.reservation` / `mappings.customer` を施設ごとに編集してください。

- **ファイル名が規則と違う場合は？**
  対象 prefix に一致しない CSV は取り込み対象外です。

- **処理順序や接続は？**
  施設単位で DB 接続を開き、施設内で並んだ CSV を順次投入します。
