# Bybit USDT Perpetual Futures Trading Bot

> **주의**: 선물 거래는 레버리지로 인해 원금 전액 손실 위험이 있습니다.
> 이 소프트웨어는 수익을 보장하지 않으며, 사용에 따른 투자 손실의 책임은 사용자 본인에게 있습니다.

---

Python으로 설계·구현한 **Bybit USDT 무기한 선물 자동매매 시스템**입니다.
두 가지 독립 전략(스윙/스캘핑)을 설정 한 줄로 전환할 수 있으며,
시장 국면(Regime)과 매크로 레짐(BULL/BEAR)을 실시간으로 분류해 심볼 필터링·레버리지·주문 방식을 자동 결정합니다.
Fly.io 클라우드에 컨테이너로 배포되어 24/7 무중단 운영 중이며, GitHub Actions로 CI/CD 자동 배포가 구성돼 있습니다.

---

## 프로젝트 하이라이트

| 항목 | 내용 |
|------|------|
| **구현 언어** | Python 3.11 |
| **전략 유형** | 스윙(15분봉 멀티 시그널) + 스캘핑(3분봉 EMA Ribbon+VWAP+RSI) — 설정으로 전환 |
| **매크로 필터** | BTC 일봉 EMA200 기반 BULL/BEAR 자동 판단 → 거래 심볼 자동 전환 |
| **리스크 관리** | 동적 레버리지 역산, 다층 손실 가드, 청산 버퍼 검증, 수수료 손익분기 체크 |
| **수수료 최적화** | 신호 강도별 PostOnly(Maker 0.020%) / 시장가(Taker 0.055%) 자동 선택 |
| **테스트** | pytest 276개 테스트 케이스 전체 통과 |
| **배포** | Docker → Fly.io (nrt/Tokyo, 월 ≈ $2.17) + GitHub Actions CI/CD 자동 배포 |
| **모니터링** | Slack Webhook + Socket Mode 명령어 봇 |

---

## 목차

