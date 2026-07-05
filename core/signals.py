"""Journalise automatiquement les événements d'authentification, quelle que
soit la vue qui les déclenche (CustomLoginView, LogoutView, admin Django...).

Branché une seule fois via CoreConfig.ready() — voir core/apps.py.
"""
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from .audit import AuditAction, log_audit


@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    log_audit(AuditAction.LOGIN, f'Connexion réussie : {user.get_username()}')


@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    nom = user.get_username() if user else 'inconnu'
    log_audit(AuditAction.LOGOUT, f'Déconnexion : {nom}')


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request=None, **kwargs):
    # Django masque déjà le mot de passe dans `credentials` (_clean_credentials) ;
    # log_audit()/_redact() le masquerait de toute façon si ce n'était pas le cas.
    username = credentials.get('username', '?')
    log_audit(AuditAction.LOGIN_FAILED, f'Échec de connexion pour « {username} »')
