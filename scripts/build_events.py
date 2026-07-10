#!/usr/bin/env python3
"""
DART OpenAPI에서 '국민연금 대량보유 공시'를 받아 events.json 생성 — '포착' 탭 국민연금 렌즈 데이터.

- 최근 WINDOW_DAYS 국민연금공단이 제출한 '주식등의대량보유상황보고서'를 수집하고,
  각 공시의 보유비율(stkrt)·증감(stkrt_irds)을 majorstock API로 채워 방향(늘림/줄임)까지 붙인다.
- 이벤트 1건 = events 배열의 1행(표준 카드 스키마). 렌즈가 늘어나도 같은 스키마로 append만 하면 된다.
- 매일 GitHub Actions가 실행. 내용이 실제로 바뀐 날만 커밋(version=그날 날짜) → 앱이 괜히 매일 재다운 안 함.

★인증키는 환경변수 DART_KEY(= GitHub Secret)에서만 읽는다. 코드/커밋에 절대 넣지 않는다
  (이 repo는 public이라 커밋하면 전 세계에 노출된다).
"""
import io
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone

KEY = os.environ.get("DART_KEY")
WINDOW_DAYS = 90                 # 포착 피드에 실을 최근 기간
OUT = "events.json"
KST = timezone(timedelta(hours=9))
API = "https://opendart.fss.or.kr/api"

# 자사주 취득 '목적' 필터 — '노이즈만 제외' 방식(개수 캡 대신).
# 진짜 주주환원 신호(소각·주가안정·주주가치 등)가 하나라도 있으면 포함,
# 절차성(임직원 보상·교환사채·상환)만 있으면 제외. 목적이 섞이면 진짜 신호 우선(포함).
TREASURY_INCLUDE = ("소각", "안정", "주주가치", "주주환원", "기업가치", "저평가")
TREASURY_EXCLUDE = ("임직원", "종업원", "상여", "스톡옵션", "스톡그랜트", "RSU", "우리사주", "교환사채", "상환", "성과급", "합병")


def _mask(url):
    """로그·예외에 남길 URL에서 인증키 값을 가린다. public repo라 액션 로그도 공개이므로 필수."""
    return url.split("crtfc_key=")[0] + "crtfc_key=***"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "maegi-data/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except Exception as e:
        # 원본 예외/트레이스백에 키가 실린 URL이 새지 않도록 마스킹해 다시 던진다(from None으로 체인 차단).
        raise RuntimeError(f"DART 요청 실패: {_mask(url)} ({type(e).__name__})") from None


def corp_map():
    """상장사 stock_code -> corp_code (DART 고유번호). majorstock 조회에 corp_code가 필요하다."""
    raw = _get(f"{API}/corpCode.xml?crtfc_key={KEY}")
    xml = zipfile.ZipFile(io.BytesIO(raw)).read(zipfile.ZipFile(io.BytesIO(raw)).namelist()[0])
    root = ET.fromstring(xml)
    m = {}
    for e in root.iter("list"):
        sc = (e.findtext("stock_code") or "").strip()
        cc = (e.findtext("corp_code") or "").strip()
        if sc and cc:
            m[sc] = cc
    return m


def nps_filings(bgn, end):
    """국민연금공단이 낸 대량보유 지분공시(pblntf_ty=D) 목록을 페이지네이션으로 전부 수집."""
    out, page = [], 1
    while True:
        url = (f"{API}/list.json?crtfc_key={KEY}&bgn_de={bgn}&end_de={end}"
               f"&pblntf_ty=D&page_count=100&page_no={page}")
        d = json.loads(_get(url))
        if d.get("status") != "000":
            break
        for x in d.get("list", []):
            if x.get("flr_nm") == "국민연금공단" and "대량보유" in x.get("report_nm", ""):
                out.append(x)
        if page >= int(d.get("total_page", 1)):
            break
        page += 1
    return out


