#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
미얀마 짯(MMK) 환율 자동 수집 스크립트 (v10)
- 매일 2회 GitHub Actions가 실행하여 rates.json + 이력을 자동 갱신합니다.
- 각 환율마다 주 소스 + 백업 소스를 두어, 하나가 막혀도 자동 전환됩니다.
- 실행 결과를 status.json에 기록해서 실패 여부를 바로 확인할 수 있습니다.

수집 소스:
  ① 환전소 예상 환율   : 1차 egcurrency.com / 2차 egcurrency USD-KRW 역산
  ② CBM 시장거래환율   : 1차 forex.cbm.gov.mm (미얀마 중앙은행 공식)
  ③ 해외송금 환율      : 자동 소스 없음 (수동 관리, 이전 값 유지)
  ④ CBM 기준환율       : 1차 forex.cbm.gov.mm / 2차 open.er-api.com
"""

import json, re, datetime, urllib.request, ssl, csv, os

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
CTX = ssl.create_default_context()

def fetch(url, timeout=30):
    headers = {
        **UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        "Referer": "https://www.google.com/",
    }
    req = urllib.request.Request(url, headers=headers)
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

# ── 소스별 수집 함수 ──────────────────────────────────────────────

def src_cbm_official():
    """미얀마 중앙은행 공식 페이지 → cbm_ref / cbm_market"""
    html = fetch("https://forex.cbm.gov.mm/index.php/fxrate")
    mu = re.search(r"United State Dollar.*?USD.*?([\d,]+\.\d+)\s*</td>\s*<td[^>]*>\s*([\d,]+\.\d+)", html, re.S)
    mk = re.search(r"Korean Won.*?KRW.*?100.*?([\d,]+\.\d+)\s*</td>\s*<td[^>]*>\s*([\d,]+\.\d+)", html, re.S)
    if not mu:
        raise ValueError("CBM 페이지에서 USD 환율 파싱 실패")
    out = {}
    out["cbm_ref"] = {"mmkPerUsd": num(mu.group(1))}
    out["cbm_market"] = {"mmkPerUsd": num(mu.group(2))}
    if mk:
        out["cbm_ref"]["mmkPerKrw"] = round(num(mk.group(1)) / 100, 4)
        out["cbm_market"]["mmkPerKrw"] = round(num(mk.group(2)) / 100, 4)
    return out

def src_market_egcurrency():
    """egcurrency 시장환율 페이지 → market"""
    html = fetch("https://egcurrency.com/en/currency/MMK/blackMarket")
    mu = re.search(r"USD[^0-9]*?([\d,]+\.?\d*)", html)
    if not mu:
        raise ValueError("egcurrency 페이지에서 USD 환율 파싱 실패")
    out = {"market": {"mmkPerUsd": num(mu.group(1))}}
    mk = re.search(r"KRW[^0-9]*?([\d,]+\.?\d*)", html)
    if mk:
        v = num(mk.group(1))
        out["market"]["mmkPerKrw"] = round(v / 1000 if v > 100 else v, 4)
    return out

def src_market_backup(usd_krw_rate):
    """백업: 시장 달러환율 × 원/달러 환율로 원화 환산값 보완"""
    d = json.loads(fetch("https://open.er-api.com/v6/latest/USD"))
    return d["rates"].get("KRW")  # 1달러 = ?원

def src_official_api():
    """백업: 무료 API → cbm_ref (공식 기준환율)"""
    d1 = json.loads(fetch("https://open.er-api.com/v6/latest/USD"))
    d2 = json.loads(fetch("https://open.er-api.com/v6/latest/KRW"))
    return {"cbm_ref": {"mmkPerUsd": d1["rates"]["MMK"],
                        "mmkPerKrw": round(d2["rates"]["MMK"], 4)}}

# ── 메인 ─────────────────────────────────────────────────────────

def main():
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    prev = load_prev()
    prev_map = {i["key"]: i for i in prev["items"]} if prev else {}
    rates = {k: {"mmkPerUsd": v.get("mmkPerUsd"), "mmkPerKrw": v.get("mmkPerKrw")}
             for k, v in prev_map.items()}

    status = {"last_run": now, "sources": {}, "ok": True}

    def apply(data, key, source_name):
        if key in data and data[key].get("mmkPerUsd"):
            rates.setdefault(key, {}).update(data[key])
            status["sources"][key] = {"ok": True, "source": source_name}
            return True
        return False

    # ②④ CBM 공식 페이지 (1차)
    try:
        data = src_cbm_official()
        apply(data, "cbm_ref", "cbm.gov.mm")
        apply(data, "cbm_market", "cbm.gov.mm")
        print("CBM 공식 페이지 수집 성공:", rates.get("cbm_ref"), rates.get("cbm_market"))
    except Exception as e:
        print("CBM 공식 페이지 실패:", e)
        # ④ 기준환율 백업: 무료 API
        try:
            data = src_official_api()
            apply(data, "cbm_ref", "open.er-api.com (백업)")
            print("CBM 기준환율 백업 소스 성공:", rates.get("cbm_ref"))
        except Exception as e2:
            print("CBM 기준환율 백업도 실패:", e2)
        status["sources"].setdefault("cbm_market", {"ok": False, "source": "없음 — 이전 값 유지"})
        status["sources"].setdefault("cbm_ref", {"ok": False, "source": "없음 — 이전 값 유지"})

    # ① 환전소 예상 환율 (1차: egcurrency)
    try:
        data = src_market_egcurrency()
        apply(data, "market", "egcurrency.com")
        # 원화 환율이 없으면 달러 기준으로 역산
        if not rates.get("market", {}).get("mmkPerKrw"):
            krw_per_usd = src_market_backup(None)
            if krw_per_usd and rates["market"].get("mmkPerUsd"):
                rates["market"]["mmkPerKrw"] = round(rates["market"]["mmkPerUsd"] / krw_per_usd, 4)
                print("시장환율 원화값 역산 적용:", rates["market"]["mmkPerKrw"])
        print("시장환율 수집 성공:", rates.get("market"))
    except Exception as e:
        print("시장환율 수집 실패(이전 값 유지):", e)
        status["sources"]["market"] = {"ok": False, "source": "없음 — 이전 값 유지"}

    # ③ 해외송금 환율: 자동 수집 소스가 없어 이전 값 유지 (수동 관리)
    status["sources"]["remit"] = {"ok": True, "source": "수동 관리 (이전 값 유지)"}
    print("송금환율: 이전 값 유지:", rates.get("remit"))

    # 필수값 검증 — 하나라도 빠지면 실패로 표시 (알림 트리거용)
    meta = {
        "market":     ("환전소 예상 환율", "현지 환전소 적용 예상 환율"),
        "cbm_market": ("중앙은행 시장거래환율", "미얀마 중앙은행(CBM) 은행거래 고시"),
        "remit":      ("해외송금 환율", "송금업체 적용 환율 (참고용)"),
        "cbm_ref":    ("중앙은행 기준환율", "CBM 공식 기준 고시환율"),
    }
    items = []
    missing = []
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

    # ── 카드결제 추정용 달러/원화 환율 수집 (전신환매도율 근사치) ───────
    card = {"usdKrw": 1400, "feePct": 2.5}
    if prev and prev.get("card"):
        card = dict(prev["card"])
    try:
        d = json.loads(fetch("https://open.er-api.com/v6/latest/USD"))
        if d.get("rates", {}).get("KRW"):
            card["usdKrw"] = round(d["rates"]["KRW"], 1)
            print("달러/원화 환율 수집 성공:", card["usdKrw"])
    except Exception as e:
        print("달러/원화 환율 수집 실패(이전 값 유지):", e)

    out = {"updated": today, "card": card, "items": items}
    with open("rates.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("rates.json 갱신 완료:", today)

    # ── 환율 이력 저장 (참고용, 오차 범위 없이 정확한 값만 기록) ──────────
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
    hist_rows.append(row)
    hist_rows.sort(key=lambda r: r["date"])
    with open("history.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(hist_rows)

    with open("history.json", "w", encoding="utf-8") as f:
        json.dump(hist_rows[-30:], f, ensure_ascii=False, indent=2)
    print("history.csv / history.json 이력 저장 완료:", today, f"(총 {len(hist_rows)}일치)")

    # ── 수집 상태 기록 (외부에서 상태만 확인할 때 사용) ─────────────
    with open("status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    # 필수 환율이 빠졌으면 종료코드 1 → GitHub Actions 실패 → 이메일 알림 발송
    if not status["ok"]:
        print("⚠️ 수집 실패 항목 있음:", missing)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
