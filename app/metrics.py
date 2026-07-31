from __future__ import annotations

from typing import Optional

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _HAS_PROMETHEUS = True
except ImportError:  # pragma: no cover - 可选依赖
    _HAS_PROMETHEUS = False


class NullMetrics:
    """prometheus_client 未安装时的 no-op 实现,保证主流程不依赖可选包。"""

    def record_message(self, *, result: str, chat_id: Optional[int] = None) -> None:
        ...

    def record_verification(self, *, result: str) -> None:
        ...

    def observe_llm_latency(self, *, provider: str, seconds: float) -> None:
        ...

    def observe_llm_outcome(self, *, provider: str, flagged: bool) -> None:
        ...

    def set_llm_in_flight(self, value: int) -> None:
        ...

    def set_score_redis_degraded(self, value: int) -> None:
        ...


class PrometheusMetrics:
    """Telegram-group-guard-bot 的 Prometheus 指标定义与收集器。

    所有指标挂在 self.registry 上,便于单进程多实例隔离。
    """

    def __init__(self) -> None:
        if not _HAS_PROMETHEUS:
            raise RuntimeError(
                "prometheus_client 未安装,设置 ENABLE_METRICS=true 时需 pip install prometheus_client"
            )
        self.registry = CollectorRegistry()

        self.messages = Counter(
            "kkbot_messages_total",
            "处理消息总数,按处理结果分桶",
            labelnames=("result",),
            registry=self.registry,
        )
        self.verifications = Counter(
            "kkbot_verification_total",
            "验证流程结果",
            labelnames=("result",),
            registry=self.registry,
        )
        self.llm_latency = Histogram(
            "kkbot_llm_latency_seconds",
            "LLM 调用延迟",
            labelnames=("provider",),
            buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 30, 60),
            registry=self.registry,
        )
        self.llm_outcome = Counter(
            "kkbot_llm_outcome_total",
            "LLM 调用结果",
            labelnames=("provider", "flagged"),
            registry=self.registry,
        )
        self.llm_in_flight = Gauge(
            "kkbot_llm_in_flight",
            "正在进行的 LLM 调用数",
            registry=self.registry,
        )
        self.score_redis_degraded = Gauge(
            "kkbot_score_redis_degraded",
            "评分 Redis 是否处于降级(1=是, 0=否)",
            registry=self.registry,
        )

    def record_message(self, *, result: str, chat_id: Optional[int] = None) -> None:
        self.messages.labels(result=result).inc()

    def record_verification(self, *, result: str) -> None:
        self.verifications.labels(result=result).inc()

    def observe_llm_latency(self, *, provider: str, seconds: float) -> None:
        self.llm_latency.labels(provider=provider).observe(seconds)

    def observe_llm_outcome(self, *, provider: str, flagged: bool) -> None:
        self.llm_outcome.labels(provider=provider, flagged=str(flagged)).inc()

    def set_llm_in_flight(self, value: int) -> None:
        self.llm_in_flight.set(value)

    def set_score_redis_degraded(self, value: int) -> None:
        self.score_redis_degraded.set(1 if value else 0)

    def expose(self) -> bytes:
        return generate_latest(self.registry)


def build_metrics(enabled: bool) -> object:
    """根据是否启用返回真实实现或 no-op。"""
    if not enabled:
        return NullMetrics()
    return PrometheusMetrics()


__all__ = ["NullMetrics", "PrometheusMetrics", "build_metrics"]
