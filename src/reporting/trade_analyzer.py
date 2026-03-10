"""
Trade Loss Analyzer — 손실 거래 패턴 분석 및 자동 개선 리포트.

12시간마다 실행되어 최근 손실 거래를 분석하고, 패턴을 식별해
Slack으로 리포트합니다.

기록 항목 (trades.extra JSON):
  진입 시점:
    - signal_strength, direction, regime, trade_mode
    - category_scores (trend, reversion, volume, ict, ...)
    - atr_pct (ATR / entry_price), spread_pct, funding_rate
    - vol_percentile, drawdown_pct, consecutive_losses
    - leverage, stop_distance_pct, tp1_distance_pct
    - entry_order_type (Market/Limit)
    - macro_regime (BULL/BEAR_SIDEWAYS)
    - auto_regime, auto_direction_bias (auto 모드)

  청산 시점 (update_trade_extra로 추가):
    - exit_reason, exit_price, bars_in_trade
    - max_favorable_pct (최대 유리 이동 %), max_adverse_pct (최대 불리 이동 %)
    - pnl_net, pnl_pct
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from src.api.slack import SlackNotifier
from src.config import Settings
from src.db.database import Database
from src.logger import get_logger

log = get_logger(__name__)
_KST = timezone(timedelta(hours=9))


class TradeAnalyzer:
    """손실 거래 패턴을 분석하고 개선 인사이트를 추출합니다."""

    def __init__(self, db: Database, slack: SlackNotifier, settings: Settings):
        self._db = db
        self._slack = slack
        self._s = settings

    # ── Public: 스케줄러에서 호출 ──────────────────────────────────────────

    def run_analysis(self) -> None:
        """최근 거래를 분석하고 Slack으로 리포트 발송."""
        try:
            report = self._analyze_recent_losses(days=7)
            if report:
                self._slack.send_message(report)
                log.info("Trade analysis report sent to Slack")
            else:
                log.info("Trade analysis: insufficient data for report")
        except Exception as e:
            log.exception("Trade analysis failed: %s", e)

    # ── Analysis Core ─────────────────────────────────────────────────────

    def _analyze_recent_losses(self, days: int = 7) -> Optional[str]:
        """최근 N일간 손실 거래를 분석하고 리포트 문자열 반환."""
        trades = self._db.get_trades_with_extra(days=days)
        if len(trades) < 5:
            return None

        losses = [t for t in trades if (t.get("pnl") or 0) < 0]
        wins = [t for t in trades if (t.get("pnl") or 0) > 0]

        if not losses:
            return None

        total = len(trades)
        win_rate = len(wins) / total * 100 if total > 0 else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0

        # ── 패턴 분석 ─────────────────────────────────────────────────────
        patterns = []

        # 1. 레짐별 승률
        regime_stats = self._stats_by_field(trades, "regime")
        worst_regime = self._find_worst(regime_stats)
        if worst_regime:
            patterns.append(worst_regime)

        # 2. 트레이드 모드별 승률
        mode_stats = self._stats_by_field(trades, "trade_mode")
        worst_mode = self._find_worst(mode_stats)
        if worst_mode:
            patterns.append(worst_mode)

        # 3. 심볼별 승률
        symbol_stats = self._stats_by_key(trades, "symbol")
        worst_symbol = self._find_worst(symbol_stats)
        if worst_symbol:
            patterns.append(worst_symbol)

        # 4. 신호 강도 버킷별 승률
        strength_stats = self._stats_by_strength_bucket(trades)
        worst_strength = self._find_worst(strength_stats)
        if worst_strength:
            patterns.append(worst_strength)

        # 5. 손절 원인 분석
        exit_reasons = self._count_exit_reasons(losses)

        # 6. 고확신 손실 (strength >= 0.75) — 가장 문제가 되는 거래
        high_conf_losses = [
            t for t in losses
            if self._get_extra(t, "signal_strength", 0) >= 0.75
        ]

        # 7. 스프레드 / 펀딩 / 변동성 분석
        condition_insights = self._analyze_market_conditions(losses, wins)

        # 8. 카테고리 점수 패턴 (어떤 신호가 손실과 연관?)
        category_insights = self._analyze_category_scores(losses, wins)

        # 9. 최대 유리/불리 이동 분석 (MFE/MAE)
        mfe_mae_insights = self._analyze_mfe_mae(losses)

        # ── 리포트 생성 ───────────────────────────────────────────────────
        now_kst = datetime.now(tz=_KST).strftime("%m/%d %H:%M")
        lines = [
            f"📊 *거래 분석 리포트* ({now_kst} KST)",
            f"기간: 최근 {days}일 | 총 {total}건 (승 {len(wins)} / 패 {len(losses)})",
            f"승률: {win_rate:.1f}% | 평균수익: {avg_win:+.2f} | 평균손실: {avg_loss:+.2f}",
            "",
        ]

        if high_conf_losses:
            lines.append(f"⚠️ *고확신 손실* (strength≥0.75): {len(high_conf_losses)}건")
            for t in high_conf_losses[:5]:
                extra = self._parse_extra(t)
                lines.append(
                    f"  • {t['symbol']} {extra.get('regime','?')} "
                    f"str={extra.get('signal_strength', '?'):.2f} "
                    f"mode={extra.get('trade_mode','?')} "
                    f"exit={t.get('exit_reason','?')} "
                    f"pnl={t.get('pnl',0):+.2f}"
                )
                # MFE/MAE가 있으면 표시
                mfe = extra.get("max_favorable_pct")
                mae = extra.get("max_adverse_pct")
                if mfe is not None or mae is not None:
                    lines.append(
                        f"    MFE={mfe:.2%} MAE={mae:.2%}"
                        if mfe is not None and mae is not None
                        else ""
                    )
            lines.append("")

        if exit_reasons:
            lines.append("📋 *손실 청산 사유*:")
            for reason, count in sorted(exit_reasons.items(), key=lambda x: -x[1]):
                pct = count / len(losses) * 100
                lines.append(f"  • {reason}: {count}건 ({pct:.0f}%)")
            lines.append("")

        if patterns:
            lines.append("🔍 *패턴 인사이트*:")
            for p in patterns:
                lines.append(f"  • {p}")
            lines.append("")

        if condition_insights:
            lines.append("🌡️ *시장 조건별 분석*:")
            for insight in condition_insights:
                lines.append(f"  • {insight}")
            lines.append("")

        if category_insights:
            lines.append("📈 *신호 카테고리 분석*:")
            for insight in category_insights:
                lines.append(f"  • {insight}")
            lines.append("")

        if mfe_mae_insights:
            lines.append("📐 *MFE/MAE 분석* (최대 유리/불리 이동):")
            for insight in mfe_mae_insights:
                lines.append(f"  • {insight}")
            lines.append("")

        # 빈 리포트 방지
        if len(lines) <= 4:
            return None

        return "\n".join(lines)

    # ── Helper: extra 필드 파싱 ───────────────────────────────────────────

    @staticmethod
    def _parse_extra(trade: Dict) -> Dict:
        extra_raw = trade.get("extra", "{}")
        if isinstance(extra_raw, str):
            try:
                return json.loads(extra_raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        return extra_raw if isinstance(extra_raw, dict) else {}

    @staticmethod
    def _get_extra(trade: Dict, key: str, default: Any = None) -> Any:
        extra = TradeAnalyzer._parse_extra(trade)
        return extra.get(key, default)

    # ── Helper: 필드별 승률 통계 ─────────────────────────────────────────

    def _stats_by_field(self, trades: List[Dict], extra_field: str) -> Dict[str, Dict]:
        """extra JSON 내 필드 기준으로 승/패 통계."""
        buckets: Dict[str, Dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
        for t in trades:
            val = self._get_extra(t, extra_field, "unknown")
            pnl = t.get("pnl", 0) or 0
            key = str(val)
            buckets[key]["total"] += 1
            if pnl > 0:
                buckets[key]["wins"] += 1
            elif pnl < 0:
                buckets[key]["losses"] += 1
        return dict(buckets)

    def _stats_by_key(self, trades: List[Dict], db_field: str) -> Dict[str, Dict]:
        """DB 필드 기준으로 승/패 통계."""
        buckets: Dict[str, Dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
        for t in trades:
            val = t.get(db_field, "unknown")
            pnl = t.get("pnl", 0) or 0
            key = str(val)
            buckets[key]["total"] += 1
            if pnl > 0:
                buckets[key]["wins"] += 1
            elif pnl < 0:
                buckets[key]["losses"] += 1
        return dict(buckets)

    def _stats_by_strength_bucket(self, trades: List[Dict]) -> Dict[str, Dict]:
        """신호 강도 구간별 승률."""
        buckets: Dict[str, Dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
        for t in trades:
            strength = self._get_extra(t, "signal_strength", 0)
            if strength >= 0.85:
                bucket = "≥0.85"
            elif strength >= 0.75:
                bucket = "0.75-0.84"
            elif strength >= 0.65:
                bucket = "0.65-0.74"
            else:
                bucket = "<0.65"
            pnl = t.get("pnl", 0) or 0
            buckets[bucket]["total"] += 1
            if pnl > 0:
                buckets[bucket]["wins"] += 1
            elif pnl < 0:
                buckets[bucket]["losses"] += 1
        return dict(buckets)

    @staticmethod
    def _find_worst(stats: Dict[str, Dict]) -> Optional[str]:
        """승률이 가장 낮은 항목 식별 (최소 3건 이상)."""
        worst_key = None
        worst_wr = 1.0
        for key, s in stats.items():
            if s["total"] < 3:
                continue
            wr = s["wins"] / s["total"]
            if wr < worst_wr:
                worst_wr = wr
                worst_key = key
        if worst_key and worst_wr < 0.50:
            s = stats[worst_key]
            return (
                f"{worst_key}: 승률 {worst_wr:.0%} "
                f"({s['wins']}승 {s['losses']}패 / {s['total']}건)"
            )
        return None

    @staticmethod
    def _count_exit_reasons(losses: List[Dict]) -> Dict[str, int]:
        counts: Dict[str, int] = defaultdict(int)
        for t in losses:
            reason = t.get("exit_reason", "UNKNOWN")
            counts[reason] += 1
        return dict(counts)

    # ── 시장 조건 분석 ───────────────────────────────────────────────────

    def _analyze_market_conditions(self, losses: List[Dict], wins: List[Dict]) -> List[str]:
        insights = []

        # 스프레드 분석
        loss_spreads = [self._get_extra(t, "spread_pct", 0) for t in losses]
        win_spreads = [self._get_extra(t, "spread_pct", 0) for t in wins]
        if loss_spreads and win_spreads:
            avg_loss_spread = sum(loss_spreads) / len(loss_spreads)
            avg_win_spread = sum(win_spreads) / len(win_spreads)
            if avg_loss_spread > avg_win_spread * 1.3 and avg_loss_spread > 0:
                insights.append(
                    f"손실 시 평균 스프레드 {avg_loss_spread:.3%} "
                    f"(승리 시 {avg_win_spread:.3%}) — 스프레드 넓을 때 손실 집중"
                )

        # 변동성 분석
        loss_vols = [self._get_extra(t, "vol_percentile", 0.5) for t in losses]
        win_vols = [self._get_extra(t, "vol_percentile", 0.5) for t in wins]
        if loss_vols and win_vols:
            avg_loss_vol = sum(loss_vols) / len(loss_vols)
            avg_win_vol = sum(win_vols) / len(win_vols)
            if avg_loss_vol > 0.7 and avg_loss_vol > avg_win_vol + 0.1:
                insights.append(
                    f"고변동성(vol pct {avg_loss_vol:.0%})에서 손실 집중 "
                    f"(승리 시 {avg_win_vol:.0%})"
                )

        # 펀딩비 분석
        loss_funding = [abs(self._get_extra(t, "funding_rate", 0)) for t in losses]
        win_funding = [abs(self._get_extra(t, "funding_rate", 0)) for t in wins]
        if loss_funding and win_funding:
            avg_loss_fund = sum(loss_funding) / len(loss_funding)
            avg_win_fund = sum(win_funding) / len(win_funding)
            if avg_loss_fund > avg_win_fund * 1.5 and avg_loss_fund > 0.0003:
                insights.append(
                    f"높은 펀딩비({avg_loss_fund:.4%})에서 손실 빈번 "
                    f"(승리 시 {avg_win_fund:.4%})"
                )

        # ATR 분석 (손절 타이트 vs 넓은)
        loss_atrs = [self._get_extra(t, "atr_pct", 0) for t in losses]
        win_atrs = [self._get_extra(t, "atr_pct", 0) for t in wins]
        if loss_atrs and win_atrs:
            avg_loss_atr = sum(loss_atrs) / len(loss_atrs)
            avg_win_atr = sum(win_atrs) / len(win_atrs)
            if avg_loss_atr < avg_win_atr * 0.7 and avg_loss_atr > 0:
                insights.append(
                    f"저변동(ATR {avg_loss_atr:.3%})에서 손절 빈번 "
                    f"— stop이 너무 타이트할 수 있음"
                )

        return insights

    # ── 카테고리 점수 분석 ────────────────────────────────────────────────

    def _analyze_category_scores(self, losses: List[Dict], wins: List[Dict]) -> List[str]:
        """어떤 신호 카테고리가 손실/승리와 연관되는지 분석."""
        insights = []
        categories = ["trend", "reversion", "volatility", "volume", "ict",
                       "vwap", "orderbook", "squeeze", "momentum", "trend_ema"]

        for cat in categories:
            loss_scores = []
            win_scores = []
            for t in losses:
                scores = self._get_extra(t, "category_scores", {})
                if isinstance(scores, dict) and cat in scores:
                    loss_scores.append(abs(scores[cat]))
            for t in wins:
                scores = self._get_extra(t, "category_scores", {})
                if isinstance(scores, dict) and cat in scores:
                    win_scores.append(abs(scores[cat]))

            if len(loss_scores) >= 3 and len(win_scores) >= 3:
                avg_loss = sum(loss_scores) / len(loss_scores)
                avg_win = sum(win_scores) / len(win_scores)
                # 손실 시 특정 카테고리가 약하면 문제
                if avg_loss < avg_win * 0.6 and avg_win > 0.1:
                    insights.append(
                        f"{cat}: 손실 시 약한 신호(avg {avg_loss:.2f}) "
                        f"vs 승리 시(avg {avg_win:.2f})"
                    )
                # 손실 시 특정 카테고리가 반대 방향이면 심각
                loss_raw = []
                for t in losses:
                    scores = self._get_extra(t, "category_scores", {})
                    if isinstance(scores, dict) and cat in scores:
                        loss_raw.append(scores[cat])
                if loss_raw:
                    # 진입 방향과 반대인 카테고리 점수 비율
                    contrary = sum(1 for s in loss_raw if s < -0.1) / len(loss_raw)
                    if contrary > 0.5:
                        insights.append(
                            f"{cat}: 손실 거래의 {contrary:.0%}에서 "
                            f"진입 반대 신호 — 이 카테고리 필터 강화 고려"
                        )

        return insights[:5]  # 최대 5개 인사이트

    # ── MFE/MAE 분석 ─────────────────────────────────────────────────────

    def _analyze_mfe_mae(self, losses: List[Dict]) -> List[str]:
        """Maximum Favorable/Adverse Excursion 분석.
        MFE가 높은 손실 = TP를 더 타이트하게 잡았으면 승리했을 거래.
        MAE가 작은 손실 = 시간 기반 청산이 원인일 가능성.
        """
        insights = []
        mfe_data = []
        mae_data = []

        for t in losses:
            mfe = self._get_extra(t, "max_favorable_pct")
            mae = self._get_extra(t, "max_adverse_pct")
            if mfe is not None:
                mfe_data.append((mfe, t))
            if mae is not None:
                mae_data.append((mae, t))

        if not mfe_data:
            return insights

        # MFE > TP1까지의 절반 이상 도달한 손실 = "거의 성공했던" 거래
        near_winners = [(mfe, t) for mfe, t in mfe_data if mfe > 0.003]  # 0.3% 이상
        if near_winners:
            avg_mfe = sum(m for m, _ in near_winners) / len(near_winners)
            insights.append(
                f"'{len(near_winners)}건 손실이 최대 {avg_mfe:.2%} 유리 이동 후 반전 "
                f"— TP1을 타이트하게 조정하면 일부 포착 가능"
            )

        # 진입 직후 바로 손절된 거래 (MFE < 0.1%)
        instant_stops = [(mfe, t) for mfe, t in mfe_data if mfe < 0.001]
        if len(instant_stops) >= 2:
            insights.append(
                f"{len(instant_stops)}건이 진입 직후 손절 (MFE<0.1%) "
                f"— 진입 타이밍 또는 stop floor 검토 필요"
            )

        return insights
