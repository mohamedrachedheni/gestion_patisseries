from functools import wraps

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic.base import View


# ─── Mapping groupe → URL home ────────────────────────────────────────────────

GROUP_HOME = {
    'Administration': 'administration:home',
    'Commercial':     'commercial:home',
    'Production':     'production:home',
}


def get_group_home_url(user):
    """Retourne l'URL home correspondant au groupe de l'utilisateur."""
    if not user.is_authenticated:
        return reverse_lazy('core:login')
    if user.is_superuser:
        return reverse_lazy('administration:home')
    for group_name, url_name in GROUP_HOME.items():
        if user.groups.filter(name=group_name).exists():
            return reverse_lazy(url_name)
    # Utilisateur authentifié mais sans groupe défini
    return reverse_lazy('core:login')


# ─── Mixin CBV ────────────────────────────────────────────────────────────────

class GroupRequiredMixin(LoginRequiredMixin):
    """
    Restreint l'accès à un ou plusieurs groupes.

    Usage sur une CBV :
        class MyView(GroupRequiredMixin, TemplateView):
            group_required = 'Administration'
            # ou
            group_required = ['Administration', 'Commercial']

    Règles :
    - Non authentifié → page de login.
    - Mauvais groupe → redirection silencieuse vers la home de son propre groupe.
    - Superuser → accès total.
    """
    group_required = None

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not self._has_group_access(request.user):
            return redirect(get_group_home_url(request.user))
        # Auth ok + groupe ok : on court-circuite LoginRequiredMixin (déjà vérifié)
        return View.dispatch(self, request, *args, **kwargs)

    def _has_group_access(self, user):
        if user.is_superuser:
            return True
        if not self.group_required:
            return True
        required = (
            [self.group_required]
            if isinstance(self.group_required, str)
            else list(self.group_required)
        )
        return user.groups.filter(name__in=required).exists()


# ─── Décorateur FBV ───────────────────────────────────────────────────────────

def group_required(*group_names):
    """
    Décorateur pour les vues fonctionnelles.

    Usage :
        @group_required('Commercial')
        def ma_vue(request): ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.conf import settings as s
                return redirect(s.LOGIN_URL)
            if request.user.is_superuser or request.user.groups.filter(name__in=group_names).exists():
                return view_func(request, *args, **kwargs)
            return redirect(get_group_home_url(request.user))
        return wrapped
    return decorator
