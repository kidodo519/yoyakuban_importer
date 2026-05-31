from __future__ import annotations

import csv
import datetime as dtx
import os
import sys
import time
import traceback
from argparse import ArgumentParser
from glob import glob
from typing import Dict, Any, List, Optional, Tuple

import yaml
import jaconv
import psycopg2
from psycopg2 import extras

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains
except Exception:
    webdriver = None
    ChromeOptions = None
    ChromeService = None
    By = None

try:
    import requests
except Exception:
    requests = None


# --------------------------
# Utils
# --------------------------
def ymd_compact(d: dtx.date) -> str:
    return f"{d.year}{d.month:02d}{d.day:02d}"


def parse_date_yyyy_mm_dd(text: str) -> dtx.date:
    t = text.replace("/", "-")
    return dtx.date(int(t[0:4]), int(t[5:7]), int(t[8:10]))


def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def load_yaml(path: str) -> Any:
    with open(path, "r", encoding="utf-8-sig") as f:
        return yaml.safe_load(f)


def load_config(base_dir: str) -> Dict[str, Any]:
    cfg = load_yaml(os.path.join(base_dir, "config.yaml"))
    fac = load_yaml(os.path.join(base_dir, "config_facility.yaml"))
    facility_configs = {
        name: load_facility_config(base_dir, name, body)
        for name, body in fac.items()
        if isinstance(body, dict)
    }
    return {"cfg": cfg, "fac": fac, "facility_configs": facility_configs}


def load_facility_config(base_dir: str, facility: str, fac_body: Dict[str, Any]) -> Dict[str, Any]:
    config_file = fac_body.get("config_file")
    if not config_file:
        if "mappings" in fac_body:
            return fac_body
        return {}

    path = config_file if os.path.isabs(config_file) else os.path.join(base_dir, config_file)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"[{facility}] config_file が見つかりません: {path}")

    facility_cfg = load_yaml(path)
    if not isinstance(facility_cfg, dict):
        raise ValueError(f"[{facility}] config_file は YAML マッピングである必要があります: {path}")
    return facility_cfg


def resolve_mappings(cfg: Dict[str, Any], facility_cfg: Dict[str, Any], facility: str) -> Dict[str, Any]:
    mappings = facility_cfg.get("mappings") or cfg.get("mappings")
    if not mappings:
        raise KeyError(
            f"[{facility}] mappings が見つかりません。"
            "config_facility.yaml の config_file で config/[施設名].yaml を指定してください。"
        )
    return mappings


# --------------------------
# Notifications (STRICT)
# --------------------------
def notifications_enabled(cfg: Dict[str, Any]) -> bool:
    try:
        return bool(cfg.get("setting", {}).get("message", {}).get("error", True))
    except Exception:
        return True


def webhook_post(url: Optional[str], content: str, enabled: bool) -> None:
    if not enabled:
        return
    if not url or not requests:
        return
    try:
        requests.post(url, json={"text": content}, timeout=10)
    except Exception:
        pass


def resolve_webhook(cfg: Dict[str, Any], fac_cfg: Dict[str, Any], facility: str) -> Optional[str]:
    url = None
    try:
        url = fac_cfg.get(facility, {}).get("webhook", {}).get("url")
    except Exception:
        pass
    if not url:
        url = cfg.get("webhook", {}).get("url")
    return url


# --------------------------
# Credentials & slug enumeration (STRICT)
# --------------------------
def _select_value_by_slug(val, login_slug: str, facility: str):
    if isinstance(val, dict):
        if login_slug in val:
            return val[login_slug]
        if facility in val:
            return val[facility]
        if len(val) == 1:
            return next(iter(val.values()))
        raise KeyError(f"資格情報マッピングに '{login_slug}' も '{facility}' も見つかりません。")
    return val


