# Security Guide — Bybit Futures Bot

## 민감 정보 관리 (Sensitive Information Management)

이 봇은 실제 자금을 다루므로 API 키, 웹훅 URL 등의 민감 정보를 **절대 GitHub에 올리지 마세요.**

---

## 빠른 시작 (Quick Start)

```bash
# 1. 템플릿 복사
cp .env.example .env

# 2. 실제 값 입력 (vim, nano, VS Code 등)
nano .env

# 3. .env가 gitignore에 포함됐는지 확인
git status  # .env가 리스트에 없어야 함

# 4. 절대 이렇게 하지 마세요 !!
# git add .env  ← 절대 금지
```

---

## 민감 정보 분류 (Sensitivity Levels)

| 변수 | 민감도 | 설명 |
|------|--------|------|
| `BYBIT_API_KEY` | 🔴 **최고위험** | 거래소 API 키 — 유출 시 자금 도난 가능 |
| `BYBIT_API_SECRET` | 🔴 **최고위험** | API 시크릿 — 절대 공유 금지 |
| `SLACK_WEBHOOK_URL` | 🟠 **위험** | 유출 시 알림 스팸 발생 |
| `TRADING_MODE` | 🟢 안전 | `dry-run` / `paper` / `live` |
| `QUALITY_GATE_PASSED` | 🟡 주의 | `true`로 변경하면 실거래 활성화 |
| 나머지 파라미터 | 🟢 안전 | 전략 설정값 — 공개해도 무방 |

---

## Pre-commit 시크릿 스캐너 설치 (Recommended)

```bash
# pre-commit 설치
pip install pre-commit detect-secrets

# .pre-commit-config.yaml 생성
cat > .pre-commit-config.yaml << 'EOF'
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.4.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.5.0
    hooks:
      - id: check-added-large-files
      - id: check-merge-conflict
      - id: detect-private-key
EOF

# 베이스라인 생성 (현재 파일 스캔)
detect-secrets scan > .secrets.baseline

# hooks 설치
pre-commit install

echo "✅ Pre-commit hooks installed. Secrets will be blocked from commits."
```

---

## 프로덕션 배포 시 권장사항 (Production Deployment)

### Oracle Cloud / 서버 환경

```bash
# .env 파일 권한 제한 (소유자만 읽기)
chmod 600 .env

# 환경변수 로드 확인
python -c "from src.config import settings; print(settings.TRADING_MODE)"
```

### 환경별 파일 분리

```
.env             ← 로컬 개발 (gitignore됨)
.env.production  ← 프로덕션 서버 (gitignore됨)
.env.example     ← 템플릿만 Git에 포함
```

### API 키 권한 최소화 (Bybit)

Bybit API 키 생성 시 필요한 권한만 부여:
- ✅ `Unified Trading` (필수)
- ✅ `Read Position` (필수)
- ✅ `Place Order` (필수)
- ❌ `Withdraw` ← **절대 허용 금지**
- ❌ `Transfer` ← 비활성화 권장

IP 화이트리스트 설정 (강력 권장):
- 봇이 실행될 서버 IP만 허용
- Bybit API Management → IP restriction

---

## GitHub Repository 설정

### Branch Protection
```
main 브랜치 보호 규칙:
- Require pull request before merging
- Require status checks (pytest)
- Do not allow force pushes
```

### GitHub Secrets (Actions 사용 시)
```yaml
# .github/workflows/test.yml에서만 시크릿 사용
# 실제 API 키는 GitHub Secrets에 저장
# Settings → Secrets and variables → Actions
BYBIT_API_KEY: ${{ secrets.BYBIT_API_KEY }}
```

---

## 시크릿 유출 발생 시 (Incident Response)

1. **즉시** Bybit에서 해당 API 키 비활성화
2. 새 API 키 생성
3. `.env` 업데이트
4. Git 히스토리 정리 (만약 커밋됐다면):
   ```bash
   # BFG Repo Cleaner 사용
   bfg --delete-files .env --no-blob-protection
   git push --force
   ```
5. Slack 웹훅 URL 재발급

---

## 체크리스트 (Pre-Deployment Checklist)

- [ ] `.env` 파일이 `.gitignore`에 포함됨
- [ ] `git status`에 `.env`가 나타나지 않음
- [ ] API 키에 Withdraw 권한 없음
- [ ] Bybit API IP 화이트리스트 설정
- [ ] `BYBIT_TESTNET=true` (테스트 후 `false`로 변경)
- [ ] `TRADING_MODE=dry-run` (페이퍼 트레이딩 검증 후 변경)
- [ ] `QUALITY_GATE_PASSED=false` (백테스트 통과 후에만 `true`)
- [ ] Pre-commit hooks 설치됨
- [ ] `.env` 파일 권한 `chmod 600 .env`
