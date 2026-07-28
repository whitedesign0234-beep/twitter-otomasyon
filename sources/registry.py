# sources/registry.py
"""Kaynak eklentilerinin kayıt defteri (registry).

@register("rss") dekoratörü sınıfı deftere ekler; YAML'daki `type` alanı bu
defterden ilgili sınıfı bulur. Böylece yeni tip eklemek orkestrasyonu değiştirmez.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from sources.base import ContentSource

_REGISTRY: dict[str, type] = {}

T = TypeVar("T")


def register(type_name: str) -> Callable[[type[T]], type[T]]:
    """Verilen tip adıyla bir kaynak sınıfını deftere kaydeden dekoratör."""

    def decorator(cls: type[T]) -> type[T]:
        """Sınıfı registry'ye ekler ve olduğu gibi geri döndürür."""
        key = type_name.lower()
        if key in _REGISTRY:
            raise ValueError(f"'{type_name}' kaynak tipi zaten kayıtlı")
        _REGISTRY[key] = cls
        return cls

    return decorator


def create_source(type_name: str) -> ContentSource:
    """Tip adına karşılık gelen kaynak eklentisinin bir örneğini üretir."""
    key = type_name.lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Bilinmeyen kaynak tipi: {type_name!r}. Kayıtlılar: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[key]()


def registered_types() -> list[str]:
    """Kayıtlı tüm kaynak tiplerini döndürür (teşhis amaçlı)."""
    return sorted(_REGISTRY)
