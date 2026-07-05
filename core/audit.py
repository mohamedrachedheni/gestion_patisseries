"""Point d'entrée unique du journal d'audit.

Usage :
    from core.audit import log_audit, AuditAction

    log_audit(AuditAction.CREATE, "Création employé", table='Employe', record_id=employe.pk,
              new_value={'matricule': employe.matricule, 'service': employe.service})

Le contexte (utilisateur, IP, user-agent, URL, module/fonction/ligne appelants...)
est injecté automatiquement par AuditContextFilter — inutile de le repasser ici.
"""
import json
import logging

audit_logger = logging.getLogger('app.audit')

# Longueur max d'un old_value/new_value sérialisé, pour ne jamais produire une
# ligne de log énorme (ex: un import Excel de 10 000 lignes ne doit pas
# recopier tout le fichier dans le journal).
_MAX_VALUE_LENGTH = 500

# Clés jamais journalisées en clair, quelle que soit la casse — voir _redact().
_SENSITIVE_KEYS = {
    'password', 'password1', 'password2', 'old_password', 'new_password', 'mot_de_passe',
    'token', 'access_token', 'refresh_token', 'jwt', 'auth_token',
    'secret', 'secret_key', 'api_key', 'apikey', 'client_secret',
    'cookie', 'csrfmiddlewaretoken', 'sessionid', 'authorization',
    'card_number', 'numero_carte', 'cvv', 'cvc',
}
_MASK = '***MASQUÉ***'


class AuditAction:
    """Types d'événements attendus dans le journal d'audit — voir le cahier
    des charges : uniquement les opérations critiques, jamais la simple
    consultation/navigation."""
    CREATE = 'CREATE'
    UPDATE = 'UPDATE'
    DELETE = 'DELETE'
    VALIDATE = 'VALIDATE'
    CANCEL = 'CANCEL'
    LOGIN = 'LOGIN'
    LOGOUT = 'LOGOUT'
    LOGIN_FAILED = 'LOGIN_FAILED'
    PASSWORD_CHANGE = 'PASSWORD_CHANGE'
    IMPORT_EXCEL = 'IMPORT_EXCEL'
    IMPORT_CSV = 'IMPORT_CSV'
    EXPORT_EXCEL = 'EXPORT_EXCEL'
    EXPORT_PDF = 'EXPORT_PDF'
    DB_BACKUP = 'DB_BACKUP'


def _redact(value):
    """Masque récursivement toute clé sensible (mot de passe, token, secret,
    carte bancaire...) avant qu'une valeur n'atteigne le journal."""
    if isinstance(value, dict):
        return {
            k: (_MASK if str(k).lower() in _SENSITIVE_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def _serialize(value):
    if value is None:
        return '-'
    redacted = _redact(value)
    text = redacted if isinstance(redacted, str) else json.dumps(redacted, default=str, ensure_ascii=False)
    if len(text) > _MAX_VALUE_LENGTH:
        text = text[:_MAX_VALUE_LENGTH] + '…(tronqué)'
    return text


def log_audit(action, message, *, table=None, record_id=None, old_value=None, new_value=None):
    """Écrit un événement dans le journal d'audit (logs/audit/).

    action : voir AuditAction. table/record_id/old_value/new_value : à fournir
    pour CREATE/UPDATE/DELETE (nom de table, ID de l'enregistrement, valeurs
    avant/après). old_value/new_value peuvent être un dict de champs modifiés :
    les clés sensibles y sont automatiquement masquées.
    """
    audit_logger.info(
        '[%s] %s',
        action, message,
        extra={
            'action': action,
            'table': table or '-',
            'record_id': record_id if record_id is not None else '-',
            'old_value': _serialize(old_value),
            'new_value': _serialize(new_value),
        },
        stacklevel=2,  # attribue module/fonction/ligne à l'appelant, pas à log_audit()
    )
