from __future__ import annotations

from dataclasses import dataclass

from TradingBot.config.env_config import EnvConfig, load_env_config
from TradingBot.config.yaml_config import AppConfig, load_app_config
'''
What patterns are being used?
1) Factory function (simple factory)

build_settings_runtime() is a factory: it creates a fully-formed SettingsRuntime in one place.

Centralises object creation.

Hides “how to build it” from the rest of the app.

Lets you change construction without touching call sites.

2) Aggregator / Facade over configuration sources

SettingsRuntime is an aggregate that bundles multiple config sources behind a single object. In effect, it's acting like a small facade over “environment + YAML”.

Callers can depend on one thing (SettingsRuntime) rather than N config modules.

It gives you a stable “configuration API” for the application layer.

3) Composition root (wiring at the edge)

Even though it's tiny, this is also the idea of a composition root: “wire dependencies once, at startup, then pass the result in”.

The app core should not be calling load_env_config() or load_app_config() all over the place.

It should receive a SettingsRuntime (or narrower slices of it).

If more configs are added, what changes and what must be maintained?
With the current approach, adding a new config means:

Create a new config type + loader (for example RiskConfig, load_risk_config()).

Add a field to SettingsRuntime.

Update the factory build_settings_runtime() to load it and populate the dataclass.

Update call sites only if they need the new config.

That's not bad. The key point is: only two files must always change when you add a new config:

SettingsRuntime (the aggregate)

build_settings_runtime() (the factory)

Everything else stays stable unless it actually needs that config.

The real design tradeoff
Pros of this design

Low ceremony (good).

Explicit wiring in one place (good).

Strong typing and immutability (frozen=True) (good).

Cons as configs grow

SettingsRuntime can become a “god object” if you keep stuffing everything into it.

build_settings_runtime() becomes a construction hotspot (still acceptable if kept tidy, but it grows).

What's the “maintenance-safe” way to scale this?

If you want to keep this pattern but make it scale cleanly, you typically evolve to one of these:

Option A: Nested config domains (recommended if you expect growth)

Group configs by domain so SettingsRuntime stays stable:

SettingsRuntime(env_cfg, app_cfg, broker_cfg, risk_cfg, …) becomes:

SettingsRuntime(env=EnvSettings, app=AppSettings, trading=TradingSettings, …)

This limits churn and keeps call sites clearer.

Option B: Registry/Plugin loader (only if you really want zero edits per new config)

You can design a registration mechanism where new configs self-register and build_settings_runtime() does't change. But that adds indirection and “magic”, and usually becomes harder to debug than it's worth unless you're building a framework.


Bottom line

You're using a factory function plus an aggregate/facade style config object, assembled in a composition root manner.

If new configs appear, you currently need to maintain exactly two places: the SettingsRuntime dataclass and the build_settings_runtime() factory.

To keep it maintainable as it grows, the cleanest scaling move is domain grouping (Option A), not registries or heavy patterns.
'''

@dataclass(frozen=True)
class SettingsRuntime:
    env_cfg: EnvConfig
    app_cfg: AppConfig


def build_settings_runtime() -> SettingsRuntime:
    env_cfg = load_env_config()
    app_cfg = load_app_config()
    return SettingsRuntime(env_cfg=env_cfg, app_cfg=app_cfg)