def majorstock_by_rcept(corp_code):
    """corp_code의 대량보유 보고 이력 → {접수번호: 행}. 같은 공시(rcept_no)로 비율·증감을 매칭한다."""
    d = json.loads(_get(f"{API}/majorstock.json?crtfc_key={KEY}&corp_code={corp_code}"))
    if d.get("status") != "000":
        return {}
    return {r.get("rcept_no"): r for r in d.get("list", [])}


def treasury_filings(bgn, end):
    """자기주식 취득/처분 '결정' 공시(신탁계약 제외 — 신탁은 실제 매입과 다른 틀). (공시행, '취득'/'처분')."""
    out, page = [], 1
    while True:
        url = (f"{API}/list.json?crtfc_key={KEY}&bgn_de={bgn}&end_de={end}"
               f"&pblntf_ty=B&page_count=100&page_no={page}")
        d = json.loads(_get(url))
        if d.get("status") != "000":
            break
        for x in d.get("list", []):
            nm = x.get("report_nm", "")
            if "신탁" in nm:
                continue
            if "자기주식취득결정" in nm:
                out.append((x, "취득"))
            elif "자기주식처분결정" in nm:
                out.append((x, "처분"))
        if page >= int(d.get("total_page", 1)):
            break
        page += 1
    return out


def treasury_detail(corp_code, bgn, end):
    """corp_code 자기주식취득결정 상세 → {접수번호: (취득예정금액 원, 취득목적)}."""
    try:
        d = json.loads(_get(f"{API}/tsstkAqDecsn.json?crtfc_key={KEY}&corp_code={corp_code}&bgn_de={bgn}&end_de={end}"))
    except Exception:
        return {}
    if d.get("status") != "000":
        return {}
    out = {}
    for r in d.get("list", []):
        amt = _num(r.get("aqpln_prc_ostk"))  # 취득예정금액(보통주, 원)
        out[r.get("rcept_no")] = (int(amt) if amt else None, (r.get("aq_pp") or "").strip())
    return out


def treasury_dp_detail(corp_code, bgn, end):
    """corp_code 자기주식처분결정 상세 → {접수번호: (처분예정금액 원, 처분목적)}."""
    try:
        d = json.loads(_get(f"{API}/tsstkDpDecsn.json?crtfc_key={KEY}&corp_code={corp_code}&bgn_de={bgn}&end_de={end}"))
    except Exception:
        return {}
    if d.get("status") != "000":
        return {}
    out = {}
    for r in d.get("list", []):
        amt = _num(r.get("dppln_prc_ostk"))  # 처분예정금액(보통주, 원)
        out[r.get("rcept_no")] = (int(amt) if amt else None, (r.get("dp_pp") or "").strip())
    return out


