import logging
from datetime import datetime

from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from .audit import AuditAction, log_audit
from .mixins import get_group_home_url
from .models import CodeConnexion
from .services import (
    DELAI_MIN_RENVOI,
    MAX_TENTATIVES_CODE,
    enregistrer_echec,
    envoyer_code_connexion,
    est_verrouille,
    generer_code_connexion,
    masquer_email,
    reinitialiser_echecs,
)

SESSION_PENDING_USER = 'connexion_2fa_user_id'
SESSION_DERNIER_ENVOI = 'connexion_2fa_dernier_envoi'

technical_logger = logging.getLogger('app.technical')

MESSAGE_ECHEC_ENVOI = (
    "Impossible d'envoyer le code de connexion pour le moment "
    '(problème technique côté serveur mail). Réessayez dans quelques instants '
    'ou contactez un administrateur si le problème persiste.'
)


def _envoyer_code_ou_echec(user):
    """Génère et envoie le code de connexion ; retourne (code_connexion, None)
    en cas de succès, ou (None, message_erreur) si l'envoi échoue — un email
    SMTP indisponible ne doit jamais faire planter la page de connexion."""
    try:
        code_connexion = generer_code_connexion(user)
        envoyer_code_connexion(user, code_connexion)
    except Exception:
        technical_logger.exception(
            "Échec d'envoi du code de connexion à « %s »", user.get_username(),
        )
        return None, MESSAGE_ECHEC_ENVOI
    return code_connexion, None


class CustomLoginView(LoginView):
    """
    Page de connexion, en deux étapes :
    1) identifiant + mot de passe (cette vue) — verrouillage temporaire après
       échecs répétés (voir core/services.py) ;
    2) code à 6 chiffres envoyé par email, saisi sur LoginCodeView — la
       session n'est ouverte qu'une fois ce code validé.
    - Utilisateur déjà connecté → redirigé directement vers sa home de groupe.
    - Le paramètre 'next' est ignoré : chaque utilisateur a une home fixe.
    """
    template_name = 'registration/login.html'
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        identifiant = request.POST.get('username', '')
        verrouille_jusqu_a = est_verrouille(identifiant)
        if verrouille_jusqu_a:
            secondes = max(1, int((verrouille_jusqu_a - timezone.now()).total_seconds()))
            log_audit(AuditAction.LOGIN_FAILED, f'Connexion refusée (compte verrouillé) pour « {identifiant} »')
            messages.error(
                request,
                f'Trop de tentatives échouées. Réessayez dans {secondes} seconde(s).',
            )
            return redirect('core:login')
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        enregistrer_echec(self.request.POST.get('username', ''))
        return super().form_invalid(form)

    def form_valid(self, form):
        # Identifiant + mot de passe corrects : on ne connecte pas tout de
        # suite, on déclenche la 2ᵉ étape (code envoyé par email).
        user = form.get_user()
        reinitialiser_echecs(user.get_username())

        if not user.email:
            messages.error(
                self.request,
                "Votre compte n'a pas d'adresse email associée. "
                'Contactez un administrateur pour pouvoir vous connecter.',
            )
            return self.render_to_response(self.get_context_data(form=form))

        _code_connexion, erreur = _envoyer_code_ou_echec(user)
        if erreur:
            messages.error(self.request, erreur)
            return self.render_to_response(self.get_context_data(form=form))

        self.request.session[SESSION_PENDING_USER] = user.pk
        self.request.session[SESSION_DERNIER_ENVOI] = timezone.now().isoformat()
        return redirect('core:login-code')


class LoginCodeView(View):
    """
    2ᵉ étape de la connexion : saisie du code à 6 chiffres reçu par email.
    N'établit la session (login()) qu'une fois le code validé — voir
    CustomLoginView.form_valid().
    """
    template_name = 'registration/login_code.html'

    def _utilisateur_en_attente(self, request):
        user_id = request.session.get(SESSION_PENDING_USER)
        if not user_id:
            return None
        return get_user_model().objects.filter(pk=user_id, is_active=True).first()

    def _code_en_cours(self, user):
        return CodeConnexion.objects.filter(user=user, utilise=False).order_by('-cree_le').first()

    def _annuler(self, request):
        request.session.pop(SESSION_PENDING_USER, None)
        request.session.pop(SESSION_DERNIER_ENVOI, None)

    def get(self, request):
        user = self._utilisateur_en_attente(request)
        if user is None:
            return redirect('core:login')
        return render(request, self.template_name, {'email_masque': masquer_email(user.email)})

    def post(self, request):
        user = self._utilisateur_en_attente(request)
        if user is None:
            return redirect('core:login')

        if 'annuler' in request.POST:
            self._annuler(request)
            return redirect('core:login')

        if 'renvoyer' in request.POST:
            return self._renvoyer(request, user)

        code_obj = self._code_en_cours(user)
        code_saisi = (request.POST.get('code') or '').strip()
        contexte = {'email_masque': masquer_email(user.email)}

        if code_obj is None or code_obj.expire_le < timezone.now():
            messages.error(request, 'Le code a expiré. Veuillez vous reconnecter.')
            self._annuler(request)
            return redirect('core:login')

        if code_obj.tentatives >= MAX_TENTATIVES_CODE:
            messages.error(request, 'Trop de tentatives incorrectes. Veuillez vous reconnecter.')
            self._annuler(request)
            return redirect('core:login')

        if code_saisi != code_obj.code:
            code_obj.tentatives += 1
            code_obj.save(update_fields=['tentatives'])
            log_audit(AuditAction.LOGIN_FAILED, f'Code de connexion incorrect pour « {user.get_username()} »')
            messages.error(request, 'Code incorrect.')
            return render(request, self.template_name, contexte)

        code_obj.utilise = True
        code_obj.save(update_fields=['utilise'])
        self._annuler(request)
        login(request, user)
        return redirect(get_group_home_url(user))

    def _renvoyer(self, request, user):
        contexte = {'email_masque': masquer_email(user.email)}
        dernier_envoi = request.session.get(SESSION_DERNIER_ENVOI)
        if dernier_envoi and timezone.now() - datetime.fromisoformat(dernier_envoi) < DELAI_MIN_RENVOI:
            messages.error(request, 'Veuillez patienter avant de demander un nouveau code.')
            return render(request, self.template_name, contexte)

        _code_connexion, erreur = _envoyer_code_ou_echec(user)
        if erreur:
            messages.error(request, erreur)
            return render(request, self.template_name, contexte)

        request.session[SESSION_DERNIER_ENVOI] = timezone.now().isoformat()
        messages.success(request, 'Un nouveau code vous a été envoyé.')
        return render(request, self.template_name, contexte)


class GroupRouterView(LoginRequiredMixin, View):
    """
    Vue racine (/) : redirige l'utilisateur vers la home de son groupe.
    Sert de LOGIN_REDIRECT_URL de secours.
    """
    def get(self, request):
        return redirect(get_group_home_url(request.user))
