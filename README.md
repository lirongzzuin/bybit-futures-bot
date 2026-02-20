# Bybit USDT Perpetual Futures Trading Bot

자동화된 Bybit USDT 무기한 선물 트레이딩 봇. 롱/숏 동등 지원, 동적 레버리지, 다중 신호 전략(ICT + 모멘텀 + 레짐 필터), 엄격한 리스크 관리, 무료 클라우드 배포를 지원합니다.

> **경고**: 선물 거래는 레버리지로 인해 원금 전액 손실 위험이 있습니다.
> 반드시 백테스트 품질 게이트를 통과한 뒤 소액으로 페이퍼 트레이딩을 충분히 검증한 후 실거래를 시작하세요.
> 이 소프트웨어는 수익을 보장하지 않습니다.

---

## 목차

1. [아키텍처 개요](#1-아키텍처-개요)
2. [전략 설명](#2-전략-설명)
3. [설치](#3-설치)
4. [환경변수 설정](#4-환경변수-설정)
5. [실행 방법 (모드별)](#5-실행-방법-모드별)
6. [백테스트](#6-백테스트)
7. [배포 가이드](#7-배포-가이드)
8. [LIVE 전환 체크리스트](#8-live-전환-체크리스트)
9. [안전장치 상세](#9-안전장치-상세)
10. [트러블슈팅](#10-트러블슈팅)

---

## 1. 아키텍처 개요

```
┌─────────────────────────────────────────────────────────┐
│                       Bot Scheduler                      │
│           (APScheduler, 15분 주기 / 9AM 브리핑)          │
└────────────────────────┬────────────────────────────────┘
                         │
         ┌───────────────▼───────────────┐
         │         run_cycle()            │
         └───────────────┬───────────────┘
                         │  (for each symbol)
    ┌────────────────────▼────────────────────────┐
    │  DataCollector                               │
    │  OHLCV + Orderbook + Funding + OI           │
    └────────────────────┬────────────────────────┘
                         │
    ┌────────────────────▼────────────────────────┐
    │  Strategy Layer                              │
    │  1. RegimeDetector  (ADX + EMA + OI)        │
    │  2. ICTAnalyzer     (BOS/CHoCH/FVG/Sweep)  │
    │  3. SignalGenerator (10+ signals, 5 cats)   │
    │  4. SignalCombiner  (regime-weighted score) │
    └────────────────────┬────────────────────────┘
                         │
    ┌────────────────────▼────────────────────────┐
    │  Risk Layer                                  │
    │  RiskEngine   (dynamic leverage + sizing)   │
    │  RiskGuards   (hard limits + halt logic)    │
    └────────────────────┬────────────────────────┘
                         │
    ┌────────────────────▼────────────────────────┐
    │  Execution Layer                             │
    │  StateMachine  (FLAT / LONG / SHORT)        │
    │  OrderManager  (Limit→Market fallback)      │
    └────────────────────┬────────────────────────┘
                         │
    ┌────────────────────▼────────────────────────┐
    │  Bybit API (pybit v5)                        │
    │  Rate-limit retry + Circuit breaker         │
    └─────────────────────────────────────────────┘
```

### 배포 아키텍처

```
1순위 (권장): Oracle Cloud Always Free AMD VM
  ├── Ubuntu 22.04 (1 GB RAM, 1 vCPU)
  ├── Python 3.11 + venv
  ├── systemd 서비스 (자동 재시작 + watchdog)
  └── 로그 로테이션 설정

대안: GitHub Actions 스케줄
  ├── 매 15분 workflow dispatch
  ├── SQLite 상태 캐시 (actions/cache)
  └── 무료 2000분/월 (약 67시간, 개인 레포 기준)
```

---

## 2. 전략 설명

### 레짐 인식 (Regime Detection)

| 레짐 | 조건 | 전략 방향 |
|------|------|----------|
| TRENDING_UP | ADX>25, +DI>-DI, EMA 정렬 | 롱 우선, 숏 신호 40% 억제 |
| TRENDING_DOWN | ADX>25, -DI>+DI, EMA 역정렬 | 숏 우선, 롱 신호 40% 억제 |
| RANGING | ADX<20, EMA 혼재 | 평균회귀 (RSI 극단 + BB) |
| HIGH_VOLATILITY | 변동성 85th 퍼센타일 이상 | 모든 신호 강도 60% 축소 |

### 다중 신호 (Multi-Signal)

**카테고리 1: 추세/모멘텀**
- EMA 크로스 (9/21), EMA 정렬 (9/21/50)
- MACD (12/26/9), RSI(14) 모멘텀

**카테고리 2: 평균회귀**
- RSI 극단값 (<30 / >70)
- 볼린저 밴드 극단 (pct_b < 0.05 / > 0.95)

**카테고리 3: 변동성**
- ATR 확장 (추세 모멘텀 확인)
- 변동성 퍼센타일 (신호 강도 조절)

**카테고리 4: 거래량/OI/펀딩**
- 거래량 Z-스코어 스파이크 (>2σ)
- OI 변화 + 가격 방향 (지속/다이버전스)
- 펀딩 레이트 편향 (군중심리 역이용)

**카테고리 5: ICT (공개 자료 기반)**
- BOS (Break of Structure): 스윙 하이/로우 돌파
- CHoCH (Change of Character): 추세 전환 경고
- FVG (Fair Value Gap): 3-캔들 불균형 구간
- 유동성 스윕: 윅이 스윙 레벨 돌파 후 반전
- 프리미엄/디스카운트: 최근 레인지 40/60% 필터

### 레버리지 결정 알고리즘

```
base_leverage = target_risk_pct / stop_distance_pct

dampening_factors:
  × vol_modifier      (변동성 퍼센타일 기반: 0.4 ~ 1.0)
  × liquidity_modifier (스프레드 기반: 0.3 ~ 1.0)
  × funding_modifier   (펀딩 레이트 기반: 0.6 ~ 1.0)
  × signal_modifier    (신호 강도 기반: 0.775 ~ 1.0)
  × health_modifier    (드로우다운+연속손실: 0.25 ~ 1.0)

hard_caps:
  ≤ MAX_LEVERAGE_CAP (기본 20×, 리스크 엔진 상한)
  ≤ symbol_max_leverage (거래소 허용 상한)
  ≥ 1×

1 트레이드 최대 손실 = equity × MAX_RISK_PER_TRADE_PCT
```

---

## 3. 설치

### 로컬 개발

```bash
git clone <your-repo-url> bybit-futures-bot
cd bybit-futures-bot

python3.11 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# → .env 편집
```

### Docker

```bash
cp .env.example .env
# → .env 편집

docker compose up -d bot
docker compose logs -f bot
```

---

## 4. 환경변수 설정

`.env.example`을 복사해 `.env`를 만들고 값을 채워 넣습니다.

```bash
cp .env.example .env
```

핵심 변수:

| 변수 | 설명 | 예시 |
|------|------|------|
| `TRADING_MODE` | dry-run \| paper \| live | `dry-run` |
| `BYBIT_API_KEY` | Bybit API 키 | - |
| `BYBIT_API_SECRET` | Bybit API 시크릿 | - |
| `BYBIT_TESTNET` | 테스트넷 여부 | `true` |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL | - |
| `MAX_LEVERAGE_CAP` | 리스크 엔진 레버리지 상한 | `20` |
| `MAX_RISK_PER_TRADE_PCT` | 트레이드당 최대 리스크 (%) | `0.01` |
| `QUALITY_GATE_PASSED` | 백테스트 통과 여부 | `false` |

---

## 5. 실행 방법 (모드별)

### Dry-run (모의 실행, 기본값)
API 호출은 하지만 주문은 전혀 넣지 않습니다.

```bash
# .env: TRADING_MODE=dry-run
python -m src.main

# 또는 Docker
docker compose up bot
```

### Paper Trading (가상 자본, 실제 신호)
실제 Bybit API에서 시장 데이터를 받아 신호를 생성하지만, 주문은 실제로 넣지 않습니다. 가상 자본으로 PnL을 시뮬레이션합니다.

```bash
# .env: TRADING_MODE=paper, PAPER_CAPITAL_USDT=10000
python -m src.main
```

### Live Trading (실거래)
반드시 백테스트 품질 게이트를 통과한 후에만 활성화할 수 있습니다.

```bash
# 1. 백테스트 먼저 실행
python -m src.backtest.engine --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT --years 5

# 2. 리포트 확인 (reports/ 폴더)
# 3. 모든 지표가 품질 게이트를 통과하면:
# .env: QUALITY_GATE_PASSED=true, TRADING_MODE=live, BYBIT_TESTNET=false
python -m src.main
```

---

## 6. 백테스트

### 실행

```bash
# 기본 (상위 5개 코인, 5년, 15분봉)
python -m src.backtest.engine

# 커스텀
python -m src.backtest.engine \
  --symbols BTCUSDT ETHUSDT SOLUSDT \
  --years 3 \
  --interval 15

# Shell 스크립트 사용
bash scripts/run_backtest.sh
```

### 생성 파일 (reports/ 폴더)

| 파일 | 내용 |
|------|------|
| `backtest_YYYYMMDD_HHMMSS.html` | HTML 리포트 (CAGR/MDD/샤프/월별 성과) |
| `backtest_YYYYMMDD_HHMMSS_trades.csv` | 전체 트레이드 내역 |
| `backtest_YYYYMMDD_HHMMSS_equity.csv` | 자산 곡선 |
| `backtest_YYYYMMDD_HHMMSS_metrics.json` | 핵심 지표 JSON |

### 품질 게이트

| 지표 | 기본 기준 | 변수명 |
|------|-----------|--------|
| Sharpe Ratio | ≥ 1.0 | `QUALITY_GATE_SHARPE` |
| Max Drawdown | ≤ 30% | `QUALITY_GATE_MDD` |
| Win Rate | ≥ 45% | `QUALITY_GATE_WIN_RATE` |
| Profit Factor | ≥ 1.5 | `QUALITY_GATE_PROFIT_FACTOR` |
| Min Trades | ≥ 100 | `QUALITY_GATE_MIN_TRADES` |

모든 기준 통과 시 → **"Set QUALITY_GATE_PASSED=true"** 메시지 출력

워크포워드 검증: 데이터 80% 인샘플 / 20% 아웃오브샘플로 과최적화를 방지합니다.

---

## 7. 배포 가이드

### 1순위: Oracle Cloud Always Free

Oracle Cloud Always Free 티어에서 AMD 컴퓨팅 VM (1 GB RAM) 2개를 영구 무료로 사용할 수 있습니다.

```bash
# 1. Oracle Cloud 계정 생성: cloud.oracle.com/free
# 2. AMD Compute VM 생성 (Ubuntu 22.04 LTS)
# 3. SSH 접속 후 셋업 스크립트 실행

# 로컬에서 코드 전송
./scripts/deploy.sh ubuntu@<your-oracle-ip>

# Oracle VM에서
ssh ubuntu@<your-oracle-ip>
cd bybit-futures-bot
chmod +x scripts/setup_oracle.sh
./scripts/setup_oracle.sh

# 환경변수 설정
nano .env

# 백테스트 실행
./venv/bin/python -m src.backtest.engine

# 봇 시작
sudo systemctl start bybit-bot
sudo systemctl status bybit-bot

# 로그 확인
tail -f logs/bot.log
```

**Oracle VM 방화벽 설정** (외부 인바운드 불필요 — 봇은 아웃바운드만 사용):
```bash
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
```

### 대안: GitHub Actions

무료 레포지토리에서 월 2000분 무료 실행 가능합니다.

```bash
# 1. GitHub 레포지토리 Secrets 설정
# Settings → Secrets → Actions:
# BYBIT_API_KEY, BYBIT_API_SECRET, SLACK_WEBHOOK_URL,
# TRADING_MODE, BYBIT_TESTNET, QUALITY_GATE_PASSED

# 2. paper_trading.yml 워크플로우 활성화
# .github/workflows/paper_trading.yml 이미 포함됨

# 3. 매 15분마다 자동 실행됨
```

**제한사항**: 15분 간격으로 실행되므로 실시간성이 낮습니다. 긴급 청산이 필요한 상황에서 최대 15분 지연이 발생할 수 있습니다. 운영용으로는 Oracle VM을 강력히 권장합니다.

---

## 8. LIVE 전환 체크리스트

```
[ ] Bybit API 키 생성
    - 권한: 주문 읽기/쓰기, 계좌 읽기
    - IP 화이트리스트 설정
    - 출금 권한 없음 (절대 부여 금지)

[ ] 테스트넷에서 최소 1주일 Paper Trading 완료
    - BYBIT_TESTNET=true
    - 주요 알림 (진입/청산/에러) 모두 확인

[ ] 백테스트 품질 게이트 통과 확인
    - reports/ 폴더에서 HTML 리포트 열기
    - IS/OOS 양쪽 모두 기준 충족 확인

[ ] 리스크 파라미터 검토
    - MAX_RISK_PER_TRADE_PCT ≤ 0.01 (1%)
    - MAX_LEVERAGE_CAP ≤ 20
    - MAX_DAILY_LOSS_PCT ≤ 0.05 (5%)
    - MAX_DRAWDOWN_PCT ≤ 0.20 (20%)

[ ] 메인넷 전환
    - BYBIT_TESTNET=false
    - QUALITY_GATE_PASSED=true
    - TRADING_MODE=live

[ ] 소액으로 시작
    - 첫 1주: 전체 자본의 10% 미만으로 운용

[ ] 모니터링 확인
    - Slack 알림 정상 수신
    - 일일 브리핑 9AM 수신
    - 로그 파일 정상 기록

[ ] 비상 정지 절차 숙지
    - sudo systemctl stop bybit-bot  (또는 docker compose stop)
    - Bybit 웹/앱에서 포지션 직접 청산
```

---

## 9. 안전장치 상세

### 하드 가드레일

| 조건 | 동작 |
|------|------|
| 일일 손실 ≥ 5% | 당일 신규 진입 중단 (PAUSE) |
| 주간 손실 ≥ 10% | 주간 신규 진입 중단 (PAUSE) |
| 드로우다운 ≥ 20% | 강제 종료 (HALT) + Slack 알림 |
| 드로우다운 ≥ 14% | 레버리지 축소 (REDUCE) |
| 스프레드 ≥ 1% | 진입 중단 (극단적 유동성 악화) |
| 변동성 ≥ 97th 퍼센타일 | 레버리지 50% 축소 |

### 레버리지 안전장치

- 드로우다운 > 15%: 레버리지 최대 5×로 강제 하향
- 드로우다운 > 10%: 레버리지 최대 10×로 강제 하향
- 연속 손실 5회: 레버리지 약 60% 수준으로 자동 감소
- 고변동 구간: 레버리지 40% 수준

### 주문 안전장치

- 고유 client_order_id (분 단위 해시)로 중복 주문 방지
- 재시작 시 거래소 포지션 재동기화
- 지정가 타임아웃 → 시장가 자동 전환 (최대 3회 시도)
- 서킷 브레이커: API 오류 5회 연속 → 60초 차단

---

## 10. 트러블슈팅

### 봇이 시작하지 않음
```bash
# 로그 확인
tail -f logs/bot.log

# 환경변수 확인
python -c "from src.config import settings; print(settings.TRADING_MODE)"

# 의존성 확인
pip install -r requirements.txt
```

### "Quality gate not passed" 오류
```bash
# LIVE 모드는 백테스트 통과 후에만 가능
# 1. 백테스트 실행
python -m src.backtest.engine

# 2. 결과 확인 후 .env 수정
QUALITY_GATE_PASSED=true
```

### Bybit API 오류
- `10001`: 파라미터 오류 → 종목명, qty 형식 확인
- `10003`: 인증 오류 → API 키/시크릿 확인
- `10006`: 레이트 리밋 → 자동 재시도 (최대 60초)
- `110043`: 레버리지 이미 설정 → 무시됨 (정상)

### Oracle Cloud 메모리 부족
```bash
# 스왑 추가 (1 GB)
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Slack 알림 미수신
- SLACK_WEBHOOK_URL 값 확인 (https://hooks.slack.com/...)
- Webhook App 활성 상태 확인
- 방화벽 아웃바운드 443 허용 확인

---

## 파일 구조

```
bybit-futures-bot/
├── src/
│   ├── config.py                # 설정 (pydantic-settings)
│   ├── logger.py                # 로깅 설정
│   ├── main.py                  # 봇 진입점 + 스케줄러
│   ├── api/
│   │   ├── bybit_client.py      # Bybit v5 API 클라이언트
│   │   └── slack.py             # Slack 알림
│   ├── data/
│   │   ├── universe.py          # 상위 5개 종목 관리
│   │   └── collector.py         # 시장 데이터 수집
│   ├── strategy/
│   │   ├── indicators.py        # 지표 라이브러리
│   │   ├── regime.py            # 레짐 감지
│   │   ├── ict.py               # ICT 개념 구현
│   │   ├── signals.py           # 다중 신호 생성
│   │   └── combiner.py          # 신호 조합
│   ├── risk/
│   │   ├── engine.py            # 동적 레버리지/포지션 크기
│   │   └── guards.py            # 하드 가드레일
│   ├── execution/
│   │   ├── state_machine.py     # FLAT/LONG/SHORT 상태 머신
│   │   └── order_manager.py     # 주문 실행 (지정가/시장가)
│   ├── db/
│   │   └── database.py          # SQLite 영속성
│   ├── backtest/
│   │   ├── data_loader.py       # 5년치 데이터 다운로드
│   │   ├── engine.py            # 백테스트 엔진 + 워크포워드
│   │   └── report.py            # HTML/CSV 리포트 + 품질 게이트
│   └── reporting/
│       └── daily_briefing.py    # 일일 브리핑
├── tests/
│   ├── test_signals.py
│   ├── test_risk.py
│   ├── test_ict.py
│   └── test_order.py
├── scripts/
│   ├── setup_oracle.sh          # Oracle Cloud 셋업
│   ├── deploy.sh                # SSH 배포
│   └── run_backtest.sh          # 백테스트 실행
├── .github/workflows/
│   ├── backtest.yml             # 주간 자동 백테스트
│   └── paper_trading.yml        # GH Actions 대안 배포
├── .env.example                 # 환경변수 예시
├── .gitignore
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── bot.service                  # systemd 서비스
└── README.md
```

---

## 라이선스

MIT License — 사용에 따른 투자 손실에 대한 책임은 사용자 본인에게 있습니다.