def shares_total(corp_code):
    """발행 보통주 총수 — 시가총액(현재가×주식수) 계산용. 최근 보고서부터 시도, 못 구하면 None."""
    for yr, rc in (("2025", "11011"), ("2025", "11014"), ("2025", "11012"), ("2024", "11011")):
        try:
            d = json.loads(_get(f"{API}/stockTotqySttus.json?crtfc_key={KEY}&corp_code={corp_code}&bsns_year={yr}&reprt_code={rc}"))
        except Exception:
            continue
        if d.get("status") != "000":
            continue
        for r in d.get("list", []):
            if (r.get("se") or "").strip() in ("보통주", "합계"):
                n = _num(r.get("istc_totqy"))
                if n and n > 0:
                    return int(n)
    return None


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _iso(yyyymmdd):
    s = (yyyymmdd or "").strip()
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def market_symbols():
    """stock_master.json에서 종목코드 -> 야후 심볼(코스피 .KS / 코스닥 .KQ). 없으면 빈 dict."""
    try:
        d = json.load(open("stock_master.json", encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for s in d.get("stocks", []):
        code = (s.get("code") or "").strip()
        if not code:
            continue
        out[code] = f"{code}.KS" if "KOSPI" in (s.get("market") or "") else f"{code}.KQ"
    return out


def yahoo_closes(symbol):
    """야후 일봉 차트 → {날짜(YYYY-MM-DD): 종가}. 실패(거래정지·상폐·네트워크)면 빈 dict."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=6mo&interval=1d"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        res = d["chart"]["result"][0]
        ts, close = res["timestamp"], res["indicators"]["quote"][0]["close"]
        out = {}
        for t, c in zip(ts, close):
            if c is None:
                continue
            out[datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")] = c
        return out
    except Exception:
        return {}


def price_move(closes, filing_iso):
    """공시일(휴장이면 직전 거래일) 종가·최근 종가·변화율(%)·기준일. 못 구하면 (None,)*4."""
    if not closes:
        return None, None, None, None
    days = sorted(closes)
    prev = [d for d in days if d <= filing_iso]
    if not prev:
        return None, None, None, None
    base = closes[prev[-1]]
    last = closes[days[-1]]
    if not base:
        return None, None, None, None
    return round(base), round(last), round((last - base) / base * 100, 2), days[-1]


def build_events():
    today = datetime.now(KST).date()
    bgn = (today - timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    s2c = corp_map()
    sym_map = market_symbols()
    filings = nps_filings(bgn, end)

    cache = {}          # corp_code -> majorstock(rcept_no->row). 회사당 1번만 호출.
    price_cache = {}    # code -> 야후 {날짜:종가}. 종목당 1번만 호출.
    events = []
    for f in filings:
        sc = (f.get("stock_code") or "").strip()
        cc = s2c.get(sc)
        if not cc:
            continue
        if cc not in cache:
            cache[cc] = majorstock_by_rcept(cc)
        row = cache[cc].get(f.get("rcept_no"))
        ratio = _num(row.get("stkrt")) if row else None
        chg = _num(row.get("stkrt_irds")) if row else None
        direction = None
        if chg is not None:
            direction = "늘림" if chg > 0 else ("줄임" if chg < 0 else "유지")

        # 공시일 종가 vs 최근 종가 — '담은 시점부터 지금까지' 종가·등락 (야후, 못 구하면 null).
        price_base, price_last, price_change, price_as_of = None, None, None, None
        sym = sym_map.get(sc)
        if sym:
            if sc not in price_cache:
                price_cache[sc] = yahoo_closes(sym)
            price_base, price_last, price_change, price_as_of = price_move(price_cache[sc], _iso(f.get("rcept_dt")))

        events.append({
            "date": _iso(f.get("rcept_dt")),
            "code": sc,
            "name": f.get("corp_name"),
            "source": "국민연금",       # 표준 카드: 주체
            "type": "대량보유",
            "direction": direction,      # 늘림/줄임/유지
            "ratio": ratio,              # 보유비율 %
            "ratioChange": chg,          # 증감 %p
            "amount": None,              # (자사주 렌즈 전용 — 국민연금은 null)
            "purpose": None,
            "capRatio": None,
            "priceBase": price_base,      # 공시일(휴장이면 직전 거래일) 종가 (원, null 가능)
            "priceLast": price_last,      # 최근 거래일 종가 (원)
            "priceChange": price_change,  # 공시일→최근 변화율 % (null 가능)
            "priceAsOf": price_as_of,     # 최근 종가 기준일
            "rceptNo": f.get("rcept_no"),
        })

    # ── 자사주 렌즈 ── 회사가 자기 주식 취득(담음)/처분(던짐) 결정 공시.
    tcache = {}   # corp_code -> 취득 상세 {rcept_no: (금액, 목적)}
    dcache = {}   # corp_code -> 처분 상세 {rcept_no: (금액, 목적)}
    scache = {}   # corp_code -> 발행주식 총수(시총 계산용)
    for x, kind in treasury_filings(bgn, end):
        sc = (x.get("stock_code") or "").strip()
        if len(sc) != 6:
            continue
        cc = (x.get("corp_code") or "").strip()
        amount, purpose = None, None
        if cc:
            if kind == "취득":
                if cc not in tcache:
                    tcache[cc] = treasury_detail(cc, bgn, end)
                det = tcache[cc].get(x.get("rcept_no"))
            else:  # 처분
                if cc not in dcache:
                    dcache[cc] = treasury_dp_detail(cc, bgn, end)
                det = dcache[cc].get(x.get("rcept_no"))
            if det:
                amount, purpose = det

        # 목적 필터(취득·처분 대칭) — 진짜 주주환원 목적이 하나라도 있으면 포함,
        # 절차성(임직원 RSU·스톡옵션·교환 대응 등)만 있으면 제외. 섞이면 진짜 목적 우선.
        if purpose:
            has_signal = any(k in purpose for k in TREASURY_INCLUDE)
            has_noise = any(k in purpose for k in TREASURY_EXCLUDE)
            if has_noise and not has_signal:
                continue

        p_base, p_last, p_chg, p_asof = None, None, None, None
        sym = sym_map.get(sc)
        if sym:
            if sc not in price_cache:
                price_cache[sc] = yahoo_closes(sym)
            p_base, p_last, p_chg, p_asof = price_move(price_cache[sc], _iso(x.get("rcept_dt")))

        # 시총 대비 금액 비율 — 공시일 종가 × 발행주식수 = 공시 시점 시총. 못 구하면 null(억원만).
        cap_ratio = None
        if amount and p_base and cc:
            if cc not in scache:
                scache[cc] = shares_total(cc)
            sh = scache[cc]
            if sh:
                cap_ratio = round(amount / (p_base * sh) * 100, 2)

        # 크기 필터(취득·처분 대칭) — 시총 2%+ 이거나, 시총% 못 구했으면 금액 50억+. (아빠 확정)
        # 문턱 못 넘거나 금액조차 없는 소소한 건 노이즈로 뺀다.
        big = (cap_ratio is not None and cap_ratio >= 2.0) or \
              (cap_ratio is None and amount is not None and amount >= 5_000_000_000)
        if not big:
            continue

        events.append({
            "date": _iso(x.get("rcept_dt")),
            "code": sc,
            "name": x.get("corp_name"),
            "source": "자사주",
            "type": f"자사주{kind}",       # 자사주취득 / 자사주처분
            "direction": "늘림" if kind == "취득" else "줄임",
            "ratio": None,
            "ratioChange": None,
            "amount": amount,              # 취득예정 금액(원) — 처분은 null
            "purpose": purpose or None,    # 취득 목적(소각/임직원보상 등)
            "capRatio": cap_ratio,         # 시총 대비 취득 % — 못 구하면 null
            "priceBase": p_base,
            "priceLast": p_last,
            "priceChange": p_chg,
            "priceAsOf": p_asof,
            "rceptNo": x.get("rcept_no"),
        })

    # 최신 공시 우선, 같은 날은 종목코드 순.
    events.sort(key=lambda e: (e["date"], e["code"]), reverse=True)
    return events


def main():
    if not KEY:
        print("DART_KEY 환경변수가 없습니다 (GitHub Secret에 등록 필요).", file=sys.stderr)
        sys.exit(1)

    events = build_events()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    payload = {"version": today, "count": len(events), "events": events}
    new = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)

    # events 내용이 바뀐 날만 저장(version 줄은 비교에서 빼서 '날짜만 바뀐' 무의미 커밋 방지).
    old_events = None
    if os.path.exists(OUT):
        try:
            old_events = json.dumps(json.load(open(OUT, encoding="utf-8")).get("events"),
                                    ensure_ascii=False, sort_keys=True)
        except (ValueError, OSError):
            old_events = None
    if old_events == json.dumps(events, ensure_ascii=False, sort_keys=True):
        print(f"변경 없음 (이벤트 {len(events)}건)")
        return

    with open(OUT, "w", encoding="utf-8") as fp:
        fp.write(new)
    print(f"events.json 갱신: {len(events)}건 ({today})")


if __name__ == "__main__":
    main()
