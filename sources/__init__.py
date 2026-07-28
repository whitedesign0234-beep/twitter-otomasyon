# sources/__init__.py
"""İçerik kaynağı eklenti paketi.

RSS eklentisini içe aktararak registry'ye kaydını tetikler. Yeni bir kaynak
tipi eklemek: bu paketin içine yeni bir modül yaz, @register ile işaretle ve
aşağıya bir import satırı ekle (orkestrasyon koduna dokunma).
"""

from sources import rss  # noqa: F401  (yan etki: registry kaydı)
