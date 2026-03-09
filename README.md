# Bybit USDT Perpetual Futures Trading Bot

> **주의**: 선물 거래는 레버리지로 인해 원금 전액 손실 위험이 있습니다.
> 이 소프트웨어는 수익을 보장하지 않으며, 사용에 따른 투자 손실의 책임은 사용자 본인에게 있습니다.

---

Python으로 설계·구현한 **Bybit USDT 무기한 선물 자동매매 시스템**입니다.
세 가지 독립 전략(스윙/스캘핑/추세EMA)을 설정 한 줄로 전환할 수 있으며,
시장 국면(Regime)과 매크로 레짐(BULL/BEAR)을 실시간으로 분류해 심볼 필터링·레버리지·주문 방식을 자동 결정합니다.
Fly.io 클라우드에 컨테이너로 배포되어 24/7 무중단 운영 중이며, GitHub Actions로 CI/CD 자동 배포가 구성돼 있습니다.

---

## 프로젝트 하이라이트

| 항목 | 내용 |
|------|------|
| **구현 언어** | Python 3.11 |
| **전략 유형** | 스윙 + 스캘핑 + **추세EMA(Phase-locked)** + **auto(Dynamic Regime Router)** — 설정으로 전환 |
| **매크로 필터** | BTC 일봉 EMA200 기반 BULL/BEAR 자동 판단 → 거래 심볼 자동 전환 |
| **리스크 관리** | 동적 레버리지 역산, 다층 손실 가드, 청산 버퍼 검증, **Min Stop Floor**, 수수료 손익분기 체크 |
| **수수료 최적화** | 신호 강도별 PostOnly(Maker 0.020%) / 시장가(Taker 0.055%) 자동 선택 |
| **테스트** | pytest **355개** 테스트 케이스 전체 통과 |
| **배포** | Docker → Fly.io (nrt/Tokyo, 월 ≈ $2.17) + GitHub Actions CI/CD 자동 배포 |
| **모니터링** | Slack Webhook + Socket Mode 명령어 봇 |

---

## 목차

