"""Source adapters (one per website), registry and caching."""

from ezgd.sources.ankergames import AnkerGamesAdapter
from ezgd.sources.crackedgames import CrackedGamesAdapter
from ezgd.sources.fitgirl import FitGirlAdapter
from ezgd.sources.onlinefix import OnlineFixAdapter
from ezgd.sources.steamrip import SteamRipAdapter

ADAPTER_CLASSES = {
    OnlineFixAdapter.info.id: OnlineFixAdapter,
    SteamRipAdapter.info.id: SteamRipAdapter,
    CrackedGamesAdapter.info.id: CrackedGamesAdapter,
    FitGirlAdapter.info.id: FitGirlAdapter,
    AnkerGamesAdapter.info.id: AnkerGamesAdapter,
}

__all__ = ["OnlineFixAdapter", "SteamRipAdapter", "CrackedGamesAdapter", "FitGirlAdapter", "AnkerGamesAdapter",
           "ADAPTER_CLASSES"]
