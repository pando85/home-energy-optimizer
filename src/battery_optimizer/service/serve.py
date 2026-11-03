"""Wiring for ``battery-optimizer serve``: config, adapters, runtime and uvicorn."""

from __future__ import annotations

import logging
from pathlib import Path

from battery_optimizer.battery_telemetry import load_battery_telemetry
from battery_optimizer.config import TariffConfig, load_config, resolve_config_path
from battery_optimizer.paths import _ensure_private_dir, resolve_data_dir

from .clock import SystemClock
from .config import ServiceConfig, load_service_config, resolve_service_config_path
from .history import PrometheusHistory
from .runtime import ShadowService

__all__ = ["build_service", "run_server"]

log = logging.getLogger(__name__)


def _load_tariff(path_arg: str | None) -> tuple[TariffConfig | None, str | None]:
    """The tariff is needed to plan, not to run: a broken one is a not-ready reason."""
    path = resolve_config_path(path_arg)
    try:
        return load_config(path), None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"{path}: {type(exc).__name__}: {exc}"


def build_service(
    *,
    service_config: str | None = None,
    tariff_config: str | None = None,
    data_dir: str | None = None,
) -> tuple[ShadowService, ServiceConfig]:
    config = load_service_config(resolve_service_config_path(service_config))
    tariff, tariff_error = _load_tariff(tariff_config)
    resolved_data_dir = resolve_data_dir(data_dir)
    store_root = resolved_data_dir / "service" / "v1"
    _ensure_private_dir(store_root)  # fail fast if the data dir is not writable
    probe = store_root / ".write-probe"
    probe.write_bytes(b"")
    probe.unlink()

    fetcher = None
    grid_energy = config.entities.get("grid_energy")
    if config.prometheus.url and grid_energy:
        fetcher = PrometheusHistory(
            base_url=config.prometheus.url,
            timeout_s=config.prometheus.timeout_s,
            tz=config.tz,
            window_days=config.prometheus.history_days,
            grid_energy_entity=grid_energy,
            price_entity=config.entities.get("price"),
            battery_charge_energy_entity=config.entities.get("battery_charge_energy"),
            battery_discharge_energy_entity=config.entities.get("battery_discharge_energy"),
            battery_commissioned_at=load_battery_telemetry(config.source_path).commissioned_at,
            interval_seconds=config.planning.interval_seconds,
        ).fetch

    from battery_optimizer.manifest import code_version

    service = ShadowService(
        config=config,
        tariff=tariff,
        tariff_error=tariff_error,
        data_dir=resolved_data_dir,
        clock=SystemClock(),
        code_version=code_version(),
        history_fetcher=fetcher,
    )
    return service, config


def run_server(service: ShadowService, *, host: str, port: int) -> None:
    import uvicorn

    from battery_optimizer import __version__
    from battery_optimizer._logging import quiet_http_loggers

    from .api import create_app

    # httpx logs every request at INFO; the 60 s REST refresh would flood the log.
    quiet_http_loggers()
    app = create_app(service)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            lifespan="on",
            access_log=False,
            server_header=False,
            proxy_headers=False,
            timeout_graceful_shutdown=20,
        )
    )
    log.info(
        "serving mode=%s soc_source=%s version=%s code_version=%s on %s:%d (store %s)",
        service.config.mode,
        service.config.planning.soc_source,
        __version__,
        service.code_version,
        host,
        port,
        Path(service.sink.root),
    )
    server.run()