1. [아키텍처 개요](#1-아키텍처-개요)
2. [전략 선택 시스템](#2-전략-선택-시스템)
3. [추세EMA 전략 — Phase-locked Direction System](#3-추세ema-전략--phase-locked-direction-system) ★ 현재 배포
4. [스캘핑 전략 — EMA Ribbon + VWAP + RSI](#4-스캘핑-전략--ema-ribbon--vwap--rsi)
5. [스윙 전략 — 멀티 시그널 알고리즘](#5-스윙-전략--멀티-시그널-알고리즘)
6. [매크로 레짐 — BULL / BEAR 자동 전환](#6-매크로-레짐--bull--bear-자동-전환)
7. [시장 국면(Regime) 분류](#7-시장-국면regime-분류)
8. [포지션 사이징 및 레버리지 계산](#8-포지션-사이징-및-레버리지-계산)
9. [익절 & 손절 전략](#9-익절--손절-전략)
10. [리스크 가드 시스템](#10-리스크-가드-시스템)
11. [주문 실행 시스템](#11-주문-실행-시스템)
12. [Slack 연동](#12-slack-연동)
13. [데이터베이스 설계](#13-데이터베이스-설계)
14. [프로젝트 구조](#14-프로젝트-구조)
15. [설치 및 실행](#15-설치-및-실행)
16. [Fly.io 배포 & GitHub Actions CI/CD](#16-flyio-배포--github-actions-cicd)
17. [주요 설정 파라미터](#17-주요-설정-파라미터)
18. [기술 스택](#18-기술-스택)
19. [기술적 의사결정 기록](#19-기술적-의사결정-기록)

---

## 1. 아키텍처 개요

매 사이클(`STRATEGY_MODE=trend/scalp` → 3분, `swing` → 15분)마다 아래 파이프라인이 실행됩니다.

```
┌───────────────────────────────────────────────────────────────────┐
│                         Trading Cycle                             │
├───────────────────────────────────────────────────────────────────┤
│                                                                   │
│  [Macro Regime Detector]  BTC 일봉 기반 (MACRO_REGIME_CHECK_HOURS)│
│   └─ BULL → 전체 유니버스 허용 / BEAR_SIDEWAYS → BTC+ETH 한정    │
│          │                                                        │
│          ▼                                                        │
│  [Universe Manager]  4시간마다 거래량 상위 N개 자동 갱신          │
│   └─ 매크로 레짐으로 허용 심볼 필터링                             │
│          │                                                        │
│          ▼                                                        │
│  ┌───────────────────────────────────────────────────────────┐    │
│  │            STRATEGY_MODE 분기                             │    │
│  │  "trend" → collect_for_scalp() → TrendEMAStrategy         │    │
│  │  "scalp" → collect_for_scalp() → ScalpingStrategy         │    │
│  │  "swing" → collect()           → ICT + SignalGenerator    │    │
│  │             + SignalCombiner + TradeModeDetector           │    │
│  └───────────────────────────────────────────────────────────┘    │
│          │                                                        │
│          ▼                                                        │
│  [Risk Guards]  다층 손실/낙폭/변동성 가드                        │
│   └─ ALLOW / PAUSE / REDUCE / HALT                                │
│          │                                                        │
│  ┌───────┴─────────────┐                                          │
│  │  포지션 보유 중?    │                                          │
│  │  YES → 청산 조건    │  NO → 진입 조건 검사                     │
│  │  (SL/TP/Trail/      │    ├─ Risk Engine → 레버리지·사이징 계산  │
│  │   Regime/Signal/    │    └─ Order Manager → PostOnly/Market    │
│  │   Time)             │         주문 자동 선택                   │
│  └─────────────────────┘                                          │
│          │                                                        │
│          ▼                                                        │
│  [Position Sync]  거래소 포지션 ↔ 로컬 상태 비교 (강제청산 감지)  │
│  [SQLite DB + Slack]  거래 기록 저장 + 실시간 알림                │
└───────────────────────────────────────────────────────────────────┘
```

---

## 2. 전략 선택 시스템

`.env`에서 `STRATEGY_MODE` 한 줄로 전략을 전환합니다.

```env
STRATEGY_MODE=trend   # ★ 현재 배포 — Phase-locked 추세 EMA 전략 (3분봉)
STRATEGY_MODE=scalp   # 3분봉 스캘핑 전략 (EMA Ribbon + VWAP + RSI)
STRATEGY_MODE=swing   # 15분봉 스윙/추세추종 전략 (ICT + 15가지 지표)
STRATEGY_MODE=auto    # Dynamic Regime Router — 장세별 자동 전략 선택
```

| 항목 | trend 모드 | scalp 모드 | swing 모드 | auto 모드 |
|------|-----------|-----------|-----------|-----------|
| **핵심 철학** | 장세 판단 → 방향 잠금 | 모멘텀 스캘핑 | ICT + 레짐 기반 | 장세 자동 감지 → 최적 전략 선택 |
| **데이터** | 3m LTF + 15m HTF | 3m LTF + 15m HTF | 15m 메인 + 1D 레짐 | 15m→60m 리샘플 |
| **신호 생성** | TrendEMAStrategy (5점) | ScalpingStrategy (9점) | 15가지 지표 합산 | RegimeRouter 자동 |
| **방향 잠금** | BULL→LONG / BEAR→SHORT | 양방향 | 양방향 | 장세별 자동 |
| **사이클 주기** | 3분 | 3분 | 15분 | 3분 |
| **트레이드 모드** | `PURE_SCALP` | `PURE_SCALP` | `SCALP`/`TREND_RIDE` | 장세별 자동 |
| **Stop** | 1.2×ATR (min 0.4%) | 1.2×ATR (min 0.4%) | 1.5–2.5×ATR | 장세별 자동 |
| **TP1 / TP2** | 1.5× / 3.0×ATR | 1.5× / 3.0×ATR | 1.5–3.0× / 3.0–6.0×ATR | 장세별 자동 |
| **최대 보유** | 20봉 (60분) | 20봉 (60분) | 12–120봉 | 장세별 자동 |

네 전략 모두 동일한 **리스크 가드, 수수료 최적화, Slack 알림, DB 기록, 청산 로직**을 공유합니다.

---

## 3. 추세EMA 전략 — Phase-locked Direction System

`src/strategy/trend_ema.py` — **`STRATEGY_MODE=trend` 시 사용 (현재 배포 중)**

### 핵심 설계 철학

> "시장 장세(Phase)가 진입 방향을 결정한다. EMA 정렬이 1차 조건, 나머지는 확인 지표."

기존 전략의 문제였던 **역추세 진입(하락장에서 롱 진입)**을 구조적으로 차단합니다.

### 3단계 장세 판단 (HTF 15분봉 기반)

| 장세 | 조건 | 허용 방향 |
|------|------|---------|
| **BULL_TREND** | HTF: EMA21 > EMA50 > EMA200 AND ADX ≥ 22 AND +DI > -DI | **LONG만** 허용 |
| **BEAR_TREND** | HTF: EMA21 < EMA50 < EMA200 AND ADX ≥ 22 AND -DI > +DI | **SHORT만** 허용 |
| **SIDEWAYS** | 위 조건 미충족 | 볼린저 밴드 극단값에서 양방향 (높은 임계치) |

### 진입 스코어링 (5점 만점, 최소 4점 필요)

**[추세 진입 — BULL_TREND / BEAR_TREND]**

| 조건 | 점수 | 설명 |
|------|------|------|
| EMA9/EMA21 정렬 (Phase 방향 일치) | +2 | 핵심 조건 — LTF 모멘텀 확인 |
| 가격이 EMA21 ±2.0×ATR 존 내 | +1 | 풀백/반등 진입 구간 |
| RSI가 방향에 맞는 범위 (롱 40-65 / 숏 40-60) | +1 | 과매수/과매도 회피 |
| 거래량 ≥ 20봉 평균 × 1.1 | +1 | 참여도 확인 |
| 양봉 OR 핀바(하단윅>0.35×ATR) | +1 | 방향 확인 + 반전 패턴 |
| \|funding rate\| > 0.1% | -1 | 한쪽 쏠림 위험 |
| 스프레드 > 0.2% | -1 | 체결 비용 과다 |

**[횡보 진입 — SIDEWAYS]**

| 조건 | 점수 | 설명 |
|------|------|------|
| 가격이 볼린저 밴드 하단/상단 터치 | +2 | 평균 회귀 핵심 신호 |
| RSI < 35 (롱) / RSI > 65 (숏) | +1 | 강한 과매도/과매수 |
| 거래량 스파이크 (평균 × 1.5) | +1 | 참여도 확인 |
| 방향 일치 캔들 바디 > 0.15×ATR | +1 | 반전 신호 확인 |
| 패널티 동일 | -1/-1 | funding, spread |

### 점수 → 레버리지 티어 정렬

| score | strength | 레버리지 티어 | 비고 |
|-------|----------|-------------|------|
| < 4 | — | 진입 없음 | 고품질 신호만 선택 |
| 4 | 0.77 | **≥0.75 (8-10×)** | PostOnly |
| 5 | 0.85 | **≥0.85 (20×)** | PostOnly (시장가 기준 0.91) |
| SIDEWAYS 4 | 0.65 | ≥0.65 (4-5×) | 평균회귀 — 낮은 레버리지 |
| SIDEWAYS 5 | 0.69 | ≥0.65 (4-5×) | 약간 높은 신뢰 |

### 방향 잠금 검증 결과

```
BULL_TREND에서 SHORT 신호: 0건 (50회 반복 테스트)
BEAR_TREND에서 LONG  신호: 0건 (50회 반복 테스트)
→ 역추세 진입이 구조적으로 불가능함을 확인
```

---

## 4. 스캘핑 전략 — EMA Ribbon + VWAP + RSI

`src/strategy/scalping.py` — `STRATEGY_MODE=scalp` 시 사용

### 신호 생성 원리

**HTF(15m)** 로 추세 바이어스를 확인하고, **LTF(3m)** 로 진입 타이밍을 정밀 포착합니다.

### 롱 진입 스코어링 (9점 만점)

| 조건 | 점수 | 설명 |
|------|------|------|
| HTF EMA9 > EMA21 | +2 | HTF 단기 상승 추세 |
| HTF 가격 > VWAP | +1 | 세션 내 매수 우위 |
| LTF EMA9 > EMA21 | +2 | LTF 단기 상승 추세 확인 |
| 가격이 EMA21 ± 1.5×ATR 존 내 | +1 | 풀백 진입 구간 (깊은 풀백 포착) |
| RSI 45–65 (롱) / 42–55 (숏) | +1 | 모멘텀 형성 중, 과매수 아님 |
| 볼륨 ≥ 20봉 평균 × 1.2 | +1 | 참여도 확인 |
| 양봉 OR 핀바(하단윅>0.35×ATR) | +1 | 방향 확인 + 반전 패턴 |
| \|펀딩비\| > 0.1% | -1 | 한쪽 쏠림 위험 |
| 스프레드 > 0.2% | -1 | 체결 비용 과다 |

**3단계 하드 필터**: HTF EMA9/21 → LTF EMA50 → HTF EMA50 구조 확인 (모두 통과 필수)

숏 진입: 롱의 정반대 조건

### 점수 → 레버리지 티어 + 주문 유형 자동 전환

| score | strength | 레버리지 티어 | 주문 방식 |
|-------|----------|-------------|---------|
| < 8 | — | 진입 없음 | — |
| 8 | 0.83 | ≥0.80 (10-12×) | PostOnly |
| 9 | 0.91 | ≥0.85 (15-20×) | **시장가** |

> **min_score 5→8 강화 이유**: 낮은 점수 진입은 노이즈 거래 비중이 높아 승률을 떨어뜨림.
> score 8 이상만 허용하면 8-10× 이상의 레버리지 티어만 사용 → 고품질 거래만 실행.

---

## 5. 스윙 전략 — 멀티 시그널 알고리즘

`STRATEGY_MODE=swing` 시 사용. 기존 15분봉 기반 복합 시그널 전략입니다.

### 15가지 지표

| 카테고리 | 지표 | Long 신호 조건 |
|----------|------|----------------|
| **trend** | EMA Cross (9/21) | 단기 EMA가 중기 EMA 위로 교차 |
| **trend** | EMA Alignment (9/21/50) | 3선 완전 정배열 |
| **trend** | MACD Histogram (12/26/9) | 히스토그램 양전환 |
| **trend** | Hull MA (16) | 기울기 상향 전환 |
| **reversion** | RSI (14) | RSI < 30 과매도 |
| **reversion** | Bollinger Band (20, 2σ) | 가격이 하단 밴드 하회 |
| **reversion** | StochRSI (14) | K선이 D선을 30 이하에서 상향 교차 |
| **reversion** | Williams %R (14) | -80 이하 과매도 |
| **reversion** | RSI Divergence | 가격 신저가 + RSI 강세 다이버전스 |
| **volume** | CMF (20) | CMF > 0.1 매수 압력 |
| **volume** | OBV | OBV 상승 + 가격 방향 일치 |
| **volatility** | ATR / Keltner Channel | 변동성 확대 + 방향 확인 |
| **squeeze** | Squeeze Momentum | BB가 KC 안에 갇혔다가 상향 이탈 |
| **vwap** | VWAP (세션 96봉) | 가격이 VWAP 위로 복귀 |
| **orderbook** | 호가 불균형 | 매수 호가 우세 (±15% 이상) |
| **ict** | ICT 구조 분석 | BOS / CHoCH / FVG / 유동성 스윕 |

### 레짐별 카테고리 가중치

```
┌──────────────┬────────────┬─────────┬──────────────┐
│  카테고리    │  추세장    │ 횡보장  │ 고변동성장   │
├──────────────┼────────────┼─────────┼──────────────┤
│ ict          │ 30%  ←최고 │ 22%     │ 25%  ←최고  │
│ trend        │ 27%        │  5%     │ 16%          │
│ volume       │ 12%        │ 18%     │ 18%          │
│ vwap         │  8%        │ 10%     │  9%          │
│ squeeze      │  8%        │  6%     │  8%          │
│ momentum     │  6%        │  6%     │  4%          │
│ volatility   │  6%        │  4%     │ 12%          │
│ reversion    │  3%        │ 25% ←최고│  4%         │
│ orderbook    │  0%        │  4%     │  4%          │
└──────────────┴────────────┴─────────┴──────────────┘
```

---

## 5-1. Dynamic Regime Router — `STRATEGY_MODE=auto`

`src/strategy/regime_router.py` — 매 사이클 15m봉→60m 리샘플 후 장세를 감지하여 자동으로 최적 전략을 선택합니다.

### 7개 장세 → 전략 자동 매핑

| 장세 | 감지 조건 | 전략 | 방향 | 리스크 | 레버리지캡 |
|------|----------|------|------|--------|-----------|
| **STRONG_BULL** | EMA↑ + ADX>27 | trend | LONG only | 100% | 20× |
| **BULL** | EMA↑ + ADX≥18 | trend | LONG only | 90% | 15× |
| **SIDEWAYS** | 혼합 EMA | scalp | 양방향 | 80% | 10× |
| **BEAR** | EMA↓ + ADX≥18 | trend | SHORT only | 90% | 15× |
| **STRONG_BEAR** | EMA↓ + ADX>27 | trend | SHORT only | 100% | 20× |
| **VOLATILE** | ATR pct>82% | scalp | 양방향 | 50% | 8× |
| **TRANSITION** | 전환 확정 중 | — | skip | — | — |

### 디바운스 로직

- 같은 그룹 전환 (BULL↔STRONG_BULL): 즉시 확정
- 다른 그룹 전환 (BULL→BEAR 등): **3회 연속** 확인 후 확정
- macro_bear=True 시: STRONG_BULL→BEAR, BULL→SIDEWAYS 오버라이드

---

## 6. 매크로 레짐 — BULL / BEAR 자동 전환

`src/strategy/macro_regime.py` — BTC 일봉(1440분봉) 기반으로 시장 환경을 판단합니다.

### 판단 기준

| 조건 | 매크로 레짐 |
|------|-----------|
| BTC 가격 > EMA200 AND EMA50 > EMA200 AND ADX > 20 | **BULL** |
| 그 외 | **BEAR / SIDEWAYS** |

### 심볼 필터링

| 매크로 레짐 | 거래 심볼 | 최대 레버리지 |
|-------------|-----------|--------------|
| **BULL** | `BULL_MODE_SYMBOLS` 또는 전체 유니버스 | `MAX_LEVERAGE_CAP` |
| **BEAR_SIDEWAYS** | `BEAR_MODE_SYMBOLS` (BTC + ETH) | `BEAR_MAX_LEVERAGE_CAP` |

---

## 7. 시장 국면(Regime) 분류

스윙 전략에서 사용. 15분봉 + 1일봉 듀얼 타임프레임으로 현재 시장 상태를 판단합니다.

| 국면 | 판단 기준 | 전략 |
|------|-----------|------|
| **TRENDING_UP** | ADX ≥ 25, EMA 정배열, Hurst > 0.50, 추세 점수 > 0.2 | 추세 추종 Long |
| **TRENDING_DOWN** | ADX ≥ 25, EMA 역배열, Hurst > 0.50, 추세 점수 < -0.2 | 추세 추종 Short |
| **RANGING** | ADX < 20 | 지지/저항 평균 회귀 |
| **HIGH_VOLATILITY** | 변동성 백분위 > 85% | 포지션 축소, 보수적 운용 |

---

## 8. 포지션 사이징 및 레버리지 계산

레버리지는 **입력값이 아닌 결과값**입니다. 리스크 허용량과 손절 거리에서 역산합니다.

### 시그널 강도별 레버리지 에스컬레이션

| 시그널 강도 | stop_atr_scale | trade_risk_pct | 목표 레버리지 |
|------------|----------------|----------------|--------------|
| ≥ 0.85 | 0.65 | min(16%, risk×8) | ~15-20× (캡) |
| ≥ 0.80 | 0.75 | min(12%, risk×6) | ~10-12× |
| ≥ 0.75 | 0.85 | min(9%, risk×4.5) | ~7-9× |
| ≥ 0.65 | 0.92 | min(6%, risk×3) | ~4-5× |
| 기본 | 1.00 | min(4%, risk×2) | ~2-3× |

> **stop_atr_scale 상향 이유 (0.35→0.65 등)**: 이전 설정에서 ETH의 3분봉 ATR(0.19%)에
> 0.35를 곱하면 손절가 $3.78 → 즉시 노이즈에 의한 손절 발동. 스케일을 넓혀 최소 의미있는
> 가격 움직임만 손절로 처리.

낙폭 > 15%: 에스컬레이션 자동 비활성화 (기본 risk만 사용)

### 5가지 감쇠 계수

```
× vol_modifier    — 변동성 높으면 감소 (최소 0.55×)
× liq_modifier    — 스프레드 넓으면 감소 (최소 0.30×)
× fund_modifier   — 펀딩비 높으면 감소 (최소 0.60×)
× signal_modifier — 시그널 강도 반영 (0.65 + 0.35×strength)
× health_modifier — 낙폭/연속손실 반영 (최소 0.25×)
```

### 청산 버퍼 검증 (핵심 안전장치)

```
조건: liq_distance > stop_distance × 3.0
→ 청산가가 손절가보다 3배 이상 멀어야만 진입 허용
→ 일시적 급락(Wick)으로 청산당하는 상황 원천 차단
```

---

## 9. 익절 & 손절 전략

포지션 진입 후 매 사이클마다 아래 조건을 **우선순위 순서대로** 점검합니다.

```
우선순위  조건                              동작
──────────────────────────────────────────────────────────────
  0순위  TP1 도달                          → 부분청산 + 손절→진입가로 이동
  1순위  손절가(SL) 터치                   → 전량 시장가 즉시 청산
  2순위  TP2 도달                          → 잔여 전량 청산
  3순위  트레일링 스탑 발동 (tp1 이후)     → 잔여 전량 청산
  4순위  레짐 역전                          → 전량 청산
  5순위  역방향 강시그널                    → 전량 청산
  6순위  시그널 강도 약화 (< exit_thresh)  → 전량 청산
  7순위  최대 보유 봉 수 초과              → 전량 청산
```

### 모드별 파라미터

| 항목 | PURE_SCALP | SCALP | TREND_RIDE |
|------|-----------|-------|-----------|
| SL | 1.2×ATR | 1.5×ATR | 2.5×ATR |
| **Min Stop Floor** | **0.4%** | **0.7%** | **1.2%** |
| TP1 | 1.5×ATR | 2.5×ATR | 3.0×ATR |
| TP2 | 3.0×ATR | 5.5×ATR | 6.0×ATR |
| 부분청산 비율 | 50% | 60% | 40% |
| 최대 보유 봉 수 | 20봉 (60분) | 12봉 | 120봉 |

> **Min Stop Floor**: ATR이 극히 낮은 시장에서 계산상 손절가가 0.19% 등 노이즈 범위 내로
> 설정되는 것을 방지. Floor 적용 시 TP도 비례 확대하여 R:R 비율 유지.

---

## 10. 리스크 가드 시스템

포지션 진입 전 최종 관문. 아래 조건 중 하나라도 충족되면 신규 진입이 차단됩니다.

| 가드 | 조건 | 조치 |
|------|------|------|
| **일간 손실** | 당일 손실 ≥ `MAX_DAILY_LOSS_PCT` | 당일 신규 진입 중단 (PAUSE) |
| **주간 손실** | 주간 손실 ≥ `MAX_WEEKLY_LOSS_PCT` | 당주 신규 진입 중단 (PAUSE) |
| **낙폭 경고** | 낙폭 ≥ MDD 한도의 70% | 레버리지 자동 축소 (REDUCE) |
| **낙폭 한도** | 낙폭 ≥ `MAX_DRAWDOWN_PCT` | 전체 강제 청산 + Slack 경보 (HALT) |
| **변동성 스파이크** | 변동성 백분위 ≥ 97% | 포지션 50% 축소 (REDUCE) |
| **스프레드 이상** | 스프레드 ≥ 1% | 신규 진입 일시 중단 (PAUSE) |

### 강제 청산(Liquidation) 감지

매 사이클마다 거래소 실제 포지션과 로컬 상태 비교:
- 로컬 기록 있음 + 거래소 포지션 없음 → 청산으로 판단
- 즉시 로컬 상태 FLAT 초기화 + Slack 긴급 경보

---

## 11. 주문 실행 시스템

### 진입 주문 전략

```
스프레드 > SPREAD_TAKER_THRESHOLD(0.3%)
또는 시그널 강도 > SIGNAL_URGENT_THRESHOLD(0.90)
  → 시장가 주문 (즉각 체결, Taker 수수료)

그 외
  → PostOnly 지정가 주문 (Maker 수수료 절약)
     └─ 재시도 시 orderbook에서 현재 bid/ask 갱신 (stale price 방지)
     └─ 미체결 → 재시도 (새 client_id로 중복 방지)
        → 그래도 미체결 → 시장가로 전환
```

진입/청산 전 `cancel_all_orders()` 호출 → 미체결 주문 누적 방지

### 서킷 브레이커

- 연속 5회 API 오류 → 60초간 신규 주문 차단
- Rate limit → 지수 백오프 재시도 (최대 10회)
- 중복 주문 에러(코드 110072) → 정상 처리 (멱등성 보장)

---

## 12. Slack 연동

### 실시간 자동 알림

| 이벤트 | 알림 내용 |
|--------|-----------|
| 포지션 진입 | 심볼, 방향, 레버리지, 진입가, SL/TP, 주문 유형, 신호 강도 |
| 전량 청산 | 청산 이유, 실현 손익(수수료 차감), PnL%, 진입/청산가, 레버리지 |
| 강제 청산 감지 | 긴급 경보 |
| 매크로 레짐 전환 | BULL ↔ BEAR_SIDEWAYS 전환, 허용 심볼, 레버리지 캡 |
| 일간 손실 경보 | 하루 한 번만 발송 (디바운스) |
| 정기 브리핑 | 09:00 KST + 21:00 KST (하루 2회) |

### 대화형 Slack 명령어 (Socket Mode)

| 명령어 | 내용 |
|--------|------|
| `/dashboard` | 자산 현황, 일간/누적 PnL, 오픈 포지션 수 |
| `/positions` | 현재 포지션 목록 (진입가, 현재가, 레버리지, 미실현 손익) |
| `/pnl` | 오늘 / 이번 주 / 전체 PnL + 승률 + 거래 횟수 |
| `/status` | 봇 상태 (모드, 가동 시간, 마지막 사이클, 서킷 브레이커) |

---

## 13. 데이터베이스 설계

SQLite WAL(Write-Ahead Logging) 모드 운영 — 읽기/쓰기 동시 처리.

```sql
-- 거래 기록
trades:
  id, symbol, side (Buy/Sell)
  entry_price, exit_price, qty, leverage
  pnl, fee, pnl_net          ← 수수료 차감 순손익
  exit_reason                ← STOP_LOSS / TAKE_PROFIT / TIME_EXIT 등
  entry_ts, exit_ts
  mode (dry-run / paper / live)

-- 자산 추이
equity_curve:
  ts, equity, mode

-- 일별 통계
daily_stats:
  date, pnl, trades, wins, losses, mode

-- 봇 상태 (Key-Value)
bot_state:
  key, value                 ← 예: "trade_id:BTCUSDT"
```

---

## 14. 프로젝트 구조

```
bybit-futures-bot/
├── src/
│   ├── main.py                  # TradingBot, 메인 루프, APScheduler
│   ├── config.py                # 환경변수 → Pydantic Settings (타입 검증)
│   ├── logger.py                # 구조화 로깅
│   │
│   ├── api/
│   │   ├── bybit_client.py      # Bybit v5 REST API 래퍼 (서킷 브레이커)
│   │   ├── slack.py             # Slack Webhook 알림
│   │   └── slack_bot.py         # Slack Socket Mode 명령어 봇
│   │
│   ├── data/
│   │   ├── collector.py         # OHLCV + 오더북 + 펀딩비 + OI 수집
│   │   │                        # collect() : 스윙용 (15m)
│   │   │                        # collect_for_scalp() : 스캘핑/추세용 (3m LTF + 15m HTF)
│   │   └── universe.py          # 거래 유니버스 자동 선별 (4시간 갱신)
│   │
│   ├── strategy/
│   │   ├── trend_ema.py         # ★ 추세EMA 전략 (Phase-locked Direction System)
│   │   ├── scalping.py          # 스캘핑 전략 (EMA Ribbon + VWAP + RSI 9점)
│   │   ├── indicators.py        # ATR, EMA, RSI, ADX, StochRSI, OBV, CMF, VWAP 등
│   │   ├── regime.py            # 시장 국면 분류 (ADX + EMA + Hurst)
│   │   ├── macro_regime.py      # 매크로 레짐 (BTC 일봉 EMA200 기반)
│   │   ├── signals.py           # 15가지 시그널 생성기
│   │   ├── regime_router.py      # ★ Dynamic Regime Router (auto 모드)
│   │   ├── combiner.py          # 레짐별 가중 합산 → (direction, strength)
│   │   ├── filters.py           # 품질 필터 (ER, Hurst, Min ATR)
│   │   ├── ict.py               # ICT 분석 (BOS, CHoCH, FVG, Liquidity)
│   │   └── scalp.py             # 트레이드 모드 판별 (SCALP / TREND_RIDE)
│   │
│   ├── risk/
│   │   ├── engine.py            # 동적 레버리지 역산 + PURE_SCALP/SCALP/TREND_RIDE 파라미터
│   │   ├── guards.py            # 손실/낙폭/변동성 가드 (ALLOW/PAUSE/REDUCE/HALT)
│   │   └── profit_protector.py  # 수익금 자동 펀딩 계좌 이전
│   │
│   ├── execution/
│   │   ├── state_machine.py     # 포지션 FSM (FLAT / LONG / SHORT)
│   │   └── order_manager.py     # 주문 생성/재시도/취소, PostOnly↔Market 자동 선택
│   │
│   ├── db/
│   │   └── database.py          # SQLite WAL 모드 CRUD
│   │
│   └── backtest/
│       ├── engine.py            # 봉 단위 시뮬레이션 (swing/scalp)
│       ├── trend_ema_backtest.py # TrendEMA 전용 백테스트
│       ├── analyzer.py          # Sharpe, MDD, PF, 승률
│       ├── data_loader.py       # 과거 데이터 로더
│       ├── synthetic.py         # 합성 BTC 데이터 생성기 (GBM + GARCH)
│       └── report.py            # HTML 보고서 + Quality Gate 판정
│
├── src/reporting/
│   └── daily_briefing.py        # 09:00/21:00 KST 정기 브리핑 (장세 판단 + 운용 전략)
│
├── tests/                       # pytest 355개 테스트 케이스 (전체 통과)
│   ├── test_trend_ema.py        # ★ 추세EMA 전략 33개 테스트
│   ├── test_scalping_strategy.py  # 스캘핑 전략 52개 테스트
│   ├── test_risk.py
│   ├── test_signals.py
│   └── ...
│
├── .github/
│   └── workflows/
│       └── fly-deploy.yml       # GitHub Actions CI/CD (main 브랜치 push → 자동 배포)
│
├── fly.toml                     # Fly.io 배포 설정
├── Dockerfile                   # python:3.11-slim 기반
├── requirements.txt
└── .env                         # 환경변수 (git 미포함)
```

---

## 15. 설치 및 실행

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

# 전체 테스트 실행 (355개)
python3 -m pytest tests/ -v

# Dry-run 모드 (주문 없음, API 불필요)
TRADING_MODE=dry-run python -m src.main
```

### 전략 선택

```bash
# 추세EMA 전략 (권장 — 역추세 진입 차단)
STRATEGY_MODE=trend python -m src.main

# 스캘핑 전략 (3분봉)
STRATEGY_MODE=scalp python -m src.main

# 스윙 전략 (15분봉)
STRATEGY_MODE=swing python -m src.main
```

### 운영 모드

| 모드 | API 연결 | 실제 주문 | 권장 용도 |
|------|----------|-----------|-----------|
| `dry-run` | 불필요 | 없음 | 코드 동작 확인 |
| `paper` | 필요 | 없음 | 2~4주 시그널 품질 검증 |
| `live` | 필요 | **있음** | Quality Gate 통과 후만 허용 |

---

## 16. Fly.io 배포 & GitHub Actions CI/CD

### GitHub Actions 자동 배포

`main` 브랜치에 push하면 자동으로 fly.io에 배포됩니다.

```
git push origin main
  → GitHub Actions (fly-deploy.yml) 트리거
  → flyctl deploy --remote-only 실행
  → fly.io 머신 무중단 재배포
```

### 수동 배포 (필요 시)

```bash
export PATH="~/.fly/bin:$PATH"

# 환경변수 등록 (전략 변경 시)
fly secrets set STRATEGY_MODE=trend --app bybit-futures-bot

# 배포 (--no-cache: 코드 변경 시 캐시 무효화)
fly deploy --app bybit-futures-bot --strategy immediate --no-cache

# 머신이 stopped 상태인 경우 수동 시작
fly machine start <machine_id> --app bybit-futures-bot

# 로그 확인
fly logs --app bybit-futures-bot --no-tail
```

> **주의**: `fly secrets set`은 즉시 머신을 재시작합니다. 새 STRATEGY_MODE 값을 수락하는
> 코드가 아직 배포되지 않은 경우 크래시가 발생할 수 있습니다. 이 경우 `--no-cache` 배포 후
> `fly machine start`로 머신을 수동 시작합니다.

**운영 비용**: shared-cpu-1x 256MB ≈ $2.02/월 + 1GB 볼륨 $0.15/월 = **약 $2.17/월**

---

## 17. 주요 설정 파라미터

### 전략 선택

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `STRATEGY_MODE` | `swing` | `trend` / `scalp` / `swing` / `auto` 전략 선택 |

### 추세EMA 전략 파라미터 (`STRATEGY_MODE=trend`)

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `TREND_EMA_ADX_THRESHOLD` | `22.0` | HTF ADX ≥ 이 값이면 추세 국면 판단 (18→22) |
| `TREND_EMA_MIN_SCORE` | `4` | 추세 진입 최소 점수 (5점 만점) |
| `TREND_EMA_RANGE_MIN_SCORE` | `4` | 횡보 진입 최소 점수 (더 보수적) |
| `TREND_EMA_RSI_BULL_MIN/MAX` | `40.0 / 65.0` | BULL_TREND 롱 RSI 허용 구간 (38-63→40-65) |
| `TREND_EMA_RSI_BEAR_MIN/MAX` | `40.0 / 60.0` | BEAR_TREND 숏 RSI 허용 구간 (37-63→40-60) |
| `TREND_EMA_RSI_RANGE_LONG_MAX` | `35.0` | SIDEWAYS 롱: RSI 35 이하 (강한 과매도) |
| `TREND_EMA_RSI_RANGE_SHORT_MIN` | `65.0` | SIDEWAYS 숏: RSI 65 이상 (강한 과매수) |
| `TREND_EMA_ZONE_ATR_MULT` | `2.0` | EMA21 ±2.0×ATR = 풀백/반등 진입 존 |
| `TREND_EMA_VOLUME_MULT` | `1.1` | 거래량 ≥ 20봉 평균 × 1.1 |

### 스캘핑 전략 파라미터 (`STRATEGY_MODE=scalp`)

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `SCALP_PURE_LTF_TIMEFRAME` | `3` | LTF 타임프레임 (분) |
| `SCALP_PURE_HTF_TIMEFRAME` | `15` | HTF 타임프레임 (분) |
| `SCALP_PURE_RSI_LONG_MIN/MAX` | `45.0 / 65.0` | 롱 RSI 허용 구간 (40-65→45-65) |
| `SCALP_PURE_RSI_SHORT_MIN/MAX` | `42.0 / 55.0` | 숏 RSI 허용 구간 (35-60→42-55) |
| `SCALP_PURE_ENTRY_MIN_SCORE` | `8` | 최소 진입 점수 (5→8, 고품질 거래만) |
| `SCALP_PURE_STOP_ATR` | `1.2` | 손절 ATR 배수 (1.0→1.2) |
| `SCALP_PURE_TP1_ATR` | `1.5` | 1차 익절 ATR 배수 |
| `SCALP_PURE_TP2_ATR` | `3.0` | 2차 익절 ATR 배수 |

### 자본 및 리스크

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `TRADING_MODE` | `paper` | `dry-run` / `paper` / `live` |
| `MAX_RISK_PER_TRADE_PCT` | `0.02` | 거래당 최대 손실 (자본의 2%) |
| `MAX_DAILY_LOSS_PCT` | `0.08` | 일간 손실 한도 |
| `MAX_WEEKLY_LOSS_PCT` | `0.15` | 주간 손실 한도 |
| `MAX_DRAWDOWN_PCT` | `0.30` | 최대 낙폭 한도 |
| `MAX_LEVERAGE_CAP` | `20` | BULL 모드 최대 레버리지 |
| `BEAR_MAX_LEVERAGE_CAP` | `20` | BEAR 모드 최대 레버리지 |
| `MAX_CONCURRENT_POSITIONS` | `2` | 동시 최대 포지션 수 |

### 손절 바닥값 (Min Stop Floor)

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `MIN_STOP_PCT_PURE_SCALP` | `0.004` | PURE_SCALP 모드 최소 손절폭 (0.4%) |
| `MIN_STOP_PCT_SCALP` | `0.007` | SCALP 모드 최소 손절폭 (0.7%) |
| `MIN_STOP_PCT_TREND_RIDE` | `0.012` | TREND_RIDE 모드 최소 손절폭 (1.2%) |

---

## 18. 기술 스택

| 분류 | 라이브러리 / 도구 | 선택 이유 |
|------|------------------|-----------|
| **거래소 연동** | `pybit 5.8.0` (Bybit v5 API) | 공식 SDK, Hedge Mode 지원 |
| **데이터 처리** | `pandas 2.2`, `numpy 1.26`, `scipy 1.14` | 벡터 연산 + 통계 분석 |
| **설정 관리** | `pydantic-settings 2.6`, `python-dotenv` | 타입 안전 설정, 환경변수 자동 파싱 |
| **스케줄링** | `APScheduler 3.10` | 정확한 주기 실행 (scalp/trend: 3분 / swing: 15분) |
| **데이터베이스** | SQLite WAL 모드 | 경량, 서버리스, 동시 읽기/쓰기 |
| **로깅** | `structlog 24.4` | 구조화 로그 |
| **알림** | `slack-bolt 1.21` (Socket Mode) | 실시간 양방향 Slack 통신 |
| **HTTP** | `httpx 0.27`, `aiohttp 3.10` | 비동기 HTTP |
| **테스트** | `pytest 8.3` | 355개 테스트 케이스 전체 통과 |
| **배포** | Fly.io + Docker (`python:3.11-slim`) | 컨테이너 기반 무중단 배포 |
| **CI/CD** | GitHub Actions | main 브랜치 push → 자동 fly.io 배포 |

---

## 19. 기술적 의사결정 기록

### Phase-locked Direction System을 도입한 이유

이전 스캘핑 전략은 LTF EMA9>EMA21 신호만으로 방향을 결정했습니다.
결과적으로 BEAR 매크로 환경에서도 LTF 반등 시 LONG을 진입 — **역추세 진입으로 지속적 손실** 발생.

**해결 접근**:
1. HTF(15분봉) EMA21/50/200 정렬 + ADX로 "시장 장세(Phase)"를 먼저 판단
2. BULL_TREND이면 LONG만, BEAR_TREND이면 SHORT만 허용
3. 이를 통해 역추세 진입을 코드 레벨에서 구조적으로 차단

**현재 배포된 BEAR 매크로 환경**에서 HTF를 보면 EMA21 < EMA50 < EMA200이 확인되어
BEAR_TREND로 분류 → SHORT 신호만 생성됩니다.

### 추세EMA의 min_score를 3→4로 높인 이유

- score 3 = EMA 정렬(+2) + 조건 1개 = 과도한 신호 생성 (noisy)
- score 4 = EMA 정렬(+2) + 조건 2개 = 복수 확인 조건 필요
- 실전 환경에서 signal quality > signal quantity가 중요

### 스캘핑 score별 strength를 엔진 티어 경계에 정렬한 이유

**이전 설계의 문제**: 선형 공식 `0.60 + (score-5)×0.07` 적용 시 score 7 → 0.74로 계산되어
≥0.75 경계를 넘지 못합니다. 즉, score 6(0.67)과 score 7(0.74) 모두 동일한 4-5× 티어에 속해
레버리지 에스컬레이션 단계가 사라집니다.

**현재 설계**: `_SCORE_STRENGTH_MAP` 딕셔너리로 각 score를 해당 티어 중간값에 명시적 매핑
- score 7 → 0.77 (≥0.75 경계 초과 → 8-10× 티어 진입)
- score 8 → 0.83 (≥0.80 경계 초과 → 15-18× 티어 진입)
- score 9만 0.91 (≥0.90 경계 초과 → 20× + 시장가 트리거)

### PostOnly 재시도 시 orderbook 가격 갱신을 추가한 이유

이전 코드는 최초 진입가를 재시도 시에도 동일하게 사용 → 시장이 움직이면 3번 모두 동일한
stale 가격으로 주문 → 51초 대기 후 결국 시장가 전환.

`order_manager.py`의 재시도 루프에서 `get_orderbook()`으로 현재 bid/ask 갱신
→ 총 대기 시간: 51초 → 30초 이하로 단축.

### 레버리지를 "입력"이 아닌 "결과"로 설계한 이유

"거래당 최대 손실 = 자본의 2%"를 **목표로 고정**하고,
손절 거리에서 역산하여 레버리지를 결정합니다.
결과적으로 변동성이 클수록 레버리지가 낮아지고, 잔잔한 시장에서는 높아집니다.

### Min Stop Floor를 도입한 이유

ETH의 3분봉 ATR이 0.19%일 때 stop_atr_scale=0.35를 곱하면 손절가가 진입가 대비 0.07% ($3.78)
→ 일반적인 틱 노이즈에도 즉시 손절 발동. **Min Floor**(PURE_SCALP 0.4%, SCALP 0.7%, TREND_RIDE 1.2%)를
도입하여 최소 의미있는 가격 움직임에서만 손절이 발동되도록 보장.
Floor 적용 시 TP도 `tp_scale = floor / raw_stop` 비율로 비례 확대 → R:R 유지.

### DI 방향성 확인을 추가한 이유 (3중 필터)

ADX만으로는 추세 "강도"만 판단 가능 — **방향**은 알 수 없음.
ADX=25이지만 -DI>+DI인 경우 하락 추세 → 여기서 LONG 진입하면 역추세.
DI+/DI- 방향 확인을 ADX 조건에 추가하여 **추세 강도 + 방향** 동시 검증 (3중 필터: EMA + ADX + DI).

### 핀바 패턴을 캔들 스코어에 반영한 이유

기존 "마지막 봉 양봉" 조건은 방향만 확인. 핀바(하단 윅 > 0.35×ATR)는 **실제 매수세가 존재**한다는
더 강력한 반전 신호. "양봉 OR 핀바" 조건으로 변경하여 음봉이어도 아래꼬리가 길면 반등 포착 가능.

---

## 주의사항

- **API 키 권한**: 발급 시 **출금(Withdrawal) 권한은 절대 부여하지 마십시오.**
- **API 시크릿 보안**: `.env` 파일은 절대 git에 커밋하지 마십시오. (`.gitignore`에 등록됨)
- **단계적 전환**: `dry-run` → `paper` (최소 2주 검증) → `live` 순서로 전환하십시오.
- **Quality Gate 필수**: `TRADING_MODE=live` 전환은 반드시 백테스트 Quality Gate 통과 후 설정하십시오.
- **Hedge Mode**: Bybit 계정에서 Hedge Mode가 활성화되어 있어야 합니다.

---

## License

MIT
