"""A small usd-replay/1 replay written with usd-recorder's ReplayWriter, for the replay tests."""

from __future__ import annotations

import numpy as np


def write_replay(path, ticks=range(0, 101, 5), seed=3):
    from usd_recorder.replay import ReplayWriter

    rng = np.random.default_rng(seed)
    with ReplayWriter(
        path, sim="fixture", sim_version="0.1", seed=seed, tick_rate=10, state_hash_algorithm="fixture",
        playback_rate=20, title="Fixture replay",
    ) as w:
        w.entity_set("movers", label="Movers", color="#e0553d", width=0.8)
        w.entity_set("blinkers", label="Blinkers", color=None, width=None, shape="cube")
        w.field("heat", label="Heat", cols=8, rows=6, origin=(-4, -3), cell_size=1.0)
        w.scene_points("rocks", [[0, 0, 0.5], [2, -1, 0.5]], widths=[1.0, 1.5], color=["#777777", "#999999"])
        for t in ticks:
            n = 3 + t // 20
            ids = [1000 + 7 * i for i in range(n)]
            pts = np.stack([np.linspace(-4, 4, n) + t / 100, np.full(n, np.sin(t / 10)), np.full(n, 0.4)], axis=1)
            m = (t // 10) % 4
            w.sample(
                t,
                state_hash=f"{(t * 2654435761) % (1 << 64):016x}",
                entities={
                    "movers": {"ids": ids, "points": pts},
                    "blinkers": {
                        "ids": list(range(m)),
                        "points": rng.uniform(-3, 3, (m, 3)),
                        "colors": rng.integers(0, 256, (m, 3), dtype=np.uint8),
                        "widths": rng.uniform(0.2, 1.2, m),
                    },
                },
                fields={"heat": np.full((48, 3), (t // 25) * 60, dtype=np.uint8)},
                stats={"movers": n, **({"tenth": t / 10} if t % 10 == 0 else {})},
            )
    return path
