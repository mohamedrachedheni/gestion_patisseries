def is_administration(request):
    """Injecte `is_administration_user` dans le contexte de tous les templates
    — superuser ou membre du groupe Administration, même logique que
    `_is_administration()` (commercial/views.py, administration/views.py).
    Utilisé notamment par les sidebars pour afficher les liens croisés entre
    apps (Administration/Commercial/Production)."""
    user = request.user
    is_admin = user.is_authenticated and (user.is_superuser or user.groups.filter(name='Administration').exists())
    return {'is_administration_user': is_admin}
