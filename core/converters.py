from django.conf import settings
from hashids import Hashids

_hashids = Hashids(salt=settings.HASHIDS_SALT, min_length=8)


class HashidConverter:
    """Convertisseur d'URL : encode/décode les ID entiers (ex: pk) en chaîne
    opaque façon 'aK9dP2xZ', pour ne pas exposer d'ID séquentiel dans les URLs.

    Usage dans un urls.py : path('employes/<hashid:pk>/', ...) — la vue reçoit
    directement l'entier décodé, aucun changement requis côté vue ou template.
    """
    regex = '[a-zA-Z0-9]+'

    def to_python(self, value):
        decoded = _hashids.decode(value)
        if not decoded:
            raise ValueError(f'Hashid invalide : {value!r}')
        return decoded[0]

    def to_url(self, value):
        return _hashids.encode(int(value))