def enumerate_login_slugs(cfg: Dict[str, Any], fac_cfg: Dict[str, Any], facility: str) -> List[str]:
    node = fac_cfg.get(facility, {}).get("yoyakuban", {})
    id_val = node.get("id") if node else None
    if isinstance(id_val, dict) and id_val:
        return sorted(id_val.keys())

    y = cfg.get("yoyakuban", {})
    id_val = y.get("id") if y else None
    if isinstance(id_val, dict) and id_val:
        return sorted(id_val.keys())

    return [facility]


def resolve_yoyakuban_for_slug(cfg: Dict[str, Any], fac_cfg: Dict[str, Any], facility: str, login_slug: str) -> Dict[str, Any]:
    node = fac_cfg.get(facility, {}).get("yoyakuban", {})
    if node:
        idv = _select_value_by_slug(node.get("id"), login_slug, facility)
        pwv = _select_value_by_slug(node.get("pw"), login_slug, facility)
        fnv = _select_value_by_slug(node.get("facility_number"), login_slug, facility)
        return {"id": idv, "pw": pwv, "facility_number": fnv, "login_slug": login_slug}
    y = cfg.get("yoyakuban", {})
    if y:
        idv = _select_value_by_slug(y.get("id"), login_slug, facility)
        pwv = _select_value_by_slug(y.get("pw"), login_slug, facility)
        fnv = _select_value_by_slug(y.get("facility_number"), login_slug, facility)
        return {"id": idv, "pw": pwv, "facility_number": fnv, "login_slug": login_slug}
    raise KeyError(f"[{facility}] yoyakuban 資格情報が見つかりません。")


