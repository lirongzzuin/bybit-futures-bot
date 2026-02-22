# Bybit USDT Perpetual Futures Trading Bot

> **주의**: 선물 거래는 레버리지로 인해 원금 전액 손실 위험이 있습니다.
> 이 소프트웨어는 수익을 보장하지 않으며, 사용에 따른 투자 손실의 책임은 사용자 본인에게 있습니다.

---

Python으로 설계·구현한 **Bybit USDT 무기한 선물 자동매매 시스템**입니다.
시장 국면(Regime)을 실시간으로 분류하고, 15가지 기술 지표 기반 시그널을 레짐별로 가중 합산해 진입·청산 결정을 내립니다.
Fly.io 클라우드에 컨테이너로 배포되어 24/7 무중단 운영 중이며, Slack으로 실시간 알림과 대화형 명령어 조회를 지원합니다.

---

## 프로젝트 하이라이트

| 항목 | 내용 |
|------|------|
| **구현 언어** | Python 3.13 |
| **전략 유형** | 시장 국면 분류 기반 멀티 시그널 알고리즘 트레이딩 |
| **지표 수** | 15가지 기술 지표 → 9개 카테고리 가중 합산 |
| **리스크 관리** | 동적 레버리지, 다층 손실 가드, 청산 버퍼 검증 |
| **수수료 최적화** | 진입·익절: Maker(0.020%), 손절·긴급청산: Taker(0.055%) |
| **테스트** | pytest 160개 테스트 케이스 |
| **배포** | Docker → Fly.io (공유 CPU 1× / 256MB, 월 ≈ $2.17) |
| **모니터링** | Slack Webhook + Socket Mode 명령어 봇 |

---

## 목차

