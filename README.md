# Bybit Futures Bot

Bybit USDT 무기한 선물을 대상으로 한 Python 자동매매 봇입니다. 장세를 분류한 뒤 전략과 레버리지·포지션 크기를 정하고, 여러 겹의 리스크 가드를 거쳐서만 주문을 냅니다.

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![pandas](https://img.shields.io/badge/pandas-2.2-150458?logo=pandas&logoColor=white)
![pydantic](https://img.shields.io/badge/pydantic--settings-2.6-E92063)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)
![pytest](https://img.shields.io/badge/pytest-343%20passed-0A9EDC?logo=pytest&logoColor=white)
![Fly.io](https://img.shields.io/badge/Fly.io-Docker-8B5CF6)

> **투자 조언이 아닙니다.** 개인 학습·포트폴리오용 코드입니다. 레버리지 선물 거래는 원금 전액을 잃을 수 있으며, 이 코드는 수익을 보장하지 않습니다. 실제 자금으로 실행할 때의 책임은 실행한 사람에게 있습니다.

**상태:** 개발 중단. 이후 전략을 새로 짠 후속 저장소 `bybit-futures-bot-v2` 로 이어졌습니다.

---

## 무엇을 해결하나

수동으로 선물을 매매할 때 반복되던 문제를 코드로 막는 것이 목표였습니다.

- **장세와 맞지 않는 진입** — 하락장에서 롱을 잡는 식의 역추세 진입. → 장세(Regime)를 먼저 판정하고 허용 방향을 잠급니다.
- **감에 의존한 레버리지·수량** — 손절 거리와 계좌 상태를 무시한 사이징. → 손절 거리에서 레버리지를 역산하고 여러 감쇠 요인을 곱합니다.
- **수수료에 잠식되는 스캘핑** — 기대 이익이 왕복 수수료보다 작은 거래. → 진입 전에 수수료 손익분기를 검사해 거부합니다.
- **손실이 커질 때 멈추지 못함** — 낙폭 구간별로 비중 축소 → 신규 진입 중지 → 전면 중지로 단계적으로 대응합니다.

## 아키텍처

```
APScheduler (KST)
 └─ TradingBot.run_cycle()                         src/main.py
     ├─ MacroRegimeDetector  BTC 일봉 EMA50/200·ADX·ROC → BULL / BEAR·SIDEWAYS
     ├─ UniverseManager      거래대금 상위 N개 심볼 주기적 갱신
     ├─ STRATEGY_MODE 분기
     │    trend → TrendEMAStrategy   (장세별 방향 잠금)
     │    scalp → ScalpingStrategy   (EMA 리본 + VWAP + RSI)
     │    swing → ICT + SignalGenerator + SignalCombiner
     │    auto  → RegimeRouter       (7개 장세 → 전략 자동 선택)
     ├─ QualityFilter        ATR·Efficiency Ratio·Hurst 지수로 저품질 신호 제거
     ├─ RiskGuards           낙폭·스프레드·변동성 → ALLOW / REDUCE / PAUSE / HALT
     ├─ RiskEngine           레버리지·수량·손절/익절·청산가 검증
     ├─ OrderManager         PostOnly 지정가 우선, 미체결 시 재호가 → 시장가 폴백
     ├─ StateMachine         포지션 상태·부분 청산·시간 청산, 거래소 포지션과 동기화
     └─ Database (SQLite) + Slack 알림 / Socket Mode 명령어
```

| 디렉터리 | 역할 |
|---|---|
| `src/strategy/` | 지표, 장세 분류(`regime.py`, `macro_regime.py`, `regime_router.py`), 전략 4종, 신호 품질 필터 |
| `src/risk/` | `engine.py`(사이징·레버리지), `guards.py`(낙폭 가드), `correlation.py`, `profit_protector.py` |
| `src/execution/` | 주문 관리, 포지션 상태 머신 |
| `src/backtest/` | 봉 단위 백테스트 엔진, 데이터 로더, 합성 데이터, 리포트·품질 게이트 |
| `src/api/` | Bybit v5 클라이언트(pybit), Slack Webhook, Slack Socket Mode 봇 |
| `src/db/`, `src/reporting/` | 거래·자산 곡선·일별 통계 저장, 일일 브리핑 |

## 기술적으로 집중한 문제

### 1. 장세에 따라 전략과 방향을 바꾸기 — Dynamic Regime Router
- **문제:** 한 가지 전략을 모든 장에 쓰면 횡보장에서는 추세 전략이, 추세장에서는 역추세 스캘핑이 손실을 냅니다. 장세 판정이 경계에서 자주 뒤집히면(휩소) 더 나빠집니다.
- **해결:** `src/strategy/regime_router.py` 가 EMA21/50/200 배열, ADX, ATR 백분위로 7개 장세(STRONG_BULL·BULL·SIDEWAYS·BEAR·STRONG_BEAR·VOLATILE·TRANSITION)를 분류하고 장세별로 전략·허용 방향·트레이드 모드·리스크 배율을 정합니다. 같은 계열 안의 전환(BULL↔STRONG_BULL)은 즉시, 다른 계열로의 전환은 여러 번 연속 확인된 뒤에만 확정하고, 확정 전에는 TRANSITION 으로 진입을 쉽니다. 상위에는 BTC 일봉 기반 매크로 레짐이 있어 BEAR·SIDEWAYS 에서는 거래 심볼을 제한합니다.
- **결과:** `STRATEGY_MODE` 한 줄로 trend / scalp / swing / auto 를 바꿀 수 있고, 라우팅 로직은 `tests/test_regime_router.py`, `tests/test_macro_regime.py` 로 검증합니다.

### 2. 손절 거리에서 레버리지를 역산하는 사이징
- **문제:** 레버리지를 고정값으로 두면 변동성이 큰 날에는 과하게, 작은 날에는 부족하게 노출됩니다.
- **해결:** `src/risk/engine.py` 는 `레버리지 = 거래당 리스크% / 손절 거리%` 로 기본값을 구한 뒤 변동성 백분위·스프레드·펀딩비·신호 강도·계좌 상태(낙폭, 연속 손실)를 감쇠 계수로 곱합니다. 손절 거리에는 모드별 최소 하한(Min Stop Floor)을 두어 노이즈 손절을 줄이고, 수량은 리스크 기반 수량·변동성 타기팅 수량·(거래 표본이 충분할 때) 켈리 기반 수량 중 가장 작은 값으로 제한합니다. 마지막으로 청산가가 손절 거리의 3배 이상 떨어져 있지 않으면 진입을 거부합니다.
- **결과:** 신호가 강해도 계좌가 낙폭 구간에 있으면 레버리지 상한이 자동으로 낮아집니다(낙폭 12%·20% 초과 시 상한 축소).

### 3. 수수료를 진입 판단에 포함
- **문제:** 3분봉 스캘핑에서는 Taker 왕복 수수료와 슬리피지가 기대 이익을 넘는 경우가 많습니다.
- **해결:** 진입 전 첫 익절 목표까지의 예상 이동폭을 `왕복 수수료 + 슬리피지` 의 2.5~3배와 비교해 부족하면 거래하지 않습니다. 주문은 기본적으로 PostOnly 지정가(Maker)로 내고, 즉시 체결될 상황이라 취소되거나 시간 안에 체결되지 않으면 호가를 갱신해 재시도한 뒤 시장가로 폴백합니다(`src/execution/order_manager.py`). 재시도마다 새 client order id 를 써서 Bybit 의 ID 중복 오류를 피합니다.

### 4. 단계적 낙폭 가드
- **문제:** 손실 한도에 닿자마자 거래를 완전히 멈추면 회복 기회가 없고, 멈추지 않으면 파산 위험이 있습니다.
- **해결:** `src/risk/guards.py` 는 최고 자산 대비 낙폭에 따라 비중을 비례 축소(REDUCE) → 신규 진입 중지(PAUSE) → 전 포지션 청산·정지(HALT)로 넘어갑니다. 스프레드가 1%를 넘거나 변동성 백분위가 극단적이면 진입을 멈추거나 줄입니다. 여러 가드 결과 중 가장 엄격한 것을 적용합니다.

### 5. 실거래 잠금 — 백테스트 품질 게이트
- **문제:** 검증되지 않은 설정이 실수로 실거래에 들어가는 것.
- **해결:** `TRADING_MODE` 는 `dry-run` / `paper` / `live` 세 가지이며, `live` 는 `QUALITY_GATE_PASSED=true` 일 때만 시작됩니다(`Settings.check_live_allowed`). 백테스트 엔진(`src/backtest/engine.py`)은 Taker/Maker 수수료, 슬리피지, 8시간 펀딩 비용, 레버리지 손익을 봉 단위로 반영하고, 리포트 단계의 `QualityGate` 가 Sharpe·MDD·승률·Profit Factor·최소 거래 수 기준으로 통과 여부를 출력합니다.

## 품질 / 테스트

- `pytest` 13개 파일, **343개 테스트 통과** (Python 3.11 에서 실행해 확인).
- 대상: 리스크 엔진, 낙폭 가드, 주문 관리, 신호·필터, ICT 분석, 장세 라우터, 매크로 레짐, 스캘핑·추세 전략, 수익 보호 로직.
- GitHub Actions: 백테스트 리포트(수동 실행), Fly.io 배포(수동 실행), 페이퍼 트레이딩 사이클 워크플로가 있습니다.

## 실행 방법

```bash
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # 기본값은 TRADING_MODE=dry-run, BYBIT_TESTNET=true

# 테스트
TRADING_MODE=dry-run python -m pytest tests/ -q

# 백테스트 (Bybit 공개 시세 다운로드)
python -m src.backtest.engine --symbols BTCUSDT ETHUSDT --years 1 --interval 15

# 합성 데이터 백테스트 (네트워크 불필요)
python scripts/backtest_btc.py --days 365 --seed 42

# 봇 실행
python -m src.main
```

Docker 이미지(`Dockerfile`, `docker-compose.yml`)와 Fly.io 설정(`fly.toml`), Oracle Cloud VM 용 systemd 유닛(`bot.service`, `scripts/setup_oracle.sh`)이 함께 있습니다. 배포 절차는 `DEPLOYMENT.md`, 비밀값 관리 원칙은 `SECURITY.md` 를 참고하세요.

## 설정

모든 설정은 환경변수(`.env`)로 주입되며 `src/config.py` 의 pydantic `Settings` 가 검증합니다. 주요 항목은 `.env.example` 에 설명과 함께 있습니다.

| 변수 | 설명 |
|---|---|
| `TRADING_MODE` | `dry-run` / `paper` / `live` |
| `STRATEGY_MODE` | `swing`(기본) / `scalp` / `trend` / `auto` |
| `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_TESTNET` | Bybit API (출금 권한은 주지 마세요) |
| `MAX_RISK_PER_TRADE_PCT`, `MAX_LEVERAGE_CAP`, `MAX_DRAWDOWN_PCT` 등 | 리스크 한도 |
| `QUALITY_GATE_*` | 백테스트 품질 기준, `QUALITY_GATE_PASSED` 는 실거래 잠금 해제 |
| `SLACK_WEBHOOK_URL`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | 알림 / Socket Mode 명령어(`/dashboard`, `/positions`, `/pnl`, `/status`) |

## 개발 방식

Claude Code 와 함께 설계·구현했습니다 (커밋의 `Co-Authored-By` 참고).
