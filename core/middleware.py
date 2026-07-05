from .logging_context import reset_current_request, set_current_request


class RequestLogContextMiddleware:
    """Rend la requête HTTP courante disponible à AuditContextFilter (voir
    core/logging_filters.py) pendant toute la durée du traitement de la
    requête, pour que les logs d'audit s'enrichissent automatiquement
    (utilisateur, IP, user-agent...) sans rien passer explicitement.

    Placé après AuthenticationMiddleware dans MIDDLEWARE : request.user est
    déjà résolu pour toute vue/log qui s'exécute ensuite.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = set_current_request(request)
        try:
            return self.get_response(request)
        finally:
            reset_current_request(token)