1. [아키텍처 개요](#1-아키텍처-개요)
2. [데이터 수집 및 유니버스 관리](#2-데이터-수집-및-유니버스-관리)
3. [시장 국면(Regime) 분류](#3-시장-국면regime-분류)
4. [시그널 생성 — 15가지 지표](#4-시그널-생성--15가지-지표)
5. [시그널 조합 — 레짐별 가중 합산](#5-시그널-조합--레짐별-가중-합산)
6. [진입 품질 필터](#6-진입-품질-필터)
7. [트레이드 모드 선택](#7-트레이드-모드-선택)
8. [포지션 사이징 및 레버리지 계산](#8-포지션-사이징-및-레버리지-계산)
9. [익절 & 손절 전략](#9-익절--손절-전략)
10. [리스크 가드 시스템](#10-리스크-가드-시스템)
11. [주문 실행 시스템](#11-주문-실행-시스템)
12. [백테스트 & Quality Gate](#12-백테스트--quality-gate)
13. [Slack 연동](#13-slack-연동)
14. [데이터베이스 설계](#14-데이터베이스-설계)
15. [프로젝트 구조](#15-프로젝트-구조)
16. [설치 및 실행](#16-설치-및-실행)
17. [Fly.io 배포](#17-flyio-배포)
18. [주요 설정 파라미터](#18-주요-설정-파라미터)
19. [기술 스택](#19-기술-스택)
20. [기술적 의사결정 기록](#20-기술적-의사결정-기록)

---

## 1. 아키텍처 개요

**매 5분마다** 아래 파이프라인이 순서대로 실행됩니다.
각 단계는 독립 모듈로 분리되어 있어 유지보수와 테스트가 용이합니다.

```
┌─────────────────────────────────────────────────────────────────┐
│                        5-Minute Cycle                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  [Bybit REST API]                                               │
│   └─ OHLCV 250봉 + 오더북 + 펀딩비 + 미결제약정(OI)            │
│          │                                                      │
│          ▼                                                      │
│  [Universe Manager]   4시간마다 갱신                            │
│   └─ 24h 거래량 기준 상위 10개 코인 자동 선별                   │
│          │                                                      │
│          ▼                                                      │
│  [Regime Detector]   5분봉 + 60분봉 듀얼 타임프레임             │
│   └─ TRENDING_UP / TRENDING_DOWN / RANGING / HIGH_VOLATILITY    │
│          │                                                      │
│          ▼                                                      │
│  [Signal Generator]  15가지 지표 병렬 계산                      │
│   └─ 각 지표 → (direction: +1/0/-1, confidence: 0~1)           │
│          │                                                      │
│          ▼                                                      │
│  [Signal Combiner]   레짐별 카테고리 가중 합산                  │
│   └─ 최종 (direction, strength: 0~1) 출력                       │
│          │                                                      │
│          ▼                                                      │
│  [Quality Filters]   저품질 시그널 제거                         │
│   └─ Kaufman ER + Hurst Exponent + Min ATR 검사                 │
│          │                                                      │
│  ┌───────┴───────────┐                                          │
│  │ 포지션 보유 중?   │                                          │
│  │  YES → 청산 조건  │  NO → 진입 조건 검사                     │
│  │  순서 점검        │    ├─ Risk Guards 통과?                   │
│  │  (SL/TP/Trailing/ │    ├─ Risk Engine 계산                   │
│  │   Regime/Signal/  │    └─ Trade Mode 결정 (SCALP/TREND_RIDE) │
│  │   Time)           │         → Order Manager 주문 발송        │
│  └───────────────────┘                                          │
│          │                                                      │
│          ▼                                                      │
│  [Position Sync]  거래소 실제 포지션과 로컬 상태 비교            │
│   └─ 강제 청산(Liquidation) 감지 시 즉시 FLAT 처리              │
│          │                                                      │
│          ▼                                                      │
│  [SQLite DB + Slack]  거래 기록 저장 + 실시간 알림              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. 데이터 수집 및 유니버스 관리

### 데이터 수집 (`src/data/collector.py`)

매 사이클마다 각 심볼에 대해 다음을 수집합니다:

| 데이터 | 용도 |
|--------|------|
| OHLCV 250봉 (5분봉) | 기술 지표 계산 기반 데이터 |
| OHLCV 60봉 (60분봉) | 상위 타임프레임 레짐 감지 |
| 오더북 스냅샷 | 매수/매도 호가 불균형 계산 |
| 펀딩비 | 군중 포지션 편향 파악 |
| 미결제약정(OI) | 추세 강도 확인 |

### 유니버스 자동 선별 (`src/data/universe.py`)

- 4시간마다 Bybit 전체 USDT 선물 종목을 스캔
- 24시간 거래량 기준 상위 `TOP_N_COINS`(기본 **10개**) 자동 선별
- 유동성이 충분한 종목만 거래 → 슬리피지 최소화

---

## 3. 시장 국면(Regime) 분류

진입 전 **"지금 시장이 어떤 상태인가?"** 를 먼저 판단합니다.
국면에 따라 어떤 지표에 더 높은 가중치를 줄지 결정되기 때문에, 전체 전략의 핵심 판단 기준입니다.

### 분류 체계

| 국면 | 판단 기준 | 전략 |
|------|-----------|------|
| **TRENDING_UP** | ADX ≥ 25, EMA 정배열(9 > 21 > 50), Hurst > 0.50, 추세 점수 > 0.2 | 추세 추종 Long |
| **TRENDING_DOWN** | ADX ≥ 25, EMA 역배열, Hurst > 0.50, 추세 점수 < -0.2 | 추세 추종 Short |
| **RANGING** | ADX < 20 | 지지/저항 평균 회귀 |
| **HIGH_VOLATILITY** | 변동성 백분위 > 85% | 포지션 축소, 보수적 운용 |

### 추세 점수 계산

```
trend_score = (ADX 방향 점수 × 0.50) + (EMA 구조 점수 × 0.40) + (OI 방향 점수 × 0.10)
범위: -1.0 (강한 하락 추세) ~ +1.0 (강한 상승 추세)

regime_confidence = ADX 정규화값 × 0.50 + |trend_score| × 0.50
→ 높은 확신도일수록 진입 시그널 강도를 최대 1.3배 증폭
```

---

## 4. 시그널 생성 — 15가지 지표

각 지표는 독립적으로 **방향**(`+1` Long / `-1` Short / `0` 중립)과 **확신도**(0~1)를 반환합니다.

### 카테고리별 지표 목록

| 카테고리 | 지표 | Long 신호 조건 (예시) |
|----------|------|----------------------|
| **trend** | EMA Cross (9/21) | 단기 EMA가 중기 EMA 위로 교차 |
| **trend** | EMA Alignment (9/21/50) | 3선 완전 정배열 |
| **trend** | MACD Histogram (12/26/9) | 히스토그램 양전환 |
| **trend** | Hull MA (16) | 기울기 상향 전환 |
| **reversion** | RSI (14) | RSI < 30 과매도 구간 |
| **reversion** | Bollinger Band (20, 2σ) | 가격이 하단 밴드 하회 |
| **reversion** | StochRSI (14) | K선이 D선을 30 이하에서 상향 교차 (과매도 확정 조건) |
| **reversion** | Williams %R (14) | -80 이하 과매도 |
| **reversion** | RSI Divergence | 가격 신저가 + RSI 상대 고저 (강세 다이버전스) |
| **volume** | CMF — Chaikin Money Flow (20) | CMF > 0.1 매수 압력 |
| **volume** | OBV — On-Balance Volume | OBV 상승 추세 + 가격 방향 일치 |
| **volatility** | ATR / Keltner Channel | 변동성 확대 + 방향 확인 |
| **squeeze** | Squeeze Momentum (존 카터) | BB가 KC 안에 갇혔다가 상향 이탈 |
| **vwap** | VWAP (세션 96봉) | 가격이 VWAP 위로 복귀 |
| **orderbook** | 호가 불균형 | 매수 호가 우세 (±15% 이상) |
| **ict** | ICT 구조 분석 | BOS(구조 이탈) / CHoCH(추세 전환) / FVG(공정가치 갭) / 유동성 스윕 |

> **ICT(Inner Circle Trader)**: 스마트머니 개념 기반 분석.
> BOS(Break of Structure), CHoCH(Change of Character), FVG(Fair Value Gap), 유동성 스윕을 통해
> 기관 매수/매도 흔적을 감지합니다.

---

## 5. 시그널 조합 — 레짐별 가중 합산

같은 지표라도 시장 상황에 따라 다른 가중치를 부여합니다.

### 카테고리별 가중치

```
┌──────────────┬────────────────┬─────────┬──────────────────┐
│  카테고리    │  추세장        │ 횡보장  │ 고변동성장       │
├──────────────┼────────────────┼─────────┼──────────────────┤
│ ict          │ 30%  ←최고    │ 22%     │ 25%  ←최고      │
│ trend        │ 27%           │  5%     │ 16%              │
│ volume       │ 12%           │ 18%     │ 18%              │
│ vwap         │  8%           │ 10%     │  9%              │
│ squeeze      │  8%           │  6%     │  8%              │
│ momentum     │  6%           │  6%     │  4%              │
│ volatility   │  6%           │  4%     │ 12%              │
│ reversion    │  3%           │ 25% ←최고 │  4%            │
│ orderbook    │  0%           │  4%     │  4%              │
└──────────────┴────────────────┴─────────┴──────────────────┘
```

### 조합 알고리즘

```
1. 각 카테고리에서 Long/Short 점수 독립 계산
   long_score_cat  = Σ(확신도 | direction=+1) / 전체확신도
   short_score_cat = Σ(확신도 | direction=-1) / 전체확신도

2. 레짐별 가중치로 전체 Long/Short 점수 집계
   total_long  = Σ(가중치 × long_score_cat)  / 활성가중치합
   total_short = Σ(가중치 × short_score_cat) / 활성가중치합

3. 레짐 방향성 감쇠 (역방향 시그널 억제)
   TRENDING_UP   → Short 점수 × 0.4 (Long 방향 강화)
   TRENDING_DOWN → Long 점수 × 0.4 (Short 방향 강화)
   HIGH_VOL      → 양방향 × 0.75 (전반 축소)

4. 레짐 확신도 증폭
   최종 강도 = 원본 강도 × (1.0 + 0.3 × regime_confidence)
   → 최대 1.3배 증폭

5. 진입 판단
   total_long  ≥ SIGNAL_ENTRY_THRESHOLD(0.42) → Long 진입
   total_short ≥ SIGNAL_ENTRY_THRESHOLD(0.42) → Short 진입
   둘 다 미달                                 → 관망
```

---

## 6. 진입 품질 필터

시그널 강도가 임계값을 넘어도 아래 3가지 필터를 통과해야 실제 주문이 나갑니다.
저품질 진입을 차단해 **수수료 대비 수익이 나오는 거래만** 선별합니다.

| 필터 | 기준값 | 목적 |
|------|--------|------|
| **Kaufman ER** (Efficiency Ratio) | ER > 0.06 | 방향성 없는 노이즈 구간 진입 차단 |
| **Hurst Exponent** | H > 0.40 | 순수 평균회귀 구간에서 모멘텀 진입 차단 |
| **Min ATR** | ATR > 가격의 0.10% | 변동성이 극히 낮은 "죽은 시장" 진입 차단 (수수료 감안) |

> **수수료 임계값 계산 근거**:
> SCALP 모드에서 ATR 0.10% 기준으로 TP1은 최소 0.15% 상승을 요구합니다.
> Maker 수수료 0.020% + 슬리피지 0.020% = 편도 0.04%, 왕복 0.08%.
> TP1 도달 시 0.15% - 0.08% = 0.07% 순수익 → 수수료를 충분히 커버합니다.

---

## 7. 트레이드 모드 선택

진입 신호가 확정된 후, 현재 시장 환경을 8가지 항목으로 평가해 **거래 스타일**을 결정합니다.

```
점수 항목 (각 +1 또는 -1):
  +1  ADX ≥ 30                    (강한 추세)
  +1  |OI 변화율| ≥ 1.5% + 방향 일치  (기관 참여)
  +1  ICT BOS 방향 일치            (구조적 이탈)
  +1  순 시그널 강도 ≥ 0.60         (높은 확신)
  +1  변동성 백분위 < 70%           (과열되지 않은 시장)
  +1  EMA 방향 일치                (추세 지지)
  -1  |펀딩비| ≥ 0.08%             (군중 리스크)
  -1  스프레드 ≥ 0.2%              (실행 비용 과다)

점수 ≥ 2 → TREND_RIDE 모드 (추세 극대화)
점수 < 2 → SCALP 모드 (빠른 수익 확정)
```

| 항목 | SCALP 모드 | TREND_RIDE 모드 |
|------|-----------|----------------|
| 손절 (SL) | ATR × 1.0 | ATR × 2.0 |
| 1차 익절 (TP1) | ATR × 1.5 (R:R 1.5) | ATR × 3.0 (R:R 1.5) |
| 2차 익절 (TP2) | ATR × 3.0 (R:R 3.0) | ATR × 6.0 (R:R 3.0) |
| TP1 부분청산 비율 | 60% | 40% |
| 최대 보유 시간 | 12봉 (1시간) | 120봉 (10시간) |
| 트레일링 스탑 | 1.0×ATR (TP1 이후 발동) | 1.0×ATR (TP1 이후 발동) |

---

## 8. 포지션 사이징 및 레버리지 계산

레버리지는 **입력값이 아닌 결과값**입니다. 먼저 리스크 허용량과 손절 거리에서 역산합니다.

### 단계별 계산 흐름

```
Step 1. 손절 거리 계산
        stop_distance = ATR × stop_atr_mult
        stop_distance_pct = stop_distance / entry_price

Step 2. 기본 레버리지 (리스크 역산)
        base_leverage = MAX_RISK_PER_TRADE_PCT(2%) / stop_distance_pct
        ※ ATR이 0.5%이면 2%/0.5% = 4× 레버리지

Step 3. 5가지 감쇠 계수 적용 (각 0~1 범위)
        × vol_modifier    — 변동성 높으면 감소 (최소 0.55×)
        × liq_modifier    — 스프레드 넓으면 감소 (최소 0.30×)
        × fund_modifier   — 펀딩비 높으면 감소 (최소 0.60×)
        × signal_modifier — 시그널 강도 반영 (0.50 + 0.50×strength)
        × health_modifier — 낙폭/연속손실 반영 (최소 0.25×)

Step 4. 하드캡 적용
        낙폭 > 15% → 최대 5×
        낙폭 > 10% → 최대 10×
        else       → MAX_LEVERAGE_CAP(10×)
        final_leverage = min(연산값, cap, 거래소 심볼 최대 레버리지)

Step 5. 포지션 크기 계산 (3중 검증)
        ① 리스크 기반:   qty = equity × 2% / stop_distance
        ② AQR 변동성 타게팅: 일간 손익 변동성 ≈ 연 15% / √252
        ③ Quarter-Kelly: f* × 0.25 × equity / stop_distance
        → 세 방법 중 최소값 사용

Step 6. 청산 버퍼 검증 (핵심 안전장치)
        liq_distance = entry_price / final_leverage (≈)
        조건: liq_distance > stop_distance × 3.0
        ※ 청산가가 손절가보다 3배 이상 멀어야만 진입 허용
        → 일시적 시장 변동으로 청산당하는 상황 원천 차단
```

### 연속 손실 시 자동 축소

| 연속 손실 횟수 | 포지션 크기 |
|----------------|------------|
| 0회 | 100% |
| 2회 | 약 81% |
| 4회 | 약 66% |
| 6회 이상 | 최소 25% |

---

## 9. 익절 & 손절 전략

포지션 진입 후 매 사이클마다 아래 조건을 **우선순위 순서대로** 점검합니다.

```
우선순위  조건                              동작
────────────────────────────────────────────────────────────────
  0순위  TP1 도달 (tp1_hit = False)        → 부분청산 + 손절→진입가로 이동
  1순위  손절가(SL) 터치                   → 전량 시장가 즉시 청산
  2순위  TP2 도달                          → 잔여 전량 청산
  3순위  트레일링 스탑 발동 (tp1_hit = True) → 잔여 전량 청산
  4순위  레짐 역전                          → 전량 청산
  5순위  역방향 강시그널                    → 전량 청산
  6순위  시그널 강도 약화 (< 0.35)          → 전량 청산
  7순위  최대 보유 시간 초과                → 전량 청산 (시간 기반)
```

### 트레일링 스탑 설계 의도

트레일링 스탑은 **TP1 도달 이후에만 발동**됩니다.
이유: TP1 전에 트레일링을 활성화하면 ATR 노이즈만큼의 되돌림에도 손절이 발동되어
사실상 손절가보다 불리한 가격에 청산되는 문제가 발생합니다.

```
예시 — SCALP Long, ATR = 200 USDT, 진입가 = 67,000
─────────────────────────────────────────────────────
진입:  67,000 Long
SL:    66,800 (ATR × 1.0)
TP1:   67,300 (ATR × 1.5)  ← 도달 시: 60% 청산 + SL → 67,000 (브레이크이븐)
TP2:   67,600 (ATR × 3.0)  ← 잔여 40% 전량 청산

트레일링 (TP1 이후):
  고점이 67,400까지 상승 → trailing_level = 67,400 - 200 = 67,200
  가격이 67,200 하회 시 → 잔여 40% 시장가 청산

예시 — TREND_RIDE Long, ATR = 200 USDT, 진입가 = 67,000
─────────────────────────────────────────────────────
진입:  67,000 Long
SL:    66,600 (ATR × 2.0)
TP1:   67,600 (ATR × 3.0)  ← 도달 시: 40% 청산 + SL → 67,000
TP2:   68,200 (ATR × 6.0)  ← 잔여 60% 전량 청산
```

### 주문 유형별 수수료 전략

| 청산 이유 | 주문 유형 | 수수료 | 이유 |
|-----------|----------|--------|------|
| TP1, TP2 | 지정가 (Maker) | 0.020% | 여유 있는 청산 |
| SL, 가드, 청산 감지 | 시장가 (Taker) | 0.055% | 즉각 실행 필요 |
| 레짐 전환, 시그널 반전, 트레일링, 시간 만료 | 지정가 (Maker) | 0.020% | 속도보다 비용 중시 |

### 거래소 사전예약 주문 (Crash 방지 Failsafe)

포지션 진입 직후 거래소에 SL/TP를 등록해 봇 다운 시에도 보호됩니다.

| 항목 | 설정값 | 이유 |
|------|--------|------|
| **SL 주문 유형** | 시장가 (Market) | 즉각 체결 필수 |
| **TP 주문 유형** | 지정가 (Limit) | Maker 수수료 절약 |
| **TP 등록 가격** | **TP2 가격** | TP1 부분청산은 봇 내부 로직이 처리, 거래소 TP가 TP1에 걸리면 전량 시장가 청산되어 부분청산 불가 |

---

## 10. 리스크 가드 시스템

포지션 진입 전 최종 관문입니다. 아래 조건 중 하나라도 충족되면 신규 진입이 차단됩니다.

### 다층 가드 레이어

| 가드 | 조건 | 조치 |
|------|------|------|
| **일간 손실** | 당일 손실 ≥ 8% | 당일 신규 진입 중단 (PAUSE) |
| **주간 손실** | 주간 손실 ≥ 15% | 당주 신규 진입 중단 (PAUSE) |
| **최대 낙폭 경고** | 낙폭 ≥ 21% (한도의 70%) | 레버리지 자동 축소 (REDUCE) |
| **최대 낙폭 한도** | 낙폭 ≥ 30% | 전체 포지션 강제 청산 + Slack 경보 (HALT) |
| **변동성 스파이크** | 변동성 백분위 ≥ 97% | 포지션 50% 축소 (REDUCE) |
| **스프레드 이상** | 스프레드 ≥ 1% | 신규 진입 일시 중단 (PAUSE) |
| **총 노출도 한도** | 전체 포지션 노출도 ≥ 75% | 신규 진입 차단 |
| **심볼별 노출도** | 단일 심볼 노출도 ≥ 30% | 해당 심볼 신규 진입 차단 |

### 강제 청산(Liquidation) 감지

매 사이클마다 거래소 실제 포지션과 로컬 상태를 비교합니다.
- 로컬에서는 Long/Short로 기록됐지만 거래소에서 포지션이 없으면 → 청산 발생으로 판단
- 즉시 로컬 상태를 FLAT으로 초기화하고 Slack으로 경보 발송
- 이중 청산이나 오더 중복 제출 방지

---

## 11. 주문 실행 시스템

### 진입 주문 전략 (`src/execution/order_manager.py`)

```
스프레드 > 0.3% 또는 시그널 강도 > 0.90
  → 시장가 주문 (즉각 체결, Taker 수수료)

그 외
  → PostOnly 지정가 주문 (Maker 수수료 절약)
     └─ LIMIT_ORDER_TIMEOUT_SECONDS(30초) 내 미체결
        → 재시도 (최대 3회)
        → 그래도 미체결 → 시장가로 전환
```

### 멱등성(Idempotency) 보장

```python
# 결정론적 Client Order ID 생성
client_id = hash(symbol + side + "entry" + timestamp_bucket)
# → 네트워크 오류로 중복 요청 발생해도 동일 주문이 2번 체결되지 않음
```

### 서킷 브레이커 (`src/api/bybit_client.py`)

- 연속 5회 API 오류 → 60초간 신규 주문 차단
- Rate limit 응답 → 지수 백오프 재시도 (최대 10회)
- 중복 주문 에러(코드 110072) → 정상 처리로 분류 (멱등성 보장)

---

## 12. 백테스트 & Quality Gate

### 실제 거래 환경과 동일한 비용 구조

| 비용 항목 | 값 | 적용 조건 |
|-----------|-----|-----------|
| Maker 수수료 | 0.020% | 진입(PostOnly), TP1, TP2, 트레일링, 시간 만료 청산 |
| Taker 수수료 | 0.055% | SL, 가드, 청산 감지 (Market 주문) |
| 슬리피지 | 0.020% (편도) | 모든 시장가 주문 |
| 펀딩비 | 0.010% (8시간 평균) | 8시간마다 부과 |

### 부분청산 수수료 계산 (버그 수정됨)

```python
# 수정 전 (잘못된 방법 — 수수료 초기화 버그):
pos["fees"] = 0.0

# 수정 후 (올바른 방법 — 비례 배분):
pos["fees"] *= (1 - partial_pct)  # 잔여 포지션 비율만큼 수수료 유지
```

### Walk-Forward 검증

과최적화(Overfitting) 방지를 위해 데이터를 시간 순서로 분할합니다:
- **In-Sample (80%)**: 전략 파라미터 검증
- **Out-of-Sample (20%)**: 실제 성과 측정 (이 기간 성과로 라이브 전환 판단)

### Quality Gate — 라이브 전환 조건

아래 지표를 **모두** 통과해야 `TRADING_MODE=live` 전환이 허용됩니다.
(소프트웨어 레벨에서 강제: `QUALITY_GATE_PASSED=false`이면 live 시도 시 RuntimeError 발생)

| 지표 | 기준 | 의미 |
|------|------|------|
| **Sharpe Ratio** | ≥ 1.0 | 위험 대비 수익이 충분한가 |
| **최대 낙폭(MDD)** | ≤ 30% | 최악의 경우 손실이 허용 범위인가 |
| **승률** | ≥ 45% | 과반 이상 이기지 않아도 수익 구조가 되는가 |
| **Profit Factor** | ≥ 1.5 | 총 이익이 총 손실의 1.5배 이상인가 |
| **최소 거래 횟수** | ≥ 100회 | 통계적으로 유의미한 샘플인가 |

```bash
# 백테스트 실행
python -m scripts.backtest --symbol BTCUSDT --start 2024-01-01 --end 2024-12-31

# 다중 심볼 백테스트
python -m scripts.backtest --symbols BTCUSDT ETHUSDT SOLUSDT --report

# Quality Gate 통과 시 .env에 아래 설정 후 라이브 전환
QUALITY_GATE_PASSED=true
TRADING_MODE=live
```

---

## 13. Slack 연동

### 실시간 자동 알림

| 이벤트 | 알림 내용 |
|--------|-----------|
| 포지션 진입 | 심볼, 방향, 레버리지, 진입가, SL/TP 가격, 트레이드 모드 |
| 1차 익절(TP1) | 부분청산 수량, 실현 손익, 잔여 포지션 |
| 전량 청산 | 청산 이유, 실현 손익, 누적 손익 |
| 강제 청산 감지 | 긴급 경보 (즉각 대응 필요) |
| 일간 손실 경보 | 현재 낙폭, 잔여 한도 |
| **4시간 간격 정기 브리핑** | 오전 9시 → 1시 → 5시 → 9시 → 1시 → 5시 (KST 순환) |

> 모든 타임스탬프는 **KST(한국 표준시, UTC+9)** 기준으로 표시됩니다.
> 정기 브리핑은 매일 09:00 KST를 시작으로 4시간마다 전송됩니다 (09, 13, 17, 21, 01, 05시 KST).

### 대화형 Slack 명령어 (Socket Mode)

| 명령어 | 내용 |
|--------|------|
| `/dashboard` | 자산 현황, 일간/누적 PnL, 오픈 포지션 수 |
| `/positions` | 현재 포지션 목록 (진입가, 현재가, 레버리지, 미실현 손익) |
| `/pnl` | 오늘 / 이번 주 / 전체 PnL + 승률 + 거래 횟수 |
| `/status` | 봇 상태 (모드, 가동 시간, 마지막 사이클, 서킷 브레이커) |

---

## 14. 데이터베이스 설계

SQLite를 WAL(Write-Ahead Logging) 모드로 운영합니다.
WAL 모드는 읽기와 쓰기 작업이 동시에 블로킹 없이 수행되어
백테스트 실행 중에도 실시간 거래 기록이 가능합니다.

### 주요 테이블

```sql
-- 거래 기록
trades:
  id, symbol, side (Buy/Sell)
  entry_price, exit_price, qty, leverage
  pnl, fee, pnl_net                  ← 수수료 차감 순손익
  exit_reason                        ← STOP_LOSS / TAKE_PROFIT / TRAILING_STOP 등
  entry_ts, exit_ts
  mode (dry-run / paper / live)

-- 자산 추이 (시간별)
equity_curve:
  ts, equity, mode
  ← 낙폭 계산, 대시보드 시각화에 활용

-- 일별 통계
daily_stats:
  date, pnl, trades, wins, losses, mode
  ← 일간/주간 가드 판단 기준 데이터

-- 봇 상태 (Key-Value)
bot_state:
  key, value
  ← 예: "last_trade_id:BTCUSDT" → 멱등성 확인
```

---

## 15. 프로젝트 구조

```
bybit-futures-bot/
├── src/
│   ├── main.py                  # 메인 루프, APScheduler (5분 주기)
│   ├── config.py                # 환경변수 → Pydantic Settings (타입 검증)
│   ├── logger.py                # structlog 구조화 로깅
│   │
│   ├── api/
│   │   ├── bybit_client.py      # Bybit v5 REST API 래퍼 (서킷 브레이커)
│   │   ├── slack.py             # Slack Webhook 알림 (KST 타임스탬프)
│   │   └── slack_bot.py         # Slack Socket Mode 명령어 봇
│   │
│   ├── data/
│   │   ├── collector.py         # OHLCV + 오더북 + 펀딩비 + OI 수집
│   │   └── universe.py          # 거래 유니버스 자동 선별 (4시간 갱신)
│   │
│   ├── strategy/
│   │   ├── indicators.py        # ATR, EMA, RSI, StochRSI, OBV, CMF 등
│   │   ├── regime.py            # 시장 국면 분류 (ADX + EMA + Hurst)
│   │   ├── signals.py           # 15가지 시그널 생성기
│   │   ├── combiner.py          # 레짐별 가중 합산 → (direction, strength)
│   │   ├── filters.py           # 품질 필터 (ER, Hurst, Min ATR)
│   │   ├── ict.py               # ICT 개념 구현 (BOS, CHoCH, FVG, Liquidity)
│   │   └── scalp.py             # 트레이드 모드 판별 (SCALP / TREND_RIDE)
│   │
│   ├── risk/
│   │   ├── engine.py            # 동적 레버리지 + 포지션 사이징 (5단계)
│   │   ├── guards.py            # 손실 한도 / 낙폭 / 변동성 가드
│   │   └── profit_protector.py  # 수익금 자동 펀딩 계좌 이전
│   │
│   ├── execution/
│   │   ├── state_machine.py     # 포지션 FSM (FLAT / LONG / SHORT)
│   │   └── order_manager.py     # 주문 생성/재시도/취소, 멱등성 보장
│   │
│   ├── db/
│   │   └── database.py          # SQLite WAL 모드, CRUD 메서드
│   │
│   ├── backtest/
│   │   ├── engine.py            # 봉 단위 시뮬레이션 (실제 동일 로직)
│   │   ├── analyzer.py          # Sharpe, MDD, PF, 승률 계산
│   │   ├── data_loader.py       # 과거 데이터 로더
│   │   └── report.py            # HTML 보고서 생성 + Quality Gate 판정
│   │
│   └── reporting/
│       └── daily_briefing.py    # 매일 09:00 KST 일간 브리핑
│
├── tests/                       # pytest 160개 테스트 케이스
├── scripts/
│   └── backtest_btc.py          # 백테스트 실행 스크립트
├── fly.toml                     # Fly.io 배포 설정
├── Dockerfile                   # python:3.11-slim 기반
├── requirements.txt
└── .env                         # 환경변수 (git 미포함, .gitignore 등록)
```

---

## 16. 설치 및 실행

### 사전 요구사항

- Python 3.11 이상
- Bybit 계정 (Hedge Mode 활성화 필요)
- Slack App (선택 사항)

### 로컬 실행

```bash
git clone https://github.com/lirongzzuin/bybit-futures-bot.git
cd bybit-futures-bot

python3.11 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
# .env 파일에서 BYBIT_API_KEY, BYBIT_API_SECRET 등 입력

# 테스트 실행 (160개)
pytest tests/ -v

# Dry-run 모드 (주문 없음, API 불필요)
TRADING_MODE=dry-run python -m src.main

# Paper 트레이딩 (실제 시그널, 가상 자금)
TRADING_MODE=paper python -m src.main
```

### 운영 모드

| 모드 | API 연결 | 실제 주문 | 권장 용도 |
|------|----------|-----------|-----------|
| `dry-run` | 불필요 | 없음 | 코드 동작 확인, 로직 검증 |
| `paper` | 필요 | 없음 | 2~4주 시그널 품질 검증 |
| `live` | 필요 | **있음** | Quality Gate 통과 후만 허용 |

---

## 17. Fly.io 배포

```bash
# Fly CLI 설치
brew install flyctl
fly auth login

# 앱 및 퍼시스턴트 볼륨 생성 (최초 1회)
fly apps create bybit-futures-bot
fly volumes create bot_data --size 1 --region nrt   # 도쿄 리전

# 환경변수 (시크릿) 등록
fly secrets set BYBIT_API_KEY=your_key \
               BYBIT_API_SECRET=your_secret \
               SLACK_WEBHOOK_URL=your_webhook

# 배포
fly deploy

# 실시간 로그 확인
fly logs
```

**운영 비용**: shared-cpu-1x 256MB ≈ $2.02/월 + 1GB 볼륨 $0.15/월 = **약 $2.17/월**

---

## 18. 주요 설정 파라미터

모든 파라미터는 `.env` 파일로 관리됩니다. Pydantic Settings가 타입 검증과 기본값을 처리합니다.

### 자본 및 리스크

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `TRADING_MODE` | `paper` | `dry-run` / `paper` / `live` |
| `PAPER_CAPITAL_USDT` | `10000.0` | Paper 모드 초기 자본 (USDT) |
| `MAX_RISK_PER_TRADE_PCT` | `0.02` | 거래당 최대 손실 (자본의 2%) |
| `MAX_DAILY_LOSS_PCT` | `0.08` | 일간 손실 한도 8% |
| `MAX_WEEKLY_LOSS_PCT` | `0.15` | 주간 손실 한도 15% |
| `MAX_DRAWDOWN_PCT` | `0.30` | 최대 낙폭 한도 30% |
| `MAX_LEVERAGE_CAP` | `10` | 최대 레버리지 하드캡 |
| `MAX_CONCURRENT_POSITIONS` | `3` | 동시 최대 포지션 수 |
| `MAX_TOTAL_EXPOSURE_PCT` | `0.75` | 총 노출도 한도 (자본 대비 75%) |
| `MAX_SINGLE_EXPOSURE_PCT` | `0.30` | 심볼별 노출도 한도 30% |

### 시그널 설정

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `SIGNAL_ENTRY_THRESHOLD` | `0.42` | 진입 시그널 강도 임계값 |
| `SIGNAL_EXIT_THRESHOLD` | `0.35` | 청산 시그널 강도 임계값 |
| `SIGNAL_TIMEFRAME` | `5` | 메인 타임프레임 (분) |
| `TREND_TIMEFRAME` | `60` | 레짐 감지 타임프레임 (분) |
| `TOP_N_COINS` | `10` | 모니터링 코인 수 |

### 트레이드 모드 파라미터

| 파라미터 | SCALP | TREND_RIDE |
|----------|-------|-----------|
| `STOP_ATR` | `1.0` | `2.0` |
| `TP1_ATR` | `1.5` | `3.0` |
| `TP2_ATR` | `3.0` | `6.0` |
| `PARTIAL_EXIT_PCT` | `0.60` | `0.40` |
| `MAX_BARS` | `12` | `120` |
| `TREND_RIDE_SCORE_THRESHOLD` | — | `2` |

### 고급 리스크 관리

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `VOLATILITY_TARGET_PCT` | `0.15` | AQR 변동성 타게팅 (연 15%) |
| `USE_KELLY_FRACTION` | `0.25` | Quarter-Kelly 비율 |
| `TRAILING_STOP_ATR_MULTIPLIER` | `1.0` | 트레일링 스탑 거리 (ATR 배수) |
| `REENTRY_COOLDOWN_SECONDS` | `300` | 재진입 대기 시간 (5분 = 1봉) |

### 품질 필터

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `SIGNAL_FILTER_ER_THRESHOLD` | `0.06` | Kaufman ER 최소값 |
| `SIGNAL_FILTER_HURST_MIN` | `0.40` | Hurst Exponent 최소값 |
| `SIGNAL_FILTER_MIN_ATR_PCT` | `0.001` | ATR 최소 비율 (0.10%) |

---

## 19. 기술 스택

| 분류 | 라이브러리 / 도구 | 선택 이유 |
|------|------------------|-----------|
| **거래소 연동** | `pybit 5.8.0` (Bybit v5 API) | 공식 SDK, Hedge Mode 지원 |
| **데이터 처리** | `pandas 2.2`, `numpy 1.26`, `scipy 1.14` | 벡터 연산 + 통계 분석 |
| **설정 관리** | `pydantic-settings 2.6`, `python-dotenv` | 타입 안전 설정, 환경변수 자동 파싱 |
| **스케줄링** | `APScheduler 3.10` | 정확한 5분 주기 실행 |
| **데이터베이스** | SQLite WAL 모드, `aiosqlite` | 경량, 서버리스, 동시 읽기/쓰기 |
| **로깅** | `structlog 24.4` | 구조화 로그 (JSON 형태, 파싱 용이) |
| **알림** | `slack-bolt 1.21` (Socket Mode) | 실시간 양방향 Slack 통신 |
| **HTTP** | `httpx 0.27`, `aiohttp 3.10` | 비동기 HTTP, 타임아웃 관리 |
| **시각화** | `matplotlib 3.9`, `jinja2 3.1` | HTML 백테스트 보고서 |
| **테스트** | `pytest 8.3`, `pytest-asyncio`, `pytest-cov` | 160개 테스트, 커버리지 측정 |
| **배포** | Fly.io, Docker (`python:3.11-slim`) | 컨테이너 기반 무중단 배포 |

---

## 20. 기술적 의사결정 기록

실제 개발 과정에서 발생한 문제와 해결 방법을 기록합니다.

### 레버리지를 "입력"이 아닌 "결과"로 설계한 이유

많은 봇이 레버리지를 고정 입력값으로 설정합니다 (예: 항상 5×).
이 접근법은 시장 변동성이 클 때도, ATR이 작을 때도 동일한 레버리지를 사용하여
실제 리스크가 일정하지 않은 문제가 있습니다.

이 프로젝트는 반대 방향으로 설계합니다:
"거래당 최대 손실 = 자본의 2%"를 **목표로 고정**하고,
손절 거리에서 역산하여 레버리지를 결정합니다.
결과적으로 변동성이 클수록 레버리지가 낮아지고, 잔잔한 시장에서는 레버리지가 높아집니다.

### 트레일링 스탑을 TP1 이후에만 활성화하는 이유

초기 설계에서 트레일링 스탑을 진입 즉시 활성화하면
ATR 노이즈 수준의 되돌림에서 빈번하게 청산되는 문제가 관찰됐습니다.

TP1에서 부분 청산 후 손절을 진입가(브레이크이븐)로 이동시킨 상태에서
트레일링을 활성화하면, 이미 원금 리스크가 제거된 상태이므로
트레일링이 "추가 수익 보호"의 역할만 수행합니다.

### Maker 주문을 기본 전략으로 선택한 이유

Taker(시장가)와 Maker(PostOnly 지정가)의 수수료 차이:
- Taker: 0.055% / Maker: 0.020% → 거래당 약 0.035% 절약
- SCALP 모드 TP1 목표 수익 0.15% 기준으로 수수료 절약분이 수익의 23%에 해당

**적용 범위**: 진입(PostOnly), TP1, TP2, 트레일링, 레짐 전환, 시그널 반전, 시간 만료 청산 → 모두 Maker
**예외**: SL, 가드 발동, 청산 감지 시에는 속도가 최우선이므로 Taker(시장가) 사용

### 거래소 사전예약 TP를 TP2 가격으로 설정한 이유

포지션 진입 후 `set_trading_stop()` API로 거래소에 TP를 등록하면 봇 다운 시에도 보호됩니다.
이때 TP 가격을 TP1(1차 익절가)으로 설정하면 **전량 시장가 청산**이 발생합니다.

**문제**: 거래소 TP가 TP1 가격에서 `tpslMode=Full`로 트리거되면 잔여 포지션 전량을 시장가로 청산합니다.
이로 인해 봇의 60%/40% 부분청산 로직이 완전히 무력화됩니다.

**해결**: 거래소 TP는 TP2(최종 목표가) 지정가 주문으로 등록합니다.
TP1 부분청산은 봇의 5분 사이클이 정상적으로 처리하고,
봇이 다운된 경우에는 TP2에서 잔여 포지션이 지정가로 안전하게 청산됩니다.

### StochRSI 임계값을 표준값(30/70)으로 유지하는 이유

초기 구현에서 `k < 50` 조건으로 과매도 신호를 발생시켰으나,
이는 RSI가 50 이하인 모든 구간(중립 포함)에서 반전 신호를 생성하는 문제가 있었습니다.

**수정**: 과매도 기준 `k < 30`, 과매수 기준 `k > 70`으로 표준화합니다.
Confidence 계산도 `(30 - k) / 30`으로 조정하여 극단값에 가까울수록 높은 확신도를 부여합니다.
이로써 false signal이 대폭 감소하고, 실제 과매도/과매수 구간에서만 반전 시그널이 발생합니다.

### 청산 버퍼 검증을 별도 단계로 구현한 이유

레버리지가 높을수록 청산가가 진입가에 가까워집니다.
시장이 일시적으로 급락(Wick)하면 손절이 발동하기 전에 청산당할 수 있습니다.

이를 방지하기 위해 `liq_distance > stop_distance × 3.0` 조건을 강제합니다.
이 조건이 충족되지 않으면 레버리지를 자동으로 낮추거나 진입 자체를 거부합니다.

---

## 주의사항

- **API 키 권한**: 발급 시 **출금(Withdrawal) 권한은 절대 부여하지 마십시오.**
- **API 시크릿 보안**: `.env` 파일은 절대 git에 커밋하지 마십시오. (`.gitignore`에 등록됨)
- **단계적 전환**: `dry-run` → `paper` (최소 2주 검증) → `live` 순서로 전환하십시오.
- **Quality Gate 필수**: `TRADING_MODE=live` 전환은 반드시 백테스트 Quality Gate 통과 후 설정하십시오.
- **Hedge Mode**: Bybit 계정에서 Hedge Mode가 활성화되어 있어야 Long/Short 동시 포지션이 가능합니다.

---

## License

MIT
