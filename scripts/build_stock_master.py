#!/usr/bin/env python3
"""
KRX KIND 상장법인목록에서 전종목(코스피+코스닥) 코드·이름을 받아 stock_master.json 생성.

- 키·인증 없음(거래소 공식 다운로드). 매일 GitHub Actions가 실행.
- 목록이 실제로 바뀐 날만 파일 갱신(version=그날 날짜) → 앱이 괜히 매일 재다운 안 함.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&marketType="
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://kind.krx.co.kr/corpgeneral/corpList.do",
}
MARKETS = [("stockMkt", "KOSPI"), ("kosdaqMkt", "KOSDAQ")]
OUT = "stock_master.json"

# KRX 세부 업종(표준산업분류)은 159종이라 도넛에 그대로 못 쓴다 → 큰 섹터로 묶는다.
# 위에서부터 첫 매칭(구체적인 것 먼저). 종목이 아니라 '업종 분류' 기준이라 종목이 바뀌어도
# 손댈 일이 거의 없고, 뭔가 '기타'로 잘못 빠지면 여기 키워드 한 줄만 고치면 다음 Action에 반영된다.
SECTOR_RULES = [
    (("반도체",), "반도체"),
    (("전지",), "2차전지"),
    (("의약", "의료", "바이오", "연구개발"), "바이오·제약"),
    (("자동차",), "자동차"),
    (("도매", "소매", "중개", "상품 종합"), "유통"),
    (("소프트웨어", "프로그래밍", "자료처리", "포털", "정보 서비스", "컴퓨터", "통신"), "IT·통신"),
    (("전자부품", "방송 장비", "영상", "음향", "전동기", "가정용 기기", "가전",
      "광학", "측정", "정밀기기", "조명", "전구", "전기장비", "기록매체", "마그네틱"), "전기·전자"),
    (("금융", "은행", "보험", "신탁", "연금", "저축"), "금융"),
    (("철강", "비철금속", "금속", "주조"), "철강·금속"),
    (("화학", "고무", "플라스틱", "석유", "비료", "섬유 제조", "유리",
      "요업", "시멘트", "비금속"), "화학·소재"),
    (("기계", "선박", "보트", "항공기", "우주선", "무기", "총포탄", "엔진",
      "운송장비", "구조용 금속"), "기계·중공업"),
    (("건설", "공사", "건축", "부동산", "토목", "조성"), "건설·부동산"),
    (("식품", "음료", "곡물", "육류", "수산", "낙농", "제과", "담배", "사료",
      "빵", "떡", "유지", "과실", "도축", "알코올", "음식점", "도시락", "조리"), "식품·음료"),
    (("섬유", "의복", "신발", "가죽", "가방", "봉제", "직물", "방적", "편조", "액세서리"), "섬유·의류"),
    (("영화", "방송", "출판", "오디오", "광고", "예술", "녹음", "오락",
      "스포츠", "인쇄", "디자인"), "엔터·미디어"),
    (("운송", "여행", "숙박", "운수"), "운송·여행"),
]


# 게임사 — KRX 표준산업분류는 게임 개발사를 '소프트웨어'로 묶어 IT·통신에 섞는다
# (네이버·카카오 같은 인터넷 기업과 구분되지 않음). 그래서 게임 주력사만 종목코드로
# 콕 집어 '게임' 섹터로 뺀다. 코드 기준이라 사명이 바뀌어도(예: 엔씨소프트→KRX 표기 'NC') 안전.
# 새 게임주가 상장/편입되면 아래에 "코드": "회사명" 한 줄만 추가하면 다음 Action에 반영된다.
GAME_CODES = {
    "036570": "엔씨소프트",
    "251270": "넷마블",
    "259960": "크래프톤",
    "263750": "펄어비스",
    "293490": "카카오게임즈",
    "078340": "컴투스",
    "063080": "컴투스홀딩스",
    "112040": "위메이드",
    "101730": "위메이드맥스",
    "123420": "위메이드플레이",
    "069080": "웹젠",
    "095660": "네오위즈",
    "042420": "네오위즈홀딩스",
    "192080": "더블유게임즈",
    "225570": "넥슨게임즈",
    "194480": "데브시스터즈",
    "462870": "시프트업",
    "067000": "조이시티",
    "052790": "액토즈소프트",
    "058630": "엠게임",
    "023770": "플레이위드",
    "047080": "한빛소프트",
    "030350": "드래곤플라이",
    "201490": "미투온",
    "331520": "밸로프",
    "217270": "넵튠",
    "348030": "모비릭스",
    "276040": "스코넥",
    "208640": "썸에이지",
}


def to_sector(code, sic):
    if code in GAME_CODES:  # 게임사는 코드로 먼저 잡아 '게임'으로(표준분류보다 우선)
        return "게임"
    for kws, sector in SECTOR_RULES:
        if any(k in sic for k in kws):
            return sector
    return "기타"


def fetch(market_type):
    req = urllib.request.Request(BASE + market_type, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("euc-kr", "ignore")


def parse(html, market):
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        # KRX 상장법인목록 컬럼: 회사명(0) 시장(1) 종목코드(2) 업종(3) 주요제품(4) 상장일(5) ...
        if len(tds) >= 4:
            name = re.sub(r"<[^>]+>", "", tds[0]).strip()
            code = re.sub(r"\D", "", tds[2]).strip()
            sic = re.sub(r"<[^>]+>", "", tds[3]).strip()  # KRX 세부 업종(표준산업분류)
            if len(code) == 6 and name and name != "회사명":
                # 세부 업종을 큰 섹터로 묶어 저장. 이 sector가 앱 자산배분에서 업종별로 묶인다.
                rows.append({"code": code, "name": name, "market": market, "sector": to_sector(code, sic)})
    return rows


def main():
    merged = {}
    for mtype, market in MARKETS:
        for s in parse(fetch(mtype), market):
            merged[s["code"]] = s
    stocks = sorted(merged.values(), key=lambda x: x["name"])

    if len(stocks) < 1000:  # 파싱 깨짐 방어 — 비정상이면 기존 유지
        print(f"ABORT: too few stocks ({len(stocks)}) - keep existing")
        sys.exit(1)

    old = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    if old.get("stocks") == stocks:
        print("no change - skip")
        return

    kst = timezone(timedelta(hours=9))
    version = datetime.now(kst).strftime("%Y-%m-%d")
    out = {"version": version, "count": len(stocks), "stocks": stocks}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"updated: {len(stocks)} stocks, version {version}")


if __name__ == "__main__":
    main()
