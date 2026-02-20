# Bybit Futures Trading Bot

Python으로 구현한 Bybit USDT 무기한 선물 자동매매 봇입니다.
시장 국면(레짐)을 실시간으로 분류하고, 15가지 기술 지표 기반 시그널을 가중 합산해 진입/청산 결정을 내립니다.
Fly.io에 배포해 24/7 운영 중이며, Slack으로 실시간 알림 및 명령어 조회를 지원합니다.

> **주의**: 선물 거래는 레버리지로 인해 원금 전액 손실 위험이 있습니다.
> 이 소프트웨어는 수익을 보장하지 않으며, 사용에 따른 투자 손실의 책임은 사용자 본인에게 있습니다.

---

## 목차

1. [아키텍처 개요](#1-아키텍처-개요)
2. [시그널 & 진입 로직](#2-시그널--진입-로직)
3. [익절 & 손절 기준](#3-익절--손절-기준)
4. [리스크 관리](#4-리스크-관리)
5. [포지션 사이징](#5-포지션-사이징)
6. [백테스트](#6-백테스트)
7. [Slack 연동](#7-slack-연동)
8. [프로젝트 구조](#8-프로젝트-구조)
9. [설치 및 실행](#9-설치-및-실행)
10. [Fly.io 배포](#10-flyio-배포)
11. [주요 설정 파라미터](#11-주요-설정-파라미터)
12. [기술 스택](#12-기술-스택)

---

## 1. 아키텍처 개요

매 5분마다 아래 파이프라인이 실행됩니다.

```
[Bybit REST API] OHLCV + 오더북 + 펀딩비 + OI 수집
        │
        ▼
[Universe Filter]   24h 거래량 기준 상위 코인 자동 선별 (4시간마다 갱신)
        │
        ▼
[Regime Detector]   시장 국면 분류: TRENDING_UP / TRENDING_DOWN / RANGING / HIGH_VOLATILITY
        │
        ▼
[Signal Generator]  15가지 기술 지표 → 각각 방향(+1/0/-1) + 확신도(0~1) 반환
        │
        ▼
[Signal Combiner]   레짐별 가중 평균 → 최종 방향 + 강도 (0~1)
        │
        ▼
[Quality Filters]   Kaufman ER / Hurst Exponent / ATR 필터 — 저품질 시그널 제거
        │
        ▼
[Risk Engine]       포지션 크기, 레버리지, SL/TP 가격 계산
        │
        ▼
[Risk Guards]       일/주간 손실 한도 · 낙폭 · 변동성 스파이크 최종 점검
        │
        ▼
[Order Manager]     지정가(PostOnly) 주문 → 미체결 시 시장가 자동 전환, 멱등성 보장
        │
        ▼
[State Machine]     포지션 상태 관리 (FLAT / LONG / SHORT), 부분 익절, 트레일링 스탑
        │
        ▼
[SQLite DB + Slack] 거래 기록 저장, 실시간 알림 발송
```

---

## 2. 시그널 & 진입 로직

### Step 1 — 시장 국면(Regime) 분류

진입 전 현재 시장이 어떤 상태인지 먼저 파악합니다.
국면에 따라 어떤 지표에 더 높은 가중치를 줄지 결정됩니다.

| 국면 | 판단 조건 | 전략 방향 |
|------|-----------|-----------|
| TRENDING_UP | ADX > 25, EMA 정배열(9>21>50), 가격 > VWAP | 추세 추종 Long 우선 |
| TRENDING_DOWN | ADX > 25, EMA 역배열, 가격 < VWAP | 추세 추종 Short 우선 |
| RANGING | ADX < 20 | 지지/저항 평균 회귀 |
| HIGH_VOLATILITY | 변동성 백분위 > 80% | 포지션 축소, 보수적 운용 |

### Step 2 — 시그널 생성 (15가지 지표)

각 지표는 독립적으로 **방향**과 **확신도**를 계산합니다.

| 카테고리 | 지표 | Long 조건 예시 |
|----------|------|----------------|
| **trend** | EMA (9 / 21 / 50) | 단기 EMA가 중기 EMA를 상향 돌파 |
| **trend** | MACD (12/26/9) | 히스토그램 양전환 |
| **trend** | Hull MA | 기울기 상향 |
| **reversion** | RSI (14) | RSI < 30 과매도 구간 |
| **reversion** | Bollinger Band | 가격이 하단 밴드 하회 |
| **reversion** | StochRSI | K선이 D선을 20 이하에서 상향 교차 |
| **reversion** | Williams %R | -80 이하 과매도 |
| **reversion** | RSI Divergence | 가격 신저가 + RSI 상대적 고저 (강세 다이버전스) |
| **volume** | CMF (Chaikin Money Flow) | CMF > 0.1 매수 압력 |
| **volume** | OBV | OBV 상승 추세 + 가격 방향 일치 |
| **volatility** | ATR / Keltner | 변동성 확대 + 방향 확인 |
| **squeeze** | Squeeze Momentum | BB가 KC 안에 갇혔다가 이탈 (존 카터 스퀴즈) |
| **vwap** | VWAP | 가격이 VWAP 위로 복귀 |
| **orderbook** | 호가창 불균형 | 매수 호가 우세 |
| **ict** | ICT 구조 분석 | BOS(구조 이탈) · CHoCH(추세 전환) · FVG(공정가치 갭) · 유동성 스윕 |

### Step 3 — 레짐별 가중 합산

국면에 따라 카테고리별 가중치가 달라집니다.

```
추세장:  trend(27%) + ict(30%) + volume(12%) + squeeze(8%) + vwap(8%) + momentum(6%) + ...
횡보장:  reversion(25%) + ict(22%) + volume(18%) + vwap(10%) + squeeze(6%) + ...
고변동: 전반적 가중치 축소, 역추세 신호 억제
```

최종 합산 강도가 `SIGNAL_ENTRY_THRESHOLD`(기본 **0.55**) 이상이면 진입 신호로 확정됩니다.

### Step 4 — 품질 필터

신호 강도가 임계값을 넘어도 아래 3가지 필터를 통과해야 실제 주문이 나갑니다.

| 필터 | 조건 | 목적 |
|------|------|------|
| Kaufman Efficiency Ratio | ER > 0.15 | 방향성 없는 횡보장 거래 방지 |
| Hurst Exponent | H > 0.45 | 평균회귀 구간에서 모멘텀 진입 방지 |
| Min ATR | ATR > 가격의 0.04% | 변동성이 극히 낮은 "죽은 시장" 진입 방지 |

---

## 3. 익절 & 손절 기준

포지션 진입 후 아래 조건을 **매 5분마다 순서대로** 점검합니다.

### 트레이드 모드

진입 시 신호 강도와 추세 점수에 따라 두 가지 모드 중 하나로 진입합니다.

| 항목 | SCALP 모드 | TREND_RIDE 모드 |
|------|-----------|----------------|
| 손절 (SL) | ATR × 0.8 | ATR × 1.5 |
| 1차 익절 (TP1) | ATR × 1.0 | ATR × 2.0 |
| 2차 익절 (TP2) | ATR × 1.8 | ATR × 4.0 |
| 부분 청산 비율 | 60% (TP1 도달 시) | 50% (TP1 도달 시) |
| 최대 보유 시간 | 15봉 (75분) | 120봉 (10시간) |

### 청산 우선순위

```
1순위 │ 손절가(SL) 터치         → 전량 시장가 즉시 청산
2순위 │ 1차 익절가(TP1) 터치    → 부분 청산(60%), 손절 → 진입가로 이동(브레이크이븐)
3순위 │ 2차 익절가(TP2) 터치    → 잔여 전량 청산
4순위 │ 트레일링 스탑           → TP1 이후 고점 대비 ATR×1.5 되돌림 발생 시 청산
5순위 │ 레짐 역전               → 시장 국면이 반대 방향으로 전환 시 청산
6순위 │ 역방향 강시그널          → 반대 방향 신호 강도 > 0.40 시 청산
7순위 │ 최대 보유 시간 초과      → 시간 기반 강제 청산
```

### 구체적인 예시

**상황**: SCALP 모드, BTCUSDT Short 진입, ATR = 200 USDT, 진입가 = 67,000

```
진입:  $67,000  Short
SL:    $67,160  (ATR × 0.8 = 160 위) → 여기 터치 시 전량 손절
TP1:   $66,800  (ATR × 1.0 = 200 아래)
         → 포지션의 60% 청산 (수익 확정)
         → 남은 40%의 손절을 $67,000으로 이동 (리스크 제거)
TP2:   $66,640  (ATR × 1.8 = 360 아래)
         → 남은 40% 전량 청산
```

---

## 4. 리스크 관리

### 자동 가드 레이어

| 가드 | 조건 | 조치 |
|------|------|------|
| 일간 손실 한도 | 일일 손실 ≥ 5% | 당일 신규 진입 중단 |
| 주간 손실 한도 | 주간 손실 ≥ 10% | 당주 신규 진입 중단 |
| 최대 낙폭(MDD) ≥ 14% | 낙폭 임계 70% 도달 | 레버리지 자동 축소 |
| 최대 낙폭(MDD) ≥ 20% | 한도 초과 | 전체 포지션 강제 청산 + Slack 경보 |
| 변동성 스파이크 | 변동성 백분위 ≥ 97% | 포지션 축소 |
| 스프레드 이상 | 스프레드 ≥ 1% | 신규 진입 일시 중단 |

### 연속 손실 페널티

연속 손실이 누적될수록 포지션 크기가 자동으로 줄어듭니다.

| 연속 손실 | 포지션 크기 |
|-----------|------------|
| 0회 | 100% |
| 2회 | 약 70% |
| 4회 | 약 40% |
| 5회 이상 | 약 25% |

### 청산 감지

매 사이클마다 거래소의 실제 포지션과 로컬 상태를 비교합니다.
강제 청산(liquidation)이 발생하면 즉시 상태를 FLAT으로 초기화하고 Slack 경보를 발송합니다.

---

## 5. 포지션 사이징

거래당 최대 손실을 자본의 1%로 고정하는 리스크 기반 사이징을 사용합니다.

```
기본 레버리지 = 거래당 목표 손실% ÷ 손절 거리%

조정 계수 (각각 0~1 범위):
  × 변동성 계수   ← 현재 변동성이 높으면 줄임
  × 유동성 계수   ← 스프레드가 넓으면 줄임
  × 펀딩비 계수   ← 펀딩 비용이 크면 줄임
  × 시그널 강도   ← 확신도가 낮으면 줄임
  × 계좌 건강도   ← 낙폭/연속 손실이 크면 줄임

최종 레버리지 = min(조정값, MAX_LEVERAGE_CAP)
포지션 크기   = 자본 × 목표 리스크% ÷ (손절 거리% × 최종 레버리지)
```

추가로 **Quarter-Kelly 공식**(최적 배팅 크기의 25%)과
**AQR 변동성 타게팅**(연 15% 목표)으로 과도한 포지션을 이중으로 방지합니다.

---

## 6. 백테스트

실제 거래 환경과 동일한 로직으로 과거 성과를 검증합니다.

```bash
# 기간 지정 백테스트
python -m scripts.backtest --symbol BTCUSDT --start 2024-01-01 --end 2024-06-30

# 다중 심볼
python -m scripts.backtest --symbols BTCUSDT ETHUSDT SOLUSDT

# HTML 보고서 생성
python -m scripts.backtest --report
```

**백테스트 비용 설정**:
- 테이커 수수료 0.055%, 메이커 수수료 0.020%
- 슬리피지 0.02%, 평균 펀딩비 0.01% 반영

### Quality Gate — 라이브 전환 조건

아래 기준을 모두 통과해야 `TRADING_MODE=live` 전환이 허용됩니다.

| 지표 | 기준 |
|------|------|
| Sharpe Ratio | ≥ 1.0 |
| 최대 낙폭 (MDD) | ≤ 30% |
| 승률 | ≥ 45% |
| Profit Factor | ≥ 1.5 |
| 최소 거래 횟수 | ≥ 100회 |

---

## 7. Slack 연동

Slack Socket Mode를 통해 봇 상태를 실시간으로 조회할 수 있습니다.

| 명령어 | 내용 |
|--------|------|
| `/dashboard` | 자산, 일간/누적 PnL, 오픈 포지션 수 |
| `/positions` | 현재 포지션 목록 (진입가, 현재가, 레버리지, 미실현 손익) |
| `/pnl` | 오늘 / 이번 주 / 전체 PnL + 승률 + 오늘 거래 수 |
| `/status` | 봇 상태 (모드, 가동 시간, 마지막 사이클, 서킷브레이커 상태) |

**자동 알림 항목:**
- 포지션 진입 / 부분 익절(TP1) / 전량 청산(TP2/SL)
- 청산(liquidation) 감지
- 일간/주간 손실 한도 도달 경보
- 낙폭 임계 도달 경보
- 매일 오전 9시 일간 브리핑 (KST 기준)

---

## 8. 프로젝트 구조

```
bybit-futures-bot/
├── src/
│   ├── main.py                  # 메인 루프, APScheduler
│   ├── config.py                # 환경변수 → Pydantic Settings
│   ├── logger.py                # structlog 구조화 로깅
│   ├── api/
│   │   ├── bybit_client.py      # Bybit v5 REST API 래퍼 (서킷브레이커 포함)
│   │   ├── slack.py             # Slack Webhook 알림
│   │   └── slack_bot.py         # Slack Socket Mode 명령어 봇
│   ├── data/
│   │   ├── collector.py         # OHLCV + 오더북 + 펀딩비 + OI 수집
│   │   └── universe.py          # 거래 유니버스 자동 선별
│   ├── strategy/
│   │   ├── indicators.py        # 기술 지표 계산 (ATR, EMA, RSI, StochRSI, OBV 등)
│   │   ├── signals.py           # 15가지 시그널 생성기
│   │   ├── combiner.py          # 레짐별 가중 합산
│   │   ├── regime.py            # 시장 국면 분류
│   │   ├── filters.py           # 품질 필터 (ER, Hurst, ATR)
│   │   ├── ict.py               # ICT 개념 구현 (BOS, CHoCH, FVG, 유동성)
│   │   └── scalp.py             # SCALP 모드 전용 로직
│   ├── risk/
│   │   ├── engine.py            # 포지션 사이징, 레버리지 계산
│   │   ├── guards.py            # 손실 한도, 낙폭, 변동성 가드
│   │   └── profit_protector.py  # 수익금 자동 이전
│   ├── execution/
│   │   ├── order_manager.py     # 주문 생성/취소/재시도, 멱등성 보장
│   │   └── state_machine.py     # 포지션 FSM (FLAT / LONG / SHORT)
│   ├── db/
│   │   └── database.py          # SQLite (WAL 모드) CRUD
│   ├── backtest/
│   │   ├── engine.py            # 백테스트 실행 엔진
│   │   ├── analyzer.py          # 성과 지표 분석
│   │   ├── data_loader.py       # 과거 데이터 로더
│   │   └── report.py            # HTML 보고서 생성
│   └── reporting/
│       └── daily_briefing.py    # 일간 브리핑 생성
├── tests/                       # pytest (160개 테스트)
├── scripts/                     # 백테스트, 유틸리티 스크립트
├── fly.toml                     # Fly.io 배포 설정
├── Dockerfile                   # python:3.11-slim 기반
├── requirements.txt
└── .env                         # 환경변수 (git 미포함)
```

---

## 9. 설치 및 실행

### 사전 요구사항

- Python 3.11+
- Bybit 계정 (Hedge Mode 활성화 필요)
- Slack App (선택 사항)

### 로컬 실행

```bash
git clone https://github.com/lirongzzuin/bybit-futures-bot.git
cd bybit-futures-bot

python3.11 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
# .env 파일에서 BYBIT_API_KEY, BYBIT_API_SECRET, TRADING_MODE 등 입력

# 테스트 실행 (160개)
pytest tests/ -q

# Dry-run 모드로 시작 (주문 없음)
TRADING_MODE=dry-run python -m src.main

# 페이퍼 트레이딩 (실제 신호, 가상 자금)
TRADING_MODE=paper python -m src.main
```

### 운영 모드

| 모드 | API 연결 | 실제 주문 | 권장 용도 |
|------|----------|-----------|-----------|
| `dry-run` | 불필요 | 없음 | 코드 동작 확인 |
| `paper` | 필요 | 없음 | 2~4주 시그널 검증 |
| `live` | 필요 | 있음 | Quality Gate 통과 후 |

---

## 10. Fly.io 배포

```bash
# Fly CLI 설치
brew install flyctl
fly auth login

# 앱 및 볼륨 생성 (최초 1회)
fly apps create bybit-futures-bot
fly volumes create bot_data --size 1 --region nrt

# 환경변수 등록
fly secrets set BYBIT_API_KEY=xxx \
               BYBIT_API_SECRET=xxx \
               SLACK_WEBHOOK_URL=xxx

# 배포
fly deploy

# 로그 실시간 확인
fly logs
```

**운영 비용**: shared-cpu-1x 256MB ≈ $2.02/월 + 1GB 볼륨 $0.15/월 = **약 $2.17/월**

---

## 11. 주요 설정 파라미터

모든 파라미터는 `.env` 파일로 관리합니다.

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `TRADING_MODE` | `paper` | `dry-run` / `paper` / `live` |
| `MAX_RISK_PER_TRADE_PCT` | `0.01` | 거래당 최대 손실 (자본의 1%) |
| `MAX_DAILY_LOSS_PCT` | `0.05` | 일간 손실 한도 5% |
| `MAX_WEEKLY_LOSS_PCT` | `0.10` | 주간 손실 한도 10% |
| `MAX_DRAWDOWN_PCT` | `0.20` | 최대 낙폭 한도 20% |
| `MAX_LEVERAGE_CAP` | `20` | 최대 레버리지 배수 |
| `MAX_CONCURRENT_POSITIONS` | `2` | 동시 최대 포지션 수 |
| `SIGNAL_ENTRY_THRESHOLD` | `0.55` | 진입 신호 강도 임계값 |
| `SIGNAL_EXIT_THRESHOLD` | `0.40` | 역방향 청산 신호 임계값 |
| `SIGNAL_TIMEFRAME` | `5` | 메인 타임프레임 (분) |
| `TREND_TIMEFRAME` | `60` | 추세 감지 타임프레임 (분) |
| `TOP_N_COINS` | `5` | 모니터링 코인 수 |
| `USE_KELLY_FRACTION` | `0.25` | Quarter-Kelly 비율 |
| `VOLATILITY_TARGET_PCT` | `0.15` | AQR 변동성 타게팅 (연 15%) |
| `PROFIT_PROTECT_THRESHOLD_PCT` | `0.30` | 수익 이전 시작 기준 (30%) |

---

## 12. 기술 스택

| 분류 | 라이브러리 / 도구 |
|------|------------------|
| 거래소 연동 | `pybit 5.8.0` (Bybit v5 API) |
| 데이터 처리 | `pandas 2.2`, `numpy 1.26`, `scipy 1.14` |
| 설정 관리 | `pydantic-settings 2.6`, `python-dotenv` |
| 스케줄링 | `APScheduler 3.10` |
| 데이터베이스 | SQLite WAL 모드, `aiosqlite` |
| 로깅 | `structlog 24.4` (구조화 로그) |
| 알림 | `slack-bolt 1.21` (Socket Mode) |
| HTTP | `httpx 0.27`, `aiohttp 3.10` |
| 시각화 | `matplotlib 3.9`, `jinja2 3.1` |
| 테스트 | `pytest 8.3`, `pytest-asyncio`, `pytest-cov` |
| 배포 | Fly.io, Docker (`python:3.11-slim`) |

---

## 주의사항

- API 키 발급 시 **출금(Withdrawal) 권한은 절대 부여하지 마십시오.**
- `.env` 파일에 API 키가 포함되므로 **절대 git에 커밋하지 마십시오.** (`.gitignore`에 등록됨)
- `TRADING_MODE=live` 전환 전 반드시 `TRADING_MODE=paper`로 충분히 검증하십시오.

---

## License

MIT