1. [아키텍처 개요](#1-아키텍처-개요)
2. [전략 선택 시스템](#2-전략-선택-시스템)
3. [스캘핑 전략 — EMA Ribbon + VWAP + RSI](#3-스캘핑-전략--ema-ribbon--vwap--rsi)
4. [스윙 전략 — 멀티 시그널 알고리즘](#4-스윙-전략--멀티-시그널-알고리즘)
5. [매크로 레짐 — BULL / BEAR 자동 전환](#5-매크로-레짐--bull--bear-자동-전환)
6. [시장 국면(Regime) 분류](#6-시장-국면regime-분류)
7. [포지션 사이징 및 레버리지 계산](#7-포지션-사이징-및-레버리지-계산)
8. [익절 & 손절 전략](#8-익절--손절-전략)
9. [리스크 가드 시스템](#9-리스크-가드-시스템)
10. [주문 실행 시스템](#10-주문-실행-시스템)
11. [Slack 연동](#11-slack-연동)
12. [데이터베이스 설계](#12-데이터베이스-설계)
13. [프로젝트 구조](#13-프로젝트-구조)
14. [설치 및 실행](#14-설치-및-실행)
15. [Fly.io 배포 & GitHub Actions CI/CD](#15-flyio-배포--github-actions-cicd)
16. [주요 설정 파라미터](#16-주요-설정-파라미터)
17. [기술 스택](#17-기술-스택)
18. [기술적 의사결정 기록](#18-기술적-의사결정-기록)

---

## 1. 아키텍처 개요

매 사이클(`STRATEGY_MODE=scalp` → 3분, `swing` → 15분)마다 아래 파이프라인이 실행됩니다.

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
│  │  "scalp" → collect_for_scalp()  → ScalpingStrategy       │    │
│  │  "swing" → collect()            → ICT + SignalGenerator   │    │
│  │             + SignalCombiner + TradeModeDetector          │    │
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
STRATEGY_MODE=scalp   # 3분봉 스캘핑 전략 (기본 권장)
STRATEGY_MODE=swing   # 15분봉 스윙/추세추종 전략
```

| 항목 | scalp 모드 | swing 모드 |
|------|-----------|-----------|
| **데이터** | 3m LTF + 15m HTF | 15m 메인 + 1D 레짐 |
| **신호 생성** | ScalpingStrategy (9점 스코어링) | ICT + 15가지 지표 + 레짐 가중 합산 |
| **트레이드 모드** | 항상 `PURE_SCALP` | `SCALP` / `TREND_RIDE` 자동 선택 |
| **사이클 주기** | 3분 | 15분 |
| **Stop** | 1.0×ATR | 1.0–2.0×ATR |
| **TP1 / TP2** | 1.5× / 3.0×ATR | 1.5–3.0× / 3.0–6.0×ATR |
| **최대 보유** | 20봉 (60분) | 12–120봉 |

두 전략 모두 동일한 **리스크 가드, 수수료 최적화, Slack 알림, DB 기록, 청산 로직**을 공유합니다.

---

## 3. 스캘핑 전략 — EMA Ribbon + VWAP + RSI

`src/strategy/scalping.py` — `STRATEGY_MODE=scalp` 시 사용

### 신호 생성 원리

**HTF(15m)** 로 추세 바이어스를 확인하고, **LTF(3m)** 로 진입 타이밍을 정밀 포착합니다.

### 롱 진입 스코어링 (9점 만점)

| 조건 | 점수 | 설명 |
|------|------|------|
| HTF EMA9 > EMA21 | +2 | HTF 단기 상승 추세 |
| HTF 가격 > VWAP | +1 | 세션 내 매수 우위 |
| LTF EMA9 > EMA21 | +2 | LTF 단기 상승 추세 확인 |
| 가격이 EMA21 ± ATR 존 내 | +1 | 풀백 진입 구간 |
| RSI 40–65 | +1 | 모멘텀 형성 중, 과매수 아님 |
| 볼륨 ≥ 20봉 평균 × 1.2 | +1 | 참여도 확인 |
| 마지막 봉 양봉 | +1 | 즉각적 방향성 확인 |
| \|펀딩비\| > 0.1% | -1 | 한쪽 쏠림 위험 |
| 스프레드 > 0.2% | -1 | 체결 비용 과다 |

숏 진입: 롱의 정반대 조건

### 점수 → 주문 유형 자동 전환

```
score < 5              → 진입 없음 (중립)
score 5–6 (strength < 0.90) → PostOnly 지정가 (Maker 0.020%)
score 7–9 (strength ≥ 0.91) → 시장가 (Taker 0.055%, 즉시 체결 우선)
```

### 수수료 손익분기 체크 (PURE_SCALP)

| 진입 유형 | 왕복 수수료 | 최소 TP1 이동 (1.5× 배수) |
|-----------|------------|--------------------------|
| PostOnly (Maker) | 0.080% | 0.120% |
| 시장가 (Taker) | 0.115% | 0.173% |

TP1 예상 수익이 위 기준 미달 시 진입 거부 → **수수료 손해 거래 원천 차단**

---

## 4. 스윙 전략 — 멀티 시그널 알고리즘

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

### 트레이드 모드 자동 선택

진입 신호 확정 후 8가지 항목으로 거래 스타일을 결정합니다.

```
+1  ADX ≥ 30                     (강한 추세)
+1  |OI 변화율| ≥ 1.5% + 방향 일치  (기관 참여)
+1  ICT BOS 방향 일치              (구조적 이탈)
+1  순 시그널 강도 ≥ 0.60           (높은 확신)
+1  변동성 백분위 < 70%             (과열 아님)
+1  EMA 방향 일치                  (추세 지지)
-1  |펀딩비| ≥ 0.08%               (군중 리스크)
-1  스프레드 ≥ 0.2%                (실행 비용 과다)

점수 ≥ 2 → TREND_RIDE 모드 (추세 극대화)
점수 < 2 → SCALP 모드 (빠른 수익 확정)
```

---

## 5. 매크로 레짐 — BULL / BEAR 자동 전환

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

- 전환 확정: 연속 `MACRO_BULL_CONFIRM_COUNT`회 동일 신호 → 오탐 방지 디바운스
- 보유 포지션 심볼은 레짐 무관하게 항상 포함 (청산 관리)
- 전환 시 Slack 알림 자동 발송

---

## 6. 시장 국면(Regime) 분류

스윙 전략에서 사용. 15분봉 + 1일봉 듀얼 타임프레임으로 현재 시장 상태를 판단합니다.

| 국면 | 판단 기준 | 전략 |
|------|-----------|------|
| **TRENDING_UP** | ADX ≥ 25, EMA 정배열, Hurst > 0.50, 추세 점수 > 0.2 | 추세 추종 Long |
| **TRENDING_DOWN** | ADX ≥ 25, EMA 역배열, Hurst > 0.50, 추세 점수 < -0.2 | 추세 추종 Short |
| **RANGING** | ADX < 20 | 지지/저항 평균 회귀 |
| **HIGH_VOLATILITY** | 변동성 백분위 > 85% | 포지션 축소, 보수적 운용 |

```
trend_score = (ADX 방향 점수 × 0.50) + (EMA 구조 점수 × 0.40) + (OI 방향 점수 × 0.10)
범위: -1.0 (강한 하락 추세) ~ +1.0 (강한 상승 추세)

regime_confidence = ADX 정규화값 × 0.50 + |trend_score| × 0.50
→ 높은 확신도일수록 진입 시그널 강도를 최대 1.3배 증폭
```

---

## 7. 포지션 사이징 및 레버리지 계산

레버리지는 **입력값이 아닌 결과값**입니다. 리스크 허용량과 손절 거리에서 역산합니다.

### 시그널 강도별 레버리지 에스컬레이션

| 시그널 강도 | stop_atr_scale | trade_risk_pct | 목표 레버리지 |
|------------|----------------|----------------|--------------|
| ≥ 0.85 | 0.35 (타이트) | min(16%, risk×8) | ~20× (캡) |
| ≥ 0.80 | 0.50 | min(12%, risk×6) | ~15-18× |
| ≥ 0.75 | 0.65 | min(9%, risk×4.5) | ~8-10× |
| ≥ 0.65 | 0.80 | min(6%, risk×3) | ~4-5× |
| 기본 | 1.00 | min(4%, risk×2) | ~2-3× |

낙폭 > 15%: 에스컬레이션 자동 비활성화 (기본 risk만 사용)

### 5가지 감쇠 계수

```
× vol_modifier    — 변동성 높으면 감소 (최소 0.55×)
× liq_modifier    — 스프레드 넓으면 감소 (최소 0.30×)
× fund_modifier   — 펀딩비 높으면 감소 (최소 0.60×)
× signal_modifier — 시그널 강도 반영 (0.65 + 0.35×strength)
× health_modifier — 낙폭/연속손실 반영 (최소 0.25×)
```

### 낙폭 비례 레버리지 캡

| 낙폭 | 레버리지 캡 |
|------|-----------|
| > 20% | max(5, cap ÷ 4) |
| > 12% | max(10, cap ÷ 2) |
| ≤ 12% | `MAX_LEVERAGE_CAP` 또는 `BEAR_MAX_LEVERAGE_CAP` |

### 청산 버퍼 검증 (핵심 안전장치)

```
조건: liq_distance > stop_distance × 3.0
→ 청산가가 손절가보다 3배 이상 멀어야만 진입 허용
→ 일시적 급락(Wick)으로 청산당하는 상황 원천 차단
```

---

## 8. 익절 & 손절 전략

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
| SL | 1.0×ATR | 1.2×ATR | 2.0×ATR |
| TP1 | 1.5×ATR | 2.5×ATR | 3.0×ATR |
| TP2 | 3.0×ATR | 5.5×ATR | 6.0×ATR |
| 부분청산 비율 | 50% | 60% | 40% |
| 최대 보유 봉 수 | 20봉 (60분) | 12봉 | 120봉 |

### 주문 유형별 수수료 전략

| 청산 이유 | 주문 유형 | 수수료 |
|-----------|----------|--------|
| TP1, TP2, 트레일링, 레짐, 시그널, 시간 | 지정가 (Maker) | 0.020% |
| SL, 가드 발동, 강제 청산 | 시장가 (Taker) | 0.055% |

### 거래소 사전예약 SL/TP (봇 다운 시 Failsafe)

- **SL**: 시장가 — 즉각 체결 필수
- **TP**: TP2 가격으로 지정가 등록 — TP1 부분청산은 봇 내부 로직이 처리
  (TP1 가격으로 등록하면 전량 시장가 청산되어 부분청산 로직 무력화됨)

---

## 9. 리스크 가드 시스템

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

## 10. 주문 실행 시스템

### 진입 주문 전략

```
스프레드 > SPREAD_TAKER_THRESHOLD(0.3%)
또는 시그널 강도 > SIGNAL_URGENT_THRESHOLD(0.90)
  → 시장가 주문 (즉각 체결, Taker 수수료)

그 외
  → PostOnly 지정가 주문 (Maker 수수료 절약)
     └─ 미체결 → 재시도 (새 client_id로 중복 방지)
        → 그래도 미체결 → 시장가로 전환
```

진입/청산 전 `cancel_all_orders()` 호출 → 미체결 주문 누적 방지

### 서킷 브레이커

- 연속 5회 API 오류 → 60초간 신규 주문 차단
- Rate limit → 지수 백오프 재시도 (최대 10회)
- 중복 주문 에러(코드 110072) → 정상 처리 (멱등성 보장)

---

## 11. Slack 연동

### 실시간 자동 알림

| 이벤트 | 알림 내용 |
|--------|-----------|
| 포지션 진입 | 심볼, 방향, 레버리지, 진입가, SL/TP, 주문 유형, 신호 강도 |
| 전량 청산 | 청산 이유, 실현 손익(수수료 차감), PnL%, 진입/청산가, 레버리지 |
| 강제 청산 감지 | 긴급 경보 |
| 매크로 레짐 전환 | BULL ↔ BEAR_SIDEWAYS 전환, 허용 심볼, 레버리지 캡 |
| 일간 손실 경보 | 하루 한 번만 발송 (디바운스) |
| 정기 브리핑 | 09:00 KST + 21:00 KST (하루 2회) |

> 모든 타임스탬프는 **KST(UTC+9)** 기준으로 표시됩니다.

### 대화형 Slack 명령어 (Socket Mode)

| 명령어 | 내용 |
|--------|------|
| `/dashboard` | 자산 현황, 일간/누적 PnL, 오픈 포지션 수 |
| `/positions` | 현재 포지션 목록 (진입가, 현재가, 레버리지, 미실현 손익) |
| `/pnl` | 오늘 / 이번 주 / 전체 PnL + 승률 + 거래 횟수 |
| `/status` | 봇 상태 (모드, 가동 시간, 마지막 사이클, 서킷 브레이커) |

---

## 12. 데이터베이스 설계

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

## 13. 프로젝트 구조

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
│   │   │                        # collect_for_scalp() : 스캘핑용 (3m LTF + 15m HTF)
│   │   └── universe.py          # 거래 유니버스 자동 선별 (4시간 갱신)
│   │
│   ├── strategy/
│   │   ├── scalping.py          # ★ 스캘핑 전략 (EMA Ribbon + VWAP + RSI 9점 스코어링)
│   │   ├── indicators.py        # ATR, EMA, RSI, StochRSI, OBV, CMF, VWAP 등
│   │   ├── regime.py            # 시장 국면 분류 (ADX + EMA + Hurst)
│   │   ├── macro_regime.py      # 매크로 레짐 (BTC 일봉 EMA200 기반)
│   │   ├── signals.py           # 15가지 시그널 생성기
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
│   ├── backtest/
│   │   ├── engine.py            # 봉 단위 시뮬레이션
│   │   ├── analyzer.py          # Sharpe, MDD, PF, 승률
│   │   ├── data_loader.py       # 과거 데이터 로더
│   │   └── report.py            # HTML 보고서 + Quality Gate 판정
│   │
│   └── reporting/
│       └── daily_briefing.py    # 09:00 / 21:00 KST 정기 브리핑
│
├── tests/                       # pytest 276개 테스트 케이스 (전체 통과)
│   ├── test_scalping_strategy.py  # ★ 스캘핑 전략 52개 테스트
│   ├── test_risk_engine.py
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

## 14. 설치 및 실행

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

# 전체 테스트 실행 (276개)
python3 -m pytest tests/ -v

# Dry-run 모드 (주문 없음, API 불필요)
TRADING_MODE=dry-run python -m src.main

# Paper 트레이딩 (실제 시그널, 가상 자금)
TRADING_MODE=paper python -m src.main
```

### 전략 선택

```bash
# 스캘핑 전략 (3분봉, 권장)
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

## 15. Fly.io 배포 & GitHub Actions CI/CD

### GitHub Actions 자동 배포

`main` 브랜치에 push하면 자동으로 fly.io에 배포됩니다.

```
git push origin main
  → GitHub Actions (fly-deploy.yml) 트리거
  → flyctl deploy --remote-only 실행
  → fly.io 머신 무중단 재배포
```

**최초 설정 (GitHub Secret 등록)**:
1. Fly.io 토큰 발급: `fly tokens create deploy -a bybit-futures-bot`
2. GitHub → Settings → Secrets and variables → Actions → New repository secret
3. Name: `FLY_API_TOKEN`, Value: 발급된 토큰

### 수동 배포 (필요 시)

```bash
export PATH="~/.fly/bin:$PATH"

# 최초 1회
fly apps create bybit-futures-bot
fly volumes create bot_data --size 1 --region nrt

# 환경변수 등록
fly secrets set BYBIT_API_KEY=your_key \
               BYBIT_API_SECRET=your_secret \
               TRADING_MODE=live \
               STRATEGY_MODE=scalp \
               QUALITY_GATE_PASSED=true

# 배포
fly deploy --app bybit-futures-bot --strategy immediate

# 로그 확인
fly logs --app bybit-futures-bot --no-tail
```

**운영 비용**: shared-cpu-1x 256MB ≈ $2.02/월 + 1GB 볼륨 $0.15/월 = **약 $2.17/월**

---

## 16. 주요 설정 파라미터

### 전략 선택

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `STRATEGY_MODE` | `swing` | `scalp` / `swing` 전략 선택 |

### 스캘핑 전략 파라미터 (`STRATEGY_MODE=scalp`)

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `SCALP_PURE_LTF_TIMEFRAME` | `3` | LTF 타임프레임 (분) |
| `SCALP_PURE_HTF_TIMEFRAME` | `15` | HTF 타임프레임 (분) |
| `SCALP_PURE_EMA_FAST` | `9` | 단기 EMA 기간 |
| `SCALP_PURE_EMA_MID` | `21` | 중기 EMA 기간 |
| `SCALP_PURE_EMA_SLOW` | `50` | 장기 EMA 기간 |
| `SCALP_PURE_RSI_PERIOD` | `14` | RSI 기간 |
| `SCALP_PURE_RSI_LONG_MIN/MAX` | `40.0 / 65.0` | 롱 RSI 허용 구간 |
| `SCALP_PURE_RSI_SHORT_MIN/MAX` | `35.0 / 60.0` | 숏 RSI 허용 구간 |
| `SCALP_PURE_VOLUME_MULT` | `1.2` | 볼륨 스파이크 배수 |
| `SCALP_PURE_ENTRY_MIN_SCORE` | `5` | 최소 진입 점수 |
| `SCALP_PURE_ENTRY_URGENT_SCORE` | `7` | 시장가 진입 기준 점수 |
| `SCALP_PURE_STOP_ATR` | `1.0` | 손절 ATR 배수 |
| `SCALP_PURE_TP1_ATR` | `1.5` | 1차 익절 ATR 배수 |
| `SCALP_PURE_TP2_ATR` | `3.0` | 2차 익절 ATR 배수 |
| `SCALP_PURE_MAX_BARS` | `20` | 최대 보유 봉 수 (60분) |
| `SCALP_PURE_PARTIAL_EXIT_PCT` | `0.50` | TP1 부분청산 비율 |

### 자본 및 리스크

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `TRADING_MODE` | `paper` | `dry-run` / `paper` / `live` |
| `PAPER_CAPITAL_USDT` | `10000.0` | Paper 모드 초기 자본 |
| `MAX_RISK_PER_TRADE_PCT` | `0.02` | 거래당 최대 손실 (자본의 2%) |
| `MAX_DAILY_LOSS_PCT` | `0.08` | 일간 손실 한도 |
| `MAX_WEEKLY_LOSS_PCT` | `0.15` | 주간 손실 한도 |
| `MAX_DRAWDOWN_PCT` | `0.30` | 최대 낙폭 한도 |
| `MAX_LEVERAGE_CAP` | `20` | BULL 모드 최대 레버리지 |
| `BEAR_MAX_LEVERAGE_CAP` | `20` | BEAR 모드 최대 레버리지 |
| `MAX_CONCURRENT_POSITIONS` | `3` | 동시 최대 포지션 수 |

### 매크로 레짐

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `BULL_MODE_SYMBOLS` | `BTCUSDT` | BULL 모드 거래 심볼 |
| `BEAR_MODE_SYMBOLS` | `BTCUSDT,ETHUSDT` | BEAR 모드 거래 심볼 |
| `MACRO_REGIME_CHECK_HOURS` | `4` | 매크로 레짐 체크 주기 (시간) |
| `MACRO_BULL_CONFIRM_COUNT` | `2` | BULL 전환 확정 연속 횟수 |
| `MACRO_BEAR_CONFIRM_COUNT` | `2` | BEAR 전환 확정 연속 횟수 |

### 시그널 설정

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `SIGNAL_ENTRY_THRESHOLD` | `0.42` | 진입 시그널 강도 임계값 |
| `SIGNAL_EXIT_THRESHOLD` | `0.35` | 청산 시그널 강도 임계값 |
| `SIGNAL_URGENT_THRESHOLD` | `0.90` | 시장가 진입 강도 임계값 |
| `SPREAD_TAKER_THRESHOLD` | `0.003` | 스프레드 기준 시장가 전환 임계값 |
| `SIGNAL_TIMEFRAME` | `15` | 스윙 메인 타임프레임 (분) |
| `TREND_TIMEFRAME` | `1440` | 레짐 감지 타임프레임 (일봉) |

---

## 17. 기술 스택

| 분류 | 라이브러리 / 도구 | 선택 이유 |
|------|------------------|-----------|
| **거래소 연동** | `pybit 5.8.0` (Bybit v5 API) | 공식 SDK, Hedge Mode 지원 |
| **데이터 처리** | `pandas 2.2`, `numpy 1.26`, `scipy 1.14` | 벡터 연산 + 통계 분석 |
| **설정 관리** | `pydantic-settings 2.6`, `python-dotenv` | 타입 안전 설정, 환경변수 자동 파싱 |
| **스케줄링** | `APScheduler 3.10` | 정확한 주기 실행 (scalp: 3분 / swing: 15분) |
| **데이터베이스** | SQLite WAL 모드 | 경량, 서버리스, 동시 읽기/쓰기 |
| **로깅** | `structlog 24.4` | 구조화 로그 |
| **알림** | `slack-bolt 1.21` (Socket Mode) | 실시간 양방향 Slack 통신 |
| **HTTP** | `httpx 0.27`, `aiohttp 3.10` | 비동기 HTTP |
| **테스트** | `pytest 8.3` | 276개 테스트 케이스 전체 통과 |
| **배포** | Fly.io + Docker (`python:3.11-slim`) | 컨테이너 기반 무중단 배포 |
| **CI/CD** | GitHub Actions | main 브랜치 push → 자동 fly.io 배포 |

---

## 18. 기술적 의사결정 기록

### 스캘핑/스윙 전략을 별도 모드로 분리한 이유

두 전략은 적합한 시장 환경, 타임프레임, 청산 기준이 근본적으로 다릅니다.
하나의 전략에 파라미터로 통합하면 서로 다른 조건이 충돌합니다.

`STRATEGY_MODE` 설정으로 완전히 분리된 파이프라인을 선택하되,
리스크 엔진·주문 관리·상태 머신 등 **공통 인프라는 100% 공유**합니다.

### 스캘핑 score≥7을 시장가로 전환하는 이유

score 7-9의 강한 신호는 시장이 빠르게 움직이는 상황에서 발생합니다.
PostOnly 지정가는 체결 보장이 없어 진입 자체를 놓칠 수 있습니다.

높은 확신도 신호에서 슬리피지(0.035% 추가 비용)를 감수하고 즉시 체결을 보장하는 것이
진입 실패 대비 기대 수익이 높습니다.

### 레버리지를 "입력"이 아닌 "결과"로 설계한 이유

"거래당 최대 손실 = 자본의 2%"를 **목표로 고정**하고,
손절 거리에서 역산하여 레버리지를 결정합니다.
결과적으로 변동성이 클수록 레버리지가 낮아지고, 잔잔한 시장에서는 높아집니다.

### 거래소 TP를 TP2 가격으로 설정하는 이유

TP1 가격으로 등록하면 거래소가 `tpslMode=Full`로 전량 시장가 청산합니다.
이로 인해 봇의 50-60% 부분청산 로직이 완전히 무력화됩니다.

TP2로 등록하면 TP1 부분청산은 봇이 처리하고,
봇 다운 시에도 TP2에서 잔여 포지션이 안전하게 정리됩니다.

### 트레일링 스탑을 TP1 이후에만 활성화하는 이유

TP1 전에 트레일링을 활성화하면 ATR 노이즈 수준의 되돌림에서도 청산됩니다.
TP1 도달 후 손절을 진입가(브레이크이븐)로 이동한 상태에서 활성화하면,
원금 리스크가 제거된 상태에서 추가 수익만 보호합니다.

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
