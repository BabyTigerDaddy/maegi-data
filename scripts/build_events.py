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


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _iso(yyyymmdd):
    s = (yyyymmdd or "").strip()
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def build_events():
    today = datetime.now(KST).date()
    bgn = (today - timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    s2c = corp_map()
    filings = nps_filings(bgn, end)

    cache = {}          # corp_code -> majorstock(rcept_no->row). 회사당 1번만 호출.
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
        events.append({
            "date": _iso(f.get("rcept_dt")),
            "code": sc,
            "name": f.get("corp_name"),
            "source": "국민연금",       # 표준 카드: 주체
            "type": "대량보유",
            "direction": direction,      # 늘림/줄임/유지
            "ratio": ratio,              # 보유비율 %
            "ratioChange": chg,          # 증감 %p
            "rceptNo": f.get("rcept_no"),
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