# --------------------------
# CSV mapping
# --------------------------
def map_row(row: Dict[str, str], mapping: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, col in mapping["string"].items():
        v = (row.get(col, "") or "").strip()
        out[k] = jaconv.h2z(v) if v else None
    for k, col in mapping["integer"].items():
        v = (row.get(col, "") or "").strip()
        out[k] = int(v, 10) if (v and v.replace('-', '').isdigit()) else None
    for k, col in mapping["date"].items():
        v = (row.get(col, "") or "").strip()
        out[k] = parse_date_yyyy_mm_dd(v) if v and v != "0" else None
    return out


def add_generated_reservation(rec: Dict[str, Any], facility: str, facility_code: int) -> Dict[str, Any]:
    start_date = rec.get("start_date")
    res_no = rec.get("reservation_number")
    id_key = f"{res_no}_{start_date.strftime('%Y%m%d')}" if start_date and res_no else None
    return {
        **rec,
        "id_key": id_key,
        "facility_name": facility,
        "facility_code": facility_code,
        "facility_key": f"{facility_code}-{res_no}" if res_no else None,
        "import_date": dtx.date.today(),
    }


def add_generated_customer(rec: Dict[str, Any], facility: str, facility_code: int) -> Dict[str, Any]:
    return {
        **rec,
        "facility_name": facility,
        "facility_code": facility_code,
        "import_date": dtx.date.today(),
    }


# --------------------------
# DB
# --------------------------
def connect_db(db: Dict[str, Any]):
    return psycopg2.connect(
        host=db["host"],
        port=int(db["port"]),
        user=db["user"],
        password=db["password"],
        database=db["database"],
    )


# --------------------------
# Selenium helpers (dates & downloads)
# --------------------------
def _new_driver(cfg: Dict[str, Any]):
    if not webdriver:
        raise RuntimeError("Selenium が利用できません。")
    service = ChromeService(executable_path=cfg["path"]["driver_path"])
    opts = ChromeOptions()
    return webdriver.Chrome(service=service, options=opts)


def _clean_download_conflicts(download_dir: str, prefixes: List[str]) -> None:
    for pfx in prefixes:
        for fp in glob(os.path.join(download_dir, f"{pfx}*.csv")):
            try:
                os.remove(fp)
            except Exception:
                pass


def _wait_new_download(download_dir: str, prefix: str, after_ts: float, timeout: int) -> str:
    limit = time.time() + timeout
    while time.time() < limit:
        candidates = [
            fp for fp in glob(os.path.join(download_dir, f"{prefix}*.csv"))
            if os.path.getmtime(fp) >= after_ts and not fp.endswith(".crdownload")
        ]
        if candidates:
            candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            return candidates[0]
        time.sleep(0.3)
    raise FileNotFoundError(
        f"ダウンロードが完了しませんでした: {os.path.join(download_dir, prefix+'*.csv')}"
    )


def _fill_date_inputs(driver, start_str: str, end_str: str) -> None:
    wait = WebDriverWait(driver, 10)
    s = wait.until(EC.presence_of_element_located((By.ID, "search_date_from")))
    e = wait.until(EC.presence_of_element_located((By.ID, "search_date_to")))
    for el, val in ((s, start_str), (e, end_str)):
        try: driver.execute_script("arguments[0].value='';", el)
        except: pass
        el.clear()
        el.send_keys(Keys.CONTROL, 'a', Keys.DELETE)
        el.send_keys(val)
        try: driver.execute_script("arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", el)
        except: pass


def _determine_dates(status: str, status_cfg: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    if status_cfg.get("manual_date"):
        s = status_cfg.get("manual_start_date")
        e = status_cfg.get("manual_end_date")
        if s and e:
            return s.replace("-", "/"), e.replace("-", "/")
        return None
    today = dtx.date.today()
    if status == "history":
        start = today - dtx.timedelta(days=2)
        end = start
    else:  # onhand
        start = today - dtx.timedelta(days=1)
        end = today + dtx.timedelta(days=298)
    return start.strftime("%Y/%m/%d"), end.strftime("%Y/%m/%d")


# --------------------------
# Facility-level toggle for customer import
# --------------------------
def should_import_customer(fac_cfg: Dict[str, Any], facility: str, login_slug: str) -> bool:
    try:
        node = fac_cfg.get(facility, {}).get("yoyakuban", {})
        cust = node.get("customer")
        if isinstance(cust, dict):
            val = cust.get(login_slug, None)
            if isinstance(val, bool):
                return val
    except Exception:
        pass
    return True


# --------------------------
# Selenium flows  （★統合モード：保存先は施設サブフォルダではなくトップ階層の csv_*）
# --------------------------
def selenium_download_reservations(cfg: Dict[str, Any], fac_cfg: Dict[str, Any],
                                   facility: str, login_slug: str, status: str) -> str:
    status_cfg = cfg["setting"][status]
    download_dir = cfg["path"]["download_path"]
    _clean_download_conflicts(download_dir, ["reserve"])

    cred = resolve_yoyakuban_for_slug(cfg, fac_cfg, facility, login_slug)
    target_url = f"https://reserve.489ban.net/admin/{login_slug}/booking"

    driver = _new_driver(cfg)
    try:
        driver.get(target_url)
        driver.maximize_window()
        time.sleep(5)

        # login
        form_input = driver.find_elements(By.CLASS_NAME, "form-control")
        if len(form_input) < 2:
            raise RuntimeError("ログイン画面の入力要素が見つかりません。")
        form_input[0].send_keys(cred["id"])
        form_input[1].send_keys(cred["pw"])
        login_button = driver.find_elements(By.CLASS_NAME, "col-md-4")
        if not login_button:
            raise RuntimeError("ログインボタンが見つかりません。")
        login_button[0].click()
        time.sleep(5)

        def _recover_from_popup() -> None:
            actions = ActionChains(driver)
            popup_close_keys = [Keys.TAB, Keys.TAB, Keys.TAB, Keys.SPACE, Keys.TAB, Keys.ENTER]
            for key in popup_close_keys:
                actions.send_keys(key).perform()
                time.sleep(3)
            time.sleep(10)
            driver.get(target_url)
            time.sleep(5)

        # date range ～ CSV出力ボタン取得まででエラーが出た場合はポップアップ回避処理を実行して再試行
        try:
            rng = _determine_dates(status, status_cfg)
            if rng:
                _fill_date_inputs(driver, rng[0], rng[1])

            driver.execute_script("window.scrollBy(0, 300);")
            time.sleep(5)
            export_button = driver.find_elements(By.ID, "csv")
            if not export_button:
                raise RuntimeError("CSV出力ボタンが見つかりません。")
        except Exception:
            _recover_from_popup()
            rng = _determine_dates(status, status_cfg)
            if rng:
                _fill_date_inputs(driver, rng[0], rng[1])

            driver.execute_script("window.scrollBy(0, 300);")
            time.sleep(5)
            export_button = driver.find_elements(By.ID, "csv")
            if not export_button:
                raise RuntimeError("CSV出力ボタンが見つかりません。")

        clicked_at = time.time()
        export_button[0].click()

        default_sleep = 15 if status == "history" else 30
        manual_date = bool(status_cfg.get("manual_date", True))
        manual_time_sleep = int(status_cfg.get("manual_time_sleep", default_sleep))
        fixed_sleep = manual_time_sleep if manual_date else default_sleep
        total_timeout = max(10, int(status_cfg.get("total_timeout", fixed_sleep * 2)))

        time.sleep(fixed_sleep)

        path_downloaded = _wait_new_download(
            download_dir,
            "reserve",
            after_ts=clicked_at,
            timeout=total_timeout
        )
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # ★統合モード：トップ階層の csv_* へ保存
    base_dir = os.path.dirname(__file__)
    unified_dir = os.path.join(
        base_dir,
        cfg["csv"]["input_directory_history"] if status == "history"
        else cfg["csv"]["input_directory_onhand"],
    )
    ensure_dir(unified_dir)
    dst = os.path.join(
        unified_dir,
        f"{facility}_{login_slug}_yoyakuban_{status}_{ymd_compact(dtx.date.today())}.csv",
    )
    os.replace(path_downloaded, dst)
    return dst


def selenium_download_members(cfg: Dict[str, Any], fac_cfg: Dict[str, Any],
                              facility: str, login_slug: str) -> str:
    download_dir = cfg["path"]["download_path"]
    _clean_download_conflicts(download_dir, ["member"])

    cred = resolve_yoyakuban_for_slug(cfg, fac_cfg, facility, login_slug)
    target_url = f"https://reserve.489ban.net/admin/{login_slug}/member"

    driver = _new_driver(cfg)
    try:
        driver.get(target_url)
        driver.maximize_window()
        time.sleep(5)

        form_input = driver.find_elements(By.CLASS_NAME, "form-control")
        if len(form_input) < 2:
            raise RuntimeError("ログイン画面の入力要素が見つかりません。")
        form_input[0].send_keys(cred["id"])
        form_input[1].send_keys(cred["pw"])
        login_button = driver.find_elements(By.CLASS_NAME, "col-md-4")
        if not login_button:
            raise RuntimeError("ログインボタンが見つかりません。")
        login_button[0].click()
        time.sleep(5)

        driver.execute_script("window.scrollBy(0, 800);")
        time.sleep(5)
        display_button = driver.find_element(
            By.XPATH, '//input[@type="submit" and @value="表示する"]'
        )
        display_button.click()

        time.sleep(5)
        driver.execute_script("window.scrollBy(0, 800);")
        time.sleep(5)
        export_button = driver.find_element(By.XPATH, '//button[text()="CSV出力"]')        

        clicked_at = time.time()
        export_button.click()

        default_sleep = 60
        time.sleep(default_sleep)

        path_downloaded = _wait_new_download(download_dir, "member",
                                             after_ts=clicked_at,
                                             timeout=default_sleep)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # ★統合モード：トップ階層の csv_customer へ保存
    base_dir = os.path.dirname(__file__)
    unified_dir = os.path.join(base_dir, cfg["csv"]["input_directory_customer"])
    ensure_dir(unified_dir)
    dst = os.path.join(
        unified_dir,
        f"{facility}_{login_slug}_member_{ymd_compact(dtx.date.today())}.csv",
    )
    os.replace(path_downloaded, dst)
    return dst


# --------------------------
# Importers（★統合モード：トップ階層の csv_* から、facility/slug で自分の分だけ拾う）
# --------------------------
def map_and_buffer_insert(cur, insert_sql: str, csv_path: str, mapping: Dict[str, Dict[str, str]], ordered_keys: List[str], encoding: str, convert_fn):
    buf: List[List[Any]] = []
    with open(csv_path, "r", encoding=encoding, errors="ignore") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            rec = map_row(row, mapping)
            rec = convert_fn(rec)
            buf.append([rec.get(k) for k in ordered_keys])
            if len(buf) >= 10000:
                extras.execute_values(cur, insert_sql, buf)
                buf.clear()
    if buf:
        extras.execute_values(cur, insert_sql, buf)


def _unified_pick_files(dir_path: str, patterns: Tuple[str, ...]) -> List[str]:
    files = sorted({
        fp
        for pat in patterns
        for fp in glob(os.path.join(dir_path, pat))
        if os.path.isfile(fp) and not fp.endswith((".crdownload", ".tmp"))
    })
    return files


def import_reservations(cfg: Dict[str, Any], fac_cfg: Dict[str, Any], facility_configs: Dict[str, Any],
                        facility: str, login_slug: str, status: str) -> None:
    table = "yoyakuban_reservations" if status == "history" else "yoyakuban_reservations_onhand"
    base_dir = os.path.dirname(__file__)
    # ★統合モード：施設サブフォルダではなくトップ階層の csv_* を参照
    unified_dir = os.path.join(
        base_dir,
        cfg["csv"]["input_directory_history"] if status == "history" else cfg["csv"]["input_directory_onhand"],
    )
    # 自分の facility/slug のみ対象
    prefix = f"{facility}_{login_slug}_yoyakuban_{status}_"
    patterns = (f"{prefix}*.csv", f"{prefix}*.CSV")
    files = _unified_pick_files(unified_dir, patterns)
    if not files:
        return

    db = fac_cfg[facility]["db"]
    fc = resolve_yoyakuban_for_slug(cfg, fac_cfg, facility, login_slug)["facility_number"]

    conn = connect_db(db)
    try:
        with conn, conn.cursor() as cur:
            if status == "onhand":
                cur.execute(f"DELETE FROM {table} WHERE facility_code = %s;", (fc,))

            mapping = resolve_mappings(cfg, facility_configs.get(facility, {}), facility)["reservation"]
            ordered_keys = list(mapping["string"].keys()) + \
                           list(mapping["integer"].keys()) + \
                           list(mapping["date"].keys()) + \
                           ["id_key", "facility_name", "facility_code", "facility_key", "import_date"]
            insert_sql = f"INSERT INTO {table}({', '.join(ordered_keys)}) VALUES %s"

            for path in files:
                map_and_buffer_insert(
                    cur, insert_sql, path, mapping, ordered_keys, cfg["csv"]["encoding"],
                    lambda rec, fac=facility, fc_=fc: add_generated_reservation(rec, fac, fc_)
                )
                # ★後処理：history → processed-csv へ移動、onhand → 削除
                if status == "history":
                    processed = os.path.join(base_dir, cfg["csv"]["output_directory"])
                    ensure_dir(processed)
                    os.replace(path, os.path.join(processed, os.path.basename(path)))
                else:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
    except Exception:
        if notifications_enabled(cfg):
            webhook_post(
                resolve_webhook(cfg, fac_cfg, facility),
                f"[{facility}/{login_slug}/{status}] 予約データインポートでエラー\n{traceback.format_exc()}",
                True
            )
        raise
    finally:
        conn.close()


def import_customers(cfg: Dict[str, Any], fac_cfg: Dict[str, Any], facility_configs: Dict[str, Any],
                     facility: str, login_slug: str, multi_slug: bool) -> None:
    table = "yoyakuban_customer"
    base_dir = os.path.dirname(__file__)
    unified_dir = os.path.join(base_dir, cfg["csv"]["input_directory_customer"])
    prefix = f"{facility}_{login_slug}_member_"
    patterns = (f"{prefix}*.csv", f"{prefix}*.CSV")
    files = _unified_pick_files(unified_dir, patterns)
    if not files:
        return

    db = fac_cfg[facility]["db"]
    fc = resolve_yoyakuban_for_slug(cfg, fac_cfg, facility, login_slug)["facility_number"]

    conn = connect_db(db)
    try:
        with conn, conn.cursor() as cur:
            if multi_slug:
                cur.execute(f"DELETE FROM {table} WHERE facility_code = %s;", (fc,))
            else:
                cur.execute(f"TRUNCATE TABLE {table};")

            mapping = resolve_mappings(cfg, facility_configs.get(facility, {}), facility)["customer"]
            ordered_keys = (
                list(mapping["string"].keys())
                + list(mapping["integer"].keys())
                + list(mapping["date"].keys())
                + ["facility_name", "facility_code", "import_date"]
            )
            insert_sql = f"INSERT INTO {table}({', '.join(ordered_keys)}) VALUES %s"

            for path in files:
                map_and_buffer_insert(
                    cur, insert_sql, path, mapping, ordered_keys, cfg["csv"]["encoding"],
                    lambda rec, fac=facility, fc_=fc: add_generated_customer(rec, fac, fc_)
                )
                # ★後処理：customer は削除
                try:
                    os.remove(path)
                except Exception:
                    pass
    except Exception:
        if notifications_enabled(cfg):
            webhook_post(
                resolve_webhook(cfg, fac_cfg, facility),
                f"[{facility}/{login_slug}/customer] 顧客データインポートでエラー\n{traceback.format_exc()}",
                True
            )
        raise
    finally:
        conn.close()


# --------------------------
# Main（Selenium の流れ・config 判定はベース通り、保存先/読取先のみ統合モード）
# --------------------------
def main() -> int:
    p = ArgumentParser(description="yoyakuban importer (統合モード・後処理仕様追加)")
    p.add_argument("--base-dir", type=str, default=os.path.dirname(__file__))
    p.add_argument("--facility", type=str, default="")
    p.add_argument("--skip-selenium", action="store_true")
    args = p.parse_args()

    base_dir = os.path.abspath(args.base_dir)
    conf = load_config(base_dir)
    cfg, fac_cfg = conf["cfg"], conf["fac"]
    facility_configs = conf["facility_configs"]

    facilities = []
    for name, body in fac_cfg.items():
        if isinstance(body, dict) and body.get("enabled", False):
            facilities.append(name)
    if args.facility:
        facilities = [args.facility]

    for fac in facilities:
        print(f"=== {fac} ===")
        slugs = enumerate_login_slugs(cfg, fac_cfg, fac)
        if not slugs:
            raise SystemExit(f"[{fac}] login_slug を特定できません。id の辞書キーを確認してください。")

        multi_slug = len(slugs) > 1

        for slug in slugs:
            print(f" -> {slug}")
            try:
                if not args.skip_selenium:
                    if cfg["setting"]["history"]["selenium"]:
                        _ = selenium_download_reservations(cfg, fac_cfg, fac, slug, "history")
                    if cfg["setting"]["onhand"]["selenium"]:
                        _ = selenium_download_reservations(cfg, fac_cfg, fac, slug, "onhand")
                    if cfg["setting"]["customer"]["selenium"] and should_import_customer(fac_cfg, fac, slug):
                        _ = selenium_download_members(cfg, fac_cfg, fac, slug)

                if cfg["setting"]["history"]["import"]:
                    import_reservations(cfg, fac_cfg, facility_configs, fac, slug, "history")
                if cfg["setting"]["onhand"]["import"]:
                    import_reservations(cfg, fac_cfg, facility_configs, fac, slug, "onhand")
                if cfg["setting"]["customer"]["import"] and should_import_customer(fac_cfg, fac, slug):
                    import_customers(cfg, fac_cfg, facility_configs, fac, slug, multi_slug)

            except Exception:
                msg = f"[{fac}/{slug}] 予期せぬエラー\n{traceback.format_exc()}"
                print(msg, file=sys.stderr)
                if notifications_enabled(cfg):
                    webhook_post(resolve_webhook(cfg, fac_cfg, fac), msg, True)

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
