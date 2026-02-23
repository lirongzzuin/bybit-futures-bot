"""
Daily Briefing Generator.
Called by the scheduler at 9:00 AM and 9:00 PM KST.

period_pnl: 마지막 브리핑 이후 발생한 수익/손실
  - 브리핑마다 스냅샷을 찍고, 다음 브리핑 때 차이를 계산
  - 재시작 후에는 DB에 저장된 스냅샷 기준으로 복원
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from src.api.bybit_client import BybitClient
from src.api.slack import SlackNotifier
from src.config import Settings
from src.db.database import Database
from src.execution.state_machine import StateMachine
from src.logger import get_logger

log = get_logger(__name__)

_KST = timezone(timedelta(hours=9))
_SNAPSHOT_KEY = "briefing_equity_snapshot"


class DailyBriefing:
    def __init__(
        self,
        settings: Settings,
        client: BybitClient,
        db: Database,
        slack: SlackNotifier,
        state_machine: StateMachine,
    ):
        self._s = settings
        self._client = client
        self._db = db
        self._slack = slack
        self._sm = state_machine
        # 마지막 브리핑 시 equity 스냅샷 (재시작 시 DB에서 복원)
        self._last_equity: float = 0.0

    def send(self) -> None:
        """Compile and send briefing to Slack."""
        try:
            report = self._build_report()
            self._slack.send_daily_briefing(report)
            # 이번 브리핑 시 equity를 스냅샷으로 저장 (다음 브리핑의 기준점)
            self._last_equity = report["equity"]
            self._db.set_state(_SNAPSHOT_KEY, str(self._last_equity))
            log.info("Daily briefing sent | snapshot equity=%.2f", self._last_equity)
        except Exception as e:
            log.error("Daily briefing failed: %s", e)
            self._slack.send_error(str(e), context="daily_briefing")

    def _get_last_equity_snapshot(self) -> float:
        """DB에서 마지막 브리핑 equity 스냅샷을 복원한다."""
        if self._last_equity > 0:
            return self._last_equity
        try:
            val = self._db.get_state(_SNAPSHOT_KEY)
            if val:
                self._last_equity = float(val)
                return self._last_equity
        except Exception:
            pass
        return 0.0

    def _build_report(self) -> Dict[str, Any]:
        now_kst = datetime.now(tz=_KST)
        # 브리핑 세션 이름: 09시 = "오전 브리핑", 21시 = "오후 브리핑"
        session = "오전 브리핑" if now_kst.hour < 15 else "오후 브리핑"

        # Equity
        equity = self._get_equity()
        total_pnl = self._db.get_total_pnl(mode=self._s.TRADING_MODE)
        initial = self._s.PAPER_CAPITAL_USDT
        total_pct = total_pnl / initial if initial > 0 else 0

        # 기간 PnL: 마지막 브리핑 이후 수익 (스냅샷 기준)
        last_snap = self._get_last_equity_snapshot()
        if last_snap > 0:
            period_pnl = equity - last_snap
            period_pct = period_pnl / last_snap if last_snap > 0 else 0.0
        else:
            # 첫 브리핑: 오늘 PnL을 period로 사용
            period_pnl = self._db.get_today_pnl()
            period_pct = period_pnl / (equity - period_pnl) if (equity - period_pnl) > 0 else 0.0

        # Drawdown
        peak = self._db.get_peak_equity(mode=self._s.TRADING_MODE)
        drawdown = (peak - equity) / peak if peak > 0 else 0

        # Daily stats (오늘 거래 횟수)
        daily_stats = self._db.get_daily_stats(days=1)
        trades_today = daily_stats[0].get("trades", 0) if daily_stats else 0

        # 최근 100개 거래 승률
        recent_trades = self._db.get_trades(limit=100, mode=self._s.TRADING_MODE)
        wins = sum(1 for t in recent_trades if float(t.get("pnl", 0)) > 0)
        win_rate = wins / len(recent_trades) if recent_trades else 0

        # Open positions
        open_positions = self._sm.all_positions()
        pos_list = []
        total_lev = 0
        total_exposure = 0
        for p in open_positions:
            pos_list.append({
                "symbol": p.symbol,
                "side": p.state.value,
                "size": round(p.qty, 4),
                "unrealised_pnl": round(p.unrealised_pnl, 2),
            })
            total_lev += p.leverage
            total_exposure += p.qty * p.entry_price

        avg_lev = total_lev / len(open_positions) if open_positions else 0
        exposure_pct = total_exposure / equity if equity > 0 else 0

        return {
            "session": session,
            "date": now_kst.strftime("%Y-%m-%d"),
            "time": now_kst.strftime("%H:%M KST"),
            "equity": equity,
            "period_pnl": period_pnl,
            "period_pct": period_pct,
            "total_pnl": total_pnl,
            "total_pct": total_pct,
            "drawdown": drawdown,
            "positions": pos_list,
            "trades_today": trades_today,
            "win_rate": win_rate,
            "leverage_avg": avg_lev,
            "exposure_pct": exposure_pct,
            "mode": self._s.TRADING_MODE,
        }

    def _get_equity(self) -> float:
        if self._s.is_dry_run() or self._s.is_paper():
            total_pnl = self._db.get_total_pnl(mode=self._s.TRADING_MODE)
            return self._s.PAPER_CAPITAL_USDT + total_pnl
        try:
            return self._client.get_usdt_equity()
        except Exception as e:
            log.error("Failed to fetch equity: %s", e)
            return 0.0
