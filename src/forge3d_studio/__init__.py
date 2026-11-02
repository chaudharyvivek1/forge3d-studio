"""forge3d-studio — the product surface around the forge3d engine.

Studio owns jobs, storage, assets and scenes. It owns **no geometry** — every
mesh comes from the pinned forge3d package — and since v0.2.0 it owns **no
identity and no money** either: authentication, credits and reservations belong
to the central platform.
"""

__version__ = "0.2.0"
__all__ = ["__version__"]
