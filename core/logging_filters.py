import logging

from .logging_context import get_current_request

try:
    from user_agents import parse as parse_user_agent
except ImportError:  # pragma: no cover - dépendance déclarée dans requirements.txt
    parse_user_agent = None


# Valeurs de repli quand aucune requête HTTP n'est en cours (commande CLI,
# tâche planifiée type sauvegarde nocturne) — le formatter d'audit référence
# toujours ces champs, ils doivent donc systématiquement exister sur le record.
_FALLBACK = {
    'user': 'system', 'user_id': '-', 'role': '-',
    'ip': '-', 'user_agent': '-', 'browser': '-', 'os': '-', 'device_type': '-',
    'path': '-', 'method': 'CLI',
}


class AuditContextFilter(logging.Filter):
    """logging.Filter qui enrichit automatiquement chaque enregistrement de log
    avec le contexte de la requête HTTP en cours : utilisateur, rôle, IP,
    user-agent, navigateur, OS, type d'appareil, URL, méthode HTTP.

    Ne rien faire manuellement dans les appels logger.info() : brancher ce
    filtre sur un handler (voir LOGGING['handlers']['audit_file']) suffit à
    ce que tous les logs qui y transitent portent ces informations.
    """

    def filter(self, record):
        request = get_current_request()

        if request is None:
            for key, value in _FALLBACK.items():
                setattr(record, key, value)
            return True

        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            record.user = user.get_username()
            record.user_id = user.pk
            record.role = self._role(user)
        else:
            record.user = 'anonyme'
            record.user_id = '-'
            record.role = '-'

        record.ip = self._client_ip(request)
        ua_string = request.META.get('HTTP_USER_AGENT', '')
        record.user_agent = ua_string or '-'
        record.browser, record.os, record.device_type = self._parse_ua(ua_string)
        record.path = request.path
        record.method = request.method
        return True

    @staticmethod
    def _role(user):
        if user.is_superuser:
            return 'superuser'
        groupe = user.groups.first()
        return groupe.name if groupe else '-'

    @staticmethod
    def _client_ip(request):
        # Derrière un reverse proxy, REMOTE_ADDR vaut l'IP du proxy : X-Forwarded-For
        # (positionné par le proxy, à whitelister au niveau infra) donne le vrai client.
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
        if forwarded:
            return forwarded.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR', '-')

    @staticmethod
    def _parse_ua(ua_string):
        if not ua_string or parse_user_agent is None:
            return '-', '-', '-'
        ua = parse_user_agent(ua_string)
        if ua.is_mobile:
            device_type = 'Mobile'
        elif ua.is_tablet:
            device_type = 'Tablette'
        elif ua.is_pc:
            device_type = 'PC'
        else:
            device_type = 'Autre'
        return (ua.browser.family or '-'), (ua.os.family or '-'), device_type
