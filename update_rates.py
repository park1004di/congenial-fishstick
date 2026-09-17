#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
미얀마 짯(MMK) 환율 자동 수집 스크립트 (v23)
- 매일 2회 GitHub Actions가 실행하여 rates.json + 이력을 자동 갱신합니다.
- 여러 출처에서 수집 후 중간값(median)을 사용 — 한 곳이 멈추거나 이상값을 내도 안전합니다.
- 실행 결과를 status.json에 기록해서 실패 여부를 바로 확인할 수 있습니다.

수집 소스 (환율별 다중 출처):
  ① 환전소 예상 환율   : 1차 setlive.myanmarnode.com / 2차 egcurrency.com (중간값)
  ② 중앙은행 시장거래환율: forex.cbm.gov.mm (공식)
  ③ 해외송금 환율      : Remitly + Western Union (둘의 중간값)
  ④ 중앙은행 기준환율   : forex.cbm.gov.mm / 백업: open.er-api.com, xe.com
  ⑤ 카드용 달러/원화   : open.er-api.com
"""

import json, re, datetime, urllib.request, ssl, csv, os, statistics

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Referer": "https://www.google.com/",
}
CTX = ssl.create_default_context()

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read().decode("utf-8", errors="ignore")

def num(s):
    return float(re.sub(r"[^\d.]", "", s))

def load_prev():
    try:
        with open("rates.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def median(values):
    """수집된 값들의 중간값 — 이상값(극단치)에 평균보다 강함"""
    vals = [v for v in values if v]
    return round(statistics.median(vals), 2) if vals else None

# ── 출처별 수집 함수 ─────────────────────────────────────────────

def src_cbm_official():
    """미얀마 중앙은행 공식 페이지 → cbm_ref / cbm_market"""
    html = fetch("https://forex.cbm.gov.mm/index.php/fxrate")
    mu = re.search(r"United State Dollar.*?USD.*?([\d,]+\.\d+)\s*</td>\s*<td[^>]*>\s*([\d,]+\.\d+)", html, re.S)
    mk = re.search(r"Korean Won.*?KRW.*?100.*?([\d,]+\.\d+)\s*</td>\s*<td[^>]*>\s*([\d,]+\.\d+)", html, re.S)
    if not mu:
        raise ValueError("CBM 페이지에서 USD 환율 파싱 실패")
    out = {"cbm_ref": {"mmkPerUsd": num(mu.group(1))},
           "cbm_market": {"mmkPerUsd": num(mu.group(2))}}
    if mk:
        out["cbm_ref"]["mmkPerKrw"] = round(num(mk.group(1)) / 100, 4)
        out["cbm_market"]["mmkPerKrw"] = round(num(mk.group(2)) / 100, 4)
    return out

def src_market_egcurrency():
    """egcurrency 시장환율 페이지 → market (백업 출처)"""
    html = fetch("https://egcurrency.com/en/currency/MMK/blackMarket")
    mu = re.search(r"USD[^0-9]*?([\d,]+\.?\d*)", html)
    if not mu:
        raise ValueError("egcurrency USD 파싱 실패")
    out = {"market": {"mmkPerUsd": num(mu.group(1))}}
    mk = re.search(r"KRW[^0-9]*?([\d,]+\.?\d*)", html)
    if mk:
        v = num(mk.group(1))
        out["market"]["mmkPerKrw"] = round(v / 1000 if v > 100 else v, 4)
    return out

def _setlive_series(cur):
    """setlive 통화별 상세 페이지의 임베드 JSON에서 과거 시계열 추출"""
    html = fetch(f"https://setlive.myanmarnode.com/en/market-prices/currencies/{cur}")
    m = re.search(r'<script data-page="app" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError(f"setlive {cur} 데이터 블록 없음")
    days = json.loads(m.group(1))["props"]["days"]
    rows = []
    for d in days:
        if d.get("quotes"):
            q = d["quotes"][0]
            rows.append({"date": d["date"], "buy": float(q["buy"]), "sell": float(q["sell"])})
    rows.sort(key=lambda r: r["date"])
    return rows

def src_market_setlive():
    """setlive 시장환율 → market (매수/매도 중간값, 과거 시계열 포함)"""
    usd = _setlive_series("usd")
    krw = {r["date"]: r for r in _setlive_series("krw")}
    latest = usd[-1]
    out = {"market": {"mmkPerUsd": round((latest["buy"] + latest["sell"]) / 2, 2)}}
    k = krw.get(latest["date"])
    if k:
        out["market"]["mmkPerKrw"] = round((k["buy"] + k["sell"]) / 2, 4)
    # 과거 이력도 함께 반환 (이력 채우기용)
    out["_history"] = [
        {"date": r["date"], "market_usd": round((r["buy"] + r["sell"]) / 2, 2),
         "market_krw": (round((krw[r["date"]]["buy"] + krw[r["date"]]["sell"]) / 2, 4)
                        if r["date"] in krw else None)}
        for r in usd
    ]
    return out

def src_remitly():
    """Remitly 송금 환율 → remit"""
    html = fetch("https://www.remitly.com/us/ko/currency-converter/usd-to-mmk-rate")
    m = re.search(r"([\d,]+\.\d+)\s*MMK", html)
    if not m:
        raise ValueError("Remitly 파싱 실패")
    return num(m.group(1))

def src_westernunion():
    """Western Union 송금 환율 → remit (페이지 구조가 자주 바뀌어 실패해도 괜찮은 보조 출처)"""
    html = fetch("https://www.westernunion.com/us/en/currency-converter/usd-to-mmk-rate.html")
    # '1.00 USD = 3,953.12 MMK' 형태의 패턴만 허용 (다른 큰 숫자 오수집 방지)
    m = re.search(r"1(?:\.00)?\s*USD\s*(?:=|–|-|–)[^0-9]{0,40}?([\d,]{4}\.\d{2})", html)
    if not m:
        raise ValueError("WesternUnion 파싱 실패")
    return num(m.group(1))

def src_official_api():
    """백업: 무료 API → cbm_ref (공식 기준환율)"""
    d1 = json.loads(fetch("https://open.er-api.com/v6/latest/USD"))
    d2 = json.loads(fetch("https://open.er-api.com/v6/latest/KRW"))
    return {"cbm_ref": {"mmkPerUsd": d1["rates"]["MMK"],
                        "mmkPerKrw": round(d2["rates"]["MMK"], 4)}}

def src_xe():
    """백업: xe.com 중간시장 환율 → cbm_ref"""
    html = fetch("https://www.xe.com/en-us/currencyconverter/convert/?Amount=1&From=USD&To=MMK")
    m = re.search(r"([\d,]+\.\d+)\s*MMK", html)
    if not m:
        raise ValueError("XE 파싱 실패")
    return num(m.group(1))

def src_usd_krw():
    """카드 계산용 달러/원화 환율"""
    d = json.loads(fetch("https://open.er-api.com/v6/latest/USD"))
    return d["rates"].get("KRW")

def _setlive_props(path):
    """setlive 페이지의 임베드 JSON props 추출"""
    html = fetch(f"https://setlive.myanmarnode.com/en/market-prices/{path}")
    m = re.search(r'<script data-page="app" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError(f"setlive {path} 데이터 블록 없음")
    return json.loads(m.group(1))["props"]

def src_gold():
    """금 시세: 세계 금 + 미얀마 금 → gold.json용 데이터"""
    p = _setlive_props("gold")
    wg = p.get("worldGold", {})
    out = {"updated_at": p.get("asOf", ""), "world": None, "myanmar": []}
    if wg.get("quote"):
        q = wg["quote"]
        out["world"] = {
            "usd_per_oz": float(q["rate"]),
            "change": float(q.get("change") or 0),
            "usd_per_gram": float(wg.get("per_gram_usd") or 0),
            "usd_per_kyattha": float(wg.get("per_kyattha_usd") or 0),
        }
    for item in p.get("gold", []):
        if not item.get("quote"):
            continue
        out["myanmar"].append({
            "code": item["series"]["code"],
            "name": item["series"]["name"]["en"],
            "buy": float(item["quote"]["buy"]),
            "sell": float(item["quote"]["sell"]),
            "buy_change": float(item["quote"].get("buy_change") or 0),
            "sell_change": float(item["quote"].get("sell_change") or 0),
        })
    if not out["myanmar"]:
        raise ValueError("금 데이터 없음")
    return out

def src_petrol():
    """기름값: 양곤 기준 유종별 리터당 짯 → petrol.json용 데이터"""
    p = _setlive_props("petrol")
    out = {"updated_at": p.get("asOf", ""),
           "region": p.get("region", {}).get("name", {}).get("en", "Yangon"), "items": []}
    for item in p.get("items", []):
        if not item.get("quote"):
            continue
        out["items"].append({
            "code": item["series"]["code"],
            "name": item["series"]["name"]["en"],
            "rate": float(item["quote"]["rate"]),
            "change": float(item["quote"].get("change") or 0),
        })
    if not out["items"]:
        raise ValueError("기름 데이터 없음")
    return out

# ── 메인 ─────────────────────────────────────────────────────────

def main():
    # GitHub Actions 서버는 UTC를 사용 → 한국시간(UTC+9)으로 변환해서 기록
    kst = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)
    today = kst.date().isoformat()
    now = kst.isoformat(timespec="seconds")

    prev = load_prev()
    prev_map = {i["key"]: i for i in prev["items"]} if prev else {}
    rates = {k: {"mmkPerUsd": v.get("mmkPerUsd"), "mmkPerKrw": v.get("mmkPerKrw")}
             for k, v in prev_map.items()}

    status = {"last_run": now, "sources": {}, "ok": True}
    collected = {}  # {key: [출처별 수집값들]} — 중간값 계산용
    src_market_setlive_history = []  # setlive 과거 시계열 (이력 백필용)

    def collect(key, value, source_name):
        if value:
            collected.setdefault(key, []).append(value)
            status["sources"].setdefault(key, {"ok": True, "source": []})
            if isinstance(status["sources"][key]["source"], list):
                status["sources"][key]["source"].append(source_name)
            return True
        return False

    # ②④ 중앙은행 공식 페이지
    try:
        data = src_cbm_official()
        collect("cbm_ref", data.get("cbm_ref", {}).get("mmkPerUsd"), "cbm.gov.mm")
        collect("cbm_market", data.get("cbm_market", {}).get("mmkPerUsd"), "cbm.gov.mm")
        # 원화 환산값은 중간값 대상이 아니라 그대로 적용
        for k in ("cbm_ref", "cbm_market"):
            if data.get(k, {}).get("mmkPerKrw"):
                rates.setdefault(k, {})["mmkPerKrw"] = data[k]["mmkPerKrw"]
        print("CBM 수집 성공:", data)
    except Exception as e:
        print("CBM 공식 페이지 실패:", e)
        # 기준환율 백업 출처들 (둘 다 시도해서 중간값)
        for name, fn in [("open.er-api.com (백업)", lambda: src_official_api().get("cbm_ref", {}).get("mmkPerUsd")),
                         ("xe.com (백업)", src_xe)]:
            try:
                v = fn()
                collect("cbm_ref", v, name)
                print(f"CBM 기준환율 백업({name}) 성공:", v)
            except Exception as e2:
                print(f"CBM 기준환율 백업({name}) 실패:", e2)

    # ③ 해외송금 환율: Remitly + Western Union 중간값
    for name, fn in [("remitly.com", src_remitly), ("westernunion.com", src_westernunion)]:
        try:
            v = fn()
            # 송금 환율 정상 범위 검증 (시장거래환율 근처여야 함, 공식 2100은 오수집)
            if v and 3000 < v < 6000:
                collect("remit", v, name)
                print(f"송금환율({name}) 수집 성공:", v)
            else:
                print(f"송금환율({name}) 범위 이상값 무시:", v)
        except Exception as e:
            print(f"송금환율({name}) 실패:", e)

    # ① 환전소 예상 환율: setlive(1차) + egcurrency(2차) 중간값
    for name, fn in [("setlive.myanmarnode.com", src_market_setlive),
                     ("egcurrency.com", src_market_egcurrency)]:
        try:
            data = fn()
            v = data.get("market", {}).get("mmkPerUsd")
            if v and 3500 < v < 6000:
                collect("market", v, name)
                if data.get("market", {}).get("mmkPerKrw"):
                    rates.setdefault("market", {})["mmkPerKrw"] = data["market"]["mmkPerKrw"]
                if data.get("_history"):
                    src_market_setlive_history = data["_history"]
                print(f"환전소 예상 환율({name}) 수집 성공:", v)
            else:
                print(f"환전소 예상 환율({name}) 범위 이상값 무시:", v)
        except Exception as e:
            print(f"환전소 예상 환율({name}) 실패:", e)

    # ── 중간값 적용 (여러 출처 수집 시 median) ─────────────────────
    for key, vals in collected.items():
        med = median(vals)
        if med:
            rates.setdefault(key, {})["mmkPerUsd"] = med
            # 달러 환율이 새로 수집됐으면 원화 환산값도 다시 계산하도록 표시
            rates[key]["mmkPerKrw"] = None
            print(f"{key}: {len(vals)}개 출처 중간값 = {med} (수집값: {vals})")

    # 원화 환산값이 없는 항목은 달러 기준으로 역산
    try:
        krw_per_usd = src_usd_krw()
        if krw_per_usd:
            for key in rates:
                if rates[key].get("mmkPerUsd") and not rates[key].get("mmkPerKrw"):
                    rates[key]["mmkPerKrw"] = round(rates[key]["mmkPerUsd"] / krw_per_usd, 4)
                    print(f"{key} 원화값 역산: {rates[key]['mmkPerKrw']}")
    except Exception as e:
        print("달러/원화 역산 실패:", e)

    # 카드 계산용 달러/원화 환율
    card = {"usdKrw": 1400, "feePct": 2.5}
    if prev and prev.get("card"):
        card = dict(prev["card"])
    try:
        v = src_usd_krw()
        if v:
            card["usdKrw"] = round(v, 1)
            print("달러/원화 환율 수집 성공:", card["usdKrw"])
    except Exception as e:
        print("달러/원화 환율 수집 실패(이전 값 유지):", e)

    # ── 최종 조립 및 검증 ─────────────────────────────────────────
    meta = {
        "market":     ("환전소 예상 환율", "현지 환전소 적용 예상 환율"),
        "cbm_market": ("중앙은행 시장거래환율", "미얀마 중앙은행(CBM) 은행거래 고시"),
        "remit":      ("해외송금 환율", "송금업체 적용 환율 (참고용)"),
        "cbm_ref":    ("중앙은행 기준환율", "CBM 공식 기준 고시환율"),
    }
    items, missing = [], []
    for k in ["market", "cbm_market", "remit", "cbm_ref"]:
        v = rates.get(k, {})
        if v.get("mmkPerUsd") and v.get("mmkPerKrw"):
            items.append({"key": k, "name": meta[k][0], "desc": meta[k][1],
                          "mmkPerUsd": v["mmkPerUsd"], "mmkPerKrw": v["mmkPerKrw"]})
        else:
            missing.append(k)
    if missing:
        status["ok"] = False
        status["missing"] = missing

    out = {"updated": today, "card": card, "items": items}
    with open("rates.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("rates.json 갱신 완료:", today)

    # ── 환율 이력 저장 (참고용, 오차 범위 없이 정확한 값만 기록) ──
    row = {"date": today}
    for it in items:
        row[f"{it['key']}_usd"] = it["mmkPerUsd"]
        row[f"{it['key']}_krw"] = it["mmkPerKrw"]

    cols = ["date", "market_usd", "market_krw", "cbm_market_usd", "cbm_market_krw",
            "remit_usd", "remit_krw", "cbm_ref_usd", "cbm_ref_krw"]
    hist_rows = []
    if os.path.exists("history.csv"):
        with open("history.csv", encoding="utf-8-sig", newline="") as f:
            hist_rows = [r for r in csv.DictReader(f) if r.get("date") != today]

    # setlive 과거 시계열로 빠진 날짜의 시장환율 백필 (있으면)
    backfilled = 0
    if collected.get("market") and src_market_setlive_history:
        by_date = {r["date"]: r for r in hist_rows}
        for p in src_market_setlive_history:
            if p["date"] == today:
                continue
            r = by_date.setdefault(p["date"], {"date": p["date"]})
            if not r.get("market_usd"):
                r["market_usd"] = p["market_usd"]
                backfilled += 1
            if p.get("market_krw") and not r.get("market_krw"):
                r["market_krw"] = p["market_krw"]
        hist_rows = list(by_date.values())
        if backfilled:
            print(f"과거 시장환율 백필: {backfilled}일치 추가")

    hist_rows.append(row)
    hist_rows.sort(key=lambda r: r["date"])
    with open("history.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(hist_rows)
    with open("history.json", "w", encoding="utf-8") as f:
        json.dump(hist_rows[-30:], f, ensure_ascii=False, indent=2)
    print("history 저장 완료:", today, f"(총 {len(hist_rows)}일치)")

    # ── 환율 변동 로그 (값이 바뀔 때만 추가, N 표시용) ──────────────
    market_item = next((i for i in items if i["key"] == "market"), None)
    if market_item:
        log = []
        if os.path.exists("market_log.json"):
            try:
                with open("market_log.json", encoding="utf-8") as f:
                    log = json.load(f)
            except Exception:
                log = []
        last_usd = log[-1]["usd"] if log else None
        cur_usd = market_item["mmkPerUsd"]
        if last_usd != cur_usd:  # 값이 바뀐 경우에만 기록
            chg = round(cur_usd - last_usd, 2) if last_usd is not None else 0
            log.append({"t": kst.strftime("%Y-%m-%d %H:%M"),
                        "usd": cur_usd, "krw": market_item["mmkPerKrw"], "chg": chg})
            log = log[-100:]  # 최근 100건만 유지
            with open("market_log.json", "w", encoding="utf-8") as f:
                json.dump(log, f, ensure_ascii=False, indent=2)
            print(f"환율 변동 기록: {last_usd} → {cur_usd}")
        else:
            print("환율 변동 없음 (로그 추가 생략)")

    # ── 금 / 기름 데이터 수집 (별도 페이지용) ─────────────────────
    for fname, fn in [("gold.json", src_gold), ("petrol.json", src_petrol)]:
        try:
            data = fn()
            data["date"] = today
            with open(fname, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            status["sources"][fname.replace(".json", "")] = {"ok": True, "source": "setlive.myanmarnode.com"}
            print(f"{fname} 수집 성공")
        except Exception as e:
            print(f"{fname} 수집 실패(이전 파일 유지):", e)
            status["sources"][fname.replace(".json", "")] = {"ok": False, "source": "실패 — 이전 값 유지"}

    # ── 수집 상태 기록 ─────────────────────────────────────────────
    with open("status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    if not status["ok"]:
        print("⚠️ 수집 실패 항목 있음:", missing)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
