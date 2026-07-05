"""Fait transiter la requête HTTP courante jusqu'au logging.Filter d'audit,
sans avoir à la passer explicitement à chaque appel de logger.

Un ContextVar (plutôt qu'un threading.local) pour rester correct si le
projet passe un jour à des vues async — contrairement à threading.local,
un ContextVar suit correctement l'exécution à travers await/tâches.
"""
import contextvars

_current_request = contextvars.ContextVar('current_request', default=None)


def get_current_request():
    """Retourne la requête HTTP en cours de traitement, ou None (commande CLI,
    tâche planifiée, ou tout code exécuté hors du cycle requête/réponse)."""
    return _current_request.get()


def set_current_request(request):
    """Positionne la requête courante ; retourne un jeton à repasser à reset_current_request."""
    return _current_request.set(request)


def reset_current_request(token):
    _current_request.reset(token)
