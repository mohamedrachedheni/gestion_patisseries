from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect
from django.views import View

from .mixins import get_group_home_url


class CustomLoginView(LoginView):
    """
    Page de connexion.
    - Utilisateur déjà connecté → redirigé directement vers sa home de groupe.
    - Après authentification réussie → redirigé vers sa home de groupe.
    - Le paramètre 'next' est ignoré : chaque utilisateur a une home fixe.
    """
    template_name = 'registration/login.html'
    redirect_authenticated_user = True

    def get_success_url(self):
        return str(get_group_home_url(self.request.user))


class GroupRouterView(LoginRequiredMixin, View):
    """
    Vue racine (/) : redirige l'utilisateur vers la home de son groupe.
    Sert de LOGIN_REDIRECT_URL de secours.
    """
    def get(self, request):
        return redirect(get_group_home_url(request.user))
