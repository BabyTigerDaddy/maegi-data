# maegi-data

'매기' 앱이 쓰는 공개 데이터. KRX 상장법인목록(공공데이터)에서 만든 종목 마스터를 매일 자동 갱신한다. 키·인증 없음.

- `stock_master.json` — 앱이 raw 주소로 받아가는 종목 마스터(코드·이름·시장·업종).
- `scripts/build_stock_master.py` — 생성 스크립트. 게임사는 코드로 콕 집어 '게임' 섹터로 분리(KRX 표준분류가 IT에 섞는 것 보정).
- `.github/workflows/build-stock-master.yml` — 매일 08:00(KST) 자동 실행.

앱은 이 파일을 아래 raw 주소로 받는다:

```
https://raw.githubusercontent.com/BabyTigerDaddy/maegi-data/main/stock_master.json
```
